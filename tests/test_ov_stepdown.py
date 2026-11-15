# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""OV-STEPDOWN-1 — step-down hysteresis for `steps` rules, opt-in:

* ``step_down_margin`` (V): a step DOWN to a less curtailed step needs the
  signal under the current step's threshold minus the margin;
* ``timing.step_down_hold_s``: ... and no sample at or over that line for
  this long (checked after the debounce, keeping its count);
* a step UP is never delayed — with the keys it is also exempt from the
  rate limit a step down opened (B1);
* release under release_below and the dead band are unchanged.

Prod 2026-11-11: u3/u4 changed step 30/28 times in ~1 h (80 <-> 70 %)."""
import math

import pytest

from multibus.rules import RuleState, parse_rule_def, tuning_errors, tuning_of, validate_rule_def
from tests.test_rules import OV, _rule


def _tuned(margin=None, hold=None, **over):
    keys = dict(over)
    if margin is not None:
        keys['step_down_margin'] = margin
    if hold is not None:
        keys['timing'] = {**OV['timing'], **over.get('timing', {}), 'step_down_hold_s': hold}
    return _rule(mode='armed', **keys)


def _drive(st, samples, t0=0.0, dt=2.0, actual=None):
    return [st.evaluate(t0 + i * dt, v, 0.5, actual=actual, actual_age_s=1.0, actual_stale_s=60)
            for i, v in enumerate(samples)]


def _changes(ds):
    return [(d.ts, d.state) for d in ds if d.changed]


def _flap(n=1800, period=40, seed_v=251.0, amp=0.3):
    """The 11-11 shape: an hour of max-phase voltage oscillating around
    the Moderate threshold, ±amp, with a 2 s poll."""
    return [round(seed_v + amp * math.sin(2 * math.pi * i * 2.0 / period), 3) for i in range(n)]


# ── the flapping trace ───────────────────────────────────────────────────────

def test_without_the_keys_the_flapping_trace_flips_as_today():
    st = RuleState(_rule(mode='armed'))
    ds = _drive(st, [251.3] * 3 + _flap())
    flips = [c for c in _changes(ds) if c[1] in ('Warning', 'Moderate')]
    assert len(flips) > 50                               # 80 <-> 70 every half-period


def test_with_margin_and_hold_the_flapping_trace_settles_on_one_step():
    st = RuleState(_tuned(margin=0.5, hold=90))
    ds = _drive(st, [251.3] * 3 + _flap())
    flips = [c for c in _changes(ds) if c[1] in ('Warning', 'Moderate')]
    assert flips == [(4.0, 'Moderate')]                   # up once, never back down while ±0.3 of 251


def test_one_change_per_excursion_with_margin_and_hold():
    """Excursions over 251 separated by dips to 250.6 (inside the margin):
    the rule climbs once and stays; a real fall (to 249.6 for > 90 s) is
    one step down."""
    st = RuleState(_tuned(margin=0.5, hold=90))
    trace = ([251.3] * 10 + [250.6] * 10) * 6 + [250.2] * 60 + [251.3] * 10
    ds = _drive(st, trace)
    ch = [c[1] for c in _changes(ds)]
    assert ch == ['Moderate', 'Warning', 'Moderate']


@pytest.mark.parametrize('keys', [dict(), dict(margin=0, hold=0)])
def test_without_or_with_zero_keys_every_decision_is_today(keys):
    a, b = RuleState(_rule(mode='armed')), RuleState(_tuned(**keys))
    trace = [251.3] * 3 + _flap(300) + [249.0] * 10 + [247.0] * 10 + [253.4] * 3 + [250.4] * 30
    for da, db in zip(_drive(a, trace), _drive(b, trace)):
        assert da.to_dict() == db.to_dict()


# ── the margin ───────────────────────────────────────────────────────────────

def test_the_margin_keeps_the_step_until_the_signal_is_under_it():
    st = RuleState(_tuned(margin=0.5))
    _drive(st, [251.3] * 3)
    assert st.state == 'Moderate'
    ds = _drive(st, [250.6] * 10, t0=10.0)                # under 251, over 250.5
    assert st.state == 'Moderate' and not _changes(ds)
    ds = _drive(st, [250.4] * 3, t0=40.0)                 # under 250.5: debounced, then down
    assert st.state == 'Warning' and ds[-1].want['value'] == 80


def test_the_margin_steps_down_to_the_highest_step_it_still_holds():
    """From Severe (252.5) at 251.3 with margin 0.5: Moderate (251) holds,
    the step down goes there, not lower."""
    st = RuleState(_tuned(margin=0.5))
    _drive(st, [252.6] * 3)
    assert st.state == 'Severe'
    _drive(st, [252.1] * 5, t0=10.0)                      # inside 252.5 - 0.5
    assert st.state == 'Severe'
    _drive(st, [251.3] * 3, t0=40.0)
    assert st.state == 'Moderate'


def test_the_release_and_the_dead_band_are_unchanged():
    st = RuleState(_tuned(margin=0.5, hold=90))
    _drive(st, [251.3] * 3)
    ds = _drive(st, [249.0] * 20, t0=10.0)                 # the dead band: keeps Moderate, as today
    assert st.state == 'Moderate' and not _changes(ds)
    ds = _drive(st, [247.5] * 3, t0=60.0)                  # under release_below: normal, no hold
    assert st.state == 'normal' and ds[-1].want['value'] == 100


# ── the hold ─────────────────────────────────────────────────────────────────

def test_the_hold_waits_after_the_debounce_and_keeps_its_count():
    st = RuleState(_tuned(hold=90))
    _drive(st, [251.3] * 3)                                # Moderate at t=4, last >= 251 at t=4
    ds = _drive(st, [250.4] * 50, t0=6.0)                  # 6..104 s
    holds = [d for d in ds if d.action == 'hold' and 'step-down hold' in d.reason]
    assert holds and 'towards Warning' in holds[0].reason
    down = [d for d in ds if d.changed]
    assert len(down) == 1 and down[0].state == 'Warning' and down[0].ts == 94.0   # 4 + 90


def test_a_sample_over_the_line_restarts_the_hold():
    st = RuleState(_tuned(margin=0.5, hold=90))
    _drive(st, [251.3] * 3)
    _drive(st, [250.4] * 30, t0=6.0)                       # 6..64
    _drive(st, [250.6], t0=66.0)                           # back over 250.5 at 66
    ds = _drive(st, [250.4] * 60, t0=68.0)
    down = [d for d in ds if d.changed]
    assert down[0].ts == 156.0                             # 66 + 90


def test_a_step_up_is_never_delayed_by_the_keys():
    for v in (251.3, 252.6, 253.4):
        a, b = RuleState(_rule(mode='armed')), RuleState(_tuned(margin=0.5, hold=90))
        trace = [250.4] * 5 + [v] * 5
        da, db = _drive(a, trace), _drive(b, trace)
        assert [d.ts for d in da if d.changed] == [d.ts for d in db if d.changed]


def test_a_step_up_from_a_step_down_held_by_the_rate_limit_goes_at_once():
    """B1: Moderate (70 %) sent at 4, Warning wanted at 10 and held by the
    rate limit, then Severe at 16 — with the keys the 60 % goes out at once
    (it is a higher step than what was sent); without them it waits to 34."""
    plain = RuleState(_rule(mode='armed'))
    _drive(plain, [251.3] * 3 + [250.4] * 3)
    ds = _drive(plain, [252.6] * 3, t0=12.0)
    assert ds[-1].action == 'hold' and 'rate limit' in ds[-1].reason
    st = RuleState(_tuned(margin=0.1))
    _drive(st, [251.3] * 3)                                # 70 % at t=4
    ds = _drive(st, [250.4] * 3, t0=6.0)                   # Warning wanted at 10: rate limit (till 34)
    assert ds[-1].action == 'hold' and 'rate limit' in ds[-1].reason
    ds = _drive(st, [252.6] * 3, t0=12.0)                  # Severe at 16: above what was sent (70)
    assert ds[-1].action == 'run' and ds[-1].want['value'] == 60 and ds[-1].ts == 16.0


def test_the_stale_return_keeps_the_hold_running():
    st = RuleState(_tuned(hold=90))
    _drive(st, [251.3] * 3)                                # last >= 251 at t=4
    _drive(st, [250.4] * 5, t0=6.0)                        # 6..14
    st.evaluate(100.0, None, None, actual=None)            # blind for a while: stale (hold)
    assert st.state == 'stale'
    ds = _drive(st, [250.4] * 3, t0=102.0)                  # back: 98 s since t=4 — over the hold
    assert st.state == 'Warning' and ds[-1].changed


# ── keys: parse, validation, round trip ──────────────────────────────────────

def test_the_keys_round_trip_and_are_tuning():
    raw = dict(OV, step_down_margin=0.5, timing={**OV['timing'], 'step_down_hold_s': 90})
    r = parse_rule_def(raw)
    assert r.step_down_margin == 0.5 and r.timing.step_down_hold_s == 90
    d = r.to_dict()
    assert d['step_down_margin'] == 0.5 and d['timing']['step_down_hold_s'] == 90
    assert 'step_down_margin' not in parse_rule_def(OV).to_dict()
    assert 'step_down_hold_s' not in parse_rule_def(OV).to_dict()['timing']
    assert tuning_of(raw) == {'timing.step_down_hold_s': 90, 'step_down_margin': 0.5}
    assert tuning_errors(raw) == [] and validate_rule_def(raw) == []


@pytest.mark.parametrize('bad', [True, False, [], [1], {}, {'x': 1}, 'yes', '', None, float('nan'),
                                 float('inf'), -1, 1e9])
def test_odd_values_parse_as_off_and_are_refused_on_save(bad):
    raw = dict(OV, step_down_margin=bad, timing={**OV['timing'], 'step_down_hold_s': bad})
    r = parse_rule_def(raw)
    assert r.step_down_margin == 0 and r.timing.step_down_hold_s == 0
    errs = tuning_errors(raw)
    assert any(e.startswith('step_down_margin') for e in errs)
    assert any(e.startswith('timing.step_down_hold_s') for e in errs)


def test_the_margin_is_bounded_by_the_highest_step_minus_release():
    assert tuning_errors(dict(OV, step_down_margin=5.0)) == []            # 253 - 248
    assert tuning_errors(dict(OV, step_down_margin=5.1))[0].startswith('step_down_margin: a number from 0 to 5')
    assert tuning_errors(dict(OV, timing={**OV['timing'], 'step_down_hold_s': 3601}))


def test_only_a_steps_rule_takes_them():
    cond = {'id': 'c', 'kind': 'condition', 'target': {'device': 'x', 'command': 'y'},
            'when': 'a > 1', 'then': {'params': {'value': 1}}, 'step_down_margin': 0.5,
            'timing': {'step_down_hold_s': 30}}
    assert parse_rule_def(cond).step_down_margin == 0 and parse_rule_def(cond).timing.step_down_hold_s == 0
    assert sorted(tuning_errors(cond)) == ['step_down_margin: only a steps rule has it',
                                           'timing.step_down_hold_s: only a steps rule has it']
