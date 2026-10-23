# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""What an active gateway hands a standby partner so the partner holds it
warm: the values of the devices it polls (the ones a standby only listens to
it hears for itself), and — through the rules runtime — every rule's state
and every channel's protection.

The monotonic clock is a process's own: an entry travels with its AGE
(seconds since it was read, as the active saw it), and the standby puts it
back on its own clock. A value the standby already holds newer is kept.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

from . import role as gateway_role


def _polled(dev_cfg) -> bool:
    """Does the device have a source a standby does not run itself?"""
    sources = getattr(dev_cfg, 'sources', None) or []
    protos = [getattr(s, 'protocol', '') for s in sources] or [getattr(dev_cfg, 'protocol', '')]
    return any(not gateway_role.listens_only(p) for p in protos)


def export_values(config, registry, since: Optional[float] = None) -> Dict[str, Dict[str, Dict]]:
    """{device: {store key: entry + age_s}} for the polled devices; only what
    was read after `since` (a wall time) when given."""
    now = time.monotonic()
    out: Dict[str, Dict[str, Dict]] = {}
    for dev_cfg in list(config.devices):
        if not _polled(dev_cfg):
            continue
        store = registry.store_for(dev_cfg.id) or {}
        rows = {}
        for key, e in list(store.items()):
            if not isinstance(e, dict):
                continue
            ts = e.get('ts')
            if since is not None and (ts is None or ts <= since):
                continue
            row = {k: v for k, v in e.items() if k != 'mono'}
            mono = e.get('mono')
            row['age_s'] = None if mono is None else round(now - mono, 3)
            rows[str(key)] = row
        if rows:
            out[dev_cfg.id] = rows
    return out


def import_values(config, registry, data: Dict[str, Any]) -> Dict[str, int]:
    """Put the partner's values in this gateway's stores; a value held here
    newer (a device it hears itself) is kept."""
    now = time.monotonic()
    known = {d.id for d in config.devices}
    took = kept = unknown = 0
    for dev, rows in (data or {}).items():
        if dev not in known or not isinstance(rows, dict):
            unknown += 1
            continue
        store = registry.ensure_store(dev)
        for key, e in rows.items():
            try:
                k = int(key)
            except (TypeError, ValueError):
                continue
            if not isinstance(e, dict):
                continue
            mine = store.get(k)
            if mine and mine.get('ts') is not None and e.get('ts') is not None and mine['ts'] >= e['ts']:
                kept += 1
                continue
            entry = {kk: v for kk, v in e.items() if kk != 'age_s'}
            age = e.get('age_s')
            entry['mono'] = None if age is None else now - float(age)
            store[k] = entry
            took += 1
    return {'values': took, 'kept_newer': kept, 'devices_unknown': unknown}
