# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The opt-in OV tuning keys of lot C (ADR-0012): ``timing.boot_wait_s``,
``resume_on_boot`` and ``timing.urgent_retries`` — what each one changes,
and that a step or an urgent want is never delayed by them."""
import math

import pytest

from multibus.rules import (URGENT_RETRY_GAP_S, RuleState, parse_rule_def, tuning_errors, tuning_of,
                            validate_rule_def)
from tests.test_rules import OV, _rule, _run


def _tuned(timing=None, **over):
    return _rule(mode='armed', timing={**OV['timing'], **(timing or {})}, **over)


def _sends(ds):
    return [d for d in ds if d.action in ('run', 'reassert')]


# ── boot_wait_s ──────────────────────────────────────────────────────────────

def test_without_boot_wait_a_restart_under_release_sends_100_blind():
    """Today (pinned): a fresh rule under release, read-back not in yet,
    sends normal after the debounce — the 31 no-op boot writes."""
    st = RuleState(_rule(mode='armed'))
    d = _run(st, [240.0] * 4, actual=None)
    assert len(_sends(d)) == 1 and _sends(d)[0].want['value'] == 100


def test_boot_wait_holds_the_normal_want_until_the_read_back_then_sends_nothing():
    st = RuleState(_tuned({'boot_wait_s': 60}))
    d = _run(st, [240.0] * 4, actual=None)                      # 0..6 s: no read-back yet
    assert not _sends(d)
    assert d[-1].action == 'sweep' and 'waiting up to 60 s for the read-back' in d[-1].reason
    d = _run(st, [240.0] * 3, t0=8.0, actual=100)              # the read-back lands: it holds 100
    assert not _sends(d) and 'already matches' in d[0].reason and st.commanded['value'] == 100


def test_boot_wait_sends_once_the_wait_is_over_when_no_read_back_comes():
    st = RuleState(_tuned({'boot_wait_s': 20}))
    d = _run(st, [240.0] * 12, actual=None)                    # 0..22 s
    sends = _sends(d)
    assert len(sends) == 1 and sends[0].ts >= 20.0


def test_boot_wait_sends_when_the_device_holds_another_value():
    st = RuleState(_tuned({'boot_wait_s': 60}))
    _run(st, [240.0] * 4, actual=None)
    d = _run(st, [240.0] * 2, t0=8.0, actual=70)               # it holds 70: normal goes out now
    assert len(_sends(d)) == 1 and _sends(d)[0].want['value'] == 100


@pytest.mark.parametrize('v', [250.4, 251.6, 252.8])
def test_boot_wait_never_delays_a_step_want(v):
    a, b = RuleState(_rule(mode='armed')), RuleState(_tuned({'boot_wait_s': 600}))
    da, db = _run(a, [v] * 5, actual=None), _run(b, [v] * 5, actual=None)
    assert [x.to_dict() for x in da] == [x.to_dict() for x in db]


def test_boot_wait_never_delays_a_fast_step():
    st = RuleState(_tuned({'boot_wait_s': 600}))
    d = st.evaluate(0.0, 253.4, 0.5, actual=None)
    assert d.action == 'run' and d.want['value'] == 50


def test_boot_wait_never_delays_a_stale_safe_want():
    st = RuleState(_tuned({'boot_wait_s': 600}, on_stale='safe'))
    d = st.evaluate(0.0, None, None, actual=None)               # no signal at all: stale at once
    assert d.state == 'stale' and d.action == 'run'


def test_boot_wait_is_only_after_a_restart_not_after_the_first_send():
    st = RuleState(_tuned({'boot_wait_s': 600}))
    _run(st, [251.6] * 4, actual=None)                         # a step went out
    d = _run(st, [240.0] * 20, t0=40.0, actual=None)           # back under release later
    assert len(_sends(d)) == 1 and _sends(d)[0].want['value'] == 100


def test_dead_band_after_a_restart_asks_for_the_read_back_at_once():
    """§4.0: no read-back in the dead band used to answer idle, nothing to
    ask — the read-back then came with the hourly controls poll."""
    plain = RuleState(_rule(mode='armed'))
    assert _run(plain, [249.6], actual=None)[0].action == 'idle'
    st = RuleState(_tuned({'boot_wait_s': 60}))
    d = _run(st, [249.6], actual=None)
    assert d[0].action == 'sweep' and 'asking for the read-back' in d[0].reason
    d = _run(st, [249.6] * 3, t0=2.0, actual=80)               # it lands: the held step is resumed
    assert st.state == 'Warning' and not _sends(d)


# ── resume_on_boot ───────────────────────────────────────────────────────────

def test_resume_on_boot_starts_from_the_step_the_device_holds():
    st = RuleState(_tuned(resume_on_boot=True))
    d = st.evaluate(0.0, 251.6, 0.5, actual=70, actual_age_s=1.0, actual_stale_s=60)
    assert st.state == 'Moderate' and st.commanded['value'] == 70 and st.confirmed and not st.pending
    assert d.action not in ('run', 'reassert')


def test_resume_on_boot_never_delays_an_escalation():
    for v in (252.6, 253.4):
        a = RuleState(_rule(mode='armed'))
        b = RuleState(_tuned(resume_on_boot=True))
        da = _run(a, [v] * 5, actual=80)
        db = _run(b, [v] * 5, actual=80)
        ta = next(x.ts for x in da if x.action == 'run')
        tb = next(x.ts for x in db if x.action == 'run')
        assert tb <= ta and _sends(db)[0].want['value'] == _sends(da)[0].want['value']


def test_resume_on_boot_still_releases_under_release_below():
    st = RuleState(_tuned(resume_on_boot=True))
    d = _run(st, [247.0] * 4, actual=70)
    assert st.state == 'normal' and _sends(d) and _sends(d)[0].want['value'] == 100


def test_resume_on_boot_ignores_a_value_no_step_has_and_an_old_read_back():
    st = RuleState(_tuned(resume_on_boot=True))
    st.evaluate(0.0, 251.6, 0.5, actual=93, actual_age_s=1.0, actual_stale_s=60)
    assert st.commanded is None
    st = RuleState(_tuned(resume_on_boot=True))
    st.evaluate(0.0, 251.6, 0.5, actual=70, actual_age_s=999.0, actual_stale_s=60)
    assert st.commanded is None


# ── urgent_retries ───────────────────────────────────────────────────────────

def _urgent_sent(timing):
    st = RuleState(_tuned(timing))
    d = st.evaluate(0.0, 253.4, 0.5, actual=80, actual_age_s=1.0, actual_stale_s=60)
    assert d.action == 'run' and d.want['value'] == 50
    return st


def test_without_urgent_retries_a_failed_urgent_want_waits_the_rate_limit():
    st = _urgent_sent({})
    assert st.rearm_urgent() is False
    st.pending = True                                         # what the runtime does on a failure
    d = _run(st, [253.4] * 20, t0=2.0, actual=80)
    assert _sends(d)[0].ts >= 30.0


def test_urgent_retries_sends_the_same_want_again_after_the_gap():
    st = _urgent_sent({'urgent_retries': 3})
    assert st.rearm_urgent() is True
    d = _run(st, [253.4] * 5, t0=2.0, actual=80)
    sends = _sends(d)
    assert sends and sends[0].ts == pytest.approx(URGENT_RETRY_GAP_S + 1.0, abs=1.0)
    assert sends[0].want['value'] == 50 and 'urgent: sending again' in d[0].reason


def test_urgent_retries_stop_at_the_count():
    st = _urgent_sent({'urgent_retries': 2})
    t = 0.0
    for _ in range(2):
        assert st.rearm_urgent() is True
        t += URGENT_RETRY_GAP_S + 1
        assert st.evaluate(t, 253.4, 0.5, actual=80, actual_age_s=1.0, actual_stale_s=60).action == 'run'
    assert st.rearm_urgent() is False


def test_urgent_retries_never_upgrade_a_want_that_moved_on():
    st = _urgent_sent({'urgent_retries': 3})
    st.want = dict(st.want, value=60)                         # the rule decided something else since
    assert st.rearm_urgent() is False


def test_urgent_retries_do_not_apply_to_a_routine_want():
    st = RuleState(_tuned({'urgent_retries': 3}))
    _run(st, [251.6] * 3, actual=100)                          # a debounced (routine) step
    assert st.commanded['value'] == 70 and st.rearm_urgent() is False



def test_boot_wait_never_holds_back_an_operator_clamp():
    # review lot C F3: a clamp makes NORMAL a cap (it protects) — no wait
    a, b = RuleState(_rule(mode='armed')), RuleState(_tuned({'boot_wait_s': 600}))
    for st in (a, b):
        st.set_clamp(50, expires_at=None)
    da, db = _run(a, [240.0] * 4, actual=None), _run(b, [240.0] * 4, actual=None)
    assert [(d.ts, d.want) for d in _sends(db)] == [(d.ts, d.want) for d in _sends(da)]
    assert _sends(db) and _sends(db)[0].want['value'] == 50


def test_a_queued_urgent_attempt_keeps_its_urgency_without_using_a_retry():
    # review lot C F2: attempts queued behind a long in-flight command never
    # reached the wire — they must not use up the retries, or the want waits
    # the rate limit once the device is free
    st = _urgent_sent({'urgent_retries': 2})
    for _ in range(10):
        assert st.rearm_urgent(queued=True) is True
    assert st._urgent_resends == 0
    assert st.rearm_urgent() is True and st.rearm_urgent() is True and st.rearm_urgent() is False
    # still never for a want that moved on, nor without the key
    st.want = dict(st.want, value=60)
    assert st.rearm_urgent(queued=True) is False
    assert _urgent_sent({}).rearm_urgent(queued=True) is False

# ── parsing is total, validation speaks ──────────────────────────────────────

ODD = [True, False, [], [1], {}, {'x': 1}, 'yes', '', None, math.nan, math.inf, -math.inf, -1, 1e9, '60']


@pytest.mark.parametrize('odd', ODD, ids=repr)
def test_parsing_an_odd_value_never_raises_and_means_off(odd):
    timed = dict(OV, timing={**OV['timing'], 'boot_wait_s': odd, 'urgent_retries': odd})
    for raw in (timed, dict(OV, resume_on_boot=odd), dict(OV, timing=odd), dict(OV, kind='trim', resume_on_boot=odd)):
        r = parse_rule_def(raw)
        validate_rule_def(raw)
        tuning_errors(raw)
        tuning_of(raw)
        assert r.timing.boot_wait_s == 0.0 and r.timing.urgent_retries == 0      # every odd value is off
        assert r.resume_on_boot is (odd is True and raw.get('kind') == 'steps' and 'resume_on_boot' in raw)
    assert tuning_errors(timed)                                # and a save is refused, in words


def test_the_keys_are_judged_on_a_save():
    ok = dict(OV, timing={**OV['timing'], 'boot_wait_s': 60, 'urgent_retries': 3}, resume_on_boot=True)
    assert tuning_errors(ok) == []
    bad = dict(OV, timing={**OV['timing'], 'boot_wait_s': 601, 'urgent_retries': 1.5}, resume_on_boot='yes')
    errs = tuning_errors(bad)
    assert any(e.startswith('timing.boot_wait_s:') for e in errs)
    assert any(e.startswith('timing.urgent_retries:') for e in errs)
    assert any(e.startswith('resume_on_boot:') for e in errs)
    cond = {'id': 'c', 'kind': 'condition', 'resume_on_boot': True}
    assert tuning_errors(cond) == ['resume_on_boot: only a steps rule has it']


def test_to_dict_says_the_keys_only_when_set():
    assert 'resume_on_boot' not in parse_rule_def(OV).to_dict()
    assert 'boot_wait_s' not in parse_rule_def(OV).to_dict()['timing']
    d = parse_rule_def(dict(OV, timing={**OV['timing'], 'boot_wait_s': 60}, resume_on_boot=True)).to_dict()
    assert d['timing']['boot_wait_s'] == 60 and d['resume_on_boot'] is True
