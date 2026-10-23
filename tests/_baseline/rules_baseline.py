# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
# FROZEN: multibus/rules.py as of 042bdee (before the opt-in OV tuning keys,
# ADR-0012) — the reference tests/test_ov_tuning_identity.py compares the
# engine against. Never edit; replace only when a later lot changes the baseline.
"""Rules — the declarative controller (docs/rules-design.md).

A rule decides WHEN a command runs and with WHAT parameters. This module is
the pure part: definitions, validation, and a state machine that takes one
sample (the signal, its age, the read-back and its age) and returns one
Decision. No clock, no I/O, no store — the runtime wires those, so every
branch here is unit-testable with plain numbers.

Two guarantees live here, not in the caller:

* **fail closed** — a stale signal never relaxes a want; a stale read-back
  asks for a sweep instead of a blind write;
* **closed loop** — a want is re-commanded only when the read-back drifts
  from it for longer than ``reassert_s``, never on a heartbeat.
"""
from __future__ import annotations

import math
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from multibus import clock

MODES = ('shadow', 'armed')
KINDS = ('steps', 'condition', 'trim', 'schedule')
DAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
# several rules on one target (phase 3): the layer a rule speaks from, and its
# default priority — the higher wins a channel; `priority` may set its own
LAYERS = {'safety': 90, 'manual': 70, 'schedule': 50, 'optimize': 30}
MANUAL = LAYERS['manual']           # a forced rule speaks at least as manual
MAX_FORCE_S = 86400.0             # a forced value lasts at most a day
ON_STALE = ('hold', 'safe')      # or a number: the value to ask for while the signal is stale (fail closed)
ON_DISABLE = ('safe', 'hold')
_ID_RE = re.compile(r'^[a-z][a-z0-9_-]{0,47}$')
NORMAL = 'normal'
STALE = 'stale'


# ── definitions ──────────────────────────────────────────────────────────────

@dataclass
class Step:
    at: float
    value: float
    label: str = ''
    fast: bool = False                      # no debounce: the emergency path


@dataclass
class Timing:
    every_s: float = 2.0
    debounce: int = 3
    min_interval_s: float = 30.0
    reassert_s: float = 120.0
    # a change must also be wanted for this long (seconds), not only for
    # `debounce` samples — "over 55 °C for 5 minutes"
    for_s: float = 0.0
    # on/off timers: "on" is any want other than the rule's off value
    min_on_s: float = 0.0          # once started, run at least this long
    min_off_s: float = 0.0         # once stopped, stay off at least this long
    max_on_s: float = 0.0          # after this long on, stop and rest min_off_s
    max_starts_per_h: int = 0      # starts in any 60 minutes, at most

    TIMERS = ('for_s', 'min_on_s', 'min_off_s', 'max_on_s', 'max_starts_per_h')

    def to_dict(self) -> Dict:
        d = {'every_s': self.every_s, 'debounce': self.debounce,
             'min_interval_s': self.min_interval_s, 'reassert_s': self.reassert_s}
        d.update({k: getattr(self, k) for k in self.TIMERS if getattr(self, k)})
        return d

    def onoff(self) -> bool:
        return bool(self.min_on_s or self.min_off_s or self.max_on_s or self.max_starts_per_h)


@dataclass
class RuleDef:
    id: str
    label: str = ''
    enabled: bool = True
    mode: str = 'shadow'
    target: Dict[str, Any] = field(default_factory=dict)      # {device|endpoint+group, command, param}
    kind: str = 'steps'
    signal: str = ''                                          # steps: numeric expression
    # what the signal's number means, shown next to it ("W under the cap"):
    # a sum such as `20000 + meter.power` reads as nothing without it
    signal_unit: str = ''
    signal_valid: Dict[str, float] = field(default_factory=dict)
    stale_after_s: float = 60.0
    steps: List[Step] = field(default_factory=list)
    release_below: Optional[float] = None
    # params when no step matches; None = no opinion (the rule lets a lower
    # layer drive the channel while the signal is under the steps)
    normal: Optional[Dict[str, Any]] = field(default_factory=dict)
    params: Dict[str, Any] = field(default_factory=dict)      # fixed params sent with every want
    when: str = ''                                            # condition: boolean expression
    # condition with hysteresis: once `when` holds, the rule stays on `then`
    # until `release` holds (on under 25 °C, off only over 35 °C) — without
    # it, `else` follows the moment `when` turns false
    release: str = ''
    then: Optional[Dict[str, Any]] = None                     # {params: {...}} or None
    otherwise: Optional[Dict[str, Any]] = None                # `else` in YAML
    # trim: nudge the value up while the signal is over `above`, down while it
    # is under `below`, by `step` (or gain × the distance past the threshold,
    # at most `step`), within [min, max]; `off_value` (YAML `off_value`, never a bare
    # `off`, which YAML reads as false) is what it asks for under min
    above: Optional[float] = None
    below: Optional[float] = None
    step: Optional[float] = None
    gain: Optional[float] = None
    vmin: Optional[float] = None                              # `min` in YAML
    vmax: Optional[float] = None                              # `max` in YAML
    start: Optional[float] = None
    off: Optional[float] = None
    # schedule: [{days: [1..7], at: "HH:MM", value | params, label}] — the last
    # entry already reached (up to a week back) says what to ask for
    schedule: List[Dict[str, Any]] = field(default_factory=list)
    layer: str = ''                                           # safety | manual | schedule | optimize
    priority: Optional[int] = None                            # overrides the layer's

    def prio(self) -> int:
        """The priority it speaks with on its channel."""
        if self.priority is not None:
            return int(self.priority)
        return LAYERS.get(self.layer or 'schedule', LAYERS['schedule'])
    timing: Timing = field(default_factory=Timing)
    on_stale: Any = 'hold'                  # 'hold' | 'safe' | a number (param value while stale)
    on_disable: str = 'safe'
    # bound from the command the target offers (the runtime fills these in)
    param: str = 'value'
    safe_params: Dict[str, Any] = field(default_factory=dict)
    tolerance: float = 1.0

    def to_dict(self) -> Dict:
        d = {'id': self.id, 'label': self.label or self.id, 'enabled': self.enabled, 'mode': self.mode,
             'target': {k: v for k, v in self.target.items() if not k.startswith('_')},   # the binding stays private
             'kind': self.kind, 'stale_after_s': self.stale_after_s,
             'params': dict(self.params), 'timing': self.timing.to_dict(),
             'on_stale': self.on_stale, 'on_disable': self.on_disable}
        if self.signal_unit and self.kind in ('steps', 'trim'):
            d['signal_unit'] = self.signal_unit
        if self.kind == 'steps':
            d.update({'signal': self.signal, 'signal_valid': dict(self.signal_valid),
                      'steps': [{'at': s.at, 'value': s.value, 'label': s.label, 'fast': s.fast} for s in self.steps],
                      'release_below': self.release_below,
                      'normal': dict(self.normal) if self.normal is not None else None})
        elif self.kind == 'schedule':
            d['schedule'] = [{k: v for k, v in e.items() if not k.startswith('_')} for e in self.schedule]
            if self.when:
                d['when'] = self.when
        elif self.kind == 'trim':
            d.update({'signal': self.signal, 'signal_valid': dict(self.signal_valid),
                      'above': self.above, 'below': self.below, 'step': self.step,
                      'min': self.vmin, 'max': self.vmax})
            for k, v in (('gain', self.gain), ('start', self.start)):
                if v is not None:
                    d[k] = v
        else:
            d.update({'when': self.when, 'then': self.then, 'else': self.otherwise})
            if self.release:
                d['release'] = self.release
        if self.off is not None:
            d['off_value'] = self.off
        if self.layer:
            d['layer'] = self.layer
        if self.priority is not None:
            d['priority'] = self.priority
        return d

    def off_value(self) -> Optional[float]:
        """The command parameter's value that means "off" — for the on/off
        timers and the counters: `off_value` when given, else what the rule asks
        for at rest (a condition's `else`, a steps rule's `normal`)."""
        if self.off is not None:
            return self.off
        rest = (self.otherwise or {}).get('params') if self.kind == 'condition' and isinstance(self.otherwise, dict) \
            else ((self.normal or {}) if self.kind == 'steps' else None)
        return _f((rest or {}).get(self.param))


