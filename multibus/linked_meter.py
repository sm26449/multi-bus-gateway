# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""A device whose electricity is measured by OTHER devices.

A heat pump, a boiler, a charger often has no meter of its own; a meter on
its supply measures it. The device names that meter as its `consumption`
link (meter_links.py; written as the earlier single link while it is the
only one):

    energy_meter: {device: iem3150, power: power_active_total,
                   energy: energy_active_import}

and gets two fields of its own, computed from the meter's live values like
any calculated field:

    power_electric    W, always positive (a meter wired the other way round
                      reads negative) — unless the link's `sign` says
                      `as_is` or `invert`
    energy_electric   the meter's counter, in the meter's unit (Wh / kWh)

A UPS, a charger, a converter names an `input` and an `output` meter and
gets what goes through it:

    power_in, power_out     W, each meter's power (signed as its link says)
    energy_in, energy_out   each meter's counter, in its unit, when the link
                            names one
    power_loss              W, in − out
    efficiency_pct          %, out / in × 100
    load_pct                %, out against its rated power (the device's
                            `rating` attribute, rated_ac_kw)

These are computed when a METER reports (CalcEngine.run_linked), not when
the device polls — a UPS with no communication of its own is measured by its
meters alone. Loss and efficiency are published only when they are honest
(``judge_pair``): both readings fresh, measured close in time, the input
steady, the output no larger than the input beyond the meters' own error,
and — for the efficiency — an output above the minimum load. Otherwise the
value is ABSENT: taken out of the live store and its retained MQTT topic
cleared, never a number clamped to 100 % — and the verdict of that
evaluation is what the device view shows. An efficiency per hour or per day
is left to whoever stores the counters.

A heat pump's COP is ``heat_output / power_electric`` in its template.
Nothing here knows any vendor.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

LINKED_FIELDS = ('power_electric', 'energy_electric')
PAIR_POWER = ('power_in', 'power_out', 'power_loss', 'efficiency_pct', 'load_pct')
PAIR_ENERGY = ('energy_in', 'energy_out')
GATED = ('power_loss', 'efficiency_pct', 'load_pct')
_POWER_FACTOR = {'w': 1, 'kw': 1000, 'mw': 1000000}
# a place attr() accepts: an id that is not one cannot carry a rating
_PLACE_ID = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
# the poll interval a meter is assumed to keep when its groups cannot be read
_DEFAULT_INTERVAL_S = 5.0
# the input is steady when it moved less than this since its previous
# reading (a load stepping between the two meters' readings makes their
# ratio meaningless) — or less than STEADY_W, near zero
STEADY_PCT = 5.0
STEADY_W = 1.0
# a reading is too old for the pair after this many of its meter's intervals
# (no 60 s floor: a pair is judged at the meters' own pace — a bus that
# froze both meters must not keep re-publishing their last ratio)
FRESH_INTERVALS = 3.0


def _pair(links: List[Dict[str, Any]]):
    """The input and output links, both reading power — or (None, None)."""
    lin = next((x for x in links or [] if x.get('role') == 'input' and x.get('power')), None)
    lout = next((x for x in links or [] if x.get('role') == 'output' and x.get('power')), None)
    return (lin, lout) if lin and lout else (None, None)


def names_for(links: List[Dict[str, Any]]) -> List[str]:
    """The names a device's links give it (before the meter is read)."""
    out: List[str] = []
    ln = next((x for x in links or [] if x.get('role') == 'consumption'), None)
    if ln:
        out += [n for n, k in zip(LINKED_FIELDS, ('power', 'energy')) if ln.get(k)]
    lin, lout = _pair(links)
    if lin:
        out += list(PAIR_POWER)
        out += [n for n, x in zip(PAIR_ENERGY, (lin, lout)) if x.get('energy')]
    return out


def _group(config: Any, dev_cfg: Any) -> str:
    """The group a linked value carries (its InfluxDB `poll_group` tag): the
    device's own most-used group, as before — 'normal' for a device that
    reads nothing itself."""
    try:
        mine = [r.poll_group for r in config.unit_registers(dev_cfg) if getattr(r, 'poll_group', '')]
    except Exception:  # noqa: BLE001 — unreadable selection
        mine = []
    return max(set(mine), key=mine.count) if mine else 'normal'


