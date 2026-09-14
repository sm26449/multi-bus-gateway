# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
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

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

MODES = ('shadow', 'armed')
KINDS = ('steps', 'condition')
ON_STALE = ('hold', 'safe')
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


@dataclass
class RuleDef:
    id: str
    label: str = ''
    enabled: bool = True
    mode: str = 'shadow'
    target: Dict[str, Any] = field(default_factory=dict)      # {device|endpoint+group, command, param}
    kind: str = 'steps'
    signal: str = ''                                          # steps: numeric expression
    signal_valid: Dict[str, float] = field(default_factory=dict)
    stale_after_s: float = 60.0
    steps: List[Step] = field(default_factory=list)
    release_below: Optional[float] = None
    normal: Dict[str, Any] = field(default_factory=dict)      # params when no step matches
    params: Dict[str, Any] = field(default_factory=dict)      # fixed params sent with every want
    when: str = ''                                            # condition: boolean expression
    then: Optional[Dict[str, Any]] = None                     # {params: {...}} or None
    otherwise: Optional[Dict[str, Any]] = None                # `else` in YAML
    timing: Timing = field(default_factory=Timing)
    on_stale: str = 'hold'
    on_disable: str = 'safe'
    # bound from the command the target offers (the runtime fills these in)
    param: str = 'value'
    safe_params: Dict[str, Any] = field(default_factory=dict)
    tolerance: float = 1.0

    def to_dict(self) -> Dict:
        d = {'id': self.id, 'label': self.label or self.id, 'enabled': self.enabled, 'mode': self.mode,
             'target': {k: v for k, v in self.target.items() if not k.startswith('_')},   # the binding stays private
             'kind': self.kind, 'stale_after_s': self.stale_after_s,
             'params': dict(self.params), 'timing': vars(self.timing).copy(),
             'on_stale': self.on_stale, 'on_disable': self.on_disable}
        if self.kind == 'steps':
            d.update({'signal': self.signal, 'signal_valid': dict(self.signal_valid),
                      'steps': [{'at': s.at, 'value': s.value, 'label': s.label, 'fast': s.fast} for s in self.steps],
                      'release_below': self.release_below, 'normal': dict(self.normal)})
        else:
            d.update({'when': self.when, 'then': self.then, 'else': self.otherwise})
        return d


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def parse_rule_def(raw: Dict) -> RuleDef:
    """A definition from YAML/JSON. Lenient on types (validate_rule_def is the
    judge); missing fields take their defaults."""
    raw = raw or {}
    t = raw.get('timing') or {}
    timing = Timing(every_s=_f(t.get('every_s'), 2.0), debounce=int(_f(t.get('debounce'), 3)),
                    min_interval_s=_f(t.get('min_interval_s'), 30.0), reassert_s=_f(t.get('reassert_s'), 120.0))
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
        stale_after_s=_f(raw.get('stale_after_s'), 60.0), steps=steps,
        release_below=_f(raw.get('release_below')), normal=dict(raw.get('normal') or {}),
        params=dict(raw.get('params') or {}), when=str(raw.get('when', '') or ''),
        then=raw.get('then'), otherwise=raw.get('else'), timing=timing,
        on_stale=str(raw.get('on_stale', 'hold') or 'hold'),
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
    if raw.get('on_stale', 'hold') not in ON_STALE:
        errors.append(f"on_stale: one of {', '.join(ON_STALE)}")
    if raw.get('on_disable', 'safe') not in ON_DISABLE:
        errors.append(f"on_disable: one of {', '.join(ON_DISABLE)}")
    t = raw.get('timing') or {}
    if not isinstance(t, dict):
        errors.append("timing: must be an object")
        t = {}
    if _f(t.get('every_s', 2)) is None or not (0.5 <= _f(t.get('every_s', 2)) <= 3600):
        errors.append("timing.every_s: 0.5..3600 seconds")
    if _f(t.get('debounce', 3)) is None or not (1 <= _f(t.get('debounce', 3)) <= 100):
        errors.append("timing.debounce: 1..100 evaluations")
    if _f(t.get('min_interval_s', 30)) is None or not (0 <= _f(t.get('min_interval_s', 30)) <= 86400):
        errors.append("timing.min_interval_s: 0..86400 seconds")
    if _f(t.get('reassert_s', 120)) is None or not (0 <= _f(t.get('reassert_s', 120)) <= 86400):
        errors.append("timing.reassert_s: 0..86400 seconds (0 = never)")
    if _f(raw.get('stale_after_s', 60)) is None or not (1 <= _f(raw.get('stale_after_s', 60)) <= 86400):
        errors.append("stale_after_s: 1..86400 seconds")
    if kind == 'steps':
        if not str(raw.get('signal', '') or '').strip():
            errors.append("signal: required for a steps rule")
        elif validate_expr:
            e = validate_expr(raw['signal'])
            if e:
                errors.append(f"signal: {e}")
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
        if not isinstance(raw.get('normal'), dict) or not raw.get('normal'):
            errors.append("normal: the parameters asked for when no step matches (e.g. {value: 100})")
        sv = raw.get('signal_valid')
        if sv is not None and (not isinstance(sv, dict) or any(_f(sv.get(k)) is None for k in sv)):
            errors.append("signal_valid: {min, max} numbers")
    else:
        if not str(raw.get('when', '') or '').strip():
            errors.append("when: required for a condition rule")
        elif validate_expr:
            e = validate_expr(raw['when'])
            if e:
                errors.append(f"when: {e}")
        for k in ('then', 'else'):
            v = raw.get(k)
            if v is not None and (not isinstance(v, dict) or not isinstance(v.get('params', {}), dict)):
                errors.append(f"{k}: null or {{params: {{...}}}}")
        if raw.get('then') is None and raw.get('else') is None:
            errors.append("then/else: at least one outcome")
    if raw.get('params') is not None and not isinstance(raw.get('params'), dict):
        errors.append("params: an object")
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
                'failures': self.failures, 'ignored': self.ignored, 'stale_since': self.stale_since}

    # ── helpers ──
    def _want_for(self, base: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if base is None:
            return None
        w = dict(self.rule.params)
        w.update(base)
        return w

    def _desired(self, v: float) -> tuple:
        """(state, base params) the signal asks for, before clamp and debounce."""
        r = self.rule
        step = None
        for s in r.steps:
            if v >= s.at:
                step = s
        if step is not None:
            return step.label, {r.param: step.value}, step.fast
        if r.release_below is None or v < r.release_below:
            return NORMAL, dict(r.normal), False
        # the dead band between release and the first step: keep what we have
        if self._base is None or self.state == NORMAL:
            return NORMAL, dict(r.normal), False
        return self.state, dict(self._base), False

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
            self.state, self.want, self.since = state, want, now
            if want != self.commanded:
                self.pending = True
        return changed

    # ── the evaluation ──
    def evaluate(self, now: float, signal: Any, signal_age_s: Optional[float],
                 actual: Any = None, actual_age_s: Optional[float] = None,
                 actual_stale_s: Optional[float] = None) -> Decision:
        r = self.rule
        self.last_signal = signal
        # 1. validity and staleness
        valid = signal is not None
        if valid and r.kind == 'steps':
            v = _f(signal)
            sv = r.signal_valid
            if v is None or (sv.get('min') is not None and v < float(sv['min'])) \
                    or (sv.get('max') is not None and v > float(sv['max'])):
                valid = False
        if valid and (signal_age_s is None or signal_age_s <= r.stale_after_s):
            self.last_valid_ts = now - (signal_age_s or 0.0)
        else:
            if valid:
                self.last_valid_ts = now - signal_age_s
            else:
                self.ignored += 1
        stale = self.last_valid_ts is None or (now - self.last_valid_ts) > r.stale_after_s
        if stale:
            if self.state != STALE:
                self._pre_stale = (self.state, self.want)
                self.stale_since = now
                want = self.want if r.on_stale == 'hold' else self._want_for(dict(r.safe_params))
                self._transition(STALE, want, now)
                self._deb_target, self._deb_count = None, 0
                return self._act(now, 'stale', 'signal stale — ' + ('holding the last want' if r.on_stale == 'hold' else 'asking for safe'),
                                 signal, actual, changed=True, actual_age_s=actual_age_s, actual_stale_s=actual_stale_s)
            return self._act(now, 'stale', f'signal stale for {int(now - (self.stale_since or now))} s', signal, actual)
        if not valid:
            return Decision(now, 'ignored', 'sample outside the valid range', self.state, signal, self.want, actual)
        self.stale_since = None
        # 2. desired
        if r.kind == 'steps':
            d_state, base, fast = self._desired(_f(signal))
        else:
            truth = bool(signal)
            outcome = r.then if truth else r.otherwise
            d_state = 'true' if truth else 'false'
            base = dict((outcome or {}).get('params') or {}) if outcome is not None else None
            fast = False
        d_want = self._apply_clamp(self._want_for(base) if base is not None else None, now, _f(signal) if r.kind == 'steps' else None)
        d_base = base
        if self.state == STALE:
            # back from stale: the debounce decides where we really are
            self.state = self._pre_stale[0] if self._pre_stale else NORMAL
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
            else:
                if self._deb_target != key:
                    self._deb_target, self._deb_count = key, 1
                else:
                    self._deb_count += 1
                if self._deb_count >= r.timing.debounce:
                    self._deb_target, self._deb_count = None, 0
                    self._base = d_base
                    changed = self._transition(d_state, d_want, now)
                else:
                    return self._act(now, 'hold', f'debounce {self._deb_count}/{r.timing.debounce} towards {d_state}', signal, actual)
        # 4. act on the want (rate limit), else 5. the closed loop
        return self._act(now, None, '', signal, actual, changed=changed, actual_age_s=actual_age_s, actual_stale_s=actual_stale_s)

    def _act(self, now: float, forced: Optional[str], reason: str, signal, actual, *, changed: bool = False,
             actual_age_s: Optional[float] = None, actual_stale_s: Optional[float] = None) -> Decision:
        r = self.rule
        if self.paused_until is not None and now < self.paused_until:
            return Decision(now, 'hold', f'paused by an override for {int(self.paused_until - now)} s', self.state, signal, self.want, actual, changed)
        if self.want is None:
            return Decision(now, forced or 'idle', reason or 'nothing to ask', self.state, signal, None, actual, changed)
        p = r.param
        fresh = actual is not None and not (actual_stale_s is not None and actual_age_s is not None and actual_age_s > actual_stale_s)
        if self.pending:
            if fresh and isinstance(self.want.get(p), (int, float)) \
                    and abs(float(actual) - float(self.want[p])) <= r.tolerance:
                # the device already holds it: nothing to send, nothing to burn on the wire
                self.pending, self.commanded = False, dict(self.want)
                return Decision(now, forced or 'idle', (reason + ' — ' if reason else '') + 'read-back already matches, nothing to send',
                                self.state, signal, self.want, actual, changed)
            if self.last_cmd_ts is not None and r.timing.min_interval_s > 0 \
                    and now - self.last_cmd_ts < r.timing.min_interval_s:
                return Decision(now, 'hold', f'rate limit — {int(r.timing.min_interval_s - (now - self.last_cmd_ts))} s to go', self.state, signal, self.want, actual, changed)
            return self._send(now, 'run', (reason + ' — ' if reason else '') + f'{self.state}: want {self._said(self.want)}', signal, actual, changed)
        if forced:
            return Decision(now, forced, reason, self.state, signal, self.want, actual, changed)
        # closed loop: only when we have commanded something and can read it back
        if self.commanded is None or not isinstance(self.want.get(p), (int, float)):
            return Decision(now, 'idle', 'holding', self.state, signal, self.want, actual)
        if not fresh:
            self.drift_since = None
            return Decision(now, 'sweep', 'read-back missing or old — asking for a sweep', self.state, signal, self.want, actual)
        if abs(float(actual) - float(self.want[p])) > r.tolerance:
            if self.drift_since is None:
                self.drift_since = now
            if r.timing.reassert_s > 0 and now - self.drift_since >= r.timing.reassert_s:
                if self.last_cmd_ts is not None and r.timing.min_interval_s > 0 and now - self.last_cmd_ts < r.timing.min_interval_s:
                    return Decision(now, 'hold', 'drift seen, rate limit', self.state, signal, self.want, actual)
                self.drift_since = None
                return self._send(now, 'reassert', f'read-back {actual:g} ≠ want {self.want[p]:g} for {int(r.timing.reassert_s)} s', signal, actual, False)
            return Decision(now, 'idle', f'read-back {actual:g} ≠ want {self.want[p]:g}, watching', self.state, signal, self.want, actual)
        self.drift_since = None
        return Decision(now, 'idle', 'read-back matches', self.state, signal, self.want, actual)

    def _send(self, now: float, action: str, reason: str, signal, actual, changed: bool) -> Decision:
        self.pending = False
        self.commanded = dict(self.want) if self.want else None
        self.last_cmd_ts = now
        if self.rule.mode != 'armed':
            return Decision(now, 'shadow', f'would {action} — {reason}', self.state, signal, self.want, actual, changed)
        return Decision(now, action, reason, self.state, signal, self.want, actual, changed)

    @staticmethod
    def _said(want: Dict[str, Any]) -> str:
        return ', '.join(f'{k} {v:g}' if isinstance(v, (int, float)) else f'{k} {v}' for k, v in want.items())
