# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Phase 2 of rules with time: a schedule (days and times → a value, the
last entry reached is in force) and forcing a value for a while ("run for
an hour") before the rule decides again."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from multibus import clock
from multibus.rules import (RuleState, _days_of, current_entry, days_text, parse_rule_def,
                            validate_rule_def)

TZ = ZoneInfo('Europe/Bucharest')
WEEK = {
    'id': 'pump-week', 'mode': 'armed',
    'target': {'device': 'vfd1', 'command': 'run_at'},
    'kind': 'schedule',
    'schedule': [{'days': 'Mon-Fri', 'at': '06:00', 'value': 25},
                 {'days': 'Mon-Fri', 'at': '22:00', 'value': 0},
                 {'days': 'Sat,Sun', 'at': '08:00', 'value': 20, 'label': 'weekend'},
                 {'days': [6, 7], 'at': '23:00', 'value': 0}],
    'stale_after_s': 60,
    'timing': {'every_s': 10, 'debounce': 1, 'min_interval_s': 0, 'reassert_s': 0},
}


def _rule(**over):
    raw = dict(WEEK, **over)
    assert validate_rule_def(raw) == [], validate_rule_def(raw)
    r = parse_rule_def(raw)
    r.safe_params = {'value': 0}
    r.tolerance = 0.05
    return r


def _at(*ymdhm):
    return datetime(*ymdhm, tzinfo=TZ).timestamp()


@pytest.fixture(autouse=True)
def _tz():
    clock.set_timezone('Europe/Bucharest')
    yield


def _ask(st, wall, signal=True, actual=0, t=None):
    return st.evaluate(t if t is not None else wall, signal, 0.0, actual=actual, actual_age_s=1.0,
                       actual_stale_s=60, wall=wall)


# ── days and the entry in force ──────────────────────────────────────────────

def test_days_are_read_as_people_write_them():
    assert _days_of('Mon-Fri') == [1, 2, 3, 4, 5] and _days_of('1-5') == [1, 2, 3, 4, 5]
    assert _days_of('Sat,Sun') == [6, 7] and _days_of([7, 6]) == [6, 7] and _days_of('all') == list(range(1, 8))
    assert _days_of(None) == list(range(1, 8)) and _days_of('luni') is None and _days_of('Fri-Mon') is None
    assert _days_of([0]) is None and _days_of([True]) is None
    assert days_text([1, 2, 3, 4, 5]) == 'Mon-Fri' and days_text([1, 3, 6, 7]) == 'Mon,Wed,Sat-Sun'


def test_the_last_entry_reached_is_in_force_even_across_the_week():
    entries = _rule().schedule
    at = lambda *a: current_entry(entries, datetime(*a, tzinfo=TZ))   # noqa: E731
    assert at(2026, 10, 19, 7, 0)['value'] == 25          # Monday 07:00
    assert at(2026, 10, 19, 22, 0)['value'] == 0          # Monday 22:00, at the minute
    assert at(2026, 10, 19, 5, 0)['value'] == 0           # Monday 05:00: Sunday 23:00 still holds
    assert at(2026, 10, 24, 9, 0)['label'] == 'weekend'   # Saturday 09:00
    assert at(2026, 10, 24, 7, 0)['value'] == 0           # Saturday 07:00: Friday 22:00 still holds


# ── the schedule as a rule ───────────────────────────────────────────────────

def test_a_schedule_asks_for_the_entry_in_force_and_names_it():
    st = RuleState(_rule())
    d = _ask(st, _at(2026, 10, 19, 7, 0))
    assert d.action == 'run' and d.want == {'value': 25} and d.state == 'Mon-Fri 06:00'
    d = _ask(st, _at(2026, 10, 19, 22, 1), actual=25)
    assert d.action == 'run' and d.want == {'value': 0} and d.state == 'Mon-Fri 22:00'
    d = _ask(st, _at(2026, 10, 24, 9, 0), actual=0)
    assert d.want == {'value': 20} and d.state == 'weekend'


def test_when_false_asks_for_off_value():
    st = RuleState(_rule(when='tank.temperature < 60', off_value=0))
    assert _ask(st, _at(2026, 10, 19, 7, 0), signal=True).want == {'value': 25}
    d = _ask(st, _at(2026, 10, 19, 7, 1), signal=False, actual=25)
    assert d.want == {'value': 0} and d.state == 'off (when is false)'


def test_a_schedule_is_checked_in_words():
    def errs(**over):
        return validate_rule_def(dict(WEEK, **over))
    assert any('at least one entry' in e for e in errs(schedule=[]))
    assert any('schedule[0].at' in e for e in errs(schedule=[{'days': 'all', 'at': '25:00', 'value': 1}]))
    assert any('schedule[0].at' in e for e in errs(schedule=[{'days': 'all', 'at': '24:00', 'value': 1}]))
    assert any('schedule[0].days' in e for e in errs(schedule=[{'days': 'luni', 'at': '06:00', 'value': 1}]))
    assert any('numeric `value`' in e for e in errs(schedule=[{'at': '06:00'}]))
    assert any(e.startswith('off_value:') for e in errs(when='tank.temperature < 60'))
    d = parse_rule_def(WEEK).to_dict()
    assert d['schedule'][2] == {'days': [6, 7], 'at': '08:00', 'value': 20.0, 'label': 'weekend'}


# ── forcing ──────────────────────────────────────────────────────────────────

def test_a_force_wins_over_the_schedule_then_the_rule_decides_again():
    st = RuleState(_rule())
    w0 = _at(2026, 10, 19, 23, 0)                         # the schedule says off
    _ask(st, w0)
    st.force({'value': 30}, w0 + 3600)
    d = _ask(st, w0 + 10)
    assert d.action == 'run' and d.want == {'value': 30} and d.state == 'forced' and 'forced for' in d.reason
    d = _ask(st, w0 + 3601, actual=30)                    # the hour is over
    assert st.forced is None and d.action == 'run' and d.want == {'value': 0}


def test_a_force_is_capped_by_the_ceiling_and_held_by_the_motor_timers():
    st = RuleState(_rule(timing=dict(WEEK['timing'], min_off_s=600), off_value=0))
    st.set_clamp(25, None)
    w0 = _at(2026, 10, 19, 21, 59)
    _ask(st, w0, actual=0)
    _ask(st, w0 + 120, actual=25)                         # 22:01 → off, the motor stops
    st.force({'value': 40}, w0 + 7200)
    d = _ask(st, w0 + 180, actual=0)
    assert d.action == 'hold' and 'minimum stop time' in d.reason and d.want == {'value': 25}   # capped at 25
    d = _ask(st, w0 + 800, actual=0)
    assert d.action == 'run' and d.want == {'value': 25}


def test_a_force_can_be_ended_early():
    st = RuleState(_rule())
    w0 = _at(2026, 10, 19, 23, 0)
    _ask(st, w0)
    st.force({'value': 30}, w0 + 3600)
    _ask(st, w0 + 10)
    st.force({}, None)
    assert st.forced is None
    d = _ask(st, w0 + 20, actual=30)
    assert d.want == {'value': 0}
