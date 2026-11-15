# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""RULE-RB-1: "the read-back already matches, nothing to send" only for a
read-back sampled AFTER the rule's last write.

Prod 2026-11-11: 10:53:32 ov-u4 sent 70 %; at 10:54:18 it wanted 80 % again,
found the read-back still saying 80 % (polled before the 70 % landed, young
enough to be "fresh"), sent nothing — and the inverter sat at 70 % for 120 s
until the closed loop's re-check. The mirror case (a step back UP, the old
read-back matching the more protective value) left the unit under-protected
for the same 120 s."""
import threading

from multibus.rules import RuleState
from tests.test_rules import _rule

STALE_S = 3 * 3600.0          # a `controls` read-back is fresh for 3 h on prod


def _tick(st, t, v, rb_value, rb_ts):
    """One evaluation at t: the signal sampled 0.5 s ago, the read-back
    sampled at rb_ts (None = no read-back)."""
    age = None if rb_ts is None else t - rb_ts
    return st.evaluate(t, v, 0.5, actual=rb_value, actual_age_s=age, actual_stale_s=STALE_S)


def _settle(st):
    """The rule sits at Warning (80 %), sent and confirmed, the device read
    back at 80 % at t=0."""
    for t in (0.0, 2.0, 4.0):
        d = _tick(st, t, 250.4, None, None)
    assert d.action == 'run' and d.want['value'] == 80
    st.confirmed = True


def _sends(ds):
    return [d for d in ds if d.action in ('run', 'reassert')]


def test_the_2026_11_11_sequence_sends_80_again():
    st = RuleState(_rule(mode='armed'))
    _settle(st)
    # 40..44 s: the signal climbs to Moderate — 70 % goes out at 44 s; the
    # last read-back (80 %) was polled at 10 s, before that write
    ds = [_tick(st, t, 251.3, 80, 10.0) for t in (40.0, 42.0, 44.0)]
    assert _sends(ds)[-1].want['value'] == 70 and st.last_cmd_ts == 44.0
    # 86..90 s: back down to Warning; the read-back is still the 10 s poll
    ds = [_tick(st, t, 250.4, 80, 10.0) for t in (86.0, 88.0, 90.0)]
    assert not any('already matches' in d.reason for d in ds)
    assert [d.want['value'] for d in _sends(ds)] == [80]


def test_a_read_back_taken_after_the_write_still_saves_the_send():
    st = RuleState(_rule(mode='armed'))
    _settle(st)
    [_tick(st, t, 251.3, 80, 10.0) for t in (40.0, 42.0, 44.0)]      # 70 % sent at 44 s
    # the device was read at 50 s, AFTER the write: it holds 80 % (the
    # write did not take, or another face put it back) — the rule wants 80
    ds = [_tick(st, t, 250.4, 80, 50.0) for t in (86.0, 88.0, 90.0)]
    assert not _sends(ds)
    assert 'read-back already matches' in ds[-1].reason and st.commanded['value'] == 80 and st.confirmed


def test_the_mirror_case_a_step_back_up_is_never_left_on_the_old_read_back():
    """Under-protection: Moderate (70 %) read back, a step down to Warning
    (80 %) sent, then the voltage climbs again — the old 70 % read-back must
    not stand in for the write the device needs."""
    st = RuleState(_rule(mode='armed'))
    _settle(st)
    [_tick(st, t, 251.3, 80, 10.0) for t in (40.0, 42.0, 44.0)]      # 70 % sent at 44 s
    st.confirmed = True
    [_tick(st, t, 250.4, 70, 50.0) for t in (80.0, 82.0, 84.0)]      # 80 % sent at 84 s (read 70 at 50 s)
    assert st.commanded['value'] == 80 and st.last_cmd_ts == 84.0
    ds = [_tick(st, t, 251.3, 70, 50.0) for t in (120.0, 122.0, 124.0)]
    assert not any('already matches' in d.reason for d in ds)
    assert [d.want['value'] for d in _sends(ds)] == [70]


def test_a_read_back_taken_while_the_write_was_in_flight_does_not_count():
    """The runtime raises readback_floor until the write answered: a poll
    between the decision and the answer still shows the old value."""
    st = RuleState(_rule(mode='armed'))
    _settle(st)
    [_tick(st, t, 251.3, 80, 10.0) for t in (40.0, 42.0, 44.0)]      # 70 % decided at 44 s
    st.readback_floor = 60.0                                        # it answered at 60 s
    ds = [_tick(st, t, 250.4, 80, 50.0) for t in (86.0, 88.0, 90.0)]  # read at 50 s: mid-flight
    assert [d.want['value'] for d in _sends(ds)] == [80]


def test_a_read_back_of_unknown_age_after_a_write_cannot_prove_it():
    st = RuleState(_rule(mode='armed'))
    _settle(st)
    [_tick(st, t, 251.3, 80, 10.0) for t in (40.0, 42.0, 44.0)]
    ds = [st.evaluate(t, 250.4, 0.5, actual=80, actual_age_s=None, actual_stale_s=STALE_S)
          for t in (86.0, 88.0, 90.0)]
    assert [d.want['value'] for d in _sends(ds)] == [80]


def test_before_any_write_a_matching_read_back_still_saves_the_first_send():
    """A restart under release with the device already at 100 %: no write
    since load, so the read-back counts whenever it was taken (no change)."""
    st = RuleState(_rule(mode='armed'))
    ds = [_tick(st, t, 240.0, 100, -500.0) for t in (0.0, 2.0, 4.0)]
    assert not _sends(ds) and 'already matches' in ds[-1].reason


# ── the runtime: the floor follows the write's flight and its answer ─────────

from tests.test_commands import _Inverter                       # noqa: E402
from tests.test_devices_api import needs_tc                     # noqa: E402
from tests.test_rules_api import _feed, _rule_app               # noqa: E402

HIGH = {'voltage_l1_n': 251.6, 'voltage_l2_n': 230, 'voltage_l3_n': 231, 'power_limit_pct': 100}


@needs_tc
def test_the_runtime_raises_the_floor_while_the_write_flies_and_when_it_answers(tmp_path):
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    gate = threading.Event()
    rt._writer('pv-u1').submit(gate.wait, 10)            # the device's writer is busy
    for _ in range(3):
        _feed(app, clock, 'pv-u1', HIGH)
        rt.tick(clock())
        clock.t += 2
    st = rt.states[('ov-u1', 'pv-u1')]
    sent_at = st.last_cmd_ts
    assert sent_at is not None and st.readback_floor is None
    clock.t += 5
    _feed(app, clock, 'pv-u1', HIGH)
    rt.tick(clock())                                     # still in flight
    assert st.readback_floor == clock()
    gate.set()
    rt._writer('pv-u1').submit(lambda: None).result(10)
    clock.t += 7
    rt._settle()                                         # the answer
    assert st.readback_floor == clock() and [w[1][0] for w in inv.writes] == [7000]
