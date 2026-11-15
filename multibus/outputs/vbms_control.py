# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Virtual BMS — the EMS/MQTT control plane (ADR-0037 D4).

The fields the inverter obeys (CVL, CCL, DCL, DVL, charge/discharge enable) are **commandable**
by an EMS through MQTT (``mbg/devices/<vbms>/cmd/<field>``). A command is a **ceiling, never a
bypass**: it is clamped to the battery's safe envelope (``bounds`` — the datasheet caps ∩ the
live aggregation floor), and a command that goes **stale** reverts to the ``defaults`` floor. So
with no EMS, or a dead EMS, the Virtual BMS is safe on its own.

Pure and testable: commands in, resolved control values out. The MQTT subscription that routes a
topic here, and the derivation of ``bounds`` from the datasheet/aggregation, are the wiring
around it (later lots). ``handle_command`` parses one ``.../cmd/<field>`` topic.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# the fields an EMS may command (the inverter's control plane)
CONTROL_FIELDS = (
    "charge_current_limit", "discharge_current_limit",
    "charge_voltage_limit", "discharge_voltage_limit",
    "charge_enable", "discharge_enable",
)
_ENABLES = ("charge_enable", "discharge_enable")

# friendly aliases an EMS might use
_ALIASES = {
    "ccl": "charge_current_limit", "dcl": "discharge_current_limit",
    "cvl": "charge_voltage_limit", "dvl": "discharge_voltage_limit",
}


def _finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


class VbmsControl:
    """Resolve the Virtual BMS control fields from EMS commands, clamped and freshness-gated."""

    def __init__(self, bounds: Dict[str, Tuple[Optional[float], Optional[float]]],
                 defaults: Dict[str, Any], *, max_age_s: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        self.bounds = dict(bounds)                 # {field: (lo, hi)} — the safe envelope
        self.defaults = dict(defaults)             # the floor when no fresh command
        self.max_age_s = max_age_s
        self._clock = clock
        self._cmds: Dict[str, Tuple[float, float]] = {}   # field -> (clamped value, ts)

    @staticmethod
    def canonical(field: str) -> str:
        return _ALIASES.get(str(field).strip().lower(), str(field).strip().lower())

    def bounded(self, field: str) -> bool:
        """Does ``field`` have a complete envelope — a finite floor AND ceiling? Only such a
        field may be commanded (the enables always are: they are 0 or 1)."""
        if field in _ENABLES:
            return True
        lo, hi = self.bounds.get(field, (None, None))
        return _finite(lo) and _finite(hi)

    def _clamp(self, field: str, value: float) -> float:
        v = float(value)
        if not math.isfinite(v):
            raise ValueError(f"{field}: {value!r} is not a finite number")
        lo, hi = self.bounds.get(field, (None, None))
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        if field in _ENABLES:
            return 1 if v >= 0.5 else 0
        return v

    def command(self, field: str, value: Any, *, ts: Optional[float] = None) -> float:
        """Accept one EMS command. Clamps to ``bounds``; raises on an unknown field. Returns the
        clamped value actually stored (so the caller/EMS sees the ceiling that was applied)."""
        f = self.canonical(field)
        if f not in CONTROL_FIELDS:
            raise ValueError(f"not a controllable Virtual BMS field: {field!r}")
        if not self.bounded(f):
            # an EMS must not be able to put ANY number on the wire: no envelope, no command
            # (WK7-SAFE-3). The field keeps its configured floor.
            raise ValueError(f"{f} has no configured bounds — it cannot be commanded")
        clamped = self._clamp(f, value)
        self._cmds[f] = (clamped, self._clock() if ts is None else ts)
        if clamped != float(value):
            logger.warning("vbms control: %s commanded %s → clamped to %s (safe envelope)", f, value, clamped)
        else:
            logger.info("vbms control: %s = %s", f, clamped)
        return clamped

    def handle_command(self, topic: str, payload: Any) -> float:
        """Route one ``.../cmd/<field>`` MQTT message to ``command``."""
        field = str(topic).rstrip("/").rsplit("/", 1)[-1]
        return self.command(field, payload)

    def resolve(self) -> Dict[str, Any]:
        """The control values to feed the frame builder now: each field's fresh command, else its
        default floor. A stale command is dropped (→ floor), so a dead EMS is safe."""
        now = self._clock()
        out: Dict[str, Any] = {}
        for field, value in self.defaults.items():
            # the floor goes through the SAME envelope as a command: a floor of 999 under a
            # ceiling of 80 resolves to 80, not 999 (WK7-SAFE-3). One that is not a finite
            # number is dropped — the field is then absent, not sent as something else.
            try:
                out[field] = self._clamp(field, value) if field in CONTROL_FIELDS else value
            except (TypeError, ValueError):
                logger.error("vbms control: floor %s=%r is not a finite number — not used",
                             field, value)
        for field, (value, ts) in self._cmds.items():
            if now - ts <= self.max_age_s:
                out[field] = value
        return out
