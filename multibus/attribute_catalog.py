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
"""The asset attribute catalog — what can be said about a piece of equipment
(docs/attributes.md).

The catalog is the gateway's. A user picks attributes from it, never invents
one, so a name means the same thing on every installation, and every system
that reads it can trust it. Everything is optional. These are facts about the
equipment (a grid limit, an array's tilt), never a policy (a minimum SOC) and
never a computation (a forecast).

Each category says:
- the levels it may sit on: the plant (site), an installation, a device;
- the kinds of equipment it fits best (offered first, never enforced);
- whether it repeats (one row per PV orientation);
- its fields: type, unit, limits, choices, required.

Labels and hints live in the UI language files (``attr.<category>.<field>``);
the English text here is the fallback.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

LEVELS = ('site', 'installation', 'device')
# the vocabulary installation groups already use for their role
KINDS = ('inverter', 'meter', 'battery', 'sensor', 'site', 'ups', 'charger')
MAX_ROWS = 64


def _num(lo, hi, unit='', required=False, label='', integer=False):
    return {'type': 'integer' if integer else 'number', 'min': lo, 'max': hi,
            'unit': unit, 'required': required, 'label': label}


def _choice(values, required=False, label=''):
    return {'type': 'choice', 'choices': list(values), 'required': required, 'label': label}


def _text(max_len=64, required=False, label=''):
    return {'type': 'text', 'max_len': max_len, 'required': required, 'label': label}


CATALOG: Dict[str, Dict[str, Any]] = {
    'location': {
        'label': 'Location',
        'levels': ('site', 'installation'),
        'kinds': (),
        'repeatable': False,
        'fields': {
            'latitude_deg': _num(-90, 90, '°', True, 'Latitude (north +)'),
            'longitude_deg': _num(-180, 180, '°', True, 'Longitude (east +)'),
            'altitude_m': _num(-500, 9000, 'm', False, 'Altitude'),
        },
    },
    'grid_connection': {
        'label': 'Grid connection',
        'levels': ('device',),
        'kinds': ('meter', 'site'),
        'repeatable': False,
        'fields': {
            'role': _choice(('connection_point', 'sub_meter', 'generation', 'load', 'storage'),
                            True, 'What this meter measures'),
            'max_import_kw': _num(0, 100000, 'kW', False, 'Largest import allowed'),
            'max_export_kw': _num(0, 100000, 'kW', False, 'Largest export allowed'),
            'phases': _choice((1, 3), False, 'Phases'),
            'main_fuse_a': _num(1, 10000, 'A', False, 'Main fuse, per phase'),
            'positive_is': _choice(('import', 'export'), False, "What this meter's positive power means"),
        },
    },
    'pv_input': {
        'label': 'PV input',
        'levels': ('device',),
        'kinds': ('inverter',),
        'repeatable': True,
        'fields': {
            'mppt': _num(1, 32, '', True, 'MPPT input', integer=True),
            'modules': _num(1, 100000, '', False, 'Modules', integer=True),
            'module_wp': _num(1, 2000, 'Wp', False, 'Module peak power'),
            'module_model': _text(64, False, 'Module make and model'),
            'technology': _choice(('monocrystalline', 'polycrystalline', 'thin_film'), False, 'Cell technology'),
            'strings': _num(1, 1000, '', False, 'Parallel strings', integer=True),
            'kwp': _num(0.01, 100000, 'kWp', False, 'Peak power'),
            'azimuth_deg': _num(0, 360, '°', True, 'Facing (0 N, 90 E, 180 S, 270 W)'),
            'tilt_deg': _num(0, 90, '°', True, 'Tilt (0 flat, 90 vertical)'),
        },
    },
    'battery': {
        'label': 'Battery',
        'levels': ('device', 'installation'),
        'kinds': ('battery',),
        'repeatable': False,
        'fields': {
            'usable_kwh': _num(0.01, 1000000, 'kWh', False, 'Usable energy'),
            'max_charge_kw': _num(0, 100000, 'kW', False, 'Largest charge power of the system'),
            'max_discharge_kw': _num(0, 100000, 'kW', False, 'Largest discharge power of the system'),
        },
    },
    'rating': {
        'label': 'Rating',
        'levels': ('device',),
        # an inverter's nameplate; a UPS's or a charger's output rating (its
        # load_pct, linked_meter.py) — in kW: a UPS sold in kVA is kVA × PF
        'kinds': ('inverter', 'ups', 'charger'),
        'repeatable': False,
        'fields': {
            'rated_ac_kw': _num(0.01, 100000, 'kW', True, 'Rated AC power (nameplate; a UPS in kVA: kVA × power factor)'),
        },
    },
}

LIMIT_FIELDS = ('max_import_kw', 'max_export_kw')
KWP_TOLERANCE = 0.01


def catalog_view() -> Dict[str, Any]:
    """The catalog as the API hands it to the UI and to other systems."""
    return {'levels': list(LEVELS), 'kinds': list(KINDS), 'max_rows': MAX_ROWS,
            'categories': {cid: {**{k: (list(v) if isinstance(v, tuple) else v) for k, v in c.items()
                                    if k != 'fields'},
                                 'fields': {fid: dict(f) for fid, f in c['fields'].items()}}
                           for cid, c in CATALOG.items()}}


def _check_field(cat: str, fid: str, spec: Dict, value: Any, where: str) -> Tuple[Any, Optional[str]]:
    t = spec['type']
    if t == 'text':
        if not isinstance(value, str) or not value.strip():
            return None, f"{where}.{fid}: text"
        if len(value) > spec['max_len']:
            return None, f"{where}.{fid}: at most {spec['max_len']} characters"
        from .text_check import topic_issue
        issue = topic_issue(value, f"{where}.{fid}")    # invisible or look-alike characters
        if issue:
            return None, issue
        return value, None
    if t == 'choice':
        # the same value AND type: True is not the choice 1, 3.0 not 3
        match = next((c for c in spec['choices'] if c == value and type(c) is type(value)), None)
        if match is None:
            return None, f"{where}.{fid}: one of {', '.join(map(str, spec['choices']))}"
        return match, None
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return None, f"{where}.{fid}: a number"
    except OverflowError:                     # an integer of 400 digits
        return None, f"{where}.{fid}: a number"
    if t == 'integer' and float(value) != int(value):
        return None, f"{where}.{fid}: a whole number"
    if not (spec['min'] <= value <= spec['max']):
        return None, f"{where}.{fid}: {spec['min']}…{spec['max']}{(' ' + spec['unit']) if spec['unit'] else ''}"
    if fid == 'azimuth_deg':
        value = float(value) % 360.0           # 360 is north again
    return (int(value) if t == 'integer' else value), None


def _check_row(cat: str, row: Any, where: str, mppt_count: Optional[int]) -> Tuple[Dict, List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    fields = CATALOG[cat]['fields']
    if not isinstance(row, dict):
        return {}, [f"{where}: an object of attributes"], []
    clean: Dict[str, Any] = {}
    for fid, value in row.items():
        if fid not in fields:
            errors.append(f"{where}.{fid}: not an attribute of {cat}")
            continue
        if value is None:
            continue                          # left empty: not set
        v, err = _check_field(cat, fid, fields[fid], value, where)
        if err:
            errors.append(err)
        else:
            clean[fid] = v
    for fid, spec in fields.items():
        if spec.get('required') and fid not in clean and not any(e.startswith(f"{where}.{fid}:") for e in errors):
            errors.append(f"{where}.{fid}: required")
    if cat == 'grid_connection' and clean.get('role') not in (None, 'connection_point'):
        for fid in LIMIT_FIELDS:
            if fid in clean:
                errors.append(f"{where}.{fid}: only a connection point carries the grid limits")
    if cat == 'pv_input':
        n, wp, kwp = clean.get('modules'), clean.get('module_wp'), clean.get('kwp')
        if n is not None and wp is not None:
            derived = n * wp / 1000.0
            if kwp is None:
                clean['kwp'] = round(derived, 3)
            elif abs(derived - kwp) > KWP_TOLERANCE * max(derived, kwp):
                errors.append(f"{where}.kwp: {kwp} kWp, but {n} × {wp} Wp is {derived:g} kWp")
        elif kwp is None:
            errors.append(f"{where}.kwp: required (or modules and module_wp)")
        if mppt_count and clean.get('mppt', 0) > mppt_count:
            warnings.append(f"{where}.mppt: {clean['mppt']}, but the device shows {mppt_count} inputs")
    return clean, errors, warnings


def validate(cat: str, value: Any, *, level: str,
             mppt_count: Optional[int] = None) -> Tuple[Any, List[str], List[str]]:
    """One category's value as it would be stored: (clean, errors, warnings).
    A repeatable category is a list of rows; any other, one object."""
    if cat not in CATALOG:
        return None, [f"{cat}: not in the attribute catalog"], []
    spec = CATALOG[cat]
    if level not in spec['levels']:
        return None, [f"{cat}: not on a {level} (it goes on {', '.join(spec['levels'])})"], []
    if spec['repeatable']:
        if not isinstance(value, list) or not value:
            return None, [f"{cat}: a list of rows"], []
        if len(value) > MAX_ROWS:
            return None, [f"{cat}: at most {MAX_ROWS} rows"], []
        rows, errors, warnings = [], [], []
        for i, row in enumerate(value):
            clean, e, w = _check_row(cat, row, f"{cat}[{i}]", mppt_count)
            rows.append(clean)
            errors += e
            warnings += w
        return rows, errors, warnings
    clean, errors, warnings = _check_row(cat, value, cat, mppt_count)
    if not clean and not errors:
        errors.append(f"{cat}: empty — delete the category instead")
    return clean, errors, warnings


def fits(cat: str, kind: Optional[str]) -> bool:
    """The category is offered first for this kind of equipment."""
    kinds = CATALOG[cat]['kinds']
    return not kinds or (kind in kinds)


# ── coordinates are personal data (docs/attributes.md, privacy) ─────────────

COORD_FIELDS = ('latitude_deg', 'longitude_deg', 'altitude_m')
PUBLISH_MODES = ('off', 'coarse', 'exact')


def coarse_location(loc: Dict[str, Any]) -> Dict[str, Any]:
    """Two decimals (about 1 km), no altitude: still well under 0.01° of sun
    error, no longer an address."""
    out = {}
    for k in ('latitude_deg', 'longitude_deg'):
        v = loc.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = round(float(v), 2)
    return out


def mask_coordinates(obj: Any, mode: str = 'coarse', depth: int = 0) -> Any:
    """A deep copy with every location coarsened (``coarse``) or removed
    (``off``); ``exact`` leaves it. For the audit, the snapshot diff and
    anything a viewer reads."""
    if depth > 10 or mode == 'exact':
        return obj
    if isinstance(obj, dict):
        if any(k in obj for k in COORD_FIELDS) and not any(
                isinstance(v, (dict, list)) for v in obj.values()):
            return coarse_location(obj) if mode == 'coarse' else {}
        return {k: mask_coordinates(v, mode, depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_coordinates(v, mode, depth + 1) for v in obj]
    return obj
