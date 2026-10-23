# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The meters that measure a device, each in a role.

A heat pump has one meter on its supply (``consumption``); a UPS has one on
its input and one on its output. A device lists them:

    meters:
      - {role: input,  device: em-ups-in,  power: power_active_total, energy: energy_active_import}
      - {role: output, device: em-ups-out, power: power_active_total}

A role comes from a fixed catalog (ROLES) so it means the same everywhere;
one link per role; a meter may serve several devices; a chain of meters may
never loop back. The single ``energy_meter`` link of earlier versions reads
as one ``consumption`` link, and is still what gets written while that is
all a device has — an older gateway reading the file loses nothing.

What a link gives the device (``power_electric`` / ``energy_electric`` for
consumption) is linked_meter.py's; the reverse question — what does this
meter serve? — is ``served_by``, asked before the meter is deleted.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .device_kind import METER_ONLY, METER_ONLY_FIRST

logger = logging.getLogger(__name__)

# role → what it means; `pair` is the role that completes it
ROLES: Dict[str, Dict[str, Any]] = {
    'consumption': {'pair': None, 'available': True,
                    'label': 'Consumption', 'hint': 'the one meter on its supply'},
    'input': {'pair': 'output', 'available': True,
              'label': 'Input', 'hint': 'what goes in (a UPS, a charger, a converter)'},
    'output': {'pair': 'input', 'available': True,
               'label': 'Output', 'hint': 'what comes out'},
    # named now so no one stores them with another meaning; offered later
    'production': {'pair': None, 'available': False, 'label': 'Production', 'hint': ''},
    'primary': {'pair': 'secondary', 'available': False, 'label': 'Primary', 'hint': ''},
    'secondary': {'pair': 'primary', 'available': False, 'label': 'Secondary', 'hint': ''},
    'ac_side': {'pair': 'dc_side', 'available': False, 'label': 'AC side', 'hint': ''},
    'dc_side': {'pair': 'ac_side', 'available': False, 'label': 'DC side', 'hint': ''},
}
# how a meter's power reads: `auto` = always positive (a meter wired the
# other way round reads negative), `as_is`, `invert`
SIGNS = ('auto', 'as_is', 'invert')
_KEYS = ('role', 'device', 'power', 'energy', 'sign')
# what a link's fields must measure (the meter's register unit, any case)
UNITS = {'power': ('W', 'kW', 'MW'), 'energy': ('Wh', 'kWh', 'MWh')}

# When the values an input and an output meter give (linked_meter.py) are
# honest enough to publish. Kept beside the links (``meter_options``):
#   min_load_w     below this output an efficiency is noise (a 3 kVA UPS at
#                  20 W): no efficiency, no loss. 100 W by default
#   tolerance_pct  output may read above input by this much — the two meters'
#                  own error (class 1 each: ±1 %, so 2 % by default) — and the
#                  values are still published, never clamped; above it the
#                  meters disagree (a swapped or mis-scaled meter): nothing
#   max_skew_s     the two readings may be measured this far apart. Unset:
#                  twice the slower of the two meters' poll intervals, at
#                  least 5 s — two independent pollers drift up to one
#                  interval apart; twice that lets one reading come late
#                  without dropping every other value
OPTION_DEFAULTS: Dict[str, Any] = {'min_load_w': 100.0, 'tolerance_pct': 2.0, 'max_skew_s': None}
_OPTION_BOUNDS = {'min_load_w': (0.0, 10_000_000.0), 'tolerance_pct': (0.0, 20.0),
                  'max_skew_s': (1.0, 3600.0)}