def _interval(config: Any, meter: Any, field: str) -> float:
    """How often ``meter`` reads ``field`` (its poll group's interval)."""
    try:
        sources = list(getattr(meter, 'sources', None) or []) or [None]
        for src in sources:
            regs, groups = (config.load_device_registers(meter, source=src) if src is not None
                            else config.load_device_registers(meter))
            reg = next((r for r in regs if r.name == field), None)
            if reg is not None:
                g = (groups or {}).get(getattr(reg, 'poll_group', '') or 'normal')
                iv = float(getattr(g, 'interval', 0) or 0)
                return iv if iv > 0 else _DEFAULT_INTERVAL_S
    except Exception:  # noqa: BLE001 — unreadable selection: the default rhythm
        pass
    return _DEFAULT_INTERVAL_S


def pair_settings(config: Any, dev_cfg: Any) -> Optional[Dict[str, Any]]:
    """What an input / output pair is computed from, or None: each meter's
    power (and energy) reference, unit, sign and pace, and the honesty
    thresholds. Built when the device's calculated fields load (it reads the
    meters' register files) and kept by the engine — never per request."""
    from .meter_links import links_of, options_of
    lin, lout = _pair(links_of(dev_cfg))
    if not lin:
        return None
    devices = {d.id: d for d in getattr(config, 'devices', None) or []}
    sides = {}
    for side, ln in (('in', lin), ('out', lout)):
        meter = devices.get(ln['device'])
        if meter is None or ln['device'] == getattr(dev_cfg, 'id', None):
            return None
        regs = {r.name: r for r in config.unit_registers(meter)}
        if ln['power'] not in regs:
            return None
        en = ln.get('energy') if ln.get('energy') in regs else None
        sides[side] = {'device': meter.id, 'field': ln['power'], 'sign': ln.get('sign') or 'auto',
                       'unit': str(regs[ln['power']].unit or 'W'),
                       'interval': _interval(config, meter, ln['power']),
                       'energy': en, 'energy_unit': str(regs[en].unit or 'kWh') if en else '',
                       'name': meter.name or meter.id}
    opts = options_of(dev_cfg)
    skew = opts.get('max_skew_s')
    if skew is None:
        # two independent pollers on one interval are up to one interval
        # apart: a longer gap means a reading was missed (review M13)
        skew = max(1.0, max(sides['in']['interval'], sides['out']['interval']))
    return {'in': sides['in'], 'out': sides['out'], 'max_skew_s': float(skew),
            'min_load_w': float(opts['min_load_w']), 'tolerance_pct': float(opts['tolerance_pct']),
            'rated': bool(_PLACE_ID.match(str(getattr(dev_cfg, 'id', ''))))}


_UNSET: Any = object()


