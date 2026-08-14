# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Wires virtual meters into the running Janitza monitor.

Reads config/virtual_meters.yaml (which templates to run + their ports), builds
a value provider over the live ``current_values`` cache, and runs one
VirtualMeter per enabled instance. Isolated from the UI/MQTT/InfluxDB paths.
"""
from __future__ import annotations

import functools
import json
import logging
import math
import os
import re
import struct
import threading
import time
from pathlib import Path
from typing import Optional

import yaml

from .encoder import RegisterEncoder
from .register_parser import RegisterParser
from .virtual_meter import VirtualMeter, _parse_source, load_template

logger = logging.getLogger(__name__)


def _cfg_mutation(fn):
    """Serialize a whole load→modify→save cycle on virtual_meters.yaml — two
    concurrent API requests must not lose each other's changes (the per-write
    atomicity of _save_cfg protects the FILE, not the read-modify-write)."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._cfg_lock:
            return fn(self, *args, **kwargs)
    return wrapper


# Filename-safe template id (also blocks path traversal).
_ID_RE = re.compile(r"^[a-z0-9_]+$")
# Types the encoder can emit (mirrors RegisterEncoder/RegisterParser + string).
_VALID_TYPES = ["int16", "uint16", "int32", "uint32", "int64", "uint64",
                "float", "float32", "double", "string"]


# A row's derived freshness bound = GROUP_STALE_MULT × its poll interval —
# a value simply cannot refresh faster than its group polls, so judging a 60s
# slow-group row by a 15s instance bound would flap the meter (seen live at the
# 3.3.4 deploy). 2.5× tolerates one missed poll plus jitter. The vmeter takes
# max(derived, instance bound): a derived bound may only RELAX the instance
# floor, never tighten it (explicit row stale_after_s can do either).
GROUP_STALE_MULT = 2.5
# ...but the AUTO-derived bound is capped, so a misconfigured huge poll interval
# (the interval validator has only a 0.05s floor, no ceiling) can never let a
# dead source read "fresh" for hours to a control loop / ESS. 300s = 5 min is
# far above any real cadence on this system; a genuinely slower source must set
# an EXPLICIT per-row stale_after_s (uncapped) to opt out.
MAX_DERIVED_STALE_S = 300.0


def _lookup(store: dict, name: str) -> Optional[tuple]:
    """(value, MONOTONIC_ts, derived_bound) for a register NAME in one live
    cache, else None.

    The freshness clock is the driver's MONOTONIC stamp ('mono') — immune to
    any wall-clock/NTP step. It is None when the driver gave no measurement
    time (or on a legacy store entry that predates the field), so freshness
    fails CLOSED (not-fresh) rather than trusting a wall time that a clock step
    could distort. The vmeter compares this against time.monotonic().

    derived_bound is GROUP_STALE_MULT × the producing poll group's interval
    (from the store entry), or None for push sources / legacy entries — the
    instance bound then applies."""
    for info in list(store.values()):          # snapshot vs concurrent poller writes
        if not isinstance(info, dict) or info.get("name") != name:
            continue
        value = info.get("value")
        if value is None:
            return None
        _iv = info.get("interval")
        bound = (min(GROUP_STALE_MULT * float(_iv), MAX_DERIVED_STALE_S)
                 if _iv else None)
        return value, info.get("mono"), bound  # mono None → not fresh
    return None


def make_provider(current_values: dict):
    """Return provider(name) -> (value, monotonic_ts, derived_bound) reading
    ONE live cache.

    current_values is keyed by register ADDRESS; each entry carries 'name',
    'value', 'timestamp' (ISO). Sources are bound by register name.
    """
    def provider(name: str) -> Optional[tuple]:
        return _lookup(current_values, name)
    return provider


def make_multi_provider(default_store: dict, stores: dict, primary_store: dict,
                        primary_device_id: str, bounds_for=None):
    """Composite provider: resolve ``device.register`` across ALL device caches;
    a bare name stays in the instance's own store (byte-identical single-source
    behavior). Returns (value, monotonic_ts, source_stale_bound) — the third element
    lets the engine judge each row against ITS source's freshness threshold
    (a 60s BLE sensor next to a 250ms Janitza row).

    The dotted form only activates when the prefix IS a known device id —
    register names themselves may legally contain dots (HTTP/MQTT sources), so
    an unknown prefix falls back to a bare-name lookup. Device ids can't
    contain dots (validated ^[a-z0-9][a-z0-9_-]{1,63}$), so resolution is
    deterministic."""
    def provider(name: str) -> Optional[tuple]:
        if "." in name:
            dev, reg = name.split(".", 1)
            store = (primary_store if dev == primary_device_id
                     else stores.get(dev))
            if store is not None:
                got = _lookup(store, reg)
                if got is None:
                    return None
                # two bound sources may apply: the device's own staleness
                # threshold and the row's poll-cadence bound — honor the
                # looser one (both exist to prevent false-stale flapping)
                dev_bound = bounds_for(dev) if bounds_for else None
                cands = [b for b in (got[2], dev_bound) if b is not None]
                return got[0], got[1], (max(cands) if cands else None)
            # unknown prefix → the dot belongs to the register name itself
        got = _lookup(default_store, name)
        if got is None:
            return None
        return got                             # (value, mono, derived_bound)
    return provider