def _num_ok(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def check_options(raw: Any) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """``meter_options`` as a save may store them — what was sent, as sent (a
    value equal to today's default stays the operator's choice if the default
    ever changes) — and why not (``{options.key: why}``). A key left out is
    its default; null is refused, as for ``meters`` (``{}`` = all defaults)."""
    if raw is None:
        return {}, {'options': 'send {} for the defaults (null is not taken as "none")'}
    if not isinstance(raw, dict):
        return {}, {'options': 'an object {min_load_w, tolerance_pct, max_skew_s}'}
    out: Dict[str, Any] = {}
    errs: Dict[str, str] = {}
    for k, v in raw.items():
        if k not in OPTION_DEFAULTS:
            errs[f'options.{k}'] = f"unknown option — one of {', '.join(OPTION_DEFAULTS)}"
            continue
        lo, hi = _OPTION_BOUNDS[k]
        if v is None:
            errs[f'options.{k}'] = 'leave it out for the default (null is refused)'
            continue
        if not _num_ok(v) or not (lo <= float(v) <= hi):
            errs[f'options.{k}'] = f"a number from {lo:g} to {hi:g}"
            continue
        out[k] = float(v)
    return ({} if errs else out), errs


def parse_options(raw: Any, did: str, ignored: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """A stored ``meter_options`` block, read leniently: a wrong block or key
    is dropped (its default is used) with a note in ``ignored`` — never the
    device."""
    if raw in (None, '', {}):
        return {}

    def note(where: str, why: str):
        logger.warning(f"devices[{did}]: {why}")
        if ignored is not None:
            ignored[f"{did}.{where}"] = why

    if not isinstance(raw, dict):
        note('meter_options', f"meter_options {raw!r} is not a mapping: ignored")
        return {}
    out: Dict[str, Any] = {}
    for k, v in raw.items():
        good, errs = check_options({k: v})
        if errs:
            note(f'meter_options.{k}', f"meter_options.{k} {v!r}: {next(iter(errs.values()))} — the default is used")
            continue
        out.update(good)
    return out


def options_of(dev: Any) -> Dict[str, Any]:
    """Every option: the device's own over the defaults."""
    return {**OPTION_DEFAULTS, **(getattr(dev, 'meter_options', None) or {})}


def _clean(link: Dict[str, Any]) -> Dict[str, Any]:
    out = {'role': str(link['role']), 'device': str(link['device'])}
    for k in ('power', 'energy'):
        if link.get(k):
            out[k] = str(link[k])
    out['sign'] = str(link.get('sign') or 'auto')
    return out


def parse(raw: Any, did: str, ignored: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """A device's stored ``meters`` list, read leniently: a block that is not
    a list, or an entry that cannot be a link, is dropped with a note in
    ``ignored`` (which the status page and the event log show) — never the
    device."""
    if raw in (None, '', [], {}):
        return []

    def note(where: str, why: str):
        logger.warning(f"devices[{did}]: {why}")
        if ignored is not None:
            ignored[f"{did}.{where}"] = why

    if not isinstance(raw, list):
        note('meters', f"meters {raw!r} is not a list: ignored")
        return []
    out: List[Dict[str, Any]] = []
    seen = set()
    for i, ln in enumerate(raw):
        at = f'meters[{i}]'
        # every value a hand edit can put there is judged as text — a list
        # or a mapping where a name belongs drops the entry, never the device
        if not isinstance(ln, dict) or not isinstance(ln.get('role'), str) \
                or not isinstance(ln.get('device'), str) or not ln['device'] or ln['role'] not in ROLES:
            note(at, f"{at} {ln!r} is not a link (role and device): ignored")
            continue
        bad = [k for k in ('power', 'energy', 'sign') if ln.get(k) is not None and not isinstance(ln.get(k), str)]
        if bad:
            note(at, f"{at}: {', '.join(bad)} must be a name: ignored")
            continue
        if ln['role'] in seen:
            note(at, f"{at}: a second '{ln['role']}' link: ignored")
            continue
        if (ln.get('sign') or 'auto') not in SIGNS:
            note(at, f"{at}: sign {ln.get('sign')!r} is not one of {', '.join(SIGNS)}: ignored")
            continue
        seen.add(ln['role'])
        out.append(_clean(ln))
    return out


def parse_device(d: Dict[str, Any], did: str, ignored: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """A raw device entry's ``meters``, read leniently; an ``energy_meter``
    stored beside it that disagrees is noted (``meters`` is used)."""
    out = parse(d.get('meters'), did, ignored)
    em = d.get('energy_meter')
    conflict_note(did, out, em if isinstance(em, dict) else {}, ignored)
    return out


def conflict_note(did: str, meters: List[Dict[str, Any]], energy_meter: Dict[str, Any],
                  ignored: Optional[Dict[str, str]] = None) -> None:
    """Both forms stored (an older version edited ``energy_meter`` after a
    rollback): ``meters`` wins — said once, at load, when they disagree."""
    if not meters or not energy_meter:
        return
    mine = next((ln for ln in meters if ln['role'] == 'consumption'), None)
    same = mine is not None and all(str(energy_meter.get(k) or '') == str(mine.get(k) or '')
                                    for k in ('device', 'power', 'energy'))
    if not same:
        why = (f"energy_meter {energy_meter!r} disagrees with meters: meters is used "
               "(an older version may have edited it after a rollback)")
        logger.warning(f"devices[{did}]: {why}")
        if ignored is not None:
            ignored[f"{did}.energy_meter"] = why


def links_of(dev: Any) -> List[Dict[str, Any]]:
    """The meters that measure this device: ``[{role, device, power?,
    energy?, sign}]``. Its ``meters`` list when it has one, else the single
    ``energy_meter`` link as one ``consumption`` link."""
    own = getattr(dev, 'meters', None) or []
    if own:
        return [dict(ln) for ln in own if ln.get('device') != getattr(dev, 'id', None)]
    link = getattr(dev, 'energy_meter', None) or {}
    mid = str(link.get('device') or '') if isinstance(link, dict) else ''
    if not mid or mid == getattr(dev, 'id', None):
        return []
    out = {'role': 'consumption', 'device': mid}
    for k in ('power', 'energy'):
        if link.get(k):
            out[k] = str(link[k])
    out['sign'] = 'auto'
    return [out]


def link_for(dev: Any, role: str) -> Optional[Dict[str, Any]]:
    return next((ln for ln in links_of(dev) if ln['role'] == role), None)


def served_by(config: Any, meter_id: str) -> List[Tuple[str, str, int]]:
    """``[(device_id, role, index)]`` of every device this meter measures."""
    return [(d.id, ln['role'], i) for d in getattr(config, 'devices', None) or []
            for i, ln in enumerate(links_of(d)) if ln['device'] == meter_id]


def stored_form(links: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:
    """What is written for ``links``: ``(energy_meter, meters)``, one of them
    None. A single consumption link that reads its power as always positive
    is written as the earlier ``energy_meter`` (a rollback to an older image
    keeps it); anything else as ``meters``."""
    links = [_clean(ln) for ln in links]
    if not links:
        return None, None
    if len(links) == 1 and links[0]['role'] == 'consumption' and links[0]['sign'] == 'auto':
        one = {k: v for k, v in links[0].items() if k in ('device', 'power', 'energy')}
        return one, None
    return None, links


def _edges(config: Any, dev_id: str, mine: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """device → the meters that measure it, with ``dev_id``'s links replaced
    by ``mine`` (the list being saved)."""
    g = {d.id: [ln['device'] for ln in links_of(d)] for d in getattr(config, 'devices', None) or []}
    g[dev_id] = [ln['device'] for ln in mine]
    return g


def _loop_from(g: Dict[str, List[str]], start: str, target: str) -> Optional[List[str]]:
    """A path start → … → target through the links, if there is one."""
    stack = [(start, [start])]
    seen = set()
    while stack:
        node, path = stack.pop()
        if node == target:
            return path
        if node in seen:
            continue
        seen.add(node)
        for nxt in g.get(node, []):
            stack.append((nxt, path + [nxt]))
    return None


def validate(config: Any, dev: Any, raw: Any,
             reserved_names: Iterable[str] = ()) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """The list a save may store, and why not (``{path: why}``, keyed
    ``meters[i].field``). ``reserved_names``: the names the device already
    reads — a link may not give it one of them a second time."""
    errs: Dict[str, str] = {}
    if raw is None:
        # a null is a client's mistake more often than an intent: none is []
        return [], {'meters': 'send [] for no meters (null is not taken as "none")'}
    if not isinstance(raw, list):
        return [], {'meters': 'a list of links ({role, device, power, energy, sign})'}
    if getattr(dev, 'primary', False) and raw:
        return [], {'meters': 'the primary device is a meter itself'}
    if getattr(dev, 'endpoint_id', '') and raw:
        return [], {'meters': f"'{dev.id}' belongs to an installation — not supported for its units yet"}
    devices = {d.id: d for d in getattr(config, 'devices', None) or []}
    out: List[Dict[str, Any]] = []
    roles_seen: Dict[str, int] = {}
    for i, ln in enumerate(raw):
        at = f'meters[{i}]'
        if not isinstance(ln, dict):
            errs[at] = 'a link is {role, device, power, energy, sign}'
            continue
        extra = sorted(set(ln) - set(_KEYS))
        if extra:
            errs[at] = f"unknown key(s): {', '.join(extra)}"
        role = str(ln.get('role') or '')
        if role not in ROLES:
            errs[f'{at}.role'] = f"'{role}' is not a role — one of {', '.join(r for r, v in ROLES.items() if v['available'])}"
        elif not ROLES[role]['available']:
            errs[f'{at}.role'] = f"the '{role}' role is not available yet"
        elif role in roles_seen:
            errs[f'{at}.role'] = f"'{role}' is already meters[{roles_seen[role]}] — one link per role"
        else:
            roles_seen[role] = i
        mid = str(ln.get('device') or '')
        meter = devices.get(mid)
        if not mid:
            errs[f'{at}.device'] = 'name the meter device'
        elif mid == dev.id:
            errs[f'{at}.device'] = 'a device cannot be its own meter'
            meter = None
        elif meter is None:
            errs[f'{at}.device'] = f"no device '{mid}'"
        elif getattr(meter, 'endpoint_id', ''):
            # a unit's id follows its installation (renumbered, regrouped):
            # a link to it would dangle after an installation edit
            errs[f'{at}.device'] = (f"'{mid}' is a unit of the installation '{meter.endpoint_id}' — "
                                    "its id changes with the installation; name a device of its own")
            meter = None
        if not ln.get('power') and not ln.get('energy'):
            errs[f'{at}.power'] = "name the meter's power field, its energy counter, or both"
        elif meter is not None:
            regs = {r.name: r for r in config.unit_registers(meter)}
            for k in ('power', 'energy'):
                v = ln.get(k)
                if not v:
                    continue
                if str(v) not in regs:
                    errs[f'{at}.{k}'] = f"'{v}' is not a measurement of {mid}"
                    continue
                unit = str(getattr(regs[str(v)], 'unit', '') or '').strip()
                if unit.lower() not in {u.lower() for u in UNITS[k]}:
                    errs[f'{at}.{k}'] = (f"'{v}' of {mid} reads {unit or 'no unit'} — "
                                         f"a {k} field reads {' / '.join(UNITS[k])}")
        sign = str(ln.get('sign') or 'auto')
        if sign not in SIGNS:
            errs[f'{at}.sign'] = f"'{sign}' is not one of {', '.join(SIGNS)}"
        if not any(k.startswith(at) for k in errs):
            out.append(_clean(ln))
    # one meter may measure both sides on two channels — never one field
    _in = next((ln for ln in out if ln['role'] == 'input'), None)
    _out = next((ln for ln in out if ln['role'] == 'output'), None)
    if _in and _out and _in['device'] == _out['device'] and _in.get('power') and _in.get('power') == _out.get('power'):
        errs[f"meters[{roles_seen['output']}].power"] = (
            f"the input already reads {_in['device']}.{_in['power']} — the output is another field "
            "(the same one would make the efficiency 100 % always)")
    if 'consumption' in roles_seen and ({'input', 'output'} & set(roles_seen)):
        errs[f"meters[{roles_seen['consumption']}].role"] = (
            'consumption is the one meter on the supply; with input and output, '
            'the input meter is what it takes')
    if errs:
        return [], errs
    # a chain of meters may never loop back: x measured by y measured by … x
    g = _edges(config, dev.id, out)
    for i, ln in enumerate(out):
        path = _loop_from(g, ln['device'], dev.id)
        if path:
            errs[f'meters[{i}].device'] = ('the meters would loop: '
                                           + ' → '.join([dev.id] + path))
    # what a link gives must not be a name the device already reads
    from .linked_meter import names_for
    clash = sorted(set(names_for(out)) & set(reserved_names))
    if clash:
        i = next((i for i, ln in enumerate(out) if ln['role'] == 'consumption'),
                 next((i for i, ln in enumerate(out) if ln['role'] == 'output'), 0))
        errs[f'meters[{i}].role'] = (f"{dev.id} already reads {', '.join(clash)} itself — "
                                     "a meter link would give it a second value of that name")
    return ([] if errs else out), errs


def catalog() -> Dict[str, Any]:
    """What GET /api/meter-roles answers."""
    return {'roles': [{'id': r, 'label': v['label'], 'hint': v['hint'], 'pairs_with': v['pair'],
                       'available': v['available']} for r, v in ROLES.items()],
            'signs': list(SIGNS),
            'options': {k: {'default': v, 'min': _OPTION_BOUNDS[k][0], 'max': _OPTION_BOUNDS[k][1]}
                        for k, v in OPTION_DEFAULTS.items()},
            # what a device measured only by its meters may be (Add device)
            'meter_only_kinds': {'first': list(METER_ONLY_FIRST),
                                 'more': [k for k in METER_ONLY if k not in METER_ONLY_FIRST]}}