def linked_calcs(config: Any, dev_cfg: Any, ps: Any = _UNSET) -> List[Dict[str, Any]]:
    """The calculated entries a device's meters give it ([] if none). The
    consumption link's are formulas; an input / output pair's are computed
    by ``evaluate_pair`` (``_pair`` names which value). ``ps``: the pair's
    settings when the caller has them (pair_settings reads files)."""
    from .meter_links import link_for
    out: List[Dict[str, Any]] = []
    group = None
    link = link_for(dev_cfg, 'consumption') or {}
    mid = str(link.get('device') or '')
    meter = (next((d for d in config.devices if d.id == mid), None)
             if mid and mid != getattr(dev_cfg, 'id', None) else None)
    if meter is not None:
        regs = {r.name: r for r in config.unit_registers(meter)}
        group = _group(config, dev_cfg)
        p = link.get('power')
        if p in regs:
            factor = _POWER_FACTOR.get(str(regs[p].unit or 'W').strip().lower(), 1)
            sign = link.get('sign') or 'auto'
            read = {'auto': f"abs({mid}.{p})", 'as_is': f"{mid}.{p}", 'invert': f"-({mid}.{p})"}[sign]
            out.append({'name': 'power_electric', 'label': f'Electric power ({meter.name or mid})',
                        'unit': 'W', 'category': 'energy', 'poll_group': group, 'decimals': 0,
                        'expr': read + (f" * {factor}" if factor != 1 else ''),
                        '_linked': True})
        e = link.get('energy')
        if e in regs:
            out.append({'name': 'energy_electric', 'label': f'Electric energy ({meter.name or mid})',
                        'unit': regs[e].unit or 'kWh', 'category': 'energy', 'poll_group': group,
                        'expr': f"{mid}.{e}", '_linked': True})
    if ps is _UNSET:
        ps = pair_settings(config, dev_cfg)
    if ps is not None:
        group = group or _group(config, dev_cfg)
        base = {'category': 'energy', 'poll_group': group, '_linked': True, 'expr': ''}
        out += [
            {**base, '_pair': 'power_in', 'name': 'power_in', 'unit': 'W', 'decimals': 0,
             'label': f"Input power ({ps['in']['name']})"},
            {**base, '_pair': 'power_out', 'name': 'power_out', 'unit': 'W', 'decimals': 0,
             'label': f"Output power ({ps['out']['name']})"},
            {**base, '_pair': 'power_loss', 'name': 'power_loss', 'unit': 'W', 'decimals': 0,
             'label': 'Power loss (input − output)'},
            {**base, '_pair': 'efficiency_pct', 'name': 'efficiency_pct', 'unit': '%', 'decimals': 2,
             'label': 'Efficiency (output / input)'},
            {**base, '_pair': 'load_pct', 'name': 'load_pct', 'unit': '%', 'decimals': 1,
             'label': 'Load (output / rated power)'},
        ]
        for name, side in (('energy_in', 'in'), ('energy_out', 'out')):
            if ps[side]['energy']:
                out.append({**base, '_pair': name, 'name': name, 'unit': ps[side]['energy_unit'],
                            'label': f"{'Input' if side == 'in' else 'Output'} energy ({ps[side]['name']})"})
    return out


# ── an input / output pair, evaluated ───────────────────────────────────────

def _signed(v: float, unit: str, sign: str) -> float:
    v = float(v) * _POWER_FACTOR.get(str(unit or 'W').strip().lower(), 1)
    return abs(v) if sign == 'auto' else (-v if sign == 'invert' else v)


def pair_expire_after(ps: Dict[str, Any]) -> int:
    """Seconds after which Home Assistant marks a pair value unavailable when
    the gateway stops sending it. HA's MQTT sensor IGNORES an empty state
    payload (the retained clear speaks to other consumers and new
    subscribers, not to HA): ``expire_after`` is HA's own "no value". The
    values are re-sent every third of it while present (``pair_heartbeat``),
    so a steady load is never taken for a vanished one."""
    slower = max(float(ps['in']['interval']), float(ps['out']['interval']))
    return int(max(60.0, 6.0 * slower))


