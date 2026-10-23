# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""A rule that leaves armed takes back the writes it has not put on the wire
(OV-F4, design §7): a disabled rule's queued command never lands after it
stopped — over another rule, or over a target left on `hold`."""
import threading

from tests.test_commands import _Inverter
from tests.test_devices_api import needs_tc
from tests.test_rules_api import _feed, _rule_app

HIGH = {'voltage_l1_n': 251.6, 'voltage_l2_n': 230, 'voltage_l3_n': 231, 'power_limit_pct': 100}


def _wants_70(app, clock, rt):
    for _ in range(3):
        _feed(app, clock, 'pv-u1', HIGH)
        rt.tick(clock())
        clock.t += 2


@needs_tc
def test_a_queued_write_of_a_rule_that_leaves_armed_is_never_sent(tmp_path):
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    gate = threading.Event()
    rt._writer('pv-u1').submit(gate.wait, 10)            # the device's writer is busy
    _wants_70(app, clock, rt)
    assert rt._inflight_for('pv-u1')                      # the 70 % waits behind it
    assert client.post('/api/rules/ov-u1/mode', json={'mode': 'shadow'}).status_code == 200
    gate.set()
    assert rt.wait_released(10)
    rt._settle()
    # the 70 % never reached the device; the release put it back to safe
    assert [w[1][0] for w in inv.writes] == [10000]
    assert any(r.get('result') == 'cancelled' for r in rt.decisions['ov-u1'])


@needs_tc
def test_a_write_waiting_for_the_bus_asks_its_rule_first(tmp_path):
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    (dev, cl), = rt._targets(rt.rules['ov-u1'])
    code, res = rt._run_command(dev, cl, 'power_limit', {'value': 70}, who='rule ov-u1', ip='-',
                                via='rule:ov-u1', still_wanted=lambda: False)
    assert res['status'] == 'cancelled' and code == 409 and not inv.writes
    code, res = rt._run_command(dev, cl, 'power_limit', {'value': 70}, who='rule ov-u1', ip='-',
                                via='rule:ov-u1', still_wanted=lambda: True)
    assert res['status'] == 'success' and inv.writes[-1][1][0] == 7000


@needs_tc
def test_a_rule_left_on_hold_still_drops_its_queued_write(tmp_path):
    # on_disable: hold sends nothing back — its queued write must not land
    # after the operator stopped it either
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    body = {**rt.raw['ov-u1'], 'on_disable': 'hold'}
    assert client.put('/api/rules/ov-u1', json=body).status_code == 200
    gate = threading.Event()
    rt._writer('pv-u1').submit(gate.wait, 10)
    _wants_70(app, clock, rt)
    assert client.put('/api/rules/ov-u1', json={**body, 'enabled': False}).status_code == 200
    gate.set()
    rt._writer('pv-u1').submit(lambda: None).result(10)  # the writer has drained
    rt._settle()
    assert inv.writes == []


@needs_tc
def test_an_armed_rule_keeps_writing_as_before(tmp_path):
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    _wants_70(app, clock, rt)
    rt._writer('pv-u1').submit(lambda: None).result(10)
    rt._settle()
    assert [w[1][0] for w in inv.writes] == [7000]


@needs_tc
def test_a_rule_that_leaves_armed_while_a_tick_still_evaluates_it_writes_nothing(tmp_path):
    # review §7 F1: the tick reads the rules, the operator disarms, the tick
    # then decides `run` on its stale copy — that write must not land
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    orig = rt._act

    def _act(rule, *a, **k):
        if a[3].action in ('run', 'reassert'):
            rt._cancel_queued(rule.id)                  # the disarm, on the API thread
        return orig(rule, *a, **k)
    rt._act = _act
    _wants_70(app, clock, rt)
    rt._writer('pv-u1').submit(lambda: None).result(10)
    rt._settle()
    assert inv.writes == []
    assert any(r.get('result') == 'cancelled' for r in rt.decisions['ov-u1'])


@needs_tc
def test_a_reload_that_disarms_a_rule_drops_its_queued_write(tmp_path):
    # review §7 F2: an import or a restore reloads the file without _release
    import yaml
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    gate = threading.Event()
    rt._writer('pv-u1').submit(gate.wait, 10)
    _wants_70(app, clock, rt)
    path = tmp_path / 'rules.yaml'
    doc = yaml.safe_load(path.read_text())
    for r in doc['rules']:
        r['mode'] = 'shadow'
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    rt.load()
    gate.set()
    rt._writer('pv-u1').submit(lambda: None).result(10)
    rt._settle()
    assert inv.writes == []


@needs_tc
def test_disarmed_and_armed_again_the_new_want_lands(tmp_path):
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    gate = threading.Event()
    rt._writer('pv-u1').submit(gate.wait, 10)
    _wants_70(app, clock, rt)
    assert client.post('/api/rules/ov-u1/mode', json={'mode': 'shadow'}).status_code == 200
    assert client.post('/api/rules/ov-u1/mode', json={'mode': 'armed'}).status_code == 200
    gate.set()
    assert rt.wait_released(10)
    _wants_70(app, clock, rt)
    rt._writer('pv-u1').submit(lambda: None).result(10)
    rt._settle()
    # the old 70 % was dropped, the release went out, the re-armed rule sent its own 70 %
    assert [w[1][0] for w in inv.writes] == [10000, 7000]



@needs_tc
def test_a_write_cancelled_by_a_disarm_and_rearm_within_one_tick_is_sent_again(tmp_path):
    # second review S7-1: the cancelled write left pending false and
    # commanded set — the re-armed rule would wait for the reassert window
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    orig, fired = rt._act, []

    def _act(rule, *a, **k):
        if a[3].action == 'run' and not fired:
            fired.append(1)
            rt._cancel_queued(rule.id)                  # disarmed and armed again meanwhile
        return orig(rule, *a, **k)
    rt._act = _act
    _wants_70(app, clock, rt)
    rt._writer('pv-u1').submit(lambda: None).result(10)
    rt._settle()
    assert inv.writes == [] and rt.states[('ov-u1', 'pv-u1')].pending
    clock.t += 30                                       # at the rate limit, as a failed write — not after reassert_s
    _wants_70(app, clock, rt)
    rt._writer('pv-u1').submit(lambda: None).result(10)
    assert [w[1][0] for w in inv.writes] == [7000]
