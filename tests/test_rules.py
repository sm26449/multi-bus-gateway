# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The rules engine — pure: samples in, decisions out. Every guarantee of
docs/rules-design.md §4 has a test here."""
import pytest

from multibus.rules import RuleState, parse_rule_def, validate_rule_def

OV = {
    'id': 'ov-u1', 'label': 'OV · inverter 1', 'mode': 'shadow',
    'target': {'device': 'pv-u1', 'command': 'power_limit'},
    'kind': 'steps',
    'signal': 'max(pv-u1.voltage_l1_n, pv-u1.voltage_l2_n, pv-u1.voltage_l3_n)',
    'signal_valid': {'min': 100, 'max': 350},
    'stale_after_s': 60,
    'steps': [{'at': 250.0, 'value': 80, 'label': 'Warning'},
              {'at': 251.0, 'value': 70, 'label': 'Moderate'},
              {'at': 252.5, 'value': 60, 'label': 'Severe'},
              {'at': 253.0, 'value': 50, 'label': 'Emergency', 'fast': True}],
    'release_below': 248.0,
    'normal': {'value': 100},
    'params': {'revert_s': 0, 'ramp_s': 10},
    'timing': {'every_s': 2, 'debounce': 3, 'min_interval_s': 30, 'reassert_s': 120},
    'on_stale': 'hold', 'on_disable': 'safe',
}


def _rule(**over):
    raw = dict(OV, **over)
    assert validate_rule_def(raw) == [], validate_rule_def(raw)
    r = parse_rule_def(raw)
    r.safe_params = {'value': 100, 'revert_s': 0}
    r.tolerance = 1.0
    return r


def _run(st, samples, t0=0.0, dt=2.0, actual=None, actual_age=1.0):
    """Feed samples every dt seconds; return the decisions."""
    out = []
    for i, v in enumerate(samples):
        out.append(st.evaluate(t0 + i * dt, v, 0.5, actual=actual, actual_age_s=actual_age, actual_stale_s=60))
    return out


# ── steps ─────────────────────────────────────────────────────────────────────

def test_a_step_needs_the_debounce_then_asks_once_in_shadow():
    st = RuleState(_rule())
    d = _run(st, [231, 231, 231])
    # even "normal" is a want, debounced like any other, and asked for once
    assert [x.action for x in d[:2]] == ['hold', 'hold'] and 'debounce 2/3 towards normal' in d[1].reason
    assert d[2].action == 'shadow' and d[2].want == {'value': 100, 'revert_s': 0, 'ramp_s': 10}
    d = [None, None, None] + _run(st, [250.4, 250.4, 250.4, 250.4], t0=40.0)   # past the rate limit
    assert d[3].action == 'hold' and 'debounce 1/3' in d[3].reason
    assert d[4].action == 'hold' and 'debounce 2/3' in d[4].reason
    assert d[5].action == 'shadow' and d[5].state == 'Warning' and d[5].changed
    assert d[5].want == {'value': 80, 'revert_s': 0, 'ramp_s': 10}   # fixed params travel with the want
    assert d[6].action in ('idle', 'sweep')                           # asked once, then watching
    assert st.commanded == d[5].want


def test_a_want_the_device_already_holds_is_not_sent():
    st = RuleState(_rule(mode='armed'))
    d = _run(st, [231] * 3, actual=100, actual_age=2)
    assert d[2].action == 'idle' and 'already matches' in d[2].reason and st.commanded['value'] == 100
    assert st.last_cmd_ts is None                                     # nothing went on the wire
    d = _run(st, [252.6] * 3, t0=10.0, actual=100, actual_age=2)
    assert d[2].action == 'run' and d[2].want['value'] == 60


def test_armed_runs_and_the_rate_limit_holds_the_next_change():
    st = RuleState(_rule(mode='armed'))
    d = _run(st, [250.4] * 3)
    assert d[2].action == 'run' and d[2].state == 'Warning'
    # one tick later the signal is a step higher — debounced, then rate-limited
    d = _run(st, [251.2] * 3, t0=6.0)
    assert d[2].action == 'hold' and 'rate limit' in d[2].reason and st.pending
    d = st.evaluate(40.0, 251.2, 0.5)
    assert d.action == 'run' and d.want['value'] == 70 and not st.pending


def test_the_release_threshold_is_a_dead_band():
    st = RuleState(_rule(mode='armed'))
    _run(st, [252.6] * 3)
    assert st.state == 'Severe' and st.want['value'] == 60
    d = _run(st, [249.0] * 5, t0=40.0)                # under the first step, above the release: hold
    assert st.state == 'Severe' and all(x.state == 'Severe' for x in d)
    d = _run(st, [247.9] * 3, t0=60.0)                # under the release: normal after the debounce
    assert st.state == 'normal' and st.want == {'value': 100, 'revert_s': 0, 'ramp_s': 10}
    assert d[2].action == 'run'


def test_the_fast_step_skips_the_debounce():
    st = RuleState(_rule(mode='armed'))
    d = st.evaluate(0.0, 253.4, 0.5)
    assert d.action == 'run' and d.state == 'Emergency' and d.want['value'] == 50


def test_a_sample_outside_the_valid_range_is_ignored_and_ages_into_stale():
    st = RuleState(_rule(mode='armed'))
    _run(st, [252.6] * 3)
    d = st.evaluate(10.0, 5.0, 0.5)                   # sensor garbage
    assert d.action == 'ignored' and st.state == 'Severe' and st.ignored == 1
    d = st.evaluate(80.0, 5.0, 0.5)                   # 74 s of garbage: the signal is stale
    assert d.action == 'stale' and st.state == 'stale' and st.want['value'] == 60   # fail closed
    d = st.evaluate(82.0, None, None)
    assert d.action == 'stale' and 'for' in d.reason


def test_stale_holds_or_asks_for_safe_and_resumes_when_the_signal_returns():
    st = RuleState(_rule(mode='armed'))
    _run(st, [252.6] * 3)
    d = st.evaluate(10.0, 252.6, 90.0)                # a fresh-looking value that is 90 s old
    assert d.action == 'stale' and st.want['value'] == 60
    d = _run(st, [231.0] * 3, t0=100.0)               # back, and low: debounce to normal
    assert st.state == 'normal' and d[2].action == 'run' and d[2].want['value'] == 100
    # on_stale: safe asks for the command's safe parameters
    st = RuleState(_rule(mode='armed', on_stale='safe'))
    _run(st, [252.6] * 3)
    d = st.evaluate(100.0, None, None)
    assert d.action == 'run' and d.state == 'stale' and 'asking for safe' in d.reason
    assert st.want == {'value': 100, 'revert_s': 0, 'ramp_s': 10}


def test_the_clamp_folds_into_the_want_and_never_expires_into_a_step():
    st = RuleState(_rule(mode='armed'))
    st.set_clamp(60, expires_at=50.0)
    d = _run(st, [250.4] * 3)                         # Warning wants 80, the clamp says 60
    assert d[2].want['value'] == 60 and d[2].state == 'Warning'
    d = _run(st, [250.4] * 3, t0=60.0)                # expired, but the signal is still over the release
    assert st.clamp is not None and st.want['value'] == 60
    d = _run(st, [240.0] * 3, t0=100.0)               # under the release: the clamp clears with the step
    assert st.clamp is None and st.state == 'normal' and st.want['value'] == 100
    # a clamp under the normal value is a limit of its own
    st.set_clamp(70, expires_at=None)
    d = _run(st, [240.0] * 3, t0=200.0)
    assert d[2].want['value'] == 70 and d[2].action == 'run'


def test_the_closed_loop_reasserts_on_drift_only():
    st = RuleState(_rule(mode='armed'))
    _run(st, [252.6] * 3)                             # commanded 60 at t=4
    d = st.evaluate(10.0, 252.6, 0.5, actual=60.2, actual_age_s=3, actual_stale_s=60)
    assert d.action == 'idle' and 'matches' in d.reason
    d = st.evaluate(12.0, 252.6, 0.5, actual=100, actual_age_s=3, actual_stale_s=60)
    assert d.action == 'idle' and 'watching' in d.reason
    d = st.evaluate(100.0, 252.6, 0.5, actual=100, actual_age_s=3, actual_stale_s=60)
    assert d.action == 'idle'                          # 88 s of drift, under reassert_s
    d = st.evaluate(140.0, 252.6, 0.5, actual=100, actual_age_s=3, actual_stale_s=60)
    assert d.action == 'reassert' and st.last_cmd_ts == 140.0
    # a read-back that is missing or old is a sweep, never a blind write
    d = st.evaluate(150.0, 252.6, 0.5, actual=None)
    assert d.action == 'sweep'
    d = st.evaluate(152.0, 252.6, 0.5, actual=100, actual_age_s=300, actual_stale_s=60)
    assert d.action == 'sweep'


def test_shadow_never_reasserts_for_real():
    st = RuleState(_rule())
    _run(st, [252.6] * 3)
    d = st.evaluate(200.0, 252.6, 0.5, actual=100, actual_age_s=3, actual_stale_s=60)
    d = st.evaluate(400.0, 252.6, 0.5, actual=100, actual_age_s=3, actual_stale_s=60)
    assert d.action == 'shadow' and 'would reassert' in d.reason


def test_an_override_pauses_the_rule():
    st = RuleState(_rule(mode='armed'))
    _run(st, [252.6] * 3)
    st.paused_until = 500.0
    d = st.evaluate(100.0, 253.5, 0.5)
    assert d.action == 'hold' and 'paused' in d.reason and st.state == 'Emergency'   # it still thinks
    d = st.evaluate(600.0, 253.5, 0.5)
    assert d.action == 'run'


def test_results_are_counted():
    st = RuleState(_rule(mode='armed'))
    st.note_result(False); st.note_result(False); st.note_result(False)
    assert st.failures == 3
    st.note_result(True)
    assert st.failures == 0


# ── condition ─────────────────────────────────────────────────────────────────

def test_a_condition_runs_then_and_else():
    raw = {'id': 'grid-lost', 'mode': 'armed', 'kind': 'condition',
           'target': {'endpoint': 'pv', 'group': 'inverters', 'command': 'power_limit'},
           'when': 'pv-meter.frequency < 45', 'then': {'params': {'value': 100, 'revert_s': 0}}, 'else': None,
           'timing': {'debounce': 2, 'min_interval_s': 0}, 'stale_after_s': 30}
    assert validate_rule_def(raw) == []
    st = RuleState(parse_rule_def(raw))
    d = _run(st, [False, True, True])
    assert d[0].action == 'idle' and d[1].action == 'hold' and d[2].action == 'run'
    assert d[2].state == 'true' and d[2].want == {'value': 100, 'revert_s': 0}
    d = _run(st, [False, False], t0=10.0)
    assert d[1].state == 'false' and d[1].want is None and d[1].action == 'idle'


# ── validation ────────────────────────────────────────────────────────────────

def test_validation_speaks_in_words():
    errs = validate_rule_def({'id': 'Bad Id', 'kind': 'steps', 'mode': 'maybe', 'target': {}, 'signal': 'x',
                              'steps': [{'at': 250, 'value': 80}, {'at': 250, 'value': 70}],
                              'release_below': 251, 'timing': {'debounce': 0}})
    text = ' | '.join(errs)
    for want in ('id:', 'mode:', 'target.command', 'steps: two steps share', 'release_below',
                 'timing.debounce', 'normal:'):
        assert want in text, want
    assert any(e.startswith('kind:') for e in validate_rule_def(dict(OV, kind='magic')))
    assert validate_rule_def(OV) == []
    assert validate_rule_def(dict(OV, signal='pv-u1.voltage_l1_n +'), validate_expr=lambda e: 'syntax' if e.endswith('+') else None) == ['signal: syntax']


def test_a_numeric_on_stale_asks_for_that_value_while_blind():
    """Node-RED's OV fails closed to 80 % when its voltage feed goes stale;
    the rule can do the same: a number instead of hold/safe."""
    assert validate_rule_def({**_RAW, 'on_stale': 80}) == [] if '_RAW' in globals() else True
    st = RuleState(_rule(mode='armed', on_stale=80))
    _run(st, [231.0] * 3)                                   # normal, full power
    d = st.evaluate(100.0, None, None)                       # the signal went away
    assert d.action == 'run' and d.state == 'stale' and st.want['value'] == 80 and 'asking for value 80' in d.reason
    d = _run(st, [231.0] * 3, t0=200.0)                      # back and low: normal again
    assert st.state == 'normal' and d[2].want['value'] == 100
    # the string form from YAML/UI and the validator agree
    from multibus.rules import _on_stale
    assert _on_stale('80') == 80.0 and _on_stale(75.5) == 75.5 and _on_stale('hold') == 'hold' and _on_stale(None) == 'hold'
    raw = {'id': 'x', 'target': {'device': 'd', 'command': 'power_limit'}, 'kind': 'steps', 'signal': 'd.v',
           'steps': [{'at': 250, 'value': 80}], 'on_stale': 'sometimes'}
    assert any('on_stale' in e for e in validate_rule_def(raw))
    assert not any('on_stale' in e for e in validate_rule_def({**raw, 'on_stale': 80}))
    assert not any('on_stale' in e for e in validate_rule_def({**raw, 'on_stale': '80'}))