def pair_heartbeat(ps: Dict[str, Any]) -> int:
    return max(10, pair_expire_after(ps) // 3)


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def judge_pair(pin: Optional[float], pout: Optional[float],
               age_in: Optional[float], age_out: Optional[float],
               prev_in: Optional[float], ps: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Why loss and efficiency are absent now ({name: reason or None}). The
    one place the tests live; ``evaluate_pair`` publishes by it.

    Reasons, in order: no_reading, no_time (a reading without a measurement
    time), stale (older than FRESH_INTERVALS of its meter's interval), skew
    (the two measured more than max_skew_s apart), unsteady (the input moved
    more than STEADY_PCT since its previous reading, or there is none yet),
    out_exceeds_in (output above input beyond tolerance_pct); efficiency
    also below_min_load (output under min_load_w). Loss has no minimum: a
    UPS's standby loss is the number an operator wants, and an absolute
    difference does not suffer the ratio's noise."""
    def common() -> Optional[str]:
        if pin is None or pout is None:
            return 'no_reading'
        if age_in is None or age_out is None:
            return 'no_time'
        if (age_in > FRESH_INTERVALS * ps['in']['interval']
                or age_out > FRESH_INTERVALS * ps['out']['interval']):
            return 'stale'
        if abs(age_in - age_out) > ps['max_skew_s']:
            return 'skew'
        if prev_in is None or abs(pin - prev_in) > max(STEADY_PCT / 100.0 * abs(pin), STEADY_W):
            return 'unsteady'
        if pout > pin * (1.0 + ps['tolerance_pct'] / 100.0) and pout > STEADY_W:
            return 'out_exceeds_in'
        return None
    why = common()
    eff = why
    if eff is None and (pout < ps['min_load_w'] or pin <= 0):
        eff = 'below_min_load'
    return {'power_loss': why, 'efficiency_pct': eff}


def evaluate_pair(ps: Dict[str, Any], resolve: Callable, state: Dict[str, Any],
                  rating_kw: Optional[float]) -> Tuple[Dict[str, Optional[float]], Dict[str, Dict]]:
    """One evaluation of an input / output pair: ``(values, verdict)``.
    ``values``: every pair name → its value, or None when absent this round.
    ``verdict``: power_loss / efficiency_pct / load_pct → {state, reason?}.
    ``resolve`` is the engine's resolver (it records each reading's monotonic
    measurement time in ``touched_mono``); ``state`` keeps the input's
    previous reading between evaluations."""
    now = time.monotonic()

    def raw(side, key='field'):
        """(value, monotonic measurement time) of one meter field."""
        ref = ps[side][key]
        if not ref:
            return None, None
        seen = getattr(resolve, 'touched_mono', None)
        n = len(seen) if isinstance(seen, list) else 0
        try:
            v = resolve(f"{ps[side]['device']}.{ref}")
        except Exception:  # noqa: BLE001 — a missing value is absent, never an error
            v = None
        if not _num(v):
            return None, None
        monos = list(getattr(resolve, 'touched_mono', None) or [])[n:]
        return v, (monos[-1] if monos else None)

    a, mono_in = raw('in')
    b, mono_out = raw('out')
    age_in = max(0.0, now - mono_in) if mono_in is not None else None
    age_out = max(0.0, now - mono_out) if mono_out is not None else None
    pin = _signed(a, ps['in']['unit'], ps['in']['sign']) if a is not None else None
    pout = _signed(b, ps['out']['unit'], ps['out']['sign']) if b is not None else None
    # the input's previous reading: the last one measured at another moment
    # (an evaluation the output meter triggered sees the same input reading)
    prev_in = None
    if pin is not None and mono_in is not None:
        last = state.get('in_last')
        if last is None or last[0] != mono_in:
            if last is not None:
                state['in_prev'] = last
            state['in_last'] = (mono_in, pin)
        prev_in = (state.get('in_prev') or (None, None))[1]
    why = judge_pair(pin, pout, age_in, age_out, prev_in, ps)
    vals: Dict[str, Optional[float]] = {'power_in': pin, 'power_out': pout,
                                        'power_loss': None, 'efficiency_pct': None, 'load_pct': None}
    if why['power_loss'] is None:
        vals['power_loss'] = pin - pout
    if why['efficiency_pct'] is None:
        vals['efficiency_pct'] = pout / pin * 100.0
    # load: the output against the rating — only a fresh, non-negative output
    if not ps['rated'] or not _num(rating_kw) or rating_kw <= 0:
        load_why = 'no_rating'
    elif pout is None:
        load_why = 'no_reading'
    elif age_out is None:
        load_why = 'no_time'
    elif age_out > FRESH_INTERVALS * ps['out']['interval']:
        load_why = 'stale'
    elif pout < 0:
        load_why = 'negative'
    else:
        load_why = None
        vals['load_pct'] = pout / (rating_kw * 1000.0) * 100.0
    for name, side in (('energy_in', 'in'), ('energy_out', 'out')):
        if ps[side]['energy']:
            e, _age = raw(side, 'energy')
            vals[name] = e
    verdict = {n: ({'state': 'absent', 'reason': r} if r else {'state': 'published'})
               for n, r in (('power_loss', why['power_loss']), ('efficiency_pct', why['efficiency_pct']),
                            ('load_pct', load_why))}
    return vals, verdict