class VirtualMeterManager:
    def __init__(self, current_values: dict,
                 config_path: str = "config/virtual_meters.yaml",
                 templates_dir: str = "config/templates",
                 mqtt_publisher=None, modbus_client=None,
                 device_values: dict = None, primary_device_id: str = "",
                 bounds_for=None):
        self.current_values = current_values
        # bounds_for(device_id) -> the source device's own staleness threshold
        # (seconds) or None — feeds per-row freshness in composite meters.
        self.bounds_for = bounds_for
        # Per-device live caches (Phase 3): a virtual meter sources its values
        # from ONE device. Absent/primary → the legacy `current_values` (which is
        # the primary device's cache), so existing meters are byte-identical.
        self.device_values = device_values if device_values is not None else {}
        self.primary_device_id = primary_device_id
        self.config_path = Path(config_path)
        self.templates_dir = Path(templates_dir)
        self.meters: list[VirtualMeter] = []
        # guards mutations of self.meters (API thread) vs iterations
        # (state-publisher thread) — prevents 'list changed size during
        # iteration' under concurrency.
        self._meters_lock = threading.RLock()
        # serializes load→modify→save cycles on virtual_meters.yaml (see
        # _cfg_mutation); always taken OUTSIDE _meters_lock when both are held
        self._cfg_lock = threading.RLock()
        self.mqtt_publisher = mqtt_publisher        # optional: publish state to MQTT
        self.modbus_client = modbus_client          # optional: publish data-acquisition health
        self._states_stop = threading.Event()
        # Published host port range (docker-compose publishes this once). Instance
        # ports are constrained to it so an added meter is always LAN-reachable.
        self.port_start = self._env_int("VMETER_PORT_START", 1502)
        self.port_end = self._env_int("VMETER_PORT_END", 1512)

    def _snap(self) -> "list[VirtualMeter]":
        """Atomic snapshot of the live meters for safe iteration off-thread."""
        with self._meters_lock:
            return list(self.meters)

    def _inst_device(self, inst: dict) -> str:
        """The source device id for an instance (absent → the primary device)."""
        return inst.get("device") or self.primary_device_id

    @staticmethod
    def _inst_enabled(inst: dict) -> bool:
        """A config row without `enabled` counts as ON — what boot (start_all)
        has always done. Every path must apply the SAME default, or a
        hand-edited row missing the key starts at boot yet is silently stopped
        by the next template edit (audit 2026-08-14 M3)."""
        return bool(inst.get("enabled", True))

    def _store_for(self, inst: dict) -> dict:
        """The live value cache a meter reads from — its source device's cache.
        No `device` field (or the primary) → the legacy primary cache
        (byte-identical migration). An EXPLICIT non-primary device that no
        longer exists returns an EMPTY store — never the primary's data: a
        control-critical consumer must see the meter go stale (fail-safe),
        not silently receive another meter's values."""
        dev = inst.get("device")
        if not dev or dev == self.primary_device_id:
            return self.current_values
        if dev in self.device_values:
            return self.device_values[dev]
        logger.warning("vmeter %s: source device %r not found — serving nothing (fail-safe)",
                       inst.get("template"), dev)
        return {}

    def _fallback_store(self, dev: str) -> dict:
        """Live cache of the failover device (primary cache if it IS the primary).
        Empty dict if the device has no cache yet (configured but offline) — the
        failover engine treats a missing candidate as 'skip', so an armed
        fallback to an offline twin simply waits for it to come online."""
        if dev == self.primary_device_id:
            return self.current_values
        return self.device_values.get(dev, {})

    def _inst_fallback(self, inst: dict) -> str:
        """Validated secondary (failover) source device for an instance, or ''.
        Must be a KNOWN device id and differ from the primary source device; a
        bad value is ignored with a warning (a mis-set fallback must never keep a
        control-critical meter from starting). 'Known' = the primary device or a
        registered device id, so a configured-but-offline twin still validates."""
        fb = str(inst.get("device_fallback") or "").strip()
        if not fb:
            return ""
        primary = self._inst_device(inst)
        if fb == primary:
            logger.warning("vmeter '%s': device_fallback == primary source '%s' — ignored",
                           inst.get("template"), primary)
            return ""
        if not (fb == self.primary_device_id or fb in self.device_values):
            logger.warning("vmeter '%s': device_fallback '%s' is not a known device — ignored",
                           inst.get("template"), fb)
            return ""
        return fb

    def _check_fallback(self, fb: str, src_device: str) -> Optional[str]:
        """Validate a device_fallback at SAVE time — return an error string to
        reject, or None if OK/empty. Stricter than :meth:`_inst_fallback` (which
        silently ignores at start): a save-time typo should surface, not vanish."""
        fb = str(fb or "").strip()
        if not fb:
            return None
        if fb == (src_device or self.primary_device_id):
            return "device_fallback must differ from the source device"
        if not (fb == self.primary_device_id or fb in self.device_values):
            return f"unknown device_fallback {fb!r} — pick a configured device"
        return None

    def _apply_device_fallback(self, template, inst: dict, fb: str) -> None:
        """Rewrite each bare-name LIVE register into an ordered failover pair
        [name, <fb>.name], so the whole meter transparently falls back to a twin
        device — no per-register template edits, since canonical field names are
        identical across devices. Reuses the (tested) failover resolver. Registers
        that are const/sum/already-failover, or whose source is an explicit
        device.register, are left untouched. Logs a one-line coverage summary:
        which fields the twin currently publishes (live fallback ready) vs not
        (armed, waiting for the twin to come online)."""
        primary = self._inst_device(inst)
        fb_store = self._fallback_store(fb)
        wired, ready, waiting = 0, [], []
        for reg in template.registers:
            if reg.source_kind != "live" or not isinstance(reg.source, str):
                continue
            if "." in reg.source:                 # explicit cross-device — leave as authored
                continue
            reg.source_kind = "failover"
            reg.source = [reg.source, f"{fb}.{reg.source}"]   # [primary bare, twin dotted]
            wired += 1
            field = reg.source[0]
            (ready if _lookup(fb_store, field) is not None else waiting).append(field)
        logger.info(
            "vmeter '%s': device_fallback %s → %s — %d live registers wired; "
            "twin has now: [%s]; armed/waiting: [%s]",
            inst.get("template"), primary, fb, wired,
            ", ".join(sorted(ready)) or "-", ", ".join(sorted(waiting)) or "-")

    # ── state → MQTT (so alertd rules can monitor the meters) ─────────────
    def _publish_states(self) -> None:
        """Publish each configured meter's health to MQTT (retained) so external
        monitors (e.g. alertd) can alert on a meter going down / stale / erroring."""
        pub = self.mqtt_publisher
        if pub is None:
            return
        # iterate a snapshot of the live meters (no per-tick file I/O, no race
        # with API mutations of self.meters)
        for vm in self._snap():
            try:
                st = vm.status()
                # Full picture for monitoring (alertd / HA / dashboards): who is
                # serving where, who is connected (ip:port), throughput, freshness,
                # and the last error — no electrical data is duplicated here.
                payload = {
                    "id": st.get("id"), "name": st.get("name"),
                    "bind": st.get("bind"), "port": st.get("port"),
                    "unit_id": st.get("unit_id"), "registers": st.get("registers"),
                    "enabled": True, "running": bool(st.get("running")),
                    "state": st.get("state"),
                    "connections": st.get("connections", []),
                    "conn_count": st.get("conn_count", 0),
                    "peers": st.get("peers", ""),
                    "requests": st.get("requests", 0),
                    "req_rate": st.get("req_rate", 0.0),
                    "errors": st.get("errors", 0),
                    "bytes_rx": st.get("bytes_rx", 0), "bytes_tx": st.get("bytes_tx", 0),
                    "last_fresh": st.get("last_fresh"),
                    "freshness_age_s": st.get("freshness_age_s"),
                    "uptime_s": st.get("uptime_s"),
                    "last_error": st.get("last_error"),
                    # Staleness policy + bounds so a monitor (alertd/HA) sees HOW a
                    # meter degrades, not just that it did: fail/sentinel/hold, the
                    # freshness window, and (hold only) the grace cap.
                    "on_stale": st.get("on_stale"),
                    "stale_after_s": st.get("stale_after_s"),
                    "max_hold_s": st.get("max_hold_s"),
                    # per-rebuild quality counts + live redundant-source routing
                    "quality": st.get("quality", {}),
                    "failover": st.get("failover", []),
                    "ts": int(time.time()),
                }
                pub.publish_state(f"vmeter/{st.get('id')}/state", json.dumps(payload))
            except Exception as e:  # noqa: BLE001
                logger.debug("vmeter state publish failed: %s", e)
        # Also publish data-acquisition (Modbus) health so alertd can alert on a
        # Janitza comms loss DIRECTLY, instead of only inferring it from vmeter
        # freshness (which requires an enabled meter). Retained, same cadence.
        mc = self.modbus_client
        if mc is not None and hasattr(mc, "data_health"):
            try:
                threshold = getattr(getattr(mc, "config", None), "stale_after_s", 30)
                dh = mc.data_health(threshold)
                dh["ts"] = int(time.time())
                pub.publish_state("data_health", json.dumps(dh))
            except Exception as e:  # noqa: BLE001
                logger.debug("data_health publish failed: %s", e)

    def publish_ha_discovery(self) -> int:
        """Publish HA autodiscovery for every enabled instance (idempotent;
        retained). No-op without an MQTT publisher."""
        pub = self.mqtt_publisher
        if pub is None or not hasattr(pub, "publish_vmeter_discovery"):
            return 0
        running = {vm.t.id: vm for vm in self._snap()}
        meters = []
        for inst in self._load_cfg().get("instances", []):
            if not self._inst_enabled(inst):
                continue
            tid = inst.get("template")
            vm = running.get(tid)
            name = vm.t.name if vm else tid
            if vm is None:
                try:
                    _p = self._template_path(str(tid or ""))
                    if _p is not None:
                        name = load_template(str(_p)).name
                except Exception:  # noqa: BLE001
                    pass
            meters.append({"id": tid, "name": name})
        try:
            return pub.publish_vmeter_discovery(meters)
        except Exception as e:  # noqa: BLE001
            logger.warning("vmeter HA discovery failed: %s", e)
            return 0

    def start_state_publisher(self, interval: float = 10.0) -> None:
        """Background thread that publishes meter states to MQTT every `interval`s,
        and (re)asserts HA autodiscovery periodically so it self-heals after a
        broker restart. Idempotent — a second call is a no-op while live."""
        if self.mqtt_publisher is None:
            return
        if getattr(self, "_state_thread", None) and self._state_thread.is_alive():
            return                                    # already running
        self._states_stop.clear()

        def _loop():
            tick = 0
            while not self._states_stop.wait(interval):
                try:
                    self._publish_states()
                    if tick % 30 == 0:                # ~every 5 min + first tick
                        self.publish_ha_discovery()
                except Exception as e:  # noqa: BLE001
                    logger.warning("vmeter state publisher error: %s", e)
                tick += 1
        self._state_thread = threading.Thread(target=_loop, daemon=True, name="vmeter-state-pub")
        self._state_thread.start()
        logger.info("virtual-meter state publisher started (every %ss)", interval)

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        try:
            return int(os.environ.get(name, default))
        except (TypeError, ValueError):
            return default

    def port_info(self) -> dict:
        """Published range + which ports are taken + the next free one."""
        used = sorted({int(i["port"]) for i in self._load_cfg().get("instances", [])
                       if i.get("port") is not None})
        free = [p for p in range(self.port_start, self.port_end + 1) if p not in used]
        return {"start": self.port_start, "end": self.port_end, "used": used,
                "free": free, "next_free": (free[0] if free else None)}

    def _port_in_range(self, port: int) -> bool:
        return self.port_start <= port <= self.port_end

    def start_all(self) -> None:
        if not self.config_path.exists():
            logger.info("virtual meters: no %s — disabled", self.config_path)
            return
        try:
            cfg = yaml.safe_load(self.config_path.read_text()) or {}
        except Exception as e:  # noqa: BLE001
            logger.error("virtual meters: bad config %s: %s", self.config_path, e)
            return
        for inst in cfg.get("instances", []):
            if not self._inst_enabled(inst):
                continue
            try:
                self._start_one(inst)
            except Exception as e:  # noqa: BLE001
                logger.error("virtual meter instance %s failed to start: %s",
                             inst.get("template"), e)

    def _load_cfg(self) -> dict:
        if not self.config_path.exists():
            return {"instances": []}
        try:
            return yaml.safe_load(self.config_path.read_text()) or {"instances": []}
        except Exception:  # noqa: BLE001
            return {"instances": []}

    def _save_cfg(self, cfg: dict) -> None:
        # atomic write — this file decides which meters run (can feed an ESS), so
        # never leave it half-written or let a concurrent reader see a torn file.
        tmp = self.config_path.with_suffix(".yaml.tmp")
        with open(tmp, "w") as f:
            f.write(yaml.safe_dump(cfg, sort_keys=False))
            f.flush()
            os.fsync(f.fileno())               # durable before the rename —
        os.replace(tmp, self.config_path)      # power-loss must not zero the file

    def overview(self) -> list[dict]:
        """All configured instances merged with live running status + preview."""
        running = {vm.t.id: vm for vm in self._snap()}
        out = []
        for inst in self._load_cfg().get("instances", []):
            tid = inst.get("template")
            vm = running.get(tid)
            name = tid
            if vm:
                name = vm.t.name
            else:                                    # load template for the friendly name
                try:
                    _p = self._template_path(str(tid or ""))
                    if _p is not None:
                        name = load_template(str(_p)).name
                except Exception:  # noqa: BLE001
                    pass
            row = {"template": tid, "enabled": self._inst_enabled(inst),
                   "port": inst.get("port"), "unit_id": inst.get("unit_id", 1),
                   "stale_after_s": inst.get("stale_after_s", 15),
                   "update_interval_s": inst.get("update_interval_s", 1.0),
                   "device": self._inst_device(inst),
                   "device_fallback": str(inst.get("device_fallback") or ""),
                   "on_stale": inst.get("on_stale", "legacy"),
                   "max_hold_s": inst.get("max_hold_s", 30),
                   "quality_block": bool(inst.get("quality_block", False)),
                   "running": vm is not None and vm._running,
                   "name": name,
                   "preview": vm.preview() if vm else {}}
            if vm:
                row.update(vm.status())
            out.append(row)
        return out

    def list_templates(self) -> list[dict]:
        """Available templates (config/templates/*.yaml) for the UI dropdown."""
        out = []
        for p in sorted(self.templates_dir.glob("*.yaml")):
            try:
                t = load_template(str(p))
                out.append({"id": t.id, "name": t.name, "kind": t.kind,
                            "registers": len(t.registers)})
            except Exception:  # noqa: BLE001
                pass
        return out

    # ── template-level operations (the UI editor) ─────────────────────────
    @staticmethod
    def valid_types() -> list[str]:
        return list(_VALID_TYPES)

    def list_sources(self, device: str = "") -> list[dict]:
        """Register names available as live sources for a device (name/label/
        unit/value). Absent device → the primary device's registers."""
        store = self.device_values.get(device, self.current_values) if device else self.current_values
        seen: dict[str, dict] = {}
        for info in list(store.values()):              # snapshot vs concurrent poller writes
            if not isinstance(info, dict):
                continue
            name = info.get("name")
            if not name or name in seen:
                continue
            seen[name] = {"name": name, "label": info.get("label", ""),
                          "unit": info.get("unit", ""), "value": info.get("value")}
        return sorted(seen.values(), key=lambda s: s["name"])

    def _template_path(self, template_id: str) -> Optional[Path]:
        """Resolve a template id to its YAML path, guarding against traversal."""
        if not template_id or not _ID_RE.match(template_id):
            return None
        p = (self.templates_dir / f"{template_id}.yaml").resolve()
        try:
            p.relative_to(self.templates_dir.resolve())
        except ValueError:
            return None
        return p

    def get_template(self, template_id: str) -> dict:
        """Full editor-shaped view of a template (raw fields per register)."""
        path = self._template_path(template_id)
        if path is None:
            return {"error": "invalid template id"}
        if not path.exists():
            return {"error": f"unknown template {template_id}"}
        try:
            d = (yaml.safe_load(path.read_text()) or {})["template"]
        except Exception as e:  # noqa: BLE001
            return {"error": f"cannot parse template: {e}"}
        byte_order = d.get("byte_order", "big")
        tr = d.get("transport", {}) or {}
        regs = []
        for r in d.get("registers", []):
            kind, src = _parse_source(r)
            order = r.get("order", byte_order)
            row = {"addr": int(r["addr"]), "type": r["type"],
                   "scale": float(r.get("scale", 1)),
                   "order": "inherit" if order == byte_order else order,
                   "source_kind": kind, "source": src,
                   "length": int(r.get("length", 1)), "note": r.get("note", "")}
            if r.get("stale_after_s") is not None:
                row["stale_after_s"] = float(r["stale_after_s"])
            regs.append(row)
        cfg = self._load_cfg().get("instances", [])
        return {"id": d["id"], "name": d.get("name", d["id"]),
                "kind": d.get("kind", "flat"), "byte_order": byte_order,
                "transport": {"type": tr.get("type", "tcp"), "port": tr.get("port", 1502),
                              "unit_id": tr.get("unit_id", 1), "bind": tr.get("bind", "0.0.0.0")},
                "registers": regs,
                "running": any(vm.t.id == template_id and vm._running for vm in self._snap()),
                "in_use": any(i.get("template") == template_id for i in cfg)}

    def _source_value(self, name: str):
        """Current live value of a source name, searched across every device
        store (templates don't carry the device binding — instances do). None
        when the source isn't live anywhere right now."""
        # getattr-defensive: callers may exercise _normalize_registers on a
        # partially-built manager (tests, tooling) — no stores means no verdict
        for store in (getattr(self, "current_values", None) or {},
                      *(getattr(self, "device_values", None) or {}).values()):
            for info in list(store.values()):
                if isinstance(info, dict) and info.get("name") == name:
                    return info.get("value")
        return None

    def _normalize_registers(self, raw_regs: list, byte_order: str):
        """Validate + coerce editor register rows. Returns (registers, error)."""
        norm, spans = [], []
        for idx, r in enumerate(raw_regs):
            tag = f"register #{idx + 1}"
            a = r.get("addr")
            try:
                addr = int(a, 0) if isinstance(a, str) else int(a)
            except (TypeError, ValueError):
                return None, f"{tag}: bad address {a!r}"
            if not (0 <= addr <= 0xffff):
                return None, f"{tag}: address out of range (0..65535)"
            typ = str(r.get("type", "")).lower()
            if typ not in _VALID_TYPES:
                return None, f"{tag}: invalid type {typ!r}"
            kind = r.get("source_kind", "const")
            if kind not in ("const", "const_str", "live", "sum", "failover"):
                return None, f"{tag}: invalid source kind {kind!r}"
            src = r.get("source")
            if kind == "live":
                if not (isinstance(src, str) and src.strip()):
                    return None, f"{tag}: live source register name required"
                src = src.strip()
            elif kind in ("sum", "failover"):
                # a list of live source names, or a comma-separated string
                # (sum = add them all; failover = ordered, serve first fresh)
                if isinstance(src, str):
                    src = [x.strip() for x in src.split(",")]
                if not (isinstance(src, list) and all(isinstance(x, str) for x in src)):
                    return None, f"{tag}: {kind} source must be a list of register names"
                src = [x.strip() for x in src if x.strip()]
                if not src:
                    return None, f"{tag}: {kind} needs at least one source name"
            elif kind == "const_str":
                src = "" if src is None else str(src)
            else:  # const number
                try:
                    f = float(src)
                except (TypeError, ValueError):
                    return None, f"{tag}: const must be a number"
                src = int(f) if f == int(f) else f
            if kind in ("live", "sum", "failover") and typ != "string":
                # A numeric row can never encode a text value (enum/bits/string
                # sources) — the runtime degrades such a row to missing, but a
                # known-text source at save time is a plain config error.
                for _name in ([src] if isinstance(src, str) else src):
                    _v = self._source_value(_name)
                    if isinstance(_v, str):
                        return None, (f"{tag}: source '{_name}' currently carries "
                                      f"text ({_v!r}) — a {typ} row cannot encode "
                                      "it (enum/bits/string sources need a "
                                      "string-typed row)")
            try:
                scale = float(r.get("scale", 1)) or 1.0
            except (TypeError, ValueError):
                return None, f"{tag}: bad scale"
            order = r.get("order", "inherit")
            if order not in ("inherit", "big", "little", "badc", "dcba", "abcd", "cdab"):
                order = "inherit"
            length = max(1, int(r.get("length", 1) or 1))
            count = length if typ == "string" else RegisterEncoder.REGISTER_COUNTS.get(typ, 2)
            spans.append((addr, addr + count, idx))
            row = {"addr": addr, "type": typ, "scale": scale, "order": order,
                   "source_kind": kind, "source": src, "length": length,
                   "note": (r.get("note") or "").strip()}
            # composite: optional per-row freshness bound (seconds)
            rs = r.get("stale_after_s")
            if rs not in (None, "", 0):
                try:
                    rs = float(rs)
                except (TypeError, ValueError):
                    return None, f"{tag}: bad stale_after_s"
                if rs <= 0:
                    return None, f"{tag}: stale_after_s must be > 0"
                row["stale_after_s"] = rs
            norm.append(row)
        spans.sort()
        for i in range(1, len(spans)):
            if spans[i][0] < spans[i - 1][1]:
                return None, (f"registers overlap: 0x{spans[i][0]:04x} starts inside "
                              f"0x{spans[i - 1][0]:04x}'s range")
        return norm, None

    def save_template(self, template_id: str, payload: dict) -> dict:
        """Create or overwrite a template YAML (atomic) + reload a live instance."""
        path = self._template_path(template_id)
        if path is None:
            return {"error": "invalid template id (use a-z 0-9 _)"}
        name = (payload.get("name") or "").strip()
        if not name:
            return {"error": "name is required"}
        byte_order = payload.get("byte_order", "big")
        if byte_order not in ("big", "little", "badc", "dcba", "abcd", "cdab"):
            return {"error": "byte_order must be big/abcd, little/cdab, badc or dcba"}
        try:
            port, unit_id = int(payload.get("port", 1502)), int(payload.get("unit_id", 1))
        except (TypeError, ValueError):
            return {"error": "port/unit must be integers"}
        if not (1 <= port <= 65535):
            return {"error": "port must be 1..65535"}
        if not (0 <= unit_id <= 255):
            return {"error": "unit must be 0..255"}
        bind = (payload.get("bind") or "0.0.0.0").strip()
        raw_regs = payload.get("registers") or []
        if not raw_regs:
            return {"error": "at least one register is required"}
        norm, err = self._normalize_registers(raw_regs, byte_order)
        if err:
            return {"error": err}
        text = self._dump_template(template_id, name, byte_order,
                                   {"type": "tcp", "port": port, "unit_id": unit_id,
                                    "bind": bind}, norm)
        try:
            yaml.safe_load(text)                       # belt-and-braces: must reparse
        except Exception as e:  # noqa: BLE001
            return {"error": f"internal: produced invalid YAML: {e}"}
        self.templates_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(text)
        os.replace(tmp, path)                          # atomic — never a torn template
        reloaded = self._reload_instance(template_id)
        return {"saved": True, "id": template_id, "registers": len(norm), "reloaded": reloaded}

    def delete_template(self, template_id: str) -> dict:
        path = self._template_path(template_id)
        if path is None:
            return {"error": "invalid template id"}
        if not path.exists():
            return {"error": f"unknown template {template_id}"}
        if any(i.get("template") == template_id for i in self._load_cfg().get("instances", [])):
            return {"error": "template in use by an instance — remove the instance first"}
        path.unlink()
        return {"deleted": True, "id": template_id}

    # ── observability + portability ──────────────────────────────────────
    def json_view(self, template_id: str) -> dict:
        """The composite map as an HTTP-JSON feed (same staleness convention as
        the Modbus block). 'meter not running' when the instance is stopped —
        we never serve a JSON view a Modbus consumer couldn't also see."""
        vm = next((m for m in self._snap() if m.t.id == template_id), None)
        if vm is None:
            return {"error": "meter not running", "id": template_id}
        return vm.json_view()

    def get_stats(self, template_id: str, limit: int = 200) -> dict:
        """Live in-RAM observability snapshot (query log + counters + rate)."""
        vm = next((m for m in self._snap() if m.t.id == template_id), None)
        if vm is None:
            return {"error": "meter not running", "running": False, "id": template_id}
        snap = vm.stats.snapshot(limit)
        snap.update({"running": bool(vm._running), "id": template_id,
                     "name": vm.t.name, "port": vm.t.transport.get("port"),
                     "unit_id": vm.t.transport.get("unit_id", 1)})
        return snap

    @staticmethod
    def _decode_words(words: list, dtype: str, scale: float, order: str):
        """Decode raw registers back to an engineering value using RegisterParser
        (encoder and parser are now consistent across all byte orders: big/abcd,
        little/cdab, badc, dcba). Used by the Logs decode view."""
        dt = (dtype or "").lower()
        w = [int(x) & 0xffff for x in words]
        bo = order if order and order != "inherit" else "big"
        try:
            if dt == "string":
                byteswap, _ = RegisterParser.resolve_order(bo)
                if byteswap:
                    w = [((x & 0xff) << 8) | ((x >> 8) & 0xff) for x in w]
                b = b"".join(struct.pack(">H", x) for x in w)
                return b.split(b"\x00")[0].decode("latin1", "ignore")
            v = RegisterParser(bo).parse_value(w, dt)
            if v is None:
                return None
            if dt in ("float", "float32"):
                return round(v / scale, 4)
            if dt == "double":
                return round(v / scale, 6)
            return v if scale in (0, 1) else round(v / scale, 4)
        except Exception:  # noqa: BLE001
            return None

    def decode_range(self, template_id: str, addr: int, count: int) -> dict:
        """Decode the register block over [addr, addr+count) against the template:
        raw words -> value -> the source variable each maps to. Debug aid for the
        Logs view (shows exactly what a consumer's read returns)."""
        vm = next((m for m in self._snap() if m.t.id == template_id), None)
        if vm is None or not vm.serving_block_ready:
            return {"error": "meter not running", "id": template_id}
        enc = RegisterEncoder
        out = []
        for r in vm.t.registers:
            if not (addr <= int(r.addr) < addr + count):
                continue
            span = (r.length if r.type == "string"
                    else enc.REGISTER_COUNTS.get(r.type.lower(), 2))
            try:
                words = vm.served_words(int(r.addr), max(1, span))
            except Exception:  # noqa: BLE001
                words = []
            if r.source_kind == "live":
                src = r.source
            elif r.source_kind == "sum":
                src = "Σ " + "+".join(r.source) if isinstance(r.source, list) else "sum"
            elif r.source_kind == "failover":
                # ordered priority: first fresh source wins (→ shows the order)
                src = "⇢ " + " → ".join(r.source) if isinstance(r.source, list) else "failover"
            elif r.source_kind == "const_str":
                src = f'"{r.source}"'
            else:
                src = f"const {r.source}"
            out.append({"addr": int(r.addr), "type": r.type, "scale": r.scale,
                        "order": r.order, "kind": r.source_kind, "source": src,
                        "note": r.note, "words": words,
                        "value": self._decode_words(words, r.type, r.scale, r.order)})
        out.sort(key=lambda x: x["addr"])
        return {"id": template_id, "addr": addr, "count": count, "registers": out}

    def export_template(self, template_id: str) -> dict:
        """Return the raw template YAML for download."""
        path = self._template_path(template_id)
        if path is None or not path.exists():
            return {"error": "unknown template"}
        return {"id": template_id, "filename": f"{template_id}.yaml", "yaml": path.read_text()}

    def import_template(self, text: str, overwrite: bool = False) -> dict:
        """Validate (parse + structural load) then atomically save an uploaded YAML."""
        if not text or not text.strip():
            return {"error": "empty file"}
        try:
            doc = yaml.safe_load(text)
        except Exception as e:  # noqa: BLE001
            return {"error": f"invalid YAML: {e}"}
        if not isinstance(doc, dict) or "template" not in doc:
            return {"error": "not a template (missing 'template:' root)"}
        tid = str((doc.get("template") or {}).get("id", "")).strip()
        path = self._template_path(tid)
        if path is None:
            return {"error": "invalid template id (use a-z 0-9 _)"}
        if path.exists() and not overwrite:
            return {"error": f"template '{tid}' already exists", "exists": True, "id": tid}
        # full structural validation: must load as a Template with >= 1 register
        tmpname = None
        try:
            import tempfile
            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tf:
                tf.write(text)
                tmpname = tf.name
            t = load_template(tmpname)
        except Exception as e:  # noqa: BLE001
            return {"error": f"template failed validation: {e}"}
        finally:
            if tmpname:
                try:
                    os.unlink(tmpname)
                except OSError:
                    pass
        if not t.registers:
            return {"error": "template has no registers"}
        self.templates_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(text)
        os.replace(tmp, path)                          # atomic
        return {"imported": True, "id": tid, "name": t.name, "registers": len(t.registers)}

    def _reload_instance(self, template_id: str) -> bool:
        """If a live instance runs this template, restart it to pick up edits."""
        running = [m for m in self._snap() if m.t.id == template_id]
        if not running:
            return False
        inst = next((i for i in self._load_cfg().get("instances", [])
                     if i.get("template") == template_id), None)
        for vm in running:
            vm.stop()
            with self._meters_lock:
                self.meters.remove(vm)
        if inst and self._inst_enabled(inst):
            try:
                self._start_one(inst)
            except Exception as e:  # noqa: BLE001
                logger.error("reload %s failed to restart: %s", template_id, e)
        return True

    @staticmethod
    def _dump_template(tid: str, name: str, byte_order: str, transport: dict,
                       regs: list[dict]) -> str:
        """Render a clean, human-readable template YAML (hex addrs, one reg/line)."""
        def _q(s) -> str:
            # JSON string escaping is valid YAML double-quoted-scalar syntax —
            # quotes/newlines/backslashes in free text (name, source, note)
            # can't corrupt the hand-crafted layout or change its meaning
            return json.dumps(str(s), ensure_ascii=False)

        lines = ["# Managed by the Virtual Meter template editor — UI saves overwrite this file.",
                 "template:", f"  id: {tid}", f"  name: {_q(name)}", "  kind: flat",
                 f"  byte_order: {byte_order}",
                 f'  transport: {{ type: {transport["type"]}, port: {transport["port"]}, '
                 f'unit_id: {transport["unit_id"]}, bind: {_q(transport["bind"])} }}',
                 "  registers:"]
        for r in regs:
            parts = [f"addr: 0x{r['addr']:04x}", f"type: {r['type']}"]
            if r["type"] != "string" and abs(r["scale"] - 1.0) > 1e-12:
                parts.append(f"scale: {r['scale']:g}")
            if r["order"] not in ("inherit", byte_order):
                parts.append(f"order: {r['order']}")
            if r["type"] == "string":
                parts.append(f"length: {max(1, r['length'])}")
            if r["source_kind"] == "live":
                parts.append(f'source: {{ live: {_q(r["source"])} }}')
            elif r["source_kind"] in ("sum", "failover"):
                names = ", ".join(_q(n) for n in r["source"])
                parts.append(f'source: {{ {r["source_kind"]}: [{names}] }}')
            elif r["source_kind"] == "const_str":
                parts.append(f'source: {{ const_str: {_q(r["source"])} }}')
            else:
                parts.append(f'source: {{ const: {r["source"]} }}')
            if r.get("stale_after_s"):
                parts.append(f"stale_after_s: {r['stale_after_s']:g}")
            if r["note"]:
                parts.append(f"note: {_q(r['note'])}")
            lines.append("    - { " + ", ".join(parts) + " }")
        return "\n".join(lines) + "\n"

    @_cfg_mutation
    def add_instance(self, template_id: str, port: int, unit_id: int = 1,
                     stale_after_s: float = 15.0, enabled: bool = False,
                     device: str = "", on_stale: str = "legacy",
                     max_hold_s: float = 30.0, quality_block: bool = False,
                     device_fallback: str = "") -> dict:
        """Add a new virtual-meter instance (persist + optionally start).
        `device` is the SOURCE device whose live values feed the meter (absent →
        the primary device). `device_fallback` is an optional twin device the
        meter transparently fails over to per register when the source goes
        stale. `on_stale` is the composite staleness policy
        (legacy|fail|sentinel|hold); `max_hold_s` bounds the hold policy."""
        if on_stale not in ("legacy", "fail", "sentinel", "hold"):
            return {"error": f"on_stale must be legacy|fail|sentinel|hold, not {on_stale!r}"}
        # a non-finite or non-positive bound would defeat the freshness watchdog
        # (inf/nan makes every age look in-bound) — reject it, like update_instance
        for _nm, _v in (("stale_after_s", stale_after_s), ("max_hold_s", max_hold_s)):
            try:
                _f = float(_v)
            except (TypeError, ValueError):
                return {"error": f"{_nm} must be a number"}
            if not math.isfinite(_f) or _f <= 0:
                return {"error": f"{_nm} must be a positive, finite number"}
        tmpl_path = self._template_path(template_id)   # validates id, blocks traversal
        if tmpl_path is None or not tmpl_path.exists():
            return {"error": f"unknown template {template_id}"}
        cfg = self._load_cfg()
        cfg.setdefault("instances", [])
        if any(i.get("template") == template_id for i in cfg["instances"]):
            return {"error": f"instance for {template_id} already exists"}
        port = int(port)
        if not self._port_in_range(port):
            return {"error": f"port {port} is outside the published range "
                             f"{self.port_start}-{self.port_end} — pick a free port in "
                             f"range, or widen VMETER_PORT_END and recreate the container"}
        if any(int(i.get("port", -1)) == port for i in cfg["instances"]):
            return {"error": f"port {port} is already used by another instance"}
        inst = {"template": template_id, "enabled": bool(enabled),
                "port": port, "unit_id": int(unit_id),
                "stale_after_s": float(stale_after_s)}
        if device and device != self.primary_device_id:
            inst["device"] = device
        fb_err = self._check_fallback(device_fallback, device or self.primary_device_id)
        if fb_err:
            return {"error": fb_err}
        if str(device_fallback or "").strip():
            inst["device_fallback"] = str(device_fallback).strip()
        if on_stale != "legacy":                       # legacy = absent (back-compat)
            inst["on_stale"] = on_stale
            if on_stale == "hold":
                inst["max_hold_s"] = float(max_hold_s)
        if quality_block:                              # absent = off (back-compat)
            inst["quality_block"] = True
        cfg["instances"].append(inst)
        self._save_cfg(cfg)
        if enabled:
            try:
                self._start_one(inst)
            except Exception as e:  # noqa: BLE001
                return {"error": f"added but failed to start: {e}"}
        return {"template": template_id, "added": True, "enabled": bool(enabled)}

    @_cfg_mutation
    def remove_instance(self, template_id: str) -> dict:
        """Stop + remove a virtual-meter instance."""
        cfg = self._load_cfg()
        before = len(cfg.get("instances", []))
        cfg["instances"] = [i for i in cfg.get("instances", []) if i.get("template") != template_id]
        if len(cfg["instances"]) == before:
            return {"error": f"no instance for {template_id}"}
        self._save_cfg(cfg)
        for vm in [m for m in self._snap() if m.t.id == template_id]:
            vm.stop()
            with self._meters_lock:
                self.meters.remove(vm)
        # Clear the RETAINED state topic — otherwise the removed meter haunts
        # MQTT (and any alertd rule watching it) forever.
        if self.mqtt_publisher:
            try:
                self.mqtt_publisher.publish_state(f"vmeter/{template_id}/state", "")
            except Exception:  # noqa: BLE001
                pass
        return {"template": template_id, "removed": True}

    @_cfg_mutation
    def set_enabled(self, template_id: str, on: bool) -> dict:
        """Persist enabled flag + start/stop the instance live."""
        cfg = self._load_cfg()
        inst = next((i for i in cfg.get("instances", []) if i.get("template") == template_id), None)
        if inst is None:
            return {"error": f"no instance for template {template_id}"}
        old = dict(inst)                       # rollback snapshot — start may fail
        inst["enabled"] = bool(on)
        self._save_cfg(cfg)
        running = {vm.t.id: vm for vm in self._snap()}
        if on and template_id not in running:
            try:
                self._start_one(inst)
            except Exception as e:  # noqa: BLE001
                # roll the flag back — otherwise the API 500s while `enabled:
                # true` stays persisted and the next boot trips over the same
                # bad config (audit 2026-08-14 M3)
                inst.clear()
                inst.update(old)
                self._save_cfg(cfg)
                return {"error": f"failed to start (enable reverted): {e}"}
        elif not on and template_id in running:
            vm = running[template_id]
            vm.stop()
            with self._meters_lock:
                self.meters.remove(vm)
            # clear the retained state so consumers don't see a ghost health for
            # a meter that is no longer serving
            if self.mqtt_publisher:
                try:
                    self.mqtt_publisher.publish_state(f"vmeter/{template_id}/state", "")
                except Exception:  # noqa: BLE001
                    pass
        return {"template": template_id, "enabled": bool(on)}

    @_cfg_mutation
    def update_instance(self, template_id: str, port=None, unit_id=None,
                        stale_after_s=None, update_interval_s=None,
                        device=None, on_stale=None, max_hold_s=None,
                        quality_block=None, device_fallback=None) -> dict:
        """Edit an existing instance's port / unit_id / stale_after_s /
        update_interval_s / source device / staleness policy (partial — only
        provided fields change). Persists, then live-restarts the meter if it
        is running so everything (incl. the source cache) takes effect."""
        cfg = self._load_cfg()
        inst = next((i for i in cfg.get("instances", []) if i.get("template") == template_id), None)
        if inst is None:
            return {"error": f"no instance for template {template_id}"}
        old = dict(inst)                          # snapshot for rollback on restart failure
        if on_stale is not None:
            if on_stale not in ("legacy", "fail", "sentinel", "hold"):
                return {"error": f"on_stale must be legacy|fail|sentinel|hold, not {on_stale!r}"}
            if on_stale == "legacy":
                inst.pop("on_stale", None)        # legacy = absent (back-compat)
                inst.pop("max_hold_s", None)
            else:
                inst["on_stale"] = on_stale
        if quality_block is not None:
            if bool(quality_block):
                inst["quality_block"] = True
            else:
                inst.pop("quality_block", None)   # absent = off (back-compat)
        if max_hold_s is not None:
            try:
                max_hold_s = float(max_hold_s)
            except (TypeError, ValueError):
                return {"error": "max_hold_s must be a number"}
            if not math.isfinite(max_hold_s) or max_hold_s <= 0:
                return {"error": "max_hold_s must be a positive, finite number"}
            inst["max_hold_s"] = max_hold_s
        if device is not None:
            # empty / primary → drop the field (meter reads the primary cache)
            if device and device != self.primary_device_id:
                inst["device"] = device
            else:
                inst.pop("device", None)
        if device_fallback is not None:
            fb = str(device_fallback).strip()
            if fb:
                # validate against the (possibly just-updated) source device
                err = self._check_fallback(fb, inst.get("device") or self.primary_device_id)
                if err:
                    return {"error": err}
                inst["device_fallback"] = fb
            else:
                inst.pop("device_fallback", None)      # empty = clear (back-compat)
        if port is not None:
            try:
                port = int(port)
            except (TypeError, ValueError):
                return {"error": "port must be an integer"}
            if port != int(inst.get("port", -1)):
                if not self._port_in_range(port):
                    return {"error": f"port {port} is outside the published range "
                                     f"{self.port_start}-{self.port_end} — pick a free port in "
                                     f"range, or widen VMETER_PORT_END and recreate the container"}
                if any(int(i.get("port", -1)) == port for i in cfg["instances"] if i is not inst):
                    return {"error": f"port {port} is already used by another instance"}
                inst["port"] = port
        if unit_id is not None:
            try:
                unit_id = int(unit_id)
            except (TypeError, ValueError):
                return {"error": "unit_id must be an integer"}
            if not (0 <= unit_id <= 255):
                return {"error": "unit_id must be 0..255"}
            inst["unit_id"] = unit_id
        if stale_after_s is not None:
            try:
                stale_after_s = float(stale_after_s)
            except (TypeError, ValueError):
                return {"error": "stale_after_s must be a number"}
            # inf/nan would defeat the freshness watchdog (inf → never stale,
            # nan → every comparison False) — same guard as add_instance
            if not math.isfinite(stale_after_s) or stale_after_s <= 0:
                return {"error": "stale_after_s must be a positive, finite number"}
            inst["stale_after_s"] = stale_after_s
        if update_interval_s is not None:
            try:
                update_interval_s = float(update_interval_s)
            except (TypeError, ValueError):
                return {"error": "update_interval_s must be a number"}
            if not math.isfinite(update_interval_s) or update_interval_s <= 0:
                return {"error": "update_interval_s must be a positive, finite number"}
            inst["update_interval_s"] = update_interval_s
        self._save_cfg(cfg)
        restarted = False
        running = [m for m in self._snap() if m.t.id == template_id]
        if running and self._inst_enabled(inst):
            for vm in running:
                vm.stop()
                with self._meters_lock:
                    self.meters.remove(vm)
            try:
                self._start_one(inst)
                restarted = True
            except Exception as e:  # noqa: BLE001
                logger.error("update %s failed to restart: %s", template_id, e)
                # roll the persisted config back to the last-good values so a
                # later enable/restart doesn't reuse a bad port/unit
                inst.clear()
                inst.update(old)
                self._save_cfg(cfg)
                return {"error": f"failed to restart (reverted): {e}"}
        return {"template": template_id, "updated": True, "restarted": restarted,
                "port": inst.get("port"), "unit_id": inst.get("unit_id", 1),
                "stale_after_s": inst.get("stale_after_s", 15),
                "update_interval_s": inst.get("update_interval_s", 1.0),
                "device": self._inst_device(inst)}

    def _start_one(self, inst: dict) -> None:
        # Composite-aware provider: bare names read the instance's own store
        # (exactly the old single-source behavior); `device.register` names
        # reach any device's cache, carrying that source's staleness bound.
        provider = make_multi_provider(self._store_for(inst), self.device_values,
                                       self.current_values, self.primary_device_id,
                                       bounds_for=self.bounds_for)
        tmpl_path = self._template_path(str(inst.get("template", "")))
        if tmpl_path is None:                # invalid id / traversal attempt
            raise ValueError(f"invalid template id {inst.get('template')!r} "
                             "in virtual_meters.yaml — instance not started")
        template = load_template(str(tmpl_path))
        # instance-level redundant source: rewrite bare-name live registers into
        # failover pairs against a twin device (validated; canonical names make
        # the twin fields identical). No-op when no valid device_fallback is set.
        fb = self._inst_fallback(inst)
        if fb:
            self._apply_device_fallback(template, inst, fb)
        if "port" in inst:
            template.transport["port"] = int(inst["port"])
        if "unit_id" in inst:
            template.transport["unit_id"] = int(inst["unit_id"])
        if "bind" in inst:
            template.transport["bind"] = inst["bind"]
        vm = VirtualMeter(template, provider,
                          stale_after_s=float(inst.get("stale_after_s", 15)),
                          update_interval_s=float(inst.get("update_interval_s", 1.0)),
                          debug_reads=bool(inst.get("debug_reads", False)),
                          on_stale=str(inst.get("on_stale", "legacy")),
                          max_hold_s=float(inst.get("max_hold_s", 30)),
                          quality_block=bool(inst.get("quality_block", False)))
        vm.start()
        with self._meters_lock:
            self.meters.append(vm)
        logger.info("virtual meter '%s' started (port=%s)", template.id,
                    template.transport.get("port"))

    def stop_all(self) -> None:
        self._states_stop.set()                       # stop the MQTT state publisher
        for vm in self._snap():
            try:
                vm.stop()
            except Exception:  # noqa: BLE001
                pass
        with self._meters_lock:
            self.meters.clear()

    def status(self) -> list[dict]:
        return [vm.status() for vm in self._snap()]

    def health(self) -> dict:
        """Aggregate health of the ENABLED meters, for /health + container probe.

        Per meter: ok (serving + fresh) · stale (serving, source stale = correct
        fail-safe) · down (enabled but not serving = genuine fault). Overall maps
        ok→ok, any-stale→degraded, any-down→down. 'down' is the only state that
        should fail a container probe — a stale source is expected (the meter is
        fail-safing) and a restart would not fix it; a 'down' meter (crashed /
        failed to start) is a real fault a restart might clear."""
        now = time.time()
        running = {vm.t.id: vm for vm in self._snap()}
        rank = {"ok": 0, "stale": 1, "down": 2}
        meters, worst = [], "ok"
        for inst in self._load_cfg().get("instances", []):
            if not self._inst_enabled(inst):
                continue
            tid = inst.get("template")
            vm = running.get(tid)
            if vm is None:
                state, age, last_err = "down", None, None
            else:
                state = vm.health_state()
                lf = vm._last_fresh_ts             # MONOTONIC → age on the mono clock
                age = round(time.monotonic() - lf, 1) if lf else None
                last_err = vm.stats.last_error()
            meters.append({"id": tid, "state": state, "freshness_age_s": age,
                           "port": inst.get("port"), "last_error": last_err})
            if rank[state] > rank[worst]:
                worst = state
        status = {"ok": "ok", "stale": "degraded", "down": "down"}[worst]
        return {"status": status, "enabled_meters": len(meters),
                "meters": meters, "ts": int(now)}