def _f(x, default=None):
    """A finite float, or ``default`` — NaN and ±inf are not setpoints,
    thresholds or clamps (F-30, 3.83.0)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _num(x: Any) -> Optional[float]:
    """A read-back as a number, or None — a state register decoded to text
    ("On") that the runtime could not map back to its code is not a number,
    and must not be compared as one."""
    if isinstance(x, bool):
        return float(x)
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _on_stale(v: Any) -> Any:
    """'hold' | 'safe' | a float (the value to ask for while the signal is stale)."""
    if isinstance(v, bool) or v is None:
        return 'hold'
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(float(v)) else str(v)   # 'inf'/'nan' fail validation
    t = str(v).strip()
    if re.fullmatch(r'-?\d+(\.\d+)?', t):
        return float(t)
    return t or 'hold'


def parse_rule_def(raw: Dict) -> RuleDef:
    """A definition from YAML/JSON. Lenient on types (validate_rule_def is the
    judge); missing fields take their defaults."""
    raw = raw or {}
    t = raw.get('timing') or {}
    timing = Timing(every_s=_f(t.get('every_s'), 2.0), debounce=int(_f(t.get('debounce'), 3)),
                    min_interval_s=_f(t.get('min_interval_s'), 30.0), reassert_s=_f(t.get('reassert_s'), 120.0),
                    for_s=_f(t.get('for_s'), 0.0), min_on_s=_f(t.get('min_on_s'), 0.0),
                    min_off_s=_f(t.get('min_off_s'), 0.0), max_on_s=_f(t.get('max_on_s'), 0.0),
                    max_starts_per_h=int(_f(t.get('max_starts_per_h'), 0)))
    steps = []
    for s in raw.get('steps') or []:
        s = s or {}
        steps.append(Step(at=_f(s.get('at'), 0.0), value=_f(s.get('value'), 0.0),
                          label=str(s.get('label', '') or ''), fast=bool(s.get('fast', False))))
    steps.sort(key=lambda s: s.at)
    for i, s in enumerate(steps):
        if not s.label:
            s.label = f'step {i + 1}'
    return RuleDef(
        id=str(raw.get('id', '') or ''), label=str(raw.get('label', '') or ''),
        enabled=bool(raw.get('enabled', True)), mode=str(raw.get('mode', 'shadow') or 'shadow'),
        target=dict(raw.get('target') or {}), kind=str(raw.get('kind', 'steps') or 'steps'),
        signal=str(raw.get('signal', '') or ''), signal_valid=dict(raw.get('signal_valid') or {}),
        signal_unit=str(raw.get('signal_unit', '') or '').strip(),
        stale_after_s=_f(raw.get('stale_after_s'), 60.0), steps=steps,
        release_below=_f(raw.get('release_below')),
        normal=None if ('normal' in raw and raw.get('normal') is None) else dict(raw.get('normal') or {}),
        layer=str(raw.get('layer', '') or ''),
        priority=int(_f(raw.get('priority'))) if _f(raw.get('priority')) is not None else None,
        params=dict(raw.get('params') or {}), when=str(raw.get('when', '') or ''),
        release=str(raw.get('release', '') or ''),
        then=raw.get('then'), otherwise=raw.get('else'), timing=timing,
        above=_f(raw.get('above')), below=_f(raw.get('below')), step=_f(raw.get('step')),
        gain=_f(raw.get('gain')), vmin=_f(raw.get('min')), vmax=_f(raw.get('max')),
        start=_f(raw.get('start')), off=_f(raw.get('off_value')),
        schedule=_schedule_entries(raw.get('schedule')),
        on_stale=_on_stale(raw.get('on_stale', 'hold')),
        on_disable=str(raw.get('on_disable', 'safe') or 'safe'),
        param=str((raw.get('target') or {}).get('param', 'value') or 'value'),
    )


def validate_rule_def(raw: Dict, *, validate_expr=None) -> List[str]:
    """Structural errors, in words. ``validate_expr(expr) -> error or None``
    lets the caller plug the expression grammar in (kept out of here so the
    engine has no dependency on the store)."""
    errors: List[str] = []
    if not isinstance(raw, dict):
        return ['a rule must be an object']
    if any(isinstance(k, bool) for k in raw) or any(isinstance(k, bool) for k in (raw.get('timing') or {})
                                                    if isinstance(raw.get('timing'), dict)):
        # YAML 1.1 reads a bare on/off/yes/no as a boolean: `off: 0` arrives
        # as {False: 0} and the field would be silently lost
        errors.append("a key reads as true/false: YAML turns a bare `off`, `on`, `yes` or `no` into a "
                      "boolean — write `off_value` (or quote the key)")
    rid = str(raw.get('id', '') or '')
    if not _ID_RE.match(rid):
        errors.append("id: use a-z 0-9 - _ (1-48 chars, starts with a letter)")
    if raw.get('mode', 'shadow') not in MODES:
        errors.append(f"mode: one of {', '.join(MODES)}")
    kind = raw.get('kind', 'steps')
    if kind not in KINDS:
        errors.append(f"kind: one of {', '.join(KINDS)}")
    tgt = raw.get('target') or {}
    if not isinstance(tgt, dict) or not tgt.get('command'):
        errors.append("target.command: required")
    if isinstance(tgt, dict) and not (tgt.get('device') or (tgt.get('endpoint') and tgt.get('group'))):
        errors.append("target: a device, or an endpoint and a group")
    _os = raw.get('on_stale', 'hold')
    if not (_os in ON_STALE
            or (isinstance(_os, (int, float)) and not isinstance(_os, bool) and math.isfinite(_os))
            or (isinstance(_os, str) and re.fullmatch(r'-?\d+(\.\d+)?', _os.strip() or 'x'))):
        errors.append(f"on_stale: one of {', '.join(ON_STALE)}, or a number (the value to ask for while stale)")
    if raw.get('on_disable', 'safe') not in ON_DISABLE:
        errors.append(f"on_disable: one of {', '.join(ON_DISABLE)}")
    su = raw.get('signal_unit')
    if su is not None and (not isinstance(su, str) or len(su) > 40):
        errors.append("signal_unit: a short text, at most 40 characters (\"W under the cap\")")
    t = raw.get('timing') or {}
    if not isinstance(t, dict):
        errors.append("timing: must be an object")
        t = {}
    if _f(t.get('every_s', 2)) is None or not (0.5 <= _f(t.get('every_s', 2)) <= 3600):
        errors.append("timing.every_s: 0.5..3600 seconds")
    if _f(t.get('debounce', 3)) is None or not (1 <= _f(t.get('debounce', 3)) <= 100):
        errors.append("timing.debounce: 1..100 samples")
    if _f(t.get('min_interval_s', 30)) is None or not (0 <= _f(t.get('min_interval_s', 30)) <= 86400):
        errors.append("timing.min_interval_s: 0..86400 seconds")
    if _f(t.get('reassert_s', 120)) is None or not (0 <= _f(t.get('reassert_s', 120)) <= 86400):
        errors.append("timing.reassert_s: 0..86400 seconds (0 = never)")
    if _f(raw.get('stale_after_s', 60)) is None or not (1 <= _f(raw.get('stale_after_s', 60)) <= 86400):
        errors.append("stale_after_s: 1..86400 seconds")
    for k in ('for_s', 'min_on_s', 'min_off_s', 'max_on_s'):
        if t.get(k) is not None and (_f(t.get(k)) is None or not (0 <= _f(t.get(k)) <= 7 * 86400)):
            errors.append(f"timing.{k}: 0..604800 seconds (0 = off)")
    if t.get('max_starts_per_h') is not None and (_f(t.get('max_starts_per_h')) is None
                                                   or not (0 <= _f(t.get('max_starts_per_h')) <= 3600)):
        errors.append("timing.max_starts_per_h: 0..3600 (0 = no limit)")
    if raw.get('layer') not in (None, '') and raw.get('layer') not in LAYERS:
        errors.append(f"layer: one of {', '.join(LAYERS)}")
    if raw.get('priority') is not None and (_f(raw.get('priority')) is None or isinstance(raw.get('priority'), bool)
                                            or not 0 <= _f(raw.get('priority')) <= 100):
        errors.append("priority: 0..100 (the higher wins the target)")
    if raw.get('off_value') is not None and _f(raw.get('off_value')) is None:
        errors.append("off_value: a number (the value that means off)")
    if any(_f(t.get(k)) for k in ('min_on_s', 'min_off_s', 'max_on_s', 'max_starts_per_h')):
        probe = parse_rule_def(raw) if isinstance(raw.get('timing'), dict) else None
        if probe is not None and probe.off_value() is None:
            errors.append("off_value: the on/off timers need the value that means off — set `off_value` "
                          "(a condition takes its `else`, a steps rule its `normal`)")
        if _f(t.get('max_on_s')) and not _f(t.get('min_off_s')):
            errors.append("timing.min_off_s: max_on_s rests the target for min_off_s — give it a rest (> 0)")
        if _f(t.get('max_on_s')) and _f(t.get('min_on_s')) and _f(t.get('max_on_s')) <= _f(t.get('min_on_s')):
            errors.append("timing.max_on_s: must be over min_on_s")
    if kind in ('steps', 'trim'):
        if not str(raw.get('signal', '') or '').strip():
            errors.append(f"signal: required for a {kind} rule")
        elif validate_expr:
            e = validate_expr(raw['signal'])
            if e:
                errors.append(f"signal: {e}")
        sv = raw.get('signal_valid')
        if sv is not None and (not isinstance(sv, dict) or any(_f(sv.get(k)) is None for k in sv)
                               or any(k not in ('min', 'max', 'max_step') for k in sv)):
            errors.append("signal_valid: {min, max, max_step} numbers")
        elif sv and _f(sv.get('max_step')) is not None and _f(sv.get('max_step')) <= 0:
            errors.append("signal_valid.max_step: the largest plausible change between two samples, > 0")
    if kind == 'trim':
        errors.extend(_trim_errors(raw))
    elif kind == 'schedule':
        errors.extend(_schedule_errors(raw, validate_expr))
    elif kind == 'steps':
        steps = raw.get('steps')
        if not isinstance(steps, list) or not steps:
            errors.append("steps: at least one step")
        else:
            ats = []
            for i, s in enumerate(steps):
                if not isinstance(s, dict) or _f(s.get('at')) is None or _f(s.get('value')) is None:
                    errors.append(f"steps[{i}]: needs a numeric `at` and `value`")
                    continue
                ats.append(_f(s['at']))
            if len(set(ats)) != len(ats):
                errors.append("steps: two steps share the same `at`")
            rb = _f(raw.get('release_below'))
            if rb is None:
                errors.append("release_below: required (the signal must fall under it to return to normal)")
            elif ats and rb > min(ats):
                errors.append(f"release_below: must be at or under the first step ({min(ats):g})")
        if not ('normal' in raw and raw.get('normal') is None) and (not isinstance(raw.get('normal'), dict)
                                                                      or not raw.get('normal')):
            errors.append("normal: the parameters asked for when no step matches (e.g. {value: 100}), "
                          "or null for no opinion (a lower layer drives the target meanwhile)")
    else:
        if not str(raw.get('when', '') or '').strip():
            errors.append("when: required for a condition rule")
        elif validate_expr:
            e = validate_expr(raw['when'])
            if e:
                errors.append(f"when: {e}")
        if str(raw.get('release', '') or '').strip() and validate_expr:
            e = validate_expr(raw['release'])
            if e:
                errors.append(f"release: {e}")
        for k in ('then', 'else'):
            v = raw.get(k)
            if v is not None and (not isinstance(v, dict) or not isinstance(v.get('params', {}), dict)):
                errors.append(f"{k}: null or {{params: {{...}}}}")
        if raw.get('then') is None and raw.get('else') is None:
            errors.append("then/else: at least one outcome")
    if raw.get('params') is not None and not isinstance(raw.get('params'), dict):
        errors.append("params: an object")
    return errors


def _days_of(v) -> Optional[List[int]]:
    """[1..7] from a list of numbers or names, "1-5", "Mon-Fri" or "all"; None if unreadable."""
    if v is None or v == 'all':
        return list(range(1, 8))
    names = {d.lower(): i + 1 for i, d in enumerate(DAYS)}

    def one(x):
        if isinstance(x, bool):
            return None
        if isinstance(x, int):
            return x if 1 <= x <= 7 else None
        return names.get(str(x).strip().lower()[:3]) or (int(x) if str(x).strip().isdigit() and 1 <= int(x) <= 7 else None)
    if isinstance(v, str):
        out = []
        for part in v.split(','):
            if '-' in part:
                a, b = (one(x) for x in part.split('-', 1))
                if a is None or b is None or b < a:
                    return None
                out.extend(range(a, b + 1))
            elif part.strip():
                d = one(part)
                if d is None:
                    return None
                out.append(d)
        return sorted(set(out)) or None
    if isinstance(v, list):
        out = [one(x) for x in v]
        return None if (not out or any(d is None for d in out)) else sorted(set(out))
    return None


def _schedule_entries(raw) -> List[Dict[str, Any]]:
    out = []
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict):
            continue
        days = _days_of(e.get('days'))
        try:
            hours = clock.hours_of(e.get('at'))
        except (ValueError, TypeError, AttributeError):
            continue
        if days is None or hours >= 24:
            continue
        ent = {'days': days, 'at': str(e.get('at')).strip()}
        if isinstance(e.get('params'), dict):
            ent['params'] = dict(e['params'])
        else:
            ent['value'] = _f(e.get('value'))
        if e.get('label'):
            ent['label'] = str(e['label'])
        ent['_h'] = hours
        out.append(ent)
    return out


def days_text(days: List[int]) -> str:
    """[1, 2, 3, 4, 5] → "Mon-Fri"; all seven → "every day"."""
    if days == list(range(1, 8)):
        return 'every day'
    runs, start = [], None
    for i, d in enumerate(days):
        if start is None:
            start = d
        if i + 1 == len(days) or days[i + 1] != d + 1:
            runs.append(DAYS[start - 1] if start == d else f'{DAYS[start - 1]}-{DAYS[d - 1]}')
            start = None
    return ','.join(runs)


def current_entry(entries: List[Dict[str, Any]], now: datetime) -> Optional[Dict[str, Any]]:
    """The schedule entry in force at ``now`` (local, aware): the latest one
    whose day and time have been reached, looking back up to a week."""
    best, best_at = None, None
    for back in range(8):
        day = now - timedelta(days=back)
        for e in entries:
            if day.isoweekday() not in e['days']:
                continue
            h, m = divmod(int(round(e['_h'] * 60)), 60)
            at = day.replace(hour=h, minute=m, second=0, microsecond=0)
            if at <= now and (best_at is None or at > best_at):
                best, best_at = e, at
        if best is not None:
            break                                    # any entry further back is older
    return best


def _schedule_errors(raw: Dict, validate_expr) -> List[str]:
    errors: List[str] = []
    entries = raw.get('schedule')
    if not isinstance(entries, list) or not entries:
        return ["schedule: at least one entry {days, at, value}"]
    if len(entries) > 100:
        errors.append("schedule: at most 100 entries")
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            errors.append(f"schedule[{i}]: an object {{days, at, value}}")
            continue
        if _days_of(e.get('days')) is None:
            errors.append(f"schedule[{i}].days: 1-7 (1 = Monday), names (Mon-Fri), a list, or all")
        try:
            if clock.hours_of(e.get('at')) >= 24:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            errors.append(f"schedule[{i}].at: a time of day \"HH:MM\", 00:00-23:59")
        if isinstance(e.get('params'), dict):
            if not e['params']:
                errors.append(f"schedule[{i}].params: at least one parameter")
        elif _f(e.get('value')) is None and not ('value' in e and e.get('value') is None):
            errors.append(f"schedule[{i}]: a numeric `value` (or `params`, or `value: null` for no opinion)")
    if str(raw.get('when', '') or '').strip():
        if validate_expr:
            e = validate_expr(raw['when'])
            if e:
                errors.append(f"when: {e}")
        if _f(raw.get('off_value')) is None:
            errors.append("off_value: a schedule with `when` asks for it while `when` is false")
    return errors


def _trim_errors(raw: Dict) -> List[str]:
    """A trim rule's own fields: a band, a step, the limits."""
    errors: List[str] = []
    num = {k: _f(raw.get(k)) for k in ('above', 'below', 'step', 'gain', 'min', 'max', 'start', 'off_value')}
    for k in ('above', 'below', 'step', 'min', 'max'):
        if num[k] is None:
            errors.append(f"{k}: a number, required for a trim rule")
    for k in ('gain', 'start', 'off_value'):
        if raw.get(k) is not None and num[k] is None:
            errors.append(f"{k}: a number")
    if num['above'] is not None and num['below'] is not None and num['below'] >= num['above']:
        errors.append("below: must be under above (between the two the rule holds — the dead band)")
    if num['step'] is not None and num['step'] <= 0:
        errors.append("step: the largest change in one adjustment, > 0")
    if num['gain'] is not None and num['gain'] <= 0:
        errors.append("gain: > 0 (value units per signal unit past the threshold)")
    if num['min'] is not None and num['max'] is not None:
        if num['min'] >= num['max']:
            errors.append("min: must be under max")
        if num['start'] is not None and not (num['min'] <= num['start'] <= num['max']):
            errors.append("start: within min..max")
        if num['off_value'] is not None and num['off_value'] >= num['min']:
            errors.append("off_value: under min (what the rule asks for below the lowest working value, e.g. 0 = stop)")
    return errors


