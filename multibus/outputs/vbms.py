# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Virtual BMS — MBG speaks a battery CAN protocol to an inverter (ADR-0037).

Increment 1: build the frame set from **canonical battery values** through a bidirectional
CAN **profile** (a device template, ADR-0020), behind an **arm gate**, and transmit it with an
injected transmitter. This is the step from "replay captured bytes" to "synthesise frames from
live state": `build_frames(values)` is pure (uses the proven `can_encode`); `transmit_once`
sends only when **armed** (default disarmed = silent).

What this increment does NOT yet do (later lots, ADR-0037 §consequences): the config/device
registration, the live aggregation that fills ``values`` (design §4), the EMS/MQTT control plane
that sets the control fields (D4), the send loop/rate, multi-port. Here the caller supplies the
values and drives the ticks, so the core is small and fully testable.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Callable, Dict, List, Optional, Protocol

from multibus.codec.can_encode import encode_signal
from multibus.codec.can_signal import _get, is_can_row, parse_can_id

logger = logging.getLogger(__name__)


class CanTx(Protocol):
    """A CAN transmitter — the ONE seam that sends. Injected so the executor logic is
    tested with a fake and never touches a bus in a unit test."""

    def send(self, can_id: int, data: bytes) -> None: ...


class FrameValueError(ValueError):
    """A value cannot be put on the wire as it is: not finite, or outside what the field's
    bits can hold. The encoder would otherwise mask it into a DIFFERENT number (WK7-SAFE-3)."""


def encode_value(buf: bytearray, row: Any, value: Any) -> None:
    """Place one canonical ``value`` into ``buf`` for ``row`` — the inverse of the decode
    pipeline: apply the row's scale (a divisor on read → a multiplier on write), then
    ``encode_signal``. Strings and bit flags pass through unscaled. Raises
    ``FrameValueError`` for a value that is not finite or does not fit the field."""
    dt = str(_get(row, "data_type") or "uint16").lower()
    if dt.startswith("string"):
        encode_signal(buf, row, value)
        return
    scale = _get(row, "scale")
    offset = _get(row, "offset") or 0
    name = _get(row, "name")
    if value is None:
        encode_signal(buf, row, 0)
        return
    try:
        number = float(value)
    except (TypeError, ValueError) as e:
        raise FrameValueError(f"{name}: {value!r} is not a number") from e
    if not math.isfinite(number):
        raise FrameValueError(f"{name}: {value!r} is not finite")
    raw = value - offset
    if dt.startswith("float"):
        encode_signal(buf, row, raw * scale if scale else raw)
        return
    if scale:
        raw = round(raw * scale)
    raw = int(raw)
    bits = int(_get(row, "bit_length"))
    signed = dt.startswith("int")
    lo, hi = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
    if not lo <= raw <= hi:
        raise FrameValueError(f"{name}: {value!r} does not fit its {bits}-bit field "
                              f"(raw {raw}, allowed {lo}..{hi})")
    encode_signal(buf, row, raw)


def rows_by_can_id(rows: List[Any], tx_only: bool = True) -> Dict[int, List[Any]]:
    """Group a profile's CAN rows by frame id. ``tx_only`` drops the inverter→BMS keepalive
    and anything a ``bms_profile`` transmit allow-list excludes (not yet modelled here: we take
    every modelled frame except an explicit ``direction: rx`` row)."""
    out: Dict[int, List[Any]] = {}
    for r in rows:
        if not is_can_row(r):
            continue
        if tx_only and str(_get(r, "direction") or "tx").lower() == "rx":
            continue
        cid = parse_can_id(_get(r, "can_id"))
        if cid is not None and _get(r, "start_bit") is not None:
            out.setdefault(cid, []).append(r)
    return out


# the fields that make a CAN frame MANDATORY for a battery BMS — the inverter needs them to run
# (limits, SOC, pack V/I/T, enables). A frame carrying none of these is an optional detail frame
# (cell extremes, SOH, module count, manufacturer, alarms) that the operator may choose not to send.
_MANDATORY_FIELDS = frozenset({
    "charge_current_limit", "discharge_current_limit", "charge_voltage_limit",
    "discharge_voltage_limit", "soc", "pack_voltage", "battery_current",
    "battery_temperature", "charge_enable", "discharge_enable",
})


class VirtualBms:
    """One virtual BMS: a profile + an arm gate. Stateless on values — the caller passes the
    current canonical ``values`` each tick (live aggregation arrives in a later lot)."""

    def __init__(self, rows: List[Any], *, name: str = "vbms", frame_len: int = 8,
                 send_optional: bool = True):
        self._frames = rows_by_can_id(rows)
        self.name = name
        self.frame_len = frame_len
        self.send_optional = send_optional
        # a frame is mandatory when it carries at least one field the inverter must have
        self._mandatory_ids = {cid for cid, rs in self._frames.items()
                               if any(_get(r, "name") in _MANDATORY_FIELDS for r in rs)}
        self._armed = False

    def _active_frames(self) -> Dict[int, List[Any]]:
        """The frames actually built/sent: all of them, or — with ``send_optional`` off — only the
        mandatory ones (the optional detail frames are dropped)."""
        if self.send_optional:
            return self._frames
        return {cid: rs for cid, rs in self._frames.items() if cid in self._mandatory_ids}

    # ── arm gate (ADR-0037 D5) ───────────────────────────────────────────
    @property
    def armed(self) -> bool:
        return self._armed

    def arm(self) -> None:
        self._armed = True
        logger.warning("virtual BMS %s ARMED — will transmit when ticked", self.name)

    def disarm(self) -> None:
        self._armed = False
        logger.info("virtual BMS %s disarmed — silent", self.name)

    # ── frame building (pure) ────────────────────────────────────────────
    def can_ids(self) -> List[int]:
        return sorted(self._active_frames())

    def build_frames(self, values: Dict[str, Any]) -> Dict[int, bytes]:
        """The frames this profile would send for ``values`` (canonical name → value). A field
        absent from ``values`` leaves its bits zero. Pure — no I/O, no transmit. With
        ``send_optional`` off, the optional detail frames are not built."""
        out: Dict[int, bytes] = {}
        for cid, rows in self._active_frames().items():
            buf = bytearray(self.frame_len)
            for r in rows:
                nm = _get(r, "name")
                if nm in values and values[nm] is not None:
                    encode_value(buf, r, values[nm])
            out[cid] = bytes(buf)
        return out

    # ── transmit (gated) ─────────────────────────────────────────────────
    def transmit_once(self, tx: CanTx, values: Dict[str, Any]) -> int:
        """Send one full frame set through ``tx`` — ONLY when armed. Returns how many frames
        were sent (0 when disarmed). The executor is the only thing that transmits, and only
        past this gate."""
        if not self._armed:
            return 0
        frames = self.build_frames(values)       # every frame is built before any is sent
        for cid in sorted(frames):
            tx.send(cid, frames[cid])
        return len(frames)


