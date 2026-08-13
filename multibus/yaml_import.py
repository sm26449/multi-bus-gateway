# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Community template import — turn an upstream YAML register map into an MBG
device-template preview.

YAML is the format most community device maps ship in, and it is richer than
CSV: a register can carry nested ``enum``/``bits`` decode maps, a write-safety
envelope, thresholds — everything MBG understands. This importer accepts a wide
range of upstream shapes (a bare list, a ``registers:`` list, or an MBG-native
``device_template:`` wrapper) and per-field aliases (``reg``/``addr``,
``type``/``datatype``, ``factor``/``gain`` …), so a map from another tool usually
imports with little or no massaging. Native MBG register fields pass through
untouched. Symmetric with :mod:`multibus.csv_import`: returns a preview the UI
reviews before saving.
"""
from __future__ import annotations

from typing import Any, Dict, List

import yaml

from .config import normalize_register_type
from .csv_import import (_COLUMNS, _DATA_TYPE_ALIASES, _norm, _parse_addr)

# MBG-native register fields copied through verbatim when present (the value is
# trusted to validate at save time). These are exactly what makes a YAML import
# higher-fidelity than CSV.
_PASSTHROUGH = ('enum', 'bits', 'mask', 'shift', 'offset', 'monotonic', 'nan',
                'writable', 'write_min', 'write_max', 'write_safe', 'thresholds',
                'topic', 'device_class', 'state_class', 'entity_category',
                'enabled_by_default', 'icon', 'suggested_display_precision')

# where the register list can hide in an upstream doc
_LIST_KEYS = ('registers', 'registry', 'points', 'signals', 'measurements',
              'sensors', 'parameters', 'map', 'items')
_META_KEYS = ('id', 'name', 'vendor', 'model', 'device', 'title', 'description')


def _get(d: Dict, canon: str, default=None):
    """Value for a canonical register field from a dict, honoring aliases."""
    norm_map = {_norm(k): k for k in d}
    for alias in _COLUMNS.get(canon, (canon,)):
        if alias in norm_map:
            return d[norm_map[alias]]
    return default


def _find_registers(doc):
    """Locate the register list + template meta in a variety of shapes."""
    if isinstance(doc, list):
        return doc, {}
    if not isinstance(doc, dict):
        return None, {}
    dt = doc.get('device_template')
    if isinstance(dt, dict) and isinstance(dt.get('registers'), list):
        return dt['registers'], {k: dt.get(k) for k in _META_KEYS}
    for key in _LIST_KEYS:
        if isinstance(doc.get(key), list):
            return doc[key], {k: doc.get(k) for k in _META_KEYS}
    return None, {}


def parse_yaml(text: str, *, default_data_type: str = 'float',
               default_poll_group: str = '') -> Dict:
    """Parse an upstream YAML register map.

    Returns ``{registers, errors, warnings, meta}`` — ``errors`` are fatal
    (unparseable / no register list); ``warnings`` are per-row skips/coercions.
    """
    if not (text or '').strip():
        return {'registers': [], 'errors': ['empty YAML'], 'warnings': [], 'meta': {}}
    try:
        doc = yaml.safe_load(text)
    except Exception as e:  # noqa: BLE001
        return {'registers': [], 'errors': [f'invalid YAML: {e}'], 'warnings': [], 'meta': {}}

    reglist, meta = _find_registers(doc)
    if reglist is None:
        return {'registers': [], 'errors': [
            "no register list found — expected a 'registers:' list, a bare list, "
            "or a 'device_template:' wrapper"], 'warnings': [], 'meta': {}}

    registers: List[Dict] = []
    warnings: List[str] = []
    seen = set()
    for i, r in enumerate(reglist, 1):
        if not isinstance(r, dict):
            warnings.append(f"item {i}: not a mapping — skipped")
            continue
        name = str(_get(r, 'name', '') or '').strip()
        if not name:
            warnings.append(f"item {i}: no name — skipped")
            continue

        addr_raw = _get(r, 'address')
        json_path = str(_get(r, 'json_path', '') or '').strip()
        addr = None
        if addr_raw is not None and str(addr_raw).strip() != '':
            try:
                addr = _parse_addr(addr_raw)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"{name}: bad address {addr_raw!r} ({e}) — skipped")
                continue
        elif not json_path:
            warnings.append(f"{name}: no address and no json_path — skipped")
            continue

        key = (addr, name)
        if key in seen:
            warnings.append(f"{name}: duplicate address+name — skipped")
            continue
        seen.add(key)

        dts = _norm(str(_get(r, 'data_type', '') or ''))
        dt = _DATA_TYPE_ALIASES.get(dts)
        if dt is None and dts:
            raw_dt = str(_get(r, 'data_type')).lower()
            if raw_dt.startswith('string') or raw_dt in ('sm16', 'sm32'):
                dt = raw_dt                          # MBG-native types pass through
            else:
                warnings.append(f"{name}: unknown type {raw_dt!r} → {default_data_type}")
        dt = dt or default_data_type

        sc_raw = _get(r, 'scale', 1)
        try:
            scale = float(sc_raw) if sc_raw not in (None, '') else 1.0
        except (TypeError, ValueError):
            warnings.append(f"{name}: bad scale {sc_raw!r} → 1")
            scale = 1.0

        reg: Dict[str, Any] = {
            'name': name,
            'label': str(_get(r, 'label', '') or name),
            'unit': str(_get(r, 'unit', '') or ''),
            'data_type': dt,
            'scale': scale,
        }
        if addr is not None:
            reg['address'] = addr
        if json_path:
            reg['json_path'] = json_path
        rtype = normalize_register_type(_get(r, 'register_type') or r.get('fc'))
        if rtype != 'holding':
            reg['register_type'] = rtype
        pg = str(_get(r, 'poll_group', '') or default_poll_group or '').strip()
        if pg:
            reg['poll_group'] = pg
        cat = str(_get(r, 'category', '') or '').strip()
        if cat:
            reg['category'] = cat

        norm_map = {_norm(k): k for k in r}
        for f in _PASSTHROUGH:
            nf = _norm(f)                            # keys in norm_map are normalized
            if nf in norm_map and r[norm_map[nf]] not in (None, ''):
                reg[f] = r[norm_map[nf]]
        registers.append(reg)

    return {'registers': registers, 'errors': [], 'warnings': warnings,
            'meta': {k: v for k, v in (meta or {}).items() if v}}