# ── the state machine ────────────────────────────────────────────────────────

@dataclass
class Decision:
    """One evaluation's outcome. ``action`` is what the runtime does:
    run / reassert (a command), shadow (would have), hold, stale, idle,
    ignored, sweep (ask for a read-back)."""
    ts: float
    action: str
    reason: str
    state: str
    signal: Any = None
    want: Optional[Dict[str, Any]] = None
    actual: Any = None
    changed: bool = False                    # state or want changed this tick

    def to_dict(self) -> Dict:
        return {'ts': round(self.ts, 3), 'action': self.action, 'reason': self.reason, 'state': self.state,
                'signal': self.signal, 'want': self.want, 'actual': self.actual, 'changed': self.changed}


class RuleState:
    """The live state of one rule on one target unit. Feed it samples; it
    answers Decisions. Its clock is the ``now`` you pass."""

    def __init__(self, rule: RuleDef):
        self.rule = rule
        self.state: str = NORMAL
        self.want: Optional[Dict[str, Any]] = None      # what the rule asks for
        self.since: Optional[float] = None
        self.commanded: Optional[Dict[str, Any]] = None  # the last want sent (or would-have in shadow)
        # the device answered `commanded` (or read it back): a release may
        # trust it — a sent want can fail, and a shadow one never left
        self.confirmed: bool = False
        self.last_cmd_ts: Optional[float] = None
        self.pending: bool = False                       # a want not yet sent (rate limit)
        self._deb_target: Optional[tuple] = None
        self._deb_count: int = 0
        self.last_valid_ts: Optional[float] = None
        self.last_signal: Any = None
        self.ignored: int = 0
        self.stale_since: Optional[float] = None
        self.clamp: Optional[Dict[str, float]] = None    # {max, expires_at}
        self.paused_until: Optional[float] = None        # override by another face
        self.failures: int = 0
        self.drift_since: Optional[float] = None
        self._pre_stale: Optional[tuple] = None          # (state, want) to return to
        self._base: Optional[Dict[str, Any]] = None      # the last desired base params (pre clamp)
        # plausibility guard (signal_valid.max_step): the last accepted sample,
        # the sample held as suspect, and the identity of the sample last seen
        self._accepted: Optional[float] = None
        self._suspect: Optional[float] = None
        self._suspect_reason: str = ''
        self._sample_ts: Optional[float] = None
        self._deb_sample_ts: Optional[float] = None      # the sample the debounce last counted
        self.guarded: int = 0                            # samples held by the guard
        # display/absolute-time clock for this evaluation (see evaluate():
        # `now` measures intervals, `_wall` stamps Decisions and compares the
        # wall-persisted absolutes paused_until / clamp.expires_at)
        self._wall: float = 0.0
        # the current want came from a `fast` step or an on_stale safe value —
        # the emergency path is exempt from the min_interval rate limit
        self._urgent: bool = False
        # trim: the value it is at, the direction being debounced and its count
        self._trim: Optional[float] = None
        self._trim_dir: int = 0
        self._trim_count: int = 0
        self._trim_since: Optional[float] = None
        # a group's trim moves as one: every unit but the first follows the
        # first one's value (the runtime sets this each tick; never mirrored)
        self.leader: Optional['RuleState'] = None
        self._deb_since: Optional[float] = None          # when the debounced want was first seen (for_s)
        # on/off: when the target was last started / stopped, the starts of the
        # last hour, a forced rest (max_on_s) and the counters
        self.on_since: Optional[float] = None
        self.off_since: Optional[float] = None
        self._starts: deque = deque()
        self._rest_until: Optional[float] = None
        self.on_today_s: float = 0.0
        self.on_total_s: float = 0.0
        self.starts_today: int = 0
        self.day: Optional[str] = None
        self._acc_ts: Optional[float] = None
        # a forced want for a while ("run for an hour"): {params, until (wall)}
        self.forced: Optional[Dict[str, Any]] = None
        # the channel's referee (rules phase 3), set by the runtime:
        # gate(want, urgent) -> None | ('yield', why, winner) | ('hold', why, '')
        self.gate: Optional[Any] = None
        self.yielding_to: Optional[str] = None

    # ── to a partner gateway (high availability) ──
    # Instants on the monotonic clock (this process's own) travel as ages,
    # seconds before the export; wall-clock and plain fields as they are. The
    # sample-identity stamps stay behind: the partner treats its next sample as
    # new rather than compare two processes' clocks.
    MIRROR_MONO = ('last_cmd_ts', 'last_valid_ts', 'stale_since', 'drift_since', '_deb_since', '_trim_since',
                   'on_since', 'off_since', '_rest_until', '_acc_ts')
    MIRROR_PLAIN = ('state', 'want', 'since', 'commanded', 'pending', '_deb_target', '_deb_count', 'last_signal',
                    'ignored', 'clamp', 'paused_until', 'failures', '_pre_stale', '_base', '_accepted', '_suspect',
                    '_suspect_reason', 'guarded', '_urgent', '_trim', '_trim_dir', '_trim_count', 'on_today_s',
                    'on_total_s', 'starts_today', 'day', 'forced', 'yielding_to')
    _MIRROR_TUPLES = ('_deb_target', '_pre_stale')

    def export_mirror(self, now: float) -> Dict[str, Any]:
        d = {k: getattr(self, k) for k in self.MIRROR_PLAIN}
        for k in self._MIRROR_TUPLES:
            if d[k] is not None:
                d[k] = list(d[k])
        d['ages'] = {k: (None if getattr(self, k) is None else now - getattr(self, k)) for k in self.MIRROR_MONO}
        d['starts'] = [now - t for t in self._starts]
        return d

    def import_mirror(self, d: Dict[str, Any], now: float) -> None:
        for k in self.MIRROR_PLAIN:
            if k in d:
                v = d[k]
                setattr(self, k, tuple(v) if k in self._MIRROR_TUPLES and isinstance(v, list) else v)
        for k, age in (d.get('ages') or {}).items():
            if k in self.MIRROR_MONO:
                setattr(self, k, None if age is None else now - float(age))
        self._starts = deque(now - float(a) for a in (d.get('starts') or []))
        self._sample_ts = self._deb_sample_ts = None

    # ── public knobs ──
    def set_clamp(self, max_value: float, expires_at: Optional[float]) -> None:
        self.clamp = {'max': float(max_value), 'expires_at': expires_at}

    def clear_clamp(self) -> None:
        self.clamp = None

    def note_result(self, ok: bool) -> None:
        self.failures = 0 if ok else self.failures + 1

    def snapshot(self) -> Dict:
        return {'state': self.state, 'want': self.want, 'since': self.since, 'commanded': self.commanded,
                'last_cmd_ts': self.last_cmd_ts, 'pending': self.pending, 'signal': self.last_signal,
                'clamp': dict(self.clamp) if self.clamp else None, 'paused_until': self.paused_until,
                'failures': self.failures, 'ignored': self.ignored, 'guarded': self.guarded,
                'stale_since': self.stale_since, 'forced': dict(self.forced) if self.forced else None,
                'yielding_to': self.yielding_to,
                **self.counters()}

    # ── on / off and the counters ──
    def _is_on(self, want: Optional[Dict[str, Any]]) -> Optional[bool]:
        """True / False for a want with a number on the rule's parameter, None
        when the rule has no off value or the want says nothing."""
        off = self.rule.off_value()
        v = _num((want or {}).get(self.rule.param)) if want else None
        if off is None or v is None:
            return None
        return abs(v - off) > 1e-9

    def starts_last_h(self, now: float) -> int:
        while self._starts and now - self._starts[0] >= 3600:
            self._starts.popleft()
        return len(self._starts)

    def counters(self) -> Dict[str, Any]:
        return {'run_today_min': int(self.on_today_s // 60), 'run_total_h': round(self.on_total_s / 3600, 2),
                'starts_today': self.starts_today}

    def restore_counters(self, saved: Dict[str, Any], today: Optional[str]) -> None:
        self.on_total_s = float(saved.get('on_total_s') or 0.0)
        if saved.get('day') and saved.get('day') == today:
            self.on_today_s = float(saved.get('on_today_s') or 0.0)
            self.starts_today = int(saved.get('starts_today') or 0)
        self.day = today

    def saved_counters(self) -> Dict[str, Any]:
        if not (self.on_total_s or self.on_today_s or self.starts_today):
            return {}
        return {'on_total_s': round(self.on_total_s, 1), 'on_today_s': round(self.on_today_s, 1),
                'starts_today': self.starts_today, 'day': self.day}

    def _accumulate(self, now: float) -> None:
        """Add the time on since the last evaluation (at most an hour: a
        stopped clock is not run time) and reset "today" at local midnight."""
        day = clock.day_key(self._wall) if self._wall else None
        if day and day != self.day:
            if self.day is not None:
                self.on_today_s, self.starts_today = 0.0, 0
            self.day = day
        if self._acc_ts is not None and self._is_on(self.commanded):
            dt = max(0.0, min(now - self._acc_ts, 3600.0))
            self.on_today_s += dt
            self.on_total_s += dt
        self._acc_ts = now

    def _note_sent(self, now: float, want: Optional[Dict[str, Any]]) -> None:
        was, on = self._is_on(self.commanded), self._is_on(want)
        if on and was is not True:
            self.on_since, self.off_since = now, None
            self._starts.append(now)
            self.starts_today += 1
        elif on is False and was is not False:
            self.off_since, self.on_since = now, None

    def _onoff_hold(self, now: float) -> Optional[str]:
        """Why a pending start or stop must wait for a timer, or None."""
        t = self.rule.timing
        if not t.onoff():
            return None
        was, on = self._is_on(self.commanded), self._is_on(self.want)
        if on is None or was is None or on == was:
            return None
        if not on and t.min_on_s and self.on_since is not None and now - self.on_since < t.min_on_s:
            return f'minimum run time — {int(t.min_on_s - (now - self.on_since))} s to go before stopping'
        if on:
            if self._rest_until is not None and now < self._rest_until:
                return f'resting after the maximum run time — {int(self._rest_until - now)} s to go'
            if t.min_off_s and self.off_since is not None and now - self.off_since < t.min_off_s:
                return f'minimum stop time — {int(t.min_off_s - (now - self.off_since))} s to go before starting'
            if t.max_starts_per_h and self.starts_last_h(now) >= t.max_starts_per_h:
                left = int(3600 - (now - self._starts[0]))
                return f'{len(self._starts)} starts in the last hour (max {t.max_starts_per_h}) — {left} s to go'
        return None

    def _rest(self, now: float, signal, actual, kw) -> Optional[Decision]:
        """max_on_s: after that long on, stop and rest min_off_s whatever the
        signal says; while resting, nothing starts it."""
        r, t = self.rule, self.rule.timing
        if self._rest_until is not None and now >= self._rest_until:
            self._rest_until = None
        if self._rest_until is None and t.max_on_s and self.on_since is not None \
                and self._is_on(self.commanded) and now - self.on_since >= t.max_on_s:
            self._rest_until = now + t.min_off_s
            self._trim = None                                    # a trim starts again from the read-back
            self._deb_target, self._deb_count, self._trim_count = None, 0, 0
            self._transition('resting', self._want_for({r.param: r.off_value()}), now)
            self._urgent = True                                  # neither the rate limit nor min_on holds a rest
            return self._act(now, None, f'on for {int(t.max_on_s)} s — resting {int(t.min_off_s)} s',
                             signal, actual, changed=True, **kw)
        if self._rest_until is not None:
            if self.pending:
                return self._act(now, None, '', signal, actual, **kw)
            return Decision(self._wall or now, 'hold', f'resting — {int(self._rest_until - now)} s to go',
                            self.state, signal, self.want, actual)
        return None

    # ── helpers ──
    def _want_for(self, base: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if base is None:
            return None
        w = dict(self.rule.params)
        w.update(base)
        return w

    def _desired(self, v: float, actual: Any = None) -> tuple:
        """(state, base params) the signal asks for, before clamp and debounce."""
        r = self.rule
        step = None
        for s in r.steps:
            if v >= s.at:
                step = s
        if step is not None:
            return step.label, {r.param: step.value}, step.fast
        if r.release_below is None or v < r.release_below:
            return NORMAL, (dict(r.normal) if r.normal is not None else None), False
        # the dead band between release and the first step: keep what we have
        if self._base is None and self.want is None:
            # nothing decided yet (a gateway restart) — what we "have" is what
            # the device holds: a limit that matches a step is that step,
            # resumed until the signal falls under release_below. Starting
            # from normal dropped a curtailment at 249.6 V (2026-10-20). No
            # read-back yet: wait for a threshold rather than guess.
            held = _num(actual)
            if held is None:
                return NORMAL, None, False
            for s in reversed(r.steps):
                if abs(held - s.value) <= r.tolerance:
                    return s.label, {r.param: s.value}, False
            return NORMAL, (dict(r.normal) if r.normal is not None else None), False
        if self._base is None or self.state == NORMAL:
            return NORMAL, (dict(r.normal) if r.normal is not None else None), False
        return self.state, dict(self._base), False

    def _truth(self, signal: Any) -> Optional[bool]:
        """A condition's answer. With ``release`` the signal is
        ``(when, release)`` and the answer is latched: true once `when`
        holds, and it stays true until `release` holds — the dead band
        between the two keeps what we have.

        None = nothing to decide: a rule that does not know its latch yet (a
        fresh start, a gateway restart) and finds the signal in the dead band
        waits for one of the two thresholds rather than guess — switching a
        running heat pump off at 30 °C between 25 and 35 would be a guess.
        When and release both true (overlapping thresholds) also keep what
        we have, instead of flipping at every check."""
        if not self.rule.release or not isinstance(signal, tuple):
            return bool(signal)
        when, released = bool(signal[0]), bool(signal[1])
        latched = {'true': True, 'false': False}.get(self.state)
        if when and released:
            return latched
        if latched is True:
            return not released
        if latched is False:
            return when
        return True if when else (False if released else None)

    def _apply_clamp(self, want: Optional[Dict[str, Any]], now: float, signal: Any) -> Optional[Dict[str, Any]]:
        c = self.clamp
        if c is None or want is None:
            return want
        if c.get('expires_at') is not None and now >= c['expires_at']:
            # never expire INTO a high signal: hold the ceiling until the
            # signal is back under the release threshold
            rb = self.rule.release_below
            if self.rule.kind != 'steps' or signal is None or rb is None or signal < rb:
                self.clamp = None
                return want
        p = self.rule.param
        if isinstance(want.get(p), (int, float)) and want[p] > c['max']:
            want = dict(want, **{p: c['max']})
        return want

    def _transition(self, state: str, want: Optional[Dict[str, Any]], now: float) -> bool:
        changed = state != self.state or want != self.want
        if changed:
            # `since` is display-facing → wall time (evaluate keeps _wall == now
            # when no separate wall clock was given)
            self.state, self.want, self.since = state, want, self._wall or now
            if want != self.commanded:
                self.pending = True
        return changed

    # ── the evaluation ──
    def evaluate(self, now: float, signal: Any, signal_age_s: Optional[float],
                 actual: Any = None, actual_age_s: Optional[float] = None,
                 actual_stale_s: Optional[float] = None,
                 sample_ts: Optional[float] = None,
                 wall: Optional[float] = None) -> Decision:
        """``now`` is the INTERVAL clock (the runtime passes monotonic, so an
        NTP/chrony step can neither freeze the rate-limit and reassert windows
        nor prematurely expire them — the 2026-07-28 +138 s jump class);
        ``wall`` is the display/absolute clock for Decision timestamps and the
        wall-persisted paused_until / clamp expiry. Omitted (unit tests), wall
        follows now — one abstract clock, as before."""
        r = self.rule
        self._wall = wall if wall is not None else now
        self._accumulate(now)
        self.last_signal = signal
        kw = dict(actual_age_s=actual_age_s, actual_stale_s=actual_stale_s)
        if self.forced is not None:
            d = self._forced_eval(now, signal, actual, kw)
            if d is not None:
                return d
        # the identity of this sample: the rule ticks faster than the signal is
        # polled, so several evaluations see the same reading — the guard and
        # the debounce count samples, not ticks
        if sample_ts is None:
            sample_ts = (now - signal_age_s) if signal_age_s is not None else now
        new_sample = self._sample_ts is None or abs(sample_ts - self._sample_ts) > 1e-6
        self._sample_ts = sample_ts
        # 1. validity and staleness
        valid = signal is not None
        if valid and r.kind in ('steps', 'trim'):
            v = _f(signal)
            sv = r.signal_valid
            if v is None or (sv.get('min') is not None and v < float(sv['min'])) \
                    or (sv.get('max') is not None and v > float(sv['max'])):
                valid = False
        # A signal whose age is UNKNOWN (no monotonic stamp on any input —
        # e.g. a calc register fed by unstamped values) must never advance
        # last_valid_ts: "now" would make it fresh forever and freeze an
        # armed rule in its current want. Fail closed, as the module
        # docstring promises — with no stamp it goes stale after
        # stale_after_s like any dead signal (audit 2026-10-01).
        if valid and signal_age_s is not None:
            self.last_valid_ts = now - signal_age_s
        elif not valid:
            self.ignored += 1
        stale = self.last_valid_ts is None or (now - self.last_valid_ts) > r.stale_after_s
        if stale:
            if self.state != STALE:
                self._pre_stale = (self.state, self.want)
                self.stale_since = now
                if r.on_stale == 'hold':
                    want, why = self.want, 'holding the last want'
                elif r.on_stale == 'safe':
                    want, why = self._want_for(dict(r.safe_params)), 'asking for safe'
                else:                                  # a fixed value: fail closed while blind
                    want, why = self._want_for({r.param: float(r.on_stale)}), f'asking for {r.param} {r.on_stale:g}'
                # the operator's clamp caps the stale want too (it used to be
                # bypassed — a numeric on_stale above the ceiling went out
                # uncapped). Cap only: expiry is NOT judged while blind — the
                # never-expire-into-a-high-signal guard can't see the signal.
                if (want is not None and self.clamp is not None
                        and isinstance(want.get(r.param), (int, float))
                        and want[r.param] > self.clamp['max']):
                    want = dict(want, **{r.param: self.clamp['max']})
                self._transition(STALE, want, now)
                # a stale SAFE/fixed want is the fail-closed emergency path —
                # it must not sit out a rate-limit window (audit 2026-10-01)
                self._urgent = r.on_stale != 'hold'
                self._deb_target, self._deb_count = None, 0
                return self._act(now, 'stale', 'signal stale — ' + why,
                                 signal, actual, changed=True, actual_age_s=actual_age_s, actual_stale_s=actual_stale_s)
            return self._act(now, 'stale', f'signal stale for {int(now - (self.stale_since or now))} s', signal, actual)
        if not valid:
            return Decision(self._wall, 'ignored', 'sample outside the valid range', self.state, signal, self.want, actual)
        self.stale_since = None
        rested = self._rest(now, signal, actual, dict(actual_age_s=actual_age_s, actual_stale_s=actual_stale_s))
        if rested is not None:
            return rested
        # 1b. plausibility: a reading that jumps more than signal_valid.max_step
        # from the last accepted one is held until the NEXT sample confirms it
        # (a one-sample artefact never reaches a step — not even a fast one;
        # a real jump costs one poll interval of delay). Seen live 2026-09-18:
        # a single Solar API reading of 273/270/270 V on all three phases with
        # the grid meter at 241 V put the Emergency step on, in shadow.
        if r.kind in ('steps', 'trim'):
            guard = self._guard(_f(signal), new_sample)
            if guard is not None:
                return Decision(self._wall, 'ignored', guard, self.state, signal, self.want, actual)
        # back from stale: restore the pre-stale state BEFORE computing the
        # desired — _desired's dead-band branch keeps `self.state`, and with
        # the restore after it, recovering straight into the dead band yielded
        # a synthetic 'stale' state (severity 99, warn events) ping-ponging
        # with this restore every tick (audit 2026-10-01). The debounce still
        # decides where we really are.
        if self.state == STALE:
            self.state = self._pre_stale[0] if self._pre_stale else NORMAL
            if r.kind == 'trim' and r.on_stale != 'hold':
                self._trim = None        # it asked for something else while blind: start again from the read-back
        if r.kind == 'trim':
            return self._trim_eval(now, _f(signal), new_sample, signal, actual, actual_age_s, actual_stale_s)
        # 2. desired
        if r.kind == 'steps':
            d_state, base, fast = self._desired(_f(signal), actual)
        elif r.kind == 'schedule':
            d_state, base = self._schedule_desired(signal)
            fast = False
        else:
            truth = self._truth(signal)
            if truth is None:                    # dead band, latch unknown: keep what we have
                d_state, base = self.state, (dict(self._base) if self._base is not None else None)
            else:
                outcome = r.then if truth else r.otherwise
                d_state = 'true' if truth else 'false'
                base = dict((outcome or {}).get('params') or {}) if outcome is not None else None
            fast = False
        d_want = self._apply_clamp(self._want_for(base) if base is not None else None, self._wall, _f(signal) if r.kind == 'steps' else None)
        d_base = base
        # 3. debounce
        key = (d_state, tuple(sorted((d_want or {}).items())))
        changed = False
        if d_state == self.state and d_want == self.want:
            self._deb_target, self._deb_count = None, 0
        else:
            if fast or (d_want is None and self.want is None):
                # the emergency path — or a label change with nothing to ask
                self._deb_target, self._deb_count = None, 0
                self._base = d_base
                changed = self._transition(d_state, d_want, now)
                self._urgent = fast
            else:
                if self._deb_target != key:
                    self._deb_target, self._deb_count = key, 1
                    self._deb_sample_ts = sample_ts
                    self._deb_since = now
                elif new_sample or self._deb_sample_ts is None or abs(sample_ts - self._deb_sample_ts) > 1e-6:
                    self._deb_count += 1                 # one more DISTINCT sample agrees
                    self._deb_sample_ts = sample_ts
                waited = now - (self._deb_since if self._deb_since is not None else now)
                if self._deb_count >= r.timing.debounce and waited < r.timing.for_s:
                    return self._act(now, 'hold', f'for {int(waited)}/{int(r.timing.for_s)} s towards {d_state}', signal, actual)
                if self._deb_count >= r.timing.debounce:
                    self._deb_target, self._deb_count = None, 0
                    self._base = d_base
                    changed = self._transition(d_state, d_want, now)
                    self._urgent = False
                else:
                    return self._act(now, 'hold', f'debounce {self._deb_count}/{r.timing.debounce} towards {d_state}', signal, actual)
        # 4. act on the want (rate limit), else 5. the closed loop
        return self._act(now, None, '', signal, actual, changed=changed, actual_age_s=actual_age_s, actual_stale_s=actual_stale_s)

    # ── trim ──
    def _fresh(self, actual, actual_age_s, actual_stale_s) -> Optional[float]:
        if actual is None or (actual_stale_s is not None and actual_age_s is not None and actual_age_s > actual_stale_s):
            return None
        return _num(actual)

    def _trim_seed(self, actual: Optional[float]) -> float:
        """Where a trim starts: what the device holds when it is read back
        (within min..max, or off), else off, else start, else min — never a
        jump the device was not already at."""
        r = self.rule
        if actual is not None:
            if r.off is not None and abs(actual - r.off) <= r.tolerance:
                return r.off
            if r.vmin - r.tolerance <= actual <= r.vmax + r.tolerance:
                return min(max(actual, r.vmin), r.vmax)
        if r.off is not None:
            return r.off
        return r.start if r.start is not None else r.vmin

    def _trim_next(self, cur: float, v: float, direction: int) -> float:
        """One adjustment from ``cur``: ±step, or gain × the distance past the
        threshold (at most step); under min it asks for off, from off it
        starts again at start (or min)."""
        r = self.rule
        if r.off is not None and cur <= r.off:
            return (r.start if r.start is not None else r.vmin) if direction > 0 else cur
        if r.gain is None:
            delta = r.step * direction
        else:
            dist = v - (r.above if direction > 0 else r.below)
            delta = max(-r.step, min(r.step, r.gain * dist))
        if direction < 0 and r.off is not None and cur <= r.vmin:
            return r.off
        return round(min(max(cur + delta, r.vmin), r.vmax), 6)

    def _trim_label(self, value: float, direction: int, moved: bool) -> str:
        r = self.rule
        if r.off is not None and value <= r.off:
            return 'off'
        if not moved and direction > 0:
            return 'at max'
        if not moved and direction < 0:
            return 'at min'
        return {1: 'raising', -1: 'lowering'}.get(direction, 'holding')

    def _trim_eval(self, now: float, v: float, new_sample: bool, signal, actual,
                   actual_age_s, actual_stale_s) -> Decision:
        r = self.rule
        kw = dict(actual_age_s=actual_age_s, actual_stale_s=actual_stale_s)
        if self.leader is not None and self.leader is not self:
            return self._trim_follow(now, signal, actual, kw)
        held = self._fresh(actual, actual_age_s, actual_stale_s)
        if held is None:
            held = _num(actual)            # an old read-back still beats a jump to min
        if self._trim is None:
            if held is None and r.target.get('_readback'):
                # the device can say what it holds, it has not yet (the first
                # poll after a restart): starting from min would send the
                # floor to an inverter at 100 % — wait for it
                return self._act(now, 'hold', 'waiting for the read-back to start from', signal, actual, **kw)
            self._trim = self._trim_seed(held)
        direction = 1 if v > r.above else (-1 if v < r.below else 0)
        if direction == 0:
            self._trim_dir, self._trim_count, self._trim_since = 0, 0, None
            return self._act(now, None, '', signal, actual,
                             changed=self._label(self._trim_label(self._trim, 0, True), now), **kw)
        # the debounce counts distinct samples in one direction
        if direction != self._trim_dir:
            self._trim_dir, self._trim_count, self._trim_since = direction, 1, now
        elif new_sample:
            self._trim_count += 1
        towards = 'raising' if direction > 0 else 'lowering'
        if self._trim_count < r.timing.debounce:
            return self._act(now, 'hold', f'debounce {self._trim_count}/{r.timing.debounce} towards {towards}', signal, actual)
        waited = now - (self._trim_since if self._trim_since is not None else now)
        if waited < r.timing.for_s:
            return self._act(now, 'hold', f'for {int(waited)}/{int(r.timing.for_s)} s towards {towards}', signal, actual)
        # no windup: one adjustment at a time, each sent (or would-be, in
        # shadow) and its rate-limit window over before the next
        if self.pending:
            return self._act(now, None, '', signal, actual, **kw)
        if self.last_cmd_ts is not None and r.timing.min_interval_s > 0 \
                and now - self.last_cmd_ts < r.timing.min_interval_s:
            left = int(r.timing.min_interval_s - (now - self.last_cmd_ts))
            return Decision(self._wall or now, 'hold', f'{towards}: next adjustment in {left} s',
                            self.state, signal, self.want, actual)
        self._trim_count = 0
        if r.mode != 'armed' and held is not None:
            # in shadow nothing moves the device: each would-be step starts
            # from what it holds, not from the last would-be step — else the
            # trim runs down to min unseen and arming sends that (2026-10-20)
            self._trim = self._trim_seed(held)
        cur = self._trim
        new = self._trim_next(cur, v, direction)
        want = self._apply_clamp(self._want_for({r.param: new}), self._wall, None)
        new = float(want[r.param])
        moved = abs(new - cur) > 1e-9
        self._trim = new
        label = self._trim_label(new, direction, moved)
        if not moved and self.want is not None:
            return self._act(now, None, '', signal, actual, changed=self._label(label, now), **kw)
        before = self.state
        self._transition(label, want, now)
        # an event per change of state, not per nudge — a trim moves often
        return self._act(now, None, '', signal, actual, changed=label != before, **kw)

    def _trim_follow(self, now: float, signal, actual, kw) -> Decision:
        """A unit of a group asks for what the group's first unit asks: the
        units step together instead of drifting apart on their own timing."""
        lead = self.leader
        if lead.want is None or lead._trim is None:
            return self._act(now, None, '', signal, actual, **kw)
        self._trim = lead._trim
        changed = self._transition(lead.state, dict(lead.want), now)
        return self._act(now, None, '', signal, actual, changed=changed, **kw)

    # ── schedule and forcing ──
    def _schedule_desired(self, signal) -> tuple:
        """(state, base params): the entry in force now, or off while the
        rule's `when` is false."""
        r = self.rule
        if not signal:
            return 'off (when is false)', {r.param: r.off}
        e = current_entry(r.schedule, clock.local(self._wall))
        if e is None:
            return NORMAL, None
        label = e.get('label') or f"{days_text(e['days'])} {e['at']}"
        if 'params' not in e and e.get('value') is None:
            return label, None                       # no opinion from here
        return label, (dict(e['params']) if 'params' in e else {r.param: e['value']})

    def force(self, params: Dict[str, Any], until: Optional[float]) -> None:
        """Ask for ``params`` until the wall time ``until`` whatever the signal
        says; None ends it now."""
        self.forced = {'params': dict(params), 'until': float(until)} if until else None
        if self.forced is None:
            self._end_force()

    def _end_force(self) -> None:
        self.forced = None
        self._deb_target, self._deb_count, self._trim, self._trim_count = None, 0, None, 0
        if self.state == 'forced':
            self.state = NORMAL                       # the rule decides again from here

    def _forced_eval(self, now: float, signal, actual, kw) -> Optional[Decision]:
        if self._wall >= self.forced['until']:
            self._end_force()
            return None
        # the operator's ceiling still caps it; the motor's timers (minimum
        # stop, starts per hour) still hold a start — _act applies them
        want = self._apply_clamp(self._want_for(dict(self.forced['params'])), self._wall, None)
        changed = self._transition('forced', want, now)
        left = int(self.forced['until'] - self._wall)
        return self._act(now, None, f'forced for {left} s more', signal, actual, changed=changed, **kw)

    def _label(self, label: str, now: float) -> bool:
        if label == self.state:
            return False
        self.state, self.since = label, self._wall or now
        return True

    def preview_trim(self, v: float, actual: Optional[float] = None) -> tuple:
        """(state, base params) the next adjustment would ask for, nothing
        kept — the editor's live preview. A rule not running yet starts from
        the read-back, as it will."""
        r = self.rule
        cur = self._trim if self._trim is not None else self._trim_seed(actual)
        direction = 1 if v > r.above else (-1 if v < r.below else 0)
        if direction == 0:
            return self._trim_label(cur, 0, True), {r.param: cur}
        new = self._trim_next(cur, v, direction)
        return self._trim_label(new, direction, abs(new - cur) > 1e-9), {r.param: new}

    def _guard(self, v: Optional[float], new_sample: bool) -> Optional[str]:
        """The plausibility guard. None = the sample is accepted (or the guard is
        off); otherwise the reason the sample is being held."""
        ms = _f((self.rule.signal_valid or {}).get('max_step'))
        if v is None or ms is None or ms <= 0:
            return None
        if not new_sample:                               # the same reading as last tick
            return self._suspect_reason if self._suspect is not None else None
        if self._accepted is None or abs(v - self._accepted) <= ms:
            self._accepted, self._suspect = v, None      # plausible against the last accepted
            return None
        if self._suspect is not None and abs(v - self._suspect) <= ms:
            self._accepted, self._suspect = v, None      # a second sample agrees: it is real
            return None
        self._suspect = v
        self.guarded += 1
        self._suspect_reason = (f'implausible jump {v - self._accepted:+g} against {self._accepted:g} '
                                f'(max step {ms:g}) — waiting for the next sample')
        return self._suspect_reason

    def _act(self, now: float, forced: Optional[str], reason: str, signal, actual, *, changed: bool = False,
             actual_age_s: Optional[float] = None, actual_stale_s: Optional[float] = None) -> Decision:
        r = self.rule
        ts = self._wall or now                     # Decision stamps are wall time
        if self.paused_until is not None and ts < self.paused_until:
            # paused_until is a wall-persisted absolute → compared on the wall clock
            return Decision(ts, 'hold', f'paused by an override for {int(self.paused_until - ts)} s', self.state, signal, self.want, actual, changed)
        if self.want is None:
            self.yielding_to = None
            return Decision(ts, forced or 'idle', reason or 'nothing to ask', self.state, signal, None, actual, changed)
        g = self.gate(self.want, self._urgent) if self.gate is not None else None
        if g and g[0] == 'yield':
            # another rule drives the channel: nothing goes out, and the want
            # stays pending — it is sent the moment this rule wins again
            self.pending, self.yielding_to = True, g[2]
            return Decision(ts, 'yield', g[1] + (f' — {reason}' if reason else ''), self.state, signal, self.want, actual, changed)
        self.yielding_to = None
        p = r.param
        fresh = actual is not None and not (actual_stale_s is not None and actual_age_s is not None and actual_age_s > actual_stale_s)
        if self.pending:
            if fresh and isinstance(self.want.get(p), (int, float)) and _num(actual) is not None \
                    and abs(_num(actual) - float(self.want[p])) <= r.tolerance:
                # the device already holds it: nothing to send, nothing to burn on the wire
                self._note_sent(now, self.want)
                self.pending, self.commanded = False, dict(self.want)
                self.confirmed = True
                return Decision(ts, forced or 'idle', (reason + ' — ' if reason else '') + 'read-back already matches, nothing to send',
                                self.state, signal, self.want, actual, changed)
            # the on/off timers — never on the emergency path (a stale safe,
            # a fast step, a rest): failing closed does not wait
            if not self._urgent:
                why = self._onoff_hold(now) or (g[1] if g and g[0] == 'hold' else None)
                if why:
                    return Decision(ts, 'hold', why, self.state, signal, self.want, actual, changed)
            # the emergency path (a `fast` step, a stale safe/fixed want) is
            # exempt from the rate limit: a curtail must not wait out the
            # window a routine step opened 20 s ago (audit 2026-10-01)
            if not self._urgent and self.last_cmd_ts is not None and r.timing.min_interval_s > 0 \
                    and now - self.last_cmd_ts < r.timing.min_interval_s:
                return Decision(ts, 'hold', f'rate limit — {int(r.timing.min_interval_s - (now - self.last_cmd_ts))} s to go', self.state, signal, self.want, actual, changed)
            return self._send(now, 'run', (reason + ' — ' if reason else '') + f'{self.state}: want {self._said(self.want)}', signal, actual, changed)
        if forced:
            return Decision(ts, forced, reason, self.state, signal, self.want, actual, changed)
        # closed loop: only when we have commanded something and can read it back
        if self.commanded is None or not isinstance(self.want.get(p), (int, float)):
            return Decision(ts, 'idle', 'holding', self.state, signal, self.want, actual)
        if not fresh or _num(actual) is None:
            self.drift_since = None
            return Decision(ts, 'sweep', 'read-back missing or old — asking for a sweep', self.state, signal, self.want, actual)
        if abs(_num(actual) - float(self.want[p])) > r.tolerance:
            if self.drift_since is None:
                self.drift_since = now
            if r.timing.reassert_s > 0 and now - self.drift_since >= r.timing.reassert_s:
                if self.last_cmd_ts is not None and r.timing.min_interval_s > 0 and now - self.last_cmd_ts < r.timing.min_interval_s:
                    return Decision(ts, 'hold', 'drift seen, rate limit', self.state, signal, self.want, actual)
                self.drift_since = None
                return self._send(now, 'reassert', f'read-back {_num(actual):g} ≠ want {self.want[p]:g} for {int(r.timing.reassert_s)} s', signal, actual, False)
            return Decision(ts, 'idle', f'read-back {_num(actual):g} ≠ want {self.want[p]:g}, watching', self.state, signal, self.want, actual)
        self.drift_since = None
        return Decision(ts, 'idle', 'read-back matches', self.state, signal, self.want, actual)

    def _send(self, now: float, action: str, reason: str, signal, actual, changed: bool) -> Decision:
        self._note_sent(now, self.want)
        self.pending = False
        self.commanded = dict(self.want) if self.want else None
        self.confirmed = False                     # until the device answers
        self.last_cmd_ts = now                     # interval clock: rate limit base
        self._urgent = False                       # the emergency send happened
        ts = self._wall or now
        if self.rule.mode != 'armed':
            return Decision(ts, 'shadow', f'would {action} — {reason}', self.state, signal, self.want, actual, changed)
        return Decision(ts, action, reason, self.state, signal, self.want, actual, changed)

    @staticmethod
    def _said(want: Dict[str, Any]) -> str:
        return ', '.join(f'{k} {v:g}' if isinstance(v, (int, float)) else f'{k} {v}' for k, v in want.items())
