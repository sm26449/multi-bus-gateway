# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Virtual BMS — the in-process executor (ADR-0037).

``VbmsManager`` is to the Virtual BMS what ``VirtualMeterManager`` is to the virtual meters: it
reads ``config.virtual_bms`` instances, builds a ``VirtualBms`` + ``VbmsControl`` per instance,
sources each one's **live** measured fields from the real pack devices MBG reads (through an
injected ``read_value``, so the value-store format stays out of here), applies the EMS control
plane, and — **only for an armed instance** — runs a ``VbmsRuntime`` loop on a thread through a
``SocketCanTx``.

Safety: an instance is **disarmed by default**; nothing transmits until ``arm(id)``. The host
must take the CAN interface out of listen-only (``SocketCanTx`` refuses otherwise). The pure
pieces (aggregation, control, encoding) are already tested; this wires them to live data and a
thread. The MQTT subscription that calls ``handle_command``, the config schema/validation, and
the arm-from-UI endpoint are thin wiring around this (lifecycle lot).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from multibus.outputs.vbms import VbmsRuntime, VirtualBms
from multibus.codec.can_signal import _get
from multibus.outputs.vbms import rows_by_can_id
from multibus.outputs.vbms_admission import (RECOVERY_DEFAULTS, LimitRamp, PackAdmission,
                                             derate, flag_bits)
from multibus.outputs.vbms_bank import SENT_AS, aggregate_bank, eligible
from multibus.outputs.vbms_control import CONTROL_FIELDS, VbmsControl

logger = logging.getLogger(__name__)

# pack value-store field  →  aggregate_bank key. Default for seplos_bms_v2/_v3; override per
# instance with ``field_map`` when a pack template names things differently.
DEFAULT_FIELD_MAP = {
    "soc": "soc", "soh": "soh",
    "pack_voltage": "voltage", "battery_current": "current",
    "max_cell_voltage": "cell_v_max", "min_cell_voltage": "cell_v_min",
    "temp_max": "temp_max", "temp_min": "temp_min",
    "total_capacity": "capacity_total",
}


_FLAGS = ("protection_flags", "alarm_flags")

# what a bank that cannot be vouched for is allowed to do: nothing, on either side
_NO_FLOW = {"charge_current_limit": 0, "discharge_current_limit": 0,
            "charge_enable": 0, "discharge_enable": 0}


def _close(a: Any, b: Any, tol: float) -> bool:
    """Both present and within ``tol`` — for the inverter cross-check (what we send vs what it read)."""
    try:
        return a is not None and b is not None and abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


def _bounds(raw: Any) -> Dict[str, Tuple[Optional[float], Optional[float]]]:
    out: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
    for field, pair in (raw or {}).items():
        lo, hi = (list(pair) + [None, None])[:2] if pair is not None else (None, None)
        out[field] = (lo, hi)
    return out


class VbmsGateClosed(RuntimeError):
    """Arming was refused: the live transmit gate is closed (the reason is the message)."""


