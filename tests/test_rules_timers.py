# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Rules with time: the clock in expressions (hour, weekday, between), a
change that must hold for a while (for_s), the on/off timers (minimum run
and stop time, maximum run time with a rest, starts per hour) and the
counters other rules can read (run_today_h, starts_today…)."""
from datetime import datetime

import pytest

from multibus import clock, expressions
from multibus.rules import RuleState, parse_rule_def, validate_rule_def

PUMP = {
    'id': 'pump', 'mode': 'armed',
    'target': {'device': 'vfd1', 'command': 'run_at'},
    'kind': 'condition', 'when': 'boiler.water_temperature > 55',
    'then': {'params': {'value': 25}}, 'else': {'params': {'value': 0}},
    'stale_after_s': 60, 'on_stale': 'safe',
    'timing': {'every_s': 5, 'debounce': 1, 'min_interval_s': 0, 'reassert_s': 0},
}


def _rule(timing=None, **over):
    raw = dict(PUMP, **over)
    raw['timing'] = dict(PUMP['timing'], **(timing or {}))
    assert validate_rule_def(raw) == [], validate_rule_def(raw)
    r = parse_rule_def(raw)
    r.safe_params = {'value': 0}
    r.tolerance = 0.05
    return r


class _Run:
    """One sample every `dt` seconds; an armed rule whose commands land."""

    def __init__(self, st, dt=5.0, wall0=None):
        self.st, self.t, self.dt, self.actual = st, 0.0, dt, 0
        self.wall0 = wall0

    def feed(self, *truths):
        out = []
        for v in truths:
            wall = None if self.wall0 is None else self.wall0 + self.t
            d = self.st.evaluate(self.t, v, 0.5, actual=self.actual, actual_age_s=1.0, actual_stale_s=60, wall=wall)
            if d.action in ('run', 'reassert'):
                self.actual = d.want['value']
            out.append(d)
            self.t += self.dt
        return out


def _sent(ds):
    return [d.want['value'] for d in ds if d.action in ('run', 'reassert')]


@pytest.fixture
def at():
    """Set the installation's clock: at(2026, 10, 19, 10, 30) in Bucharest."""
    clock.set_timezone('Europe/Bucharest')

    def set_(*ymdhm):
        from zoneinfo import ZoneInfo
        t = datetime(*ymdhm, tzinfo=ZoneInfo('Europe/Bucharest')).timestamp()
        clock.set_now(lambda: t)
        return t
    yield set_
    clock.set_now(None)


# ── the clock in expressions ─────────────────────────────────────────────────

def test_hour_weekday_and_a_window_in_the_installations_timezone(at):
    at(2026, 10, 19, 10, 30)                                    # a Monday, 07:30 UTC
    ev = lambda e: expressions.evaluate(e, lambda n: None)      # noqa: E731
    assert ev('hour()') == 10 and ev('minute()') == 30 and ev('weekday()') == 1
    assert ev('day()') == 19 and ev('month()') == 10
    assert ev('between("10:00", "16:00")') is True and ev('between("10:31", "16:00")') is False
    assert ev('between(10.5, 11)') is True                      # hours as numbers too
    assert ev('between("22:00", "06:00")') is False             # a window across midnight
    at(2026, 10, 19, 23, 15)
    assert ev('between("22:00", "06:00")') is True
    at(2026, 10, 20, 5, 59)
    assert ev('between("22:00", "06:00")') is True and ev('weekday() <= 5') is True


def test_text_is_accepted_only_where_a_time_or_a_rule_id_belongs():
    ok = lambda e: expressions.validate_expression(e)[0]        # noqa: E731
    assert ok('between("10:00", "16:00")') and ok('run_today_h("pool-pump") < 6')
    assert not ok('"abc"') and not ok('between("25:00", "1:00")') and not ok('between("x", "1:00")')
    assert not ok('run_today_h(pool)') and not ok('run_today_h("Pool Pump")') and not ok('max("1:00", 2)')
    assert expressions.validate_expression('between("10:00", "16:00") and pv.power > 3000')[2] == ['pv.power']


def test_a_counter_of_an_unknown_rule_has_no_value():
    expressions.set_rule_stats_provider(lambda rid, stat: 2.5 if rid == 'pool' else None)
    try:
        assert expressions.evaluate('run_today_h("pool") < 6', lambda n: None) is True
        with pytest.raises(expressions.MissingValue):
            expressions.evaluate('run_today_h("other") < 6', lambda n: None)
    finally:
        expressions.set_rule_stats_provider(None)


# ── for_s ────────────────────────────────────────────────────────────────────

def test_a_change_must_hold_for_for_s():
    run = _Run(RuleState(_rule({'for_s': 20})))
    d = run.feed(True, True, True, True, True)                  # 0, 5, 10, 15, 20 s
    assert _sent(d) == [25] and d[4].action == 'run'
    assert 'for 15/20 s towards true' in d[3].reason