class VbmsRuntime:
    """Drive a ``VirtualBms``: tick ``transmit_once`` at the profile rate through an injected
    ``CanTx``, until stopped. The values come from ``values_fn`` each tick (the live aggregation
    plugs in here in a later lot). On exit the transmitter is closed, so the inverter sees
    silence and falls to its safe BMS-loss state (design §8). The tx is injected — this holds no
    socket and is unit-tested with a fake."""

    def __init__(self, vbms: VirtualBms, tx: CanTx,
                 values_fn: Callable[[], Dict[str, Any]], *, period_s: float = 1.0,
                 gate: Optional[Callable[[], Optional[str]]] = None):
        self.vbms = vbms
        self.tx = tx
        self.values_fn = values_fn
        self.period_s = period_s
        # the LIVE transmit gate (WK7-SAFE-1): asked before every frame set, not only when
        # arming. It answers None (may transmit) or why not (master write switch off, the
        # gateway is standby). A reason disarms the instance and ends the loop — the tx
        # closes, the inverter sees BMS-loss — and a recovery needs an explicit re-arm.
        self.gate = gate
        self.revoked: Optional[str] = None
        self.refused: Optional[str] = None     # why the last cycle sent nothing, if it did not
        self.failures = 0                      # cycles lost to an error since the start

    def _tick_failed(self, cause: str) -> None:
        self.failures += 1
        if self.refused != cause:
            logger.error("virtual BMS %s: nothing (more) sent this cycle — %s; retrying next cycle",
                         getattr(self.vbms, "name", "?"), cause)
        self.refused = cause

    def run(self, stop: Any) -> int:
        """Loop until ``stop`` is set (a ``threading.Event``-like: ``is_set()`` + ``wait(t)``).
        Opens the tx, sends a frame set each period while armed, closes the tx on exit. Returns
        the number of frame-sets actually sent.

        If the transmitter cannot open — the interface is listen-only, or absent — this logs and
        returns 0 instead of letting the exception escape the executor thread as an unhandled
        traceback. The instance is then armed but not running (the UI shows "not transmitting"),
        which is the operator's cue to take the CAN interface out of listen-only."""
        try:
            self.tx.open()
        except Exception as e:  # noqa: BLE001 — a tx that will not open must not crash the thread
            logger.warning("virtual BMS %s: CAN transmitter would not open (%s) — not transmitting; "
                           "is the interface out of listen-only?", getattr(self.vbms, "name", "?"), e)
            self.refused = f"the CAN transmitter would not open: {type(e).__name__}: {e}"
            return 0
        sets = 0
        try:
            while not stop.is_set():
                # values first: a source failure past its hold answers None = say nothing
                reason = self.gate() if self.gate is not None else None
                if reason:
                    self.revoked = reason
                    self.vbms.disarm()
                    logger.warning("virtual BMS %s: transmission REVOKED (%s) — disarmed, silent; "
                                   "re-arm it explicitly once this is resolved",
                                   getattr(self.vbms, "name", "?"), reason)
                    break
                # ONE bad tick must not end the executor (R2D-VBMS-1): a transient send error
                # (ENOBUFS, bus-off) or a pack value that is not a number used to kill this
                # thread for good while the instance still read "armed". A failed tick sends
                # what it had sent, is logged once per distinct cause, and the next tick
                # tries again; the loop only ends on stop or on a revocation.
                try:
                    values = self.values_fn()
                except Exception as e:  # noqa: BLE001
                    self._tick_failed(f"values: {type(e).__name__}: {e}")
                    stop.wait(self.period_s)
                    continue
                try:
                    if values and self.vbms.transmit_once(self.tx, values):
                        sets += 1
                    self.refused = None
                except FrameValueError as e:
                    # a set with a value the wire cannot carry is refused AS A WHOLE (design
                    # §5.5): nothing of it is sent — silence, not a masked number
                    if self.refused != str(e):
                        logger.error("virtual BMS %s: frame set refused, nothing sent — %s",
                                     getattr(self.vbms, "name", "?"), e)
                    self.refused = str(e)
                except Exception as e:  # noqa: BLE001 — the transmitter, mid-set or not
                    self._tick_failed(f"send: {type(e).__name__}: {e}")
                stop.wait(self.period_s)
        finally:
            self.tx.close()
        return sets