def _stored(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """An instance's config as stored — without ``armed``, which is runtime state only."""
    return {k: v for k, v in cfg.items() if k != "armed"}


class VbmsInstance:
    def __init__(self, cfg: Dict[str, Any], rows: List[Any]):
        # the schema is enforced HERE too, not only by the routes: whatever brought this
        # config (the UI, an import, a restore, a hand edit read at boot), an instance that
        # fails it is never built — so it can never be armed (R2D-VBMS-2)
        from multibus.outputs.vbms_registry import structural_errors
        errs = structural_errors(cfg)
        if errs:
            raise ValueError("invalid virtual_bms instance: " + "; ".join(errs))
        self.cfg = _stored(cfg)                    # what it was built from (reconcile compares)
        self.id = cfg["id"]
        self.channel = cfg.get("channel", "can0")
        self.packs: List[str] = list(cfg.get("packs") or [])
        self.period_s = float(cfg.get("period_s", 1.0))
        self.field_map = dict(cfg.get("field_map") or DEFAULT_FIELD_MAP)
        self.manufacturer = cfg.get("manufacturer", "SHEnergy")
        self.inverter = cfg.get("inverter")        # optional: the inverter device it feeds (cross-check)
        fields = cfg.get("fields") or {}
        self.vbms = VirtualBms(rows, name=self.id,
                               send_optional=bool(fields.get("send_optional", True)))
        self.control = VbmsControl(_bounds(cfg.get("bounds")), dict(cfg.get("floor") or {}),
                                   max_age_s=float(cfg.get("command_max_age_s", 60)))
        # Can the bank be vouched for? (design §4.1, WK7-SAFE-2.) Fewer than `min_packs`
        # eligible packs is a DATA failure: both limits and enables go to 0 for `hold_s`,
        # then the executor is silent until enough packs are back. Default max(1, N-1).
        n = len(self.packs)
        mp = cfg.get("min_packs")
        self.min_packs = max(1, int(mp)) if mp is not None else max(1, n - 1)
        self.hold_s = float((cfg.get("fallback") or {}).get("hold_s", 5))
        self._fail_since: Optional[float] = None
        # which packs the bank may count on, and how fast its limits may rise (R2D-VBMS-3,
        # design §4.1/§4.6/§4.9): limits from the ADMITTED packs only, a slow return, a
        # flapping hold, and a ramp after every (re)start
        rec = {**RECOVERY_DEFAULTS, **(cfg.get("recovery") or {})}
        self.pack_limits: Dict[str, Any] = dict(cfg.get("pack_limits") or {})
        self.admission = PackAdmission(self.packs, return_after_s=rec["return_after_s"],
                                       flap_drops=rec["flap_drops"],
                                       flap_window_s=rec["flap_window_s"])
        self.ramp = LimitRamp(rec["slew_a_per_s"], max_dt_s=self.period_s + 1.0)
        # the wire width of each alarm/protection field, so a garbage flag fails safe AND fits
        self.flag_widths: Dict[str, int] = {}
        for rs in rows_by_can_id(rows).values():
            for r in rs:
                name, bl = _get(r, "name"), _get(r, "bit_length")
                if name in _FLAGS and isinstance(bl, int) and bl > 0:
                    self.flag_widths[name] = min(self.flag_widths.get(name, bl), bl)
        self._lock = threading.Lock()              # the executor, telemetry and the API all observe
        self._thread: Optional[threading.Thread] = None
        self._stop: Optional[threading.Event] = None
        self.runtime: Any = None                   # the last executor (its refused / failures)
        # ALWAYS built disarmed: `armed` is runtime state, set only by an explicit arm. A
        # hand-written `armed: true` in the file must not arm anything at boot or on a reload.

    @property
    def armed(self) -> bool:
        return self.vbms.armed

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


class VbmsManager:
    """Owns the configured Virtual BMS instances and their executor threads."""

    def __init__(self, config: Any,
                 read_value: Callable[[str, str], Any],
                 load_profile: Callable[[str], List[Any]], *,
                 tx_factory: Optional[Callable[[str], Any]] = None,
                 gate: Optional[Callable[[], Optional[str]]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 state_path: Optional[Path] = None):
        self.read_value = read_value          # (device_id, store_field) -> value | None
        self.load_profile = load_profile      # profile name -> CAN rows
        self._tx_factory = tx_factory         # channel -> CanTx (default: SocketCanTx)
        self._clock = clock
        self._wall = wall
        # where each instance's flap/hold state is kept across rebuilds and restarts
        # (R2D-VBMS-3 review): a held pack must not come back by a restart
        self._state_path = Path(state_path) if state_path is not None else None
        self._persist_lock = threading.Lock()
        # None (may transmit) or the reason it may not — read LIVE: when arming and before
        # every frame set of a running executor (WK7-SAFE-1)
        self.gate = gate
        self._frozen: Optional[str] = None    # why nothing may be armed right now (a reload)
        self._arm_lock = threading.RLock()    # arm / revoke / freeze / reconcile never interleave
        self.instances: Dict[str, VbmsInstance] = {}
        self.configure(config)

    @staticmethod
    def _instances_cfg(config: Any) -> List[Dict[str, Any]]:
        if isinstance(config, dict):
            return list(config.get("virtual_bms") or [])
        return list(getattr(config, "virtual_bms", None) or [])

    def configure(self, config: Any) -> None:
        for cfg in self._instances_cfg(config):
            try:
                rows = self.load_profile(cfg["profile"])
                self.instances[cfg["id"]] = self._build(cfg, rows)
            except Exception:  # noqa: BLE001 — a bad instance must not stop the others
                logger.exception("virtual_bms instance %r failed to configure", cfg.get("id"))

    def reconcile(self, config: Any) -> Dict[str, List[str]]:
        """Bring the running instances in line with ``config.virtual_bms`` after the config
        was reloaded or applied (WK7-LIFE-1) — until now only boot and the CRUD routes did:

        * an instance no longer configured is stopped and dropped;
        * one whose stored config changed is stopped and rebuilt **disarmed**;
        * a new one is built disarmed;
        * one whose config is identical is left exactly as it is, armed or not.

        Returns ``{removed, changed, added, kept}``. A config that cannot be built is logged
        and left out (its old instance, if any, is stopped) — never half-applied."""
        with self._arm_lock:
            return self._reconcile(config)

    def _reconcile(self, config: Any) -> Dict[str, List[str]]:
        wanted = {c.get("id"): c for c in self._instances_cfg(config) if isinstance(c, dict)}
        out: Dict[str, List[str]] = {"removed": [], "changed": [], "added": [], "kept": []}
        for iid in [i for i in self.instances if i not in wanted]:
            self.remove(iid)
            out["removed"].append(iid)
        for iid, cfg in wanted.items():
            old = self.instances.get(iid)
            if old is not None and old.cfg == _stored(cfg):
                out["kept"].append(iid)
                continue
            try:
                self.add_or_replace(cfg)
                out["changed" if old is not None else "added"].append(iid)
            except Exception:  # noqa: BLE001 — one bad instance must not stop the others
                logger.exception("virtual_bms instance %r failed to configure", iid)
                if old is not None:
                    self.remove(iid)
                    out["removed"].append(iid)
        if out["removed"] or out["changed"] or out["added"]:
            logger.warning("virtual BMS reconciled: +%s ~%s -%s (changed and new are disarmed)",
                           out["added"], out["changed"], out["removed"])
        return out

    # ── persisted admission (flap drops + held), R2D-VBMS-3 ─────────────
    def _load_saved(self) -> Dict[str, Any]:
        if self._state_path is None:
            return {}
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            logger.error("virtual BMS: pack admission state %s unreadable (%s) — starting "
                         "without it", self._state_path, e)
            return {}
        return data if isinstance(data, dict) else {}

    def _build(self, cfg: Dict[str, Any], rows: List[Any]) -> VbmsInstance:
        inst = VbmsInstance(cfg, rows)
        now = self._clock()
        inst.admission.started = now
        inst.admission.restore(self._load_saved().get(inst.id), now, self._wall())
        return inst

    def _persist(self) -> None:
        """Write every instance's admission state (atomic: tmp + fsync + replace). Called
        only when a drop, a hold or a clear changed it; a failure is logged, never raised —
        the in-memory state still holds the pack out."""
        if self._state_path is None:
            return
        now, wall = self._clock(), self._wall()
        doc: Dict[str, Any] = {}
        for inst in list(self.instances.values()):
            with inst._lock:
                inst.admission.dirty = False
                kept = inst.admission.export(now, wall)
            if kept:
                doc[inst.id] = kept
        with self._persist_lock:
            tmp = self._state_path.with_suffix(".json.tmp")
            try:
                self._state_path.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(doc, f, sort_keys=True)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self._state_path)
            except OSError as e:
                logger.error("virtual BMS: could not save pack admission state (%s)", e)

    # ── live values (measured aggregation + EMS control) ──────────────────
    def _read_packs(self, inst: VbmsInstance) -> Dict[str, Dict[str, Any]]:
        """``{pack device: its canonical values}`` for every pack that returned anything."""
        out: Dict[str, Dict[str, Any]] = {}
        for dev in inst.packs:
            p: Dict[str, Any] = {}
            for store_field, agg_key in inst.field_map.items():
                v = self.read_value(dev, store_field)
                if v is not None:
                    p[agg_key] = v
            if p:
                out[dev] = p
        return out

    def pack_values(self, inst: VbmsInstance) -> List[Dict[str, Any]]:
        return list(self._read_packs(inst).values())

    def _evaluate(self, inst: VbmsInstance,
                  by_dev: Dict[str, Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]], float]:
        """One observation: advance the pack admission (§4.1), then judge the bank on the
        ADMITTED packs. Returns ``(data_state, admitted packs, now)``.

        ``ok`` (enough admitted packs — the bank is vouched for), ``hold`` (a data failure
        inside its ``hold_s``: frames still go out, with both limits and both enables at 0) or
        ``silent`` (the failure outlasted the hold: nothing is sent). The failure clock starts
        at the first failing read by ANY caller, armed or not — an instance armed on a bank it
        cannot vouch for goes straight to what the elapsed time says. A pack that dropped comes
        back only after ``return_after_s`` (R2D-VBMS-3), and the limits ramp up from 0."""
        with inst._lock:
            now = self._clock()
            fresh = [d for d, p in by_dev.items() if eligible([p])]
            admitted = inst.admission.observe(fresh, now)
            changed = inst.admission.dirty
            if len(admitted) >= inst.min_packs:
                inst._fail_since = None
                state = "ok"
            else:
                if inst._fail_since is None:
                    inst._fail_since = now
                state = "hold" if (now - inst._fail_since) < inst.hold_s else "silent"
        if changed:
            self._persist()
        return state, [by_dev[d] for d in admitted], now

    def _compose(self, inst: VbmsInstance, by_dev: Dict[str, Dict[str, Any]],
                 admitted: List[Dict[str, Any]], data_state: str = "ok") -> Dict[str, Any]:
        """Measured fields of the ADMITTED packs + the resolved (clamped, fresh-or-floor)
        control fields, capped to what those packs can carry + the manufacturer — the
        ``values`` the frame builder and telemetry both use. On a data failure the limits and
        enables are forced to 0: no EMS command and no configured floor may keep a bank that
        cannot be vouched for charging or discharging. Alarm/protection bits are OR-ed over
        every pack that reported them, admitted or not: an alarm is never hidden."""
        control = inst.control.resolve()
        if data_state != "ok":
            control.update(_NO_FLOW)
        else:
            derate(control, admitted, len(inst.packs), inst.pack_limits)
        measured = aggregate_bank([{k: v for k, v in p.items() if k not in _FLAGS}
                                   for p in admitted])
        for field in _FLAGS:
            bits = [p[field] for p in by_dev.values() if field in p]
            if bits:
                width = inst.flag_widths.get(field, 16)
                acc = 0
                for b in bits:
                    acc |= flag_bits(b, width)
                measured[field] = acc
        return {**measured, **control, "manufacturer": inst.manufacturer}

    def _snapshot(self, inst: VbmsInstance) -> Tuple[str, Dict[str, Any], int]:
        """``(data_state, values, admitted count)`` for telemetry and previews — the limits
        no higher than what the executor last sent (the ramp is not advanced here)."""
        by_dev = self._read_packs(inst)
        state, admitted, _now = self._evaluate(inst, by_dev)
        vals = self._compose(inst, by_dev, admitted, state)
        if state == "ok":
            with inst._lock:
                vals = inst.ramp.view(vals)
        return state, vals, len(admitted)

    def values(self, inst: VbmsInstance) -> Dict[str, Any]:
        """The full ``values`` dict for this instance now (telemetry / preview)."""
        return self._snapshot(inst)[1]

    def tx_values(self, inst: VbmsInstance) -> Optional[Dict[str, Any]]:
        """What the executor sends this tick — or None for SILENCE (data failure past its
        hold). The current limits ramp up from 0 after every failure (§4.9)."""
        by_dev = self._read_packs(inst)
        state, admitted, now = self._evaluate(inst, by_dev)
        if state == "silent":
            with inst._lock:
                inst.ramp.reset()
            return None
        vals = self._compose(inst, by_dev, admitted, state)
        with inst._lock:
            if state != "ok":
                inst.ramp.reset()
                return vals
            return inst.ramp.apply(vals, now)

    def state(self, inst: VbmsInstance) -> Dict[str, Any]:
        """A telemetry snapshot for one instance: the ``values`` it is sending (what the inverter
        obeys), whether it is armed/running, its channel, and how many source packs are
        admitted (``packs_live``) with each pack's admission state."""
        data, vals, n = self._snapshot(inst)
        with inst._lock:
            admission = inst.admission.snapshot(self._clock())
        return {"values": vals, "armed": inst.armed,
                "running": inst.running, "channel": inst.channel,
                "packs_total": len(inst.packs), "packs_live": n,
                "min_packs": inst.min_packs, "data_state": data, "admission": admission}

    def clear_pack(self, instance_id: str, pack: str) -> bool:
        """Readmit a pack held out for flapping (design §3.4). It still serves its
        ``return_after_s`` and enters through the ramp. Returns whether it was held."""
        inst = self.instances[instance_id]
        with inst._lock:
            cleared = inst.admission.clear(pack)
        if cleared:
            self._persist()
            logger.warning("virtual BMS %s: pack %s cleared by the operator — it returns after "
                           "return_after_s", instance_id, pack)
        return cleared

    def preview(self, inst: VbmsInstance) -> Dict[str, Any]:
        """A transparency snapshot (ADR-0045 L2): each source pack's contributed canonical values
        and whether it is eligible (has SOC + voltage), plus the aggregated values the instance
        sends. Lets the operator see WHAT is aggregated and FROM WHERE before/while configured."""
        # ONE snapshot: the per-pack columns and the sent values come from the same read, so
        # the table never shows a pack value next to a sum computed a moment later
        by_dev = self._read_packs(inst)
        data, admitted, now = self._evaluate(inst, by_dev)
        sends = self._compose(inst, by_dev, admitted, data)
        with inst._lock:
            if data == "ok":
                sends = inst.ramp.view(sends)
            admission = inst.admission.snapshot(now)
        packs = []
        for dev in inst.packs:
            vals = by_dev.get(dev, {})
            packs.append({"id": dev, "values": vals,
                          "eligible": vals.get("soc") is not None and vals.get("voltage") is not None,
                          "admission": admission[dev]["state"]})
        out = {"packs": packs, "sends": sends,
               "sent_as": dict(SENT_AS),                     # pack key → the sent field it feeds
               "control": {f: sends.get(f) for f in CONTROL_FIELDS},   # what the inverter obeys
               "packs_total": len(inst.packs),
               "packs_live": len(admitted),
               "min_packs": inst.min_packs,
               "data_state": data,
               "send_optional": inst.vbms.send_optional,
               "frames": [f"0x{c:X}" for c in inst.vbms.can_ids()]}
        if inst.inverter:                           # cross-check: what we send vs what it received
            sent_soc, sent_v = sends.get("soc"), sends.get("pack_voltage")
            got_soc = self.read_value(inst.inverter, "bms_soc")
            got_v = self.read_value(inst.inverter, "bms_voltage")
            out["inverter_check"] = {
                "inverter": inst.inverter,
                "soc": {"sent": sent_soc, "received": got_soc,
                        "match": _close(sent_soc, got_soc, 2)},
                "voltage": {"sent": sent_v, "received": got_v,
                            "match": _close(sent_v, got_v, 1)}}
        return out

    def handle_command(self, instance_id: str, topic: str, payload: Any) -> float:
        return self.instances[instance_id].control.handle_command(topic, payload)

    def command(self, instance_id: str, field: str, payload: Any) -> float:
        """Apply one EMS command to a control field (clamped to the safe envelope). Mutates only
        the control plane; it transmits nothing — an armed executor picks the value up on its next
        cycle, a disarmed one simply stores it. Raises KeyError for an unknown instance, ValueError
        for an uncontrollable field."""
        return self.instances[instance_id].control.command(field, payload)

    def command_topics(self, topic_pattern: str = "mbg/devices/{device}") -> Dict[str, Tuple[str, str]]:
        """``{topic: (instance_id, field)}`` for every control field of every instance — the
        ``<base>/cmd/<field>`` set an EMS drives, where ``<base>`` comes from the configured device
        topic pattern (no hardcoding: the pattern is config-owned)."""
        out: Dict[str, Tuple[str, str]] = {}
        for inst in self.instances.values():
            base = topic_pattern.format(device=inst.id)
            for field in CONTROL_FIELDS:
                out[f"{base}/cmd/{field}"] = (inst.id, field)
        return out

    # ── executor threads (armed only) ────────────────────────────────────
    def _new_tx(self, channel: str) -> Any:
        if self._tx_factory is not None:
            return self._tx_factory(channel)
        from multibus.transports.can.can_tx import SocketCanTx
        return SocketCanTx(channel)

    def start_instance(self, inst: VbmsInstance) -> bool:
        """Run the executor loop for an armed instance. No-op if disarmed or already running."""
        if not inst.armed or inst.running:
            return False
        inst._stop = threading.Event()
        with inst._lock:
            inst.ramp.reset()                      # a (re)arm enters through the ramp (§4.9)
        rt = VbmsRuntime(inst.vbms, self._new_tx(inst.channel),
                         lambda inst=inst: self.tx_values(inst), period_s=inst.period_s,
                         gate=lambda: self.gate() if self.gate is not None else None)
        inst.runtime = rt

        def _run(stop=inst._stop, rt=rt, inst=inst):
            try:
                rt.run(stop)
            except BaseException as e:  # noqa: BLE001 — never an unhandled trace in the thread
                # the loop CRASHED (its own per-cycle handling did not hold): the instance
                # is not transmitting and will not start again by itself — say so (disarmed)
                # instead of "armed" for ever. A transmitter that would not OPEN is not this
                # case: run() returns, the instance stays armed-but-not-running (the UI's
                # "Armed · not transmitting", with the cause in last_error) — the operator's
                # cue to take the port out of listen-only.
                logger.exception("virtual BMS %s executor crashed — disarmed", inst.id)
                rt.refused = f"the executor crashed: {type(e).__name__}: {e}"
                if inst.vbms.armed:
                    inst.vbms.disarm()
        inst._thread = threading.Thread(target=_run,
                                        name=f"vbms-{inst.id}", daemon=True)
        inst._thread.start()
        logger.warning("virtual BMS %s executor started on %s", inst.id, inst.channel)
        return True

    def stop_instance(self, inst: VbmsInstance) -> None:
        if inst._stop is not None:
            inst._stop.set()
        if inst._thread is not None:
            inst._thread.join(timeout=inst.period_s + 2)
        inst._thread = inst._stop = None
        with inst._lock:
            inst.ramp.reset()

    # ── live reconfigure (UI CRUD, ADR-0045) ─────────────────────────────
    def add_or_replace(self, cfg: Dict[str, Any]) -> VbmsInstance:
        """Add one instance from its config dict, or replace an existing one — built **disarmed**.
        An existing instance is stopped first. The profile rows are resolved through load_profile."""
        iid = cfg["id"]
        old = self.instances.get(iid)
        if old is not None:
            self.stop_instance(old)
        self.instances[iid] = self._build(cfg, self.load_profile(cfg["profile"]))
        return self.instances[iid]

    def remove(self, instance_id: str) -> bool:
        """Stop and drop one instance. Returns whether it existed."""
        inst = self.instances.pop(instance_id, None)
        if inst is not None:
            self.stop_instance(inst)
        return inst is not None

    def arm(self, instance_id: str) -> None:
        """Arm and start one instance. Refused (``VbmsGateClosed``) while the live gate is
        closed — the same gate the running loop obeys."""
        with self._arm_lock:
            inst = self.instances[instance_id]
            reason = self._frozen or (self.gate() if self.gate is not None else None)
            if reason:
                raise VbmsGateClosed(reason)
            inst.vbms.arm()
            self.start_instance(inst)

    def freeze(self, reason: str) -> List[str]:
        """Disarm everything AND refuse every arm until ``thaw()`` — for the whole of a config
        import/restore. A revoke alone leaves a window: an arm that lands while the reload is
        still running would survive it (R2D-LIFE-1)."""
        with self._arm_lock:
            self._frozen = reason
            return self.revoke(reason)

    def thaw(self) -> None:
        with self._arm_lock:
            self._frozen = None

    def revoke(self, reason: str) -> List[str]:
        """Disarm and stop EVERY armed instance, now — the synchronous half of the live gate,
        called the moment the gateway turns standby or the master write switch goes off (the
        loop's own check at each frame set is the backstop for every other path). Returns the
        ids it disarmed; they stay disarmed until an explicit re-arm."""
        out = []
        with self._arm_lock:
            for inst in list(self.instances.values()):
                if inst.armed or inst.running:
                    inst.vbms.disarm()
                    self.stop_instance(inst)
                    out.append(inst.id)
        if out:
            logger.warning("virtual BMS: %s disarmed — %s", ", ".join(out), reason)
        return out

    def disarm(self, instance_id: str) -> None:
        inst = self.instances[instance_id]
        inst.vbms.disarm()
        self.stop_instance(inst)      # loop stops → tx closes → inverter sees safe BMS-loss

    def start_all(self) -> None:
        for inst in self.instances.values():
            self.start_instance(inst)

    def stop_all(self) -> None:
        for inst in self.instances.values():
            self.stop_instance(inst)

    def status(self) -> Dict[str, Any]:
        out = {}
        for i in self.instances.values():
            # the state only: status must answer even when a pack value would not aggregate
            data, admitted, _now = self._evaluate(i, self._read_packs(i))
            n = len(admitted)
            with i._lock:
                admission = i.admission.snapshot(self._clock())
            out[i.id] = {"armed": i.armed, "running": i.running, "channel": i.channel,
                         "packs": i.packs, "packs_live": n, "admission": admission,
                         "min_packs": i.min_packs, "data_state": data,
                         # why the last cycle sent nothing — also after the executor ended
                         # (transmitter would not open, revoked): the cause, not only the log
                         "last_error": (getattr(i.runtime, "revoked", None)
                                        or getattr(i.runtime, "refused", None))}
        return out