def test_a_blip_inside_for_s_starts_the_wait_again():
    run = _Run(RuleState(_rule({'for_s': 20})))
    d = run.feed(True, True, False, *[True] * 6)                 # 0, 5, (10 no), 15 … 40 s
    assert not _sent(d[:7]) and _sent(d) == [25] and d[7].action == 'run'   # 20 s from 15 s, not from 0


# ── on / off timers ──────────────────────────────────────────────────────────

def test_minimum_run_time_holds_a_stop():
    run = _Run(RuleState(_rule({'min_on_s': 60})))
    d = run.feed(True, False, False)                            # on at 0 s, off wanted at 5 s
    assert _sent(d) == [25] and 'minimum run time' in d[1].reason
    d = run.feed(*[False] * 12)
    assert _sent(d) == [0] and run.t >= 60


def test_minimum_stop_time_holds_a_start():
    run = _Run(RuleState(_rule({'min_off_s': 30})))
    run.feed(True, False)                                        # on, then off at 5 s
    d = run.feed(True, True)
    assert not _sent(d) and 'minimum stop time' in d[0].reason
    d = run.feed(*[True] * 6)
    assert _sent(d) == [25]


def test_starts_per_hour_are_limited():
    run = _Run(RuleState(_rule({'max_starts_per_h': 2})))
    d = run.feed(True, False, True, False, True)
    assert _sent(d) == [25, 0, 25, 0] and 'starts in the last hour (max 2)' in d[4].reason


def test_maximum_run_time_forces_a_rest_then_it_may_start_again():
    run = _Run(RuleState(_rule({'max_on_s': 30, 'min_off_s': 20})))
    d = run.feed(*[True] * 14)                                   # 0 … 65 s, always wanted
    assert _sent(d) == [25, 0, 25]
    rest = next(x for x in d if x.state == 'resting')
    assert 'resting 20 s' in rest.reason


def test_the_emergency_path_never_waits_for_a_timer():
    st = RuleState(_rule({'min_on_s': 3600}))
    run = _Run(st)
    run.feed(True)                                               # on
    d = st.evaluate(200.0, None, None, actual=25, actual_age_s=1.0, actual_stale_s=60)
    assert d.state == 'stale' and d.action == 'run' and d.want == {'value': 0}   # stale → safe, at once


def test_timers_without_an_off_value_are_refused():
    raw = dict(PUMP, timing=dict(PUMP['timing'], min_on_s=60))
    raw['else'] = None
    assert any(e.startswith('off_value:') for e in validate_rule_def(raw))
    raw['off_value'] = 0
    assert validate_rule_def(raw) == []
    raw = dict(PUMP, timing=dict(PUMP['timing'], max_on_s=60))
    assert any('min_off_s' in e for e in validate_rule_def(raw))


def test_timers_survive_a_save_and_zero_ones_are_left_out():
    d = parse_rule_def(dict(PUMP, timing=dict(PUMP['timing'], min_on_s=60))).to_dict()
    assert d['timing']['min_on_s'] == 60 and 'max_on_s' not in d['timing'] and 'for_s' not in d['timing']


# ── counters ─────────────────────────────────────────────────────────────────

def test_run_time_and_starts_are_counted_and_today_resets_at_local_midnight(at):
    wall0 = at(2026, 10, 19, 23, 50)
    st = RuleState(_rule())
    run = _Run(st, dt=60.0, wall0=wall0)
    run.feed(*[True] * 6)                                        # on from 23:50 to 23:55
    assert st.starts_today == 1 and st.on_today_s == pytest.approx(300)
    run.feed(*[True] * 6)                                        # past midnight
    assert st.day == '2026-10-20' and st.starts_today == 0 and st.on_today_s < 300
    assert st.on_total_s == pytest.approx(660)
    saved = st.saved_counters()
    again = RuleState(_rule())
    again.restore_counters(saved, '2026-10-20')
    assert again.on_total_s == pytest.approx(660) and again.on_today_s == st.on_today_s
    other_day = RuleState(_rule())
    other_day.restore_counters(saved, '2026-10-21')
    assert other_day.on_today_s == 0 and other_day.on_total_s == pytest.approx(660)


def test_shadow_counts_what_it_would_have_done():
    st = RuleState(_rule(mode='shadow'))
    _Run(st).feed(True, True, False)
    assert st.starts_today == 1


def test_a_bare_off_in_yaml_is_named_and_off_value_survives_the_file(tmp_path):
    import yaml

    from multibus.rules_runtime import RuleStore
    bare = yaml.safe_load("id: p\nkind: trim\noff: 0\n")              # YAML 1.1: {False: 0}
    assert False in bare
    with_bool = dict(PUMP)
    with_bool[False] = 0
    assert any('off_value' in e and 'boolean' in e for e in validate_rule_def(with_bool))
    store = RuleStore(tmp_path / "rules.yaml")
    store.save([dict(PUMP, off_value=0, timing=dict(PUMP['timing'], min_on_s=60))])
    (back,) = store.load()
    assert back['off_value'] == 0 and validate_rule_def(back) == []
    assert parse_rule_def(back).off_value() == 0
