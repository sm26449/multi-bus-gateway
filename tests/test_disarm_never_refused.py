# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Leaving armed is never refused, and it releases what the rule drove.

An armed rule that can no longer run — a device its signal reads went away,
its target went away, its command was disabled — sits in `errors`, is not
evaluated, and used to be stuck: shadow and disable were refused (its own
checks failed), delete released nothing (it had no safe values), and a
controller's disable over MQTT failed in silence. The inverter stayed at the
last limit the rule sent.

A release never writes over another armed rule that holds the same channel,
and never promises a write its target would refuse.
"""
import json

import pytest

from multibus import expressions
from tests.test_commands import _Inverter
from tests.test_commands_api import _Mqtt
from tests.test_endpoints import needs_tc
from tests.test_rules import OV
from tests.test_rules_api import _feed, _rule_app

H = dict(OV, id='h', mode='armed',
         signal='max(pv-u2.voltage_l1_n, pv-u2.voltage_l2_n, pv-u2.voltage_l3_n)')


@pytest.fixture
def ids():
    before = expressions._device_ids
    yield expressions.set_device_ids_provider
    expressions.set_device_ids_provider(before)


def _driven(tmp_path, ids, mq=None, rule=None):
    """`h` armed on pv-u1, reading pv-u2, holding pv-u1 at 60 %."""
    inv, other = _Inverter(sf=-2), _Inverter(sf=-2)
    ids(lambda: {'umg512', 'pv-u1', 'pv-u2', 'pv'})
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv, 'pv-u2': other}, mqtt=mq)
    assert client.post("/api/rules", json=rule or H).status_code == 200
    for _ in range(3):
        _feed(app, clock, 'pv-u2', {'voltage_l1_n': 252.6, 'voltage_l2_n': 230, 'voltage_l3_n': 231})
        _feed(app, clock, 'pv-u1', {'power_limit_pct': inv.mem[40232] / 100})
        rt.tick(clock()); clock.t += 2
    assert inv.writes[-1][1][0] == 6000                            # it drives pv-u1
    rt.alerts = []
    rt._alert = lambda sev, key, msg: rt.alerts.append((sev, key, msg))
    return inv, (cfg, app), client, clock, rt


def _pv_u2_goes_away(ids, rt):
    ids(lambda: {'umg512', 'pv-u1', 'pv'})
    rt.load()
    assert 'h' in rt.errors and rt.rules['h'].mode == 'armed'


@needs_tc
def test_a_rule_whose_signal_device_went_away_can_be_put_in_shadow_and_releases(tmp_path, ids):
    inv, app, client, clock, rt = _driven(tmp_path, ids)
    _pv_u2_goes_away(ids, rt)
    # its safe values come from the command its target still offers
    assert rt.rules['h'].safe_params == {'value': 100, 'revert_s': 0}
    r = client.post("/api/rules/h/mode", json={'mode': 'shadow'})
    assert r.status_code == 200, r.text
    assert r.json()['releasing'] == ['pv-u1'] and rt.rules['h'].mode == 'shadow'
    assert rt.wait_released(10)
    assert inv.writes[-1][1][0] == 10000                           # back to safe
    # arming it again stays refused, in words
    r = client.post("/api/rules/h/mode", json={'mode': 'armed'})
    assert r.status_code == 422 and 'pv-u2' in r.text


@needs_tc
def test_the_same_rule_can_be_disabled_and_enabling_it_stays_refused(tmp_path, ids):
    inv, app, client, clock, rt = _driven(tmp_path, ids)
    _pv_u2_goes_away(ids, rt)
    r = client.post("/api/rules/h/enable", json={'enabled': False})
    assert r.status_code == 200, r.text
    assert r.json()['releasing'] == ['pv-u1'] and rt.rules['h'].enabled is False
    assert rt.wait_released(10) and inv.writes[-1][1][0] == 10000
    r = client.post("/api/rules/h/enable", json={'enabled': True})
    assert r.status_code == 422


@needs_tc
def test_a_controller_disabling_it_over_mqtt_is_obeyed(tmp_path, ids):
    mq = _Mqtt()
    inv, app, client, clock, rt = _driven(tmp_path, ids, mq=mq)
    _pv_u2_goes_away(ids, rt)
    mq.topics['mbg/rules/h/set'](json.dumps({'enabled': False, 'source': 'ems'}))
    assert rt.rules['h'].enabled is False
    assert rt.wait_released(10) and inv.writes[-1][1][0] == 10000


@needs_tc
def test_deleting_it_releases_its_target(tmp_path, ids):
    inv, app, client, clock, rt = _driven(tmp_path, ids)
    _pv_u2_goes_away(ids, rt)
    r = client.delete("/api/rules/h")
    assert r.status_code == 200 and r.json()['releasing'] == ['pv-u1']
    assert rt.wait_released(10) and inv.writes[-1][1][0] == 10000


@needs_tc
def test_a_target_that_went_away_is_said_and_alerted_not_released_in_silence(tmp_path, ids):
    inv, app, client, clock, rt = _driven(tmp_path, ids)
    find = rt._find_device
    rt._find_device = lambda d: (None, None, None) if d == 'pv-u1' else find(d)
    rt.load()
    assert 'h' in rt.errors
    n = len(inv.writes)
    r = client.post("/api/rules/h/mode", json={'mode': 'shadow'})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['releasing'] == [] and 'pv-u1 is not here' in d['release_warning']
    assert rt.rules['h'].mode == 'shadow' and len(inv.writes) == n
    # an EMS hears it, not only the event log
    assert [a for a in rt.alerts if a[1] == 'rule:h:release']


@needs_tc
def test_a_disabled_command_is_not_promised_a_release_it_would_refuse(tmp_path, ids):
    inv, app, client, clock, rt = _driven(tmp_path, ids)
    offer = rt._commands_for

    def off(cfg):
        return [dict(c, enabled=False) if c['def'].name == 'power_limit' else c for c in offer(cfg)]
    rt._commands_for = off
    rt.load()
    assert 'not enabled' in rt.errors['h'] and rt.rules['h'].safe_params == {}
    n = len(inv.writes)
    r = client.post("/api/rules/h/enable", json={'enabled': False})
    assert r.status_code == 200, r.text
    assert r.json()['releasing'] == [] and 'cannot be sent' in r.json()['release_warning']
    assert rt.wait_released(10) and len(inv.writes) == n


@needs_tc
def test_a_rule_broken_since_start_with_no_target_has_no_invented_safe_values(tmp_path, ids):
    inv, app, client, clock, rt = _driven(tmp_path, ids)
    rt._find_device = lambda d: (None, None, None)
    rt.load()
    assert rt.rules['h'].safe_params == {}
    r = client.delete("/api/rules/h")
    assert r.status_code == 200 and r.json()['releasing'] == []
    assert 'is not here' in r.json()['release_warning']


@needs_tc
def test_on_disable_hold_leaves_the_target_and_says_nothing_more(tmp_path, ids):
    inv, app, client, clock, rt = _driven(tmp_path, ids, rule=dict(H, on_disable='hold'))
    _pv_u2_goes_away(ids, rt)
    n = len(inv.writes)
    r = client.post("/api/rules/h/mode", json={'mode': 'shadow'})
    assert r.status_code == 200, r.text
    assert r.json()['releasing'] == [] and 'release_warning' not in r.json()
    assert rt.wait_released(10) and len(inv.writes) == n and not rt.alerts


@needs_tc
def test_a_note_belongs_to_the_answer_that_made_it(tmp_path, ids):
    mq = _Mqtt()
    inv, app, client, clock, rt = _driven(tmp_path, ids, mq=mq)
    find = rt._find_device
    rt._find_device = lambda d: (None, None, None) if d == 'pv-u1' else find(d)
    rt.load()
    mq.topics['mbg/rules/h/set'](json.dumps({'enabled': False, 'source': 'ems'}))
    assert rt.rules['h'].enabled is False
    # the device comes back; a later, unrelated answer carries no old note
    rt._find_device = find
    rt.load()
    r = client.post("/api/rules/h/mode", json={'mode': 'shadow'})
    assert r.status_code == 200 and 'release_warning' not in r.json()


@needs_tc
def test_with_writes_off_the_release_is_tried_and_refused_like_any_write(tmp_path, ids):
    inv, (cfg, app), client, clock, rt = _driven(tmp_path, ids)
    cfg.security.allow_writes = False
    n = len(inv.writes)
    r = client.post("/api/rules/h/mode", json={'mode': 'shadow'})
    assert r.status_code == 200 and r.json()['releasing'] == ['pv-u1']
    assert rt.wait_released(10) and len(inv.writes) == n           # nothing on the wire


@needs_tc
def test_a_group_with_one_unit_gone_releases_the_units_still_there(tmp_path, ids):
    a, b = _Inverter(sf=-2), _Inverter(sf=-2)
    ids(lambda: {'umg512', 'pv-u1', 'pv-u2', 'pv'})
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': a, 'pv-u2': b})
    g = dict(OV, id='g', mode='armed', target={'endpoint': 'pv', 'group': 'inverters', 'command': 'power_limit'},
             signal='max(pv-u1.voltage_l1_n, pv-u2.voltage_l1_n)')
    assert client.post("/api/rules", json=g).status_code == 200, client.post("/api/rules", json=g).text
    for _ in range(3):
        for d, inv in (('pv-u1', a), ('pv-u2', b)):
            _feed(app, clock, d, {'voltage_l1_n': 252.6, 'power_limit_pct': inv.mem[40232] / 100})
        rt.tick(clock()); clock.t += 2
    assert a.writes[-1][1][0] == 6000 and b.writes[-1][1][0] == 6000
    units = rt._endpoint_devices
    rt._endpoint_devices = lambda ep: [c for c in units(ep) if c.id != 'pv-u2']
    rt.load()
    nb = len(b.writes)
    r = client.post("/api/rules/g/mode", json={'mode': 'shadow'})
    assert r.status_code == 200 and r.json()['releasing'] == ['pv-u1']
    assert rt.wait_released(10) and a.writes[-1][1][0] == 10000 and len(b.writes) == nb


@needs_tc
def test_a_release_never_writes_over_a_rule_that_holds_the_channel(tmp_path, ids):
    inv, (cfg, app), client, clock, rt = _driven(tmp_path, ids)
    # k: a higher layer on the same command of pv-u1, at Emergency (50 %)
    k = dict(OV, id='k', mode='armed', layer='manual')
    assert client.post("/api/rules", json=k).status_code == 200
    for _ in range(4):
        _feed(app, clock, 'pv-u1', {'voltage_l1_n': 253.6, 'voltage_l2_n': 230, 'voltage_l3_n': 231,
                                    'power_limit_pct': inv.mem[40232] / 100})
        _feed(app, clock, 'pv-u2', {'voltage_l1_n': 252.6, 'voltage_l2_n': 230, 'voltage_l3_n': 231})
        rt.tick(clock()); clock.t += 2
    assert inv.writes[-1][1][0] == 5000                            # k holds the channel
    n = len(inv.writes)
    r = client.post("/api/rules/h/mode", json={'mode': 'shadow'})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['releasing'] == [] and 'pv-u1 left to rule k' in d['release_warning']
    assert rt.wait_released(10) and len(inv.writes) == n and inv.mem[40232] == 5000


@needs_tc
def test_one_put_that_disarms_and_retargets_releases_the_old_target(tmp_path, ids):
    inv, app, client, clock, rt = _driven(tmp_path, ids)
    r = client.put("/api/rules/h", json=dict(H, mode='shadow', target={'device': 'pv-u2', 'command': 'power_limit'}))
    assert r.status_code == 200, r.text
    assert r.json()['releasing'] == ['pv-u1']
    assert rt.rules['h'].mode == 'shadow' and rt.rules['h'].target['device'] == 'pv-u2'
    assert rt.wait_released(10) and inv.writes[-1][1][0] == 10000
    # retargeting while staying armed is still refused
    r = client.put("/api/rules/ov-u1", json=dict(OV, mode='armed'))
    assert r.status_code == 200
    r = client.put("/api/rules/ov-u1", json=dict(OV, mode='armed', target={'device': 'pv-u2', 'command': 'power_limit'}))
    assert r.status_code == 422 and 'put it in shadow' in r.text


@needs_tc
def test_a_healthy_rule_leaves_armed_through_its_checks_as_before(tmp_path, ids):
    inv, app, client, clock, rt = _driven(tmp_path, ids)
    rt._leave_unchecked = lambda *a, **k: pytest.fail("a healthy rule must not skip its checks")
    r = client.post("/api/rules/h/mode", json={'mode': 'shadow'})
    assert r.status_code == 200 and r.json()['releasing'] == ['pv-u1'] and 'release_warning' not in r.json()
    assert rt.wait_released(10) and inv.writes[-1][1][0] == 10000


# ── after a restart: what the device holds decides the release (REL-BOOT) ──
# A gateway that restarts keeps no memory of what a rule sent: every unit
# starts with nothing commanded. Leaving armed then released nothing, and an
# inverter the rule had limited before the restart stayed limited.

def _restart(rt):
    """What a new process knows: the rules, nothing commanded yet."""
    rt.states.clear()
    rt.load()
    assert all(st.commanded is None for st in rt.states.values())


def _holds(app, clock, dev, inv):
    _feed(app, clock, dev, {'power_limit_pct': inv.mem[40232] / 100})


@needs_tc
@pytest.mark.parametrize('leave', ['shadow', 'disable', 'delete', 'mqtt'])
def test_after_a_restart_leaving_armed_releases_what_the_device_still_holds(tmp_path, ids, leave):
    mq = _Mqtt()
    inv, (cfg, app), client, clock, rt = _driven(tmp_path, ids, mq=mq)
    _restart(rt)
    _holds(app, clock, 'pv-u1', inv)                               # read back: 60 %
    if leave == 'shadow':
        r = client.post("/api/rules/h/mode", json={'mode': 'shadow'})
    elif leave == 'disable':
        r = client.post("/api/rules/h/enable", json={'enabled': False})
    elif leave == 'delete':
        r = client.delete("/api/rules/h")
    else:
        mq.topics['mbg/rules/h/set'](json.dumps({'enabled': False, 'source': 'ems'}))
        r = None
    if r is not None:
        assert r.status_code == 200 and r.json()['releasing'] == ['pv-u1'], r.text
    assert rt.wait_released(10) and inv.writes[-1][1][0] == 10000  # back to safe


@needs_tc
def test_after_a_restart_a_rule_in_errors_releases_what_the_device_holds(tmp_path, ids):
    inv, (cfg, app), client, clock, rt = _driven(tmp_path, ids)
    ids(lambda: {'umg512', 'pv-u1', 'pv'})                         # pv-u2 gone across the restart
    _restart(rt)
    assert 'h' in rt.errors
    _holds(app, clock, 'pv-u1', inv)
    r = client.post("/api/rules/h/mode", json={'mode': 'shadow'})
    assert r.status_code == 200 and r.json()['releasing'] == ['pv-u1'], r.text
    assert rt.wait_released(10) and inv.writes[-1][1][0] == 10000


@needs_tc
def test_after_a_restart_a_device_already_at_its_safe_values_is_not_written(tmp_path, ids):
    inv, (cfg, app), client, clock, rt = _driven(tmp_path, ids)
    _restart(rt)
    inv.mem[40232] = 10000                                         # it reverted on its own
    _holds(app, clock, 'pv-u1', inv)
    n = len(inv.writes)
    r = client.post("/api/rules/h/mode", json={'mode': 'shadow'})
    assert r.status_code == 200 and r.json()['releasing'] == []
    assert rt.wait_released(10) and len(inv.writes) == n


@needs_tc
def test_after_a_restart_an_unknown_device_state_is_released_to_be_sure(tmp_path, ids):
    inv, (cfg, app), client, clock, rt = _driven(tmp_path, ids)
    _restart(rt)
    app.state.registry.ensure_store('pv-u1').clear()               # nothing read back yet
    r = client.post("/api/rules/h/mode", json={'mode': 'shadow'})
    assert r.status_code == 200 and r.json()['releasing'] == ['pv-u1'], r.text
    assert rt.wait_released(10) and inv.writes[-1][1][0] == 10000


@needs_tc
def test_after_a_restart_on_disable_hold_still_sends_nothing(tmp_path, ids):
    inv, (cfg, app), client, clock, rt = _driven(tmp_path, ids, rule=dict(H, on_disable='hold'))
    _restart(rt)
    _holds(app, clock, 'pv-u1', inv)
    n = len(inv.writes)
    r = client.post("/api/rules/h/mode", json={'mode': 'shadow'})
    assert r.status_code == 200 and r.json()['releasing'] == []
    assert rt.wait_released(10) and len(inv.writes) == n


@needs_tc
def test_after_a_restart_writes_off_refuse_the_release_like_any_write(tmp_path, ids):
    inv, (cfg, app), client, clock, rt = _driven(tmp_path, ids)
    _restart(rt)
    _holds(app, clock, 'pv-u1', inv)
    cfg.security.allow_writes = False
    n = len(inv.writes)
    r = client.post("/api/rules/h/mode", json={'mode': 'shadow'})
    assert r.status_code == 200 and r.json()['releasing'] == ['pv-u1']
    assert rt.wait_released(10) and len(inv.writes) == n           # nothing on the wire


@needs_tc
def test_after_a_restart_a_release_never_writes_over_a_rule_that_holds_the_channel(tmp_path, ids):
    inv, (cfg, app), client, clock, rt = _driven(tmp_path, ids)
    k = dict(OV, id='k', mode='armed', layer='manual')
    assert client.post("/api/rules", json=k).status_code == 200
    _restart(rt)
    for _ in range(4):
        _feed(app, clock, 'pv-u1', {'voltage_l1_n': 253.6, 'voltage_l2_n': 230, 'voltage_l3_n': 231,
                                    'power_limit_pct': inv.mem[40232] / 100})
        _feed(app, clock, 'pv-u2', {'voltage_l1_n': 230, 'voltage_l2_n': 230, 'voltage_l3_n': 231})
        rt.tick(clock()); clock.t += 2
    assert inv.writes[-1][1][0] == 5000                            # k holds the channel
    n = len(inv.writes)
    r = client.post("/api/rules/h/mode", json={'mode': 'shadow'})
    assert r.status_code == 200 and r.json()['releasing'] == []
    assert 'pv-u1 left to rule k' in r.json()['release_warning']
    assert rt.wait_released(10) and len(inv.writes) == n


@needs_tc
def test_after_a_restart_writes_off_are_said_in_the_answer_and_alerted(tmp_path, ids):
    inv, (cfg, app), client, clock, rt = _driven(tmp_path, ids)
    _restart(rt)
    _holds(app, clock, 'pv-u1', inv)
    cfg.security.allow_writes = False
    r = client.post("/api/rules/h/mode", json={'mode': 'shadow'})
    assert r.status_code == 200 and 'not sent back to safe' in r.json()['release_warning']
    assert [a for a in rt.alerts if a[1] == 'rule:h:release']


@needs_tc
def test_disarming_while_it_resumes_in_the_dead_band_after_a_restart_releases(tmp_path, ids):
    # review scenario B: 249 V after the restart, read-back 60, two of three samples in
    inv, (cfg, app), client, clock, rt = _driven(tmp_path, ids)
    _restart(rt)
    for _ in range(2):
        _feed(app, clock, 'pv-u2', {'voltage_l1_n': 249.0, 'voltage_l2_n': 230, 'voltage_l3_n': 231})
        _feed(app, clock, 'pv-u1', {'power_limit_pct': 60})
        rt.tick(clock()); clock.t += 2
    r = client.post("/api/rules/h/enable", json={'enabled': False})
    assert r.status_code == 200 and r.json()['releasing'] == ['pv-u1'], r.text
    assert rt.wait_released(10) and inv.writes[-1][1][0] == 10000


@needs_tc
def test_a_send_that_failed_is_not_taken_as_done_by_the_release(tmp_path, ids):
    # review scenario D, no restart: it wants normal 100, every write fails,
    # the inverter stays at 60 % — the release must not believe the 100
    inv, (cfg, app), client, clock, rt = _driven(tmp_path, ids)
    inv.write_ok = False
    for _ in range(20):
        _feed(app, clock, 'pv-u2', {'voltage_l1_n': 240.0, 'voltage_l2_n': 230, 'voltage_l3_n': 231})
        _feed(app, clock, 'pv-u1', {'power_limit_pct': inv.mem[40232] / 100})
        rt.tick(clock()); clock.t += 2
    st = rt.states[('h', 'pv-u1')]
    assert st.commanded and st.commanded['value'] == 100 and not st.confirmed and inv.mem[40232] == 6000
    inv.write_ok = True
    r = client.post("/api/rules/h/mode", json={'mode': 'shadow'})
    assert r.status_code == 200 and r.json()['releasing'] == ['pv-u1'], r.text
    assert rt.wait_released(10) and inv.mem[40232] == 10000


@needs_tc
def test_a_confirmed_safe_send_with_no_read_back_needs_no_release(tmp_path, ids):
    # the rule sent 100 and the device answered; nothing reads it back now
    inv, (cfg, app), client, clock, rt = _driven(tmp_path, ids)
    for _ in range(25):
        _feed(app, clock, 'pv-u2', {'voltage_l1_n': 240.0, 'voltage_l2_n': 230, 'voltage_l3_n': 231})
        _feed(app, clock, 'pv-u1', {'power_limit_pct': inv.mem[40232] / 100})
        rt.tick(clock()); clock.t += 2
    assert inv.mem[40232] == 10000 and rt.states[('h', 'pv-u1')].confirmed
    app.state.registry.ensure_store('pv-u1').clear()
    n = len(inv.writes)
    r = client.post("/api/rules/h/mode", json={'mode': 'shadow'})
    assert r.status_code == 200 and r.json()['releasing'] == []
    assert rt.wait_released(10) and len(inv.writes) == n


def _normal(app, clock, rt, inv, n=25):
    """The rule took pv-u1 back to normal (100 %, answered)."""
    for _ in range(n):
        _feed(app, clock, 'pv-u2', {'voltage_l1_n': 240.0, 'voltage_l2_n': 230, 'voltage_l3_n': 231})
        _feed(app, clock, 'pv-u1', {'power_limit_pct': inv.mem[40232] / 100})
        rt.tick(clock()); clock.t += 2
    assert inv.mem[40232] == 10000


def _emergency(app, clock, rt, ticks=4, readback=100.0):
    for _ in range(ticks):
        _feed(app, clock, 'pv-u2', {'voltage_l1_n': 253.5, 'voltage_l2_n': 230, 'voltage_l3_n': 231})
        _feed(app, clock, 'pv-u1', {'power_limit_pct': readback})   # the poller still reads the old value
        rt.tick(clock()); clock.t += 2


@needs_tc
def test_a_curtail_still_on_its_way_is_followed_by_the_release(tmp_path, ids, monkeypatch):
    import threading

    import multibus.rules_runtime as rr
    inv, (cfg, app), client, clock, rt = _driven(tmp_path, ids)
    _normal(app, clock, rt, inv)
    gate, real = threading.Event(), inv.write

    def slow(*a, **k):
        gate.wait(20)
        return real(*a, **k)
    inv.write = slow
    monkeypatch.setattr(rr, 'COMMAND_WAIT_S', 0.1)
    _emergency(app, clock, rt)
    assert rt._inflight_for('pv-u1')                               # 50 % on its way
    r = client.post("/api/rules/h/mode", json={'mode': 'shadow'})
    assert r.json()['releasing'] == ['pv-u1'], r.text              # a read-back of 100 says nothing yet
    gate.set()
    assert rt.wait_released(20) and inv.mem[40232] == 10000        # safe after the curtail, on the same writer


@needs_tc
def test_a_curtail_sent_but_not_verified_is_followed_by_the_release(tmp_path, ids):
    inv, (cfg, app), client, clock, rt = _driven(tmp_path, ids)
    _normal(app, clock, rt, inv)
    rr_, ww, mute = inv.read_registers, inv.write, {'on': False}
    inv.read_registers = lambda *a, **k: None if mute['on'] else rr_(*a, **k)

    def lands(*a, **k):
        out = ww(*a, **k)
        mute['on'] = True                                          # the verify read times out
        return out
    inv.write = lands
    _emergency(app, clock, rt)
    assert inv.mem[40232] == 5000 and not rt.states[('h', 'pv-u1')].confirmed
    mute['on'], inv.write = False, ww
    r = client.post("/api/rules/h/mode", json={'mode': 'shadow'})
    assert r.json()['releasing'] == ['pv-u1'], r.text
    assert rt.wait_released(20) and inv.mem[40232] == 10000


@needs_tc
def test_a_want_queued_behind_the_rate_limit_is_not_taken_as_held(tmp_path, ids):
    inv, (cfg, app), client, clock, rt = _driven(tmp_path, ids)
    st = rt.states[('h', 'pv-u1')]
    st.pending = True                                              # decided, not sent yet
    _holds(app, clock, 'pv-u1', inv)
    r = client.post("/api/rules/h/mode", json={'mode': 'shadow'})
    assert r.json()['releasing'] == ['pv-u1'], r.text
    assert rt.wait_released(10) and inv.mem[40232] == 10000


@needs_tc
def test_an_answer_confirms_the_want_it_carried_not_a_newer_one(tmp_path, ids):
    from concurrent.futures import Future

    from multibus.rules import Decision
    inv, (cfg, app), client, clock, rt = _driven(tmp_path, ids)
    st = rt.states[('h', 'pv-u1')]
    carried = dict(st.commanded)                                   # the 60 % that went out
    st.commanded, st.confirmed = dict(carried, value=100), False   # 100 decided meanwhile, queued
    done = Future()
    done.set_result((200, {'status': 'success'}))
    unit = rt._find_device('pv-u1')[1]
    d = Decision(clock(), 'run', 'test', st.state, 252.6, carried, 100.0, True)
    rt.__dict__.setdefault('_inflight', {})[('h', 'pv-u1')] = (done, rt.rules['h'], unit, st, d,
                                                               {'ts': clock(), 'device': 'pv-u1'})
    rt._settle()                                                   # the 60 % answer comes back
    assert st.confirmed is False                                   # it does not confirm the queued 100
    st.commanded = dict(carried)
    rt.__dict__['_inflight'][('h', 'pv-u1')] = (done, rt.rules['h'], unit, st, d, {'ts': clock(), 'device': 'pv-u1'})
    rt._settle()
    assert st.confirmed is True                                    # the want it carried is confirmed


@needs_tc
def test_a_unit_under_an_override_is_left_to_whoever_commands_it(tmp_path, ids):
    inv, (cfg, app), client, clock, rt = _driven(tmp_path, ids)
    _normal(app, clock, rt, inv)
    assert client.post("/api/rules/h/override", json={'seconds': 3600}).status_code == 200
    inv.mem[40232] = 3000                                          # another system set 30 % meanwhile
    _feed(app, clock, 'pv-u1', {'power_limit_pct': 30.0})
    n = len(inv.writes)
    r = client.post("/api/rules/h/mode", json={'mode': 'shadow'})
    assert r.json()['releasing'] == [] and 'left to the override' in r.json()['release_warning']
    assert rt.wait_released(10) and len(inv.writes) == n and inv.mem[40232] == 3000
    assert [a for a in rt.alerts if a[1] == 'rule:h:release' and a[0] == 'warning']   # not only an info line


@needs_tc
def test_a_pause_with_nobody_writing_still_releases_the_rules_own_limit(tmp_path, ids):
    # review R2-1 / probe P1: paused, nothing else wrote — the 60 % is the rule's own
    inv, (cfg, app), client, clock, rt = _driven(tmp_path, ids)
    assert client.post("/api/rules/h/override", json={'seconds': 3600}).status_code == 200
    _holds(app, clock, 'pv-u1', inv)                               # still the 60 % the rule sent
    r = client.post("/api/rules/h/mode", json={'mode': 'shadow'})
    assert r.json()['releasing'] == ['pv-u1'], r.text
    assert rt.wait_released(10) and inv.mem[40232] == 10000


@needs_tc
def test_a_pause_with_no_fresh_read_back_releases_to_be_sure(tmp_path, ids):
    inv, (cfg, app), client, clock, rt = _driven(tmp_path, ids)
    assert client.post("/api/rules/h/override", json={'seconds': 3600}).status_code == 200
    app.state.registry.ensure_store('pv-u1').clear()
    r = client.post("/api/rules/h/mode", json={'mode': 'shadow'})
    assert r.json()['releasing'] == ['pv-u1'], r.text
    assert rt.wait_released(10) and inv.mem[40232] == 10000


@needs_tc
def test_a_read_back_too_old_to_trust_falls_back_to_what_was_confirmed(tmp_path, ids):
    inv, (cfg, app), client, clock, rt = _driven(tmp_path, ids)
    _restart(rt)
    inv.mem[40232] = 10000
    # an hourly controls group: a value of 100 read 10 minutes ago, nothing confirmed since the restart
    _feed(app, clock, 'pv-u1', {'power_limit_pct': 100.0}, age=600, interval=3600)
    r = client.post("/api/rules/h/mode", json={'mode': 'shadow'})
    assert r.json()['releasing'] == ['pv-u1'], r.text              # released to be sure


@needs_tc
def test_a_read_back_taken_before_the_last_write_is_not_trusted(tmp_path, ids):
    # a target whose read-back arrives late (MQTT, a Victron GX): the value
    # seen at disarm was read before the rule's last write
    inv, (cfg, app), client, clock, rt = _driven(tmp_path, ids)
    st = rt.states[('h', 'pv-u1')]
    st.confirmed = False                                           # the 60 % answer is not in yet
    _feed(app, clock, 'pv-u1', {'power_limit_pct': 100.0},
          age=clock() - st.last_cmd_ts + 1)                        # read 1 s before that write
    r = client.post("/api/rules/h/mode", json={'mode': 'shadow'})
    assert r.json()['releasing'] == ['pv-u1'], r.text
    assert rt.wait_released(10) and inv.mem[40232] == 10000


@needs_tc
def test_a_group_with_one_unit_in_flight_releases_it_and_leaves_the_safe_one(tmp_path, ids, monkeypatch):
    import threading

    import multibus.rules_runtime as rr
    a, b = _Inverter(sf=-2), _Inverter(sf=-2)
    ids(lambda: {'umg512', 'pv-u1', 'pv-u2', 'pv'})
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': a, 'pv-u2': b})
    g = dict(OV, id='g', mode='armed', target={'endpoint': 'pv', 'group': 'inverters', 'command': 'power_limit'},
             signal='max(pv-u1.voltage_l1_n, pv-u2.voltage_l1_n)')
    assert client.post("/api/rules", json=g).status_code == 200
    gate, real = threading.Event(), a.write

    def slow(*x, **k):
        gate.wait(20)
        return real(*x, **k)
    a.write = slow
    b.write_ok = False                                             # pv-u2 never takes the curtail
    monkeypatch.setattr(rr, 'COMMAND_WAIT_S', 0.1)
    for _ in range(4):
        for d in ('pv-u1', 'pv-u2'):
            _feed(app, clock, d, {'voltage_l1_n': 253.5, 'power_limit_pct': 100.0})
        rt.tick(clock()); clock.t += 2
    assert rt._inflight_for('pv-u1') and b.mem[40232] == 10000
    b.write_ok = True
    nb = len(b.writes)
    r = client.post("/api/rules/g/mode", json={'mode': 'shadow'})
    gate.set()
    assert 'pv-u1' in r.json()['releasing'], r.text
    assert rt.wait_released(20) and a.mem[40232] == 10000
    assert b.mem[40232] == 10000 and len(b.writes) - nb <= 1       # at most one redundant write, never a curtail


@needs_tc
def test_after_a_restart_a_group_releases_only_the_units_still_held(tmp_path, ids):
    a, b = _Inverter(sf=-2), _Inverter(sf=-2)
    ids(lambda: {'umg512', 'pv-u1', 'pv-u2', 'pv'})
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': a, 'pv-u2': b})
    g = dict(OV, id='g', mode='armed', target={'endpoint': 'pv', 'group': 'inverters', 'command': 'power_limit'},
             signal='max(pv-u1.voltage_l1_n, pv-u2.voltage_l1_n)')
    assert client.post("/api/rules", json=g).status_code == 200
    for _ in range(3):
        for d, inv in (('pv-u1', a), ('pv-u2', b)):
            _feed(app, clock, d, {'voltage_l1_n': 252.6, 'power_limit_pct': inv.mem[40232] / 100})
        rt.tick(clock()); clock.t += 2
    assert a.writes[-1][1][0] == 6000 and b.writes[-1][1][0] == 6000
    _restart(rt)
    b.mem[40232] = 10000                                           # pv-u2 reverted on its own
    _holds(app, clock, 'pv-u1', a)
    _holds(app, clock, 'pv-u2', b)
    nb = len(b.writes)
    r = client.post("/api/rules/g/mode", json={'mode': 'shadow'})
    assert r.status_code == 200 and r.json()['releasing'] == ['pv-u1'], r.text
    assert rt.wait_released(10) and a.writes[-1][1][0] == 10000 and len(b.writes) == nb
