# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Per-device register, poll-group, calculated and energy-field files."""
import copy
import json
import logging
import os

from pathlib import Path
from typing import Dict, List, Optional

from multibus.core.config.util import (
    validate_register_identity,
)
from multibus.core.config.models import (
    PRIMARY_DEVICE_ID, DeviceConfig,
    PollGroup,
    SelectedRegister,
)

logger = logging.getLogger("multibus.core.config")


class RegisterFilesMixin:
    def save_device_registers(self, device_id: str, registers: List[Dict],
                              poll_groups: Optional[Dict] = None,
                              source_id: str = "", replace: bool = False) -> None:
        """Persist a device's register selection, or ONE SOURCE's.

        A source keeps its own file because it has its own template and address
        space; without ``source_id`` this writes the device-level file, exactly
        as it always has. Primary keeps using save_selected_registers()."""
        if device_id == PRIMARY_DEVICE_ID and not source_id:
            self.save_selected_registers(registers)
            return
        validate_register_identity(registers)      # audit DP-9
        path = (self.source_registers_path(device_id, source_id) if source_id
                else self.device_registers_path(device_id))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + '.tmp')   # atomic: crash-safe write
        with self._file_lock:
            # ``replace``: a fresh map (a device seeded anew from a template) —
            # nothing of what an earlier device at this id kept may survive
            kept = {} if replace else self._kept_sections(path)
            # no groups given: the device's own (tuned on its page) stay
            data = {
                **kept,
                "version": "1.0",
                "registers": registers,
                "poll_groups": poll_groups or kept.get("poll_groups") or {
                    name: {"interval": g.interval, "description": g.description}
                    for name, g in self.poll_groups.items()
                },
            }
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())           # durable before the rename
            os.replace(tmp, path)
        logger.info("device %s%s: saved %d selected registers", device_id,
                    f" source {source_id}" if source_id else "", len(registers))

    def device_registers_path(self, device_id: str) -> Path:
        """Where a device's selected-registers file lives. Device #1 keeps the
        legacy path (unchanged installs); others get config/devices/<id>/.

        The id is validated as a single safe path segment here — this is the
        chokepoint every register read/write and the energy endpoints flow
        through, so a crafted ``device`` param (``..``, ``../../tmp/x``) can
        never escape config/devices/ regardless of the caller's own checks."""
        if device_id == PRIMARY_DEVICE_ID:
            return self.registers_path
        return (self.config_path.parent / 'devices'
                / self._safe_device_id(device_id) / 'selected_registers.json')

    def source_registers_path(self, device_id: str, source_id: str) -> Path:
        """Where ONE SOURCE's selected registers live.

        A source has its own template and therefore its own address space: the
        SunSpec view of an inverter reads holding registers, the Solar API view
        reads JSON paths, and merging them into one file would make neither
        editable. So each source gets its own file under
        ``devices/<device>/sources/<source>/``.

        Nothing existing moves: a device that has never declared sources has no
        such directory, and :meth:`load_device_registers` falls back to the
        device-level file. Adding a second source to a live device therefore
        leaves the first one's registers exactly where they are."""
        return (self.config_path.parent / 'devices'
                / self._safe_device_id(device_id) / 'sources'
                / self._safe_device_id(source_id) / 'selected_registers.json')

    def save_device_poll_groups(self, device_id: str, groups: Dict[str, Dict]) -> Dict[str, Dict]:
        """Update just the poll-group intervals in a device's registers file
        (keeps the register selection). Works for any device — the primary uses
        the legacy file and its in-memory groups are refreshed too.

        Merged per group, as every save (save_merge.py): a group not sent keeps
        its stored interval — a missing one would fall back to the global
        default, and a fragile datalogger to a rhythm it cannot take; a key not
        sent (a description) is kept; a group sent as None is removed (back to
        the default). Returns the groups as stored."""
        # Reject a 0/negative interval up front with a clear error: it would
        # spin the poll loop with no pause and hammer the bus. Sub-second is
        # allowed (the ESS control loop needs 250 ms) but a real floor stands.
        for name, g in (groups or {}).items():
            if g is None:
                continue
            try:
                iv = float(g.get("interval", 5))
            except (TypeError, ValueError):
                raise ValueError(f"poll group {name!r}: interval must be a number")
            if iv < 0.05:
                raise ValueError(f"poll group {name!r}: interval must be >= 0.05 s "
                                 f"(got {iv}) — 0 would flood the bus")
        path = self.device_registers_path(device_id)
        with self._file_lock:                  # serialize the read-modify-write
            if path.exists():
                with open(path) as f:
                    data = json.load(f)
            else:
                data = {"version": "1.0", "registers": [], "poll_groups": {}}
            merged = {k: dict(v) for k, v in (data.get("poll_groups") or {}).items() if isinstance(v, dict)}
            for name, g in (groups or {}).items():
                if g is None:
                    merged.pop(name, None)
                else:
                    merged[name] = {**merged.get(name, {}), **g}
            data["poll_groups"] = merged
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())           # durable before the rename
            os.replace(tmp, path)
        if device_id == PRIMARY_DEVICE_ID:
            for name, g in merged.items():
                self.poll_groups[name] = PollGroup(interval=float(g.get("interval", 5)),
                                                   description=g.get("description", ""))
            # a group removed goes back to the gateway's own rhythm in memory
            # too — the running poller and GET must agree with the file
            for name, g in (groups or {}).items():
                if g is None and name not in merged:
                    d = self.default_poll_groups.get(name)
                    if d is not None:
                        self.poll_groups[name] = PollGroup(interval=d.interval, description=d.description)
                    else:
                        self.poll_groups.pop(name, None)
        logger.info(f"device {device_id}: poll groups updated {list(groups.keys())}")
        return merged

    def load_energy_fields(self, device_id: str) -> List[Dict]:
        """The device's saved Energy-tab field selection (the cumulative counters
        to total per month), or [] if the user hasn't picked any yet."""
        if not device_id:                      # no primary (ADR-0024): nothing saved
            return []
        path = self.device_registers_path(device_id)
        if not path.exists():
            return []
        try:
            with open(path) as f:
                return json.load(f).get("energy_fields", []) or []
        except Exception:  # noqa: BLE001
            return []

    def load_calculated(self, device_id: str) -> List[Dict]:
        """The device's saved calculated registers (formula-derived measurements),
        or [] if none. Each entry: {name, label, unit, expr, poll_group, decimals}."""
        if not device_id:                      # no primary (ADR-0024): nothing saved
            return []
        path = self.device_registers_path(device_id)
        if not path.exists():
            return []
        try:
            with open(path) as f:
                return json.load(f).get("calculated", []) or []
        except Exception:  # noqa: BLE001
            return []

    def save_calculated(self, device_id: str, calculated: List[Dict]) -> None:
        """Persist the device's calculated registers alongside its register file
        (a top-level ``calculated`` list, next to registers/poll_groups)."""
        path = self.device_registers_path(device_id)
        with self._file_lock:                  # serialize the read-modify-write
            if path.exists():
                with open(path) as f:
                    data = json.load(f)
            else:
                data = {"version": "1.0", "registers": [], "poll_groups": {}}
            data["calculated"] = calculated
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + '.tmp')
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)

    def load_calculated_templates(self) -> List[Dict]:
        """User-saved reusable calculated presets (global, not per-device)."""
        p = self.config_path.parent / "calculated_templates.json"
        if not p.exists():
            return []
        try:
            with open(p) as f:
                return json.load(f).get("templates", []) or []
        except Exception:  # noqa: BLE001
            return []

    def save_calculated_templates(self, templates: List[Dict]) -> None:
        """Persist the user's reusable calculated presets (atomic)."""
        p = self.config_path.parent / "calculated_templates.json"
        with self._file_lock:                  # load()+save() cycles race otherwise
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + '.tmp')
            with open(tmp, 'w') as f:
                json.dump({"version": "1.0", "templates": templates}, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)

    def save_energy_fields(self, device_id: str, fields: List[Dict]) -> None:
        """Persist the Energy-tab field selection alongside the device's registers
        file (a top-level ``energy_fields`` list, next to registers/poll_groups)."""
        if not device_id:
            raise ValueError("no device: this gateway has no primary device")
        path = self.device_registers_path(device_id)
        with self._file_lock:                  # serialize the read-modify-write
            if path.exists():
                with open(path) as f:
                    data = json.load(f)
            else:
                data = {"version": "1.0", "registers": [], "poll_groups": {}}
            data["energy_fields"] = fields
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        logger.info(f"device {device_id}: energy fields set ({len(fields)})")

    def unit_registers(self, device: DeviceConfig, *, shared: bool = False) -> List[SelectedRegister]:
        """Everything a unit reads, across its sources — the list a consumer of
        the unit (HA discovery, the device entry) sees. Each source keeps its
        own file; a name read by two sources (the arbiter picks the fresher
        at runtime) appears once, from the first source that declares it, so
        one MQTT topic never gets two discovery entries. A device without
        sources is its own single list."""
        if not (device.sources or []):
            return self.load_device_registers(device, shared=shared)[0]
        seen: set = set()
        out: List[SelectedRegister] = []
        for src in device.sources:
            for r in self.load_device_registers(device, source=src, shared=shared)[0]:
                key = r.name
                if key in seen:
                    continue
                seen.add(key)
                out.append(r)
        return out

    def load_device_registers(self, device: DeviceConfig, source=None, *, shared: bool = False):
        """Return (selected_registers, poll_groups) for a device, or for ONE of
        its sources.

        A source's own file wins when it exists; otherwise the device-level file
        applies, which is what makes a single-source device — every device
        written before sources existed — behave exactly as before. A source may
        also override the intervals: a fast partial source and a slow complete
        one do not share a rhythm.

        ``shared=True`` (a caller that only reads, such as the device list)
        gets the cached register objects themselves, not copies."""
        if device.primary and source is None:
            return self.selected_registers, self.poll_groups
        path = None
        if source is not None:
            sp = self.source_registers_path(device.id, source.id)
            if sp.exists():
                path = sp
        if path is None:
            path = (self.registers_path if device.primary
                    else self.device_registers_path(device.id))
        if not path.exists():
            logger.info(f"device {device.id}: no selected registers yet ({path})")
            return [], dict(self.poll_groups)
        # the device list asks for every device's selection on every call: a
        # file not changed since it was last read is not read (nor snapshotted)
        # again — its parsed registers come back as copies
        sig = self._file_sig(path)
        hit = self._regs_cache.get(path) if sig else None
        if hit is not None and hit[0] == sig:
            data, parsed = hit[1], hit[2]
            regs = list(parsed) if shared else [copy.copy(r) for r in parsed]
        else:
            data = self._load_json_with_heal(path, required_key='registers',
                                             label=f"device {device.id} registers")
            if data is None:
                return [], dict(self.poll_groups)
            regs = None
        try:
            if regs is None:
                parsed = self._parse_selected_payload(data)
                if sig and self._file_sig(path) == sig:
                    self._regs_cache[path] = (sig, data, parsed)
                regs = list(parsed) if shared else [copy.copy(r) for r in parsed]
            groups = dict(self.poll_groups)
            for name, group in (data.get('poll_groups') or {}).items():
                # merged per key, as the save is: a group stored with only an
                # interval keeps the gateway group's description (QA-R1-04)
                base = groups.get(name)
                groups[name] = PollGroup(interval=group.get('interval', 5),
                                         description=group.get('description', base.description if base else ''))
            # A source's declared intervals are the last word: they describe how
            # fast THIS way of reaching the unit may be driven, which the shared
            # device file cannot know.
            for name, g in (getattr(source, 'poll_groups', None) or {}).items():
                groups[name] = g
            if source is not None and getattr(source, 'poll_groups', None):
                # and it polls ONLY the groups it declared, so a 5 s HTTP source
                # does not inherit a 3600 s static group it has no registers for
                regs = [r for r in regs if r.poll_group in source.poll_groups]
            return regs, groups
        except Exception as e:  # noqa: BLE001
            logger.error(f"device {device.id}: error loading registers: {e}")
            return [], dict(self.poll_groups)

