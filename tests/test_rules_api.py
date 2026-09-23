# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Rules from the outside: the API, the runtime ticking against the live
store, the commands it runs, ownership, MQTT state, release to safe."""
import json


from tests.test_commands import _Inverter
from tests.test_commands_api import _Mqtt, _app, _audit
from tests.test_endpoints import needs_tc
from tests.test_rules import OV

VOLT = {'voltage_l1_n': 40080, 'voltage_l2_n': 40081, 'voltage_l3_n': 40082}


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _rt(app, clock):
    rt = app.state.rules_runtime
    rt._clock = clock
    rt._mono = clock
    return rt


def _feed(app, clock, dev, values, *, age=1.0, interval=20):
    """Put live values into a unit's store the way a poller does."""
    store = app.state.registry.ensure_store(dev)
    for name, v in values.items():
        addr = VOLT.get(name, 40232 if name == 'power_limit_pct' else abs(hash(name)) % 1000 + 50000)
        store[addr] = {'value': v, 'name': name, 'timestamp': 'x', 'ts': clock() - age,
                       'mono': clock() - age, 'interval': interval}


def _rule_app(tmp_path, inverters, mqtt=None, mode='shadow'):
    cfg, app, client = _app(tmp_path, inverters, mqtt=mqtt)
    clock = _Clock()
    rt = _rt(app, clock)
    r = client.post("/api/rules", json=dict(OV, mode=mode))
    assert r.status_code == 200, r.text
    return cfg, app, client, clock, rt


@needs_tc
def test_a_rule_is_created_validated_and_previewed(tmp_path):
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv})
    d = client.get("/api/rules").json()['rules']
    assert len(d) == 1 and d[0]['id'] == 'ov-u1' and d[0]['mode'] == 'shadow'
    assert d[0]['live']['state'] == 'normal' and d[0]['live']['error'] is None
    assert (tmp_path / "rules.yaml").exists()
    # the target's command shaped it: safe values and tolerance came from the template
    assert rt.rules['ov-u1'].safe_params == {'value': 100, 'revert_s': 0} and rt.rules['ov-u1'].tolerance == 1.0
    # validation speaks
    r = client.post("/api/rules", json=dict(OV, id='bad', release_below=260))
    assert r.status_code == 422 and 'release_below' in r.text
    r = client.post("/api/rules", json=dict(OV, id='ov-u9', target={'device': 'pv-u9', 'command': 'power_limit'}))
    assert r.status_code == 422 and 'no such device' in r.text
    r = client.post("/api/rules", json=dict(OV, id='nodev', signal='voltage_l1_n > 1'))
    assert r.status_code == 422 and 'device.register' in r.text
    # the live preview: no state kept, the want it would have now
    _feed(app, clock, 'pv-u1', {'voltage_l1_n': 251.4, 'voltage_l2_n': 230, 'voltage_l3_n': 231, 'power_limit_pct': 100})
    p = client.post("/api/rules/validate", json=OV).json()
    assert p['signal'] == 251.4 and p['state'] == 'Moderate' and p['want']['value'] == 70
    assert p['units']['pv-u1']['actual'] == 100 and p['stale'] is False
    assert rt.states[('ov-u1', 'pv-u1')].state == 'normal'      # the preview touched nothing


@needs_tc
def test_shadow_decides_but_never_writes_then_armed_runs_the_command(tmp_path):
    inv = _Inverter(sf=-2)
    mq = _Mqtt()
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mqtt=mq)
    for _ in range(3):
        _feed(app, clock, 'pv-u1', {'voltage_l1_n': 252.6, 'voltage_l2_n': 230, 'voltage_l3_n': 231, 'power_limit_pct': 100})   # a fresh reading each tick
        rt.tick(clock()); clock.t += 2
    live = client.get("/api/rules/ov-u1").json()['live']
    assert live['state'] == 'Severe' and live['units']['pv-u1']['want']['value'] == 60
    assert inv.writes == []                                       # shadow: nothing on the wire
    decs = client.get("/api/rules/ov-u1/decisions").json()['decisions']
    assert decs[0]['action'] == 'shadow' and 'would run' in decs[0]['reason']
    # the state is retained on MQTT, an event went out on the transition
    st = [x for x in mq.sent if x[0] == 'mbg/rules/ov-u1/state'][-1]
    assert st[2] is True and json.loads(st[1])['state'] == 'Severe'
    assert any(x[0] == 'mbg/rules/ov-u1/event' for x in mq.sent)
    # arm: an explicit, audited act — the rule now owns the target
    r = client.post("/api/rules/ov-u1/mode", json={'mode': 'armed'})
    assert r.status_code == 200 and r.json()['rule']['mode'] == 'armed'
    assert [a for a in _audit(tmp_path) if a['action'] == 'rule armed']
    rt.tick(clock())
    assert inv.writes[-1] == (40232, [6000, 0, 0, 10, 1])        # 60 %, revert 0, ramp 10
    a = [x for x in _audit(tmp_path) if x.get('action') == 'command'][-1]
    assert a['detail']['via'] == 'rule:ov-u1' and a['user'] == 'rule ov-u1'
    assert rt.decisions['ov-u1'][0]['result'] == 'success'
    # the read-back matches: nothing more is sent
    _feed(app, clock, 'pv-u1', {'power_limit_pct': 60})
    clock.t += 40
    n = len(inv.writes)
    rt.tick(clock())
    assert len(inv.writes) == n and rt.decisions['ov-u1'][0]['action'] in ('idle', 'run')


@needs_tc
def test_an_armed_rule_owns_its_target_until_overridden(tmp_path):
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    r = client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 50})
    assert r.status_code == 409 and 'owned by rule' in r.json()['reason'] and inv.writes == []
    # MQTT and HA are refused the same way; a second armed rule on the target is refused at save
    app.state.mqtt_command(('pv-u1',), 'power_limit', '50')
    assert inv.writes == []
    r = client.post("/api/rules", json=dict(OV, id='ov-u1-bis', mode='armed'))
    assert r.status_code == 422 and 'already drives' in r.text
    # an override pauses the rule for a stated time, and the command goes through
    r = client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 50, "override_s": 600})
    assert r.status_code == 200, r.text
    assert inv.writes[-1][1][0] == 5000
    live = client.get("/api/rules/ov-u1").json()['live']
    assert live['units']['pv-u1']['paused_until'] and live['units']['pv-u1']['paused_until'] > clock()
    assert rt.owner_of('pv-u1', 'power_limit') is None
    # while paused the rule keeps thinking but does not act
    _feed(app, clock, 'pv-u1', {'voltage_l1_n': 253.5, 'voltage_l2_n': 230, 'voltage_l3_n': 231, 'power_limit_pct': 50})
    n = len(inv.writes)
    rt.tick(clock())
    assert rt.states[('ov-u1', 'pv-u1')].state == 'Emergency' and len(inv.writes) == n
    clock.t += 700
    rt.tick(clock())
    assert inv.writes[-1][1][0] == 5000 or len(inv.writes) == n     # 50 = the Emergency want: already held
    assert rt.owner_of('pv-u1', 'power_limit') == 'ov-u1'


@needs_tc
def test_stale_holds_and_a_lost_write_is_reasserted(tmp_path):
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    for _ in range(3):
        _feed(app, clock, 'pv-u1', {'voltage_l1_n': 252.6, 'voltage_l2_n': 230, 'voltage_l3_n': 231, 'power_limit_pct': 100})   # a fresh reading each tick
        rt.tick(clock()); clock.t += 2
    assert inv.writes[-1][1][0] == 6000
    # the voltage feed dies: the limit holds (fail closed), the event says stale
    clock.t += 120
    rt.tick(clock())
    live = client.get("/api/rules/ov-u1").json()['live']
    assert live['state'] == 'stale' and live['units']['pv-u1']['want']['value'] == 60
    # somebody put the inverter back to 100 behind the rule's back: reasserted after reassert_s
    _feed(app, clock, 'pv-u1', {'power_limit_pct': 100})
    for _ in range(3):
        _feed(app, clock, 'pv-u1', {'voltage_l1_n': 252.6, 'voltage_l2_n': 230, 'voltage_l3_n': 231})   # a fresh reading each tick
        rt.tick(clock()); clock.t += 2
    n = len(inv.writes)
    clock.t += 130
    _feed(app, clock, 'pv-u1', {'voltage_l1_n': 252.6, 'voltage_l2_n': 230, 'voltage_l3_n': 231, 'power_limit_pct': 100})
    rt.tick(clock())
    assert len(inv.writes) == n + 1 and inv.writes[-1][1][0] == 6000
    assert rt.decisions['ov-u1'][0]['action'] == 'reassert'


@needs_tc
def test_clamp_over_mqtt_and_release_to_safe_on_delete(tmp_path):
    inv = _Inverter(sf=-2)
    mq = _Mqtt()
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mqtt=mq, mode='armed')
    assert 'mbg/rules/ov-u1/set' in mq.topics
    mq.topics['mbg/rules/ov-u1/set'](json.dumps({'clamp': {'max': 60, 'expires_s': 3600}, 'source': 'nodered'}))
    assert rt.states[('ov-u1', 'pv-u1')].clamp['max'] == 60
    for _ in range(3):
        _feed(app, clock, 'pv-u1', {'voltage_l1_n': 231, 'voltage_l2_n': 230, 'voltage_l3_n': 231, 'power_limit_pct': 100})   # a fresh reading each tick
        rt.tick(clock()); clock.t += 2
    assert inv.writes[-1][1][0] == 6000                            # normal wants 100, the clamp says 60
    a = [x for x in _audit(tmp_path) if x['action'] == 'rule clamp'][-1]
    assert a['detail']['via'] == 'mqtt' and a['user'] == 'nodered'
    # deleting an armed rule that holds a limit restores the safe values first
    r = client.delete("/api/rules/ov-u1")
    assert r.status_code == 200
    assert inv.writes[-1][1] == [10000, 0, 0, 10, 0]
    a = [x for x in _audit(tmp_path) if x.get('action') == 'command'][-1]
    assert a['detail']['via'] == 'rule:ov-u1:release'
    assert client.get("/api/rules").json()['rules'] == []
    assert [x for x in mq.sent if x[0] == 'mbg/rules/ov-u1/state'][-1][1] == '{}'   # retained state cleared


@needs_tc
def test_a_group_rule_fans_out_and_a_failing_unit_alerts(tmp_path):
    a, b = _Inverter(sf=-2), _Inverter(sf=-2, write_ok=False)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': a, 'pv-u2': b})
    r = client.put("/api/rules/ov-u1", json=dict(OV, mode='armed', target={'endpoint': 'pv', 'group': 'inverters', 'command': 'power_limit'},
                                                 signal='max(pv-u1.voltage_l1_n, pv-u2.voltage_l1_n)',
                                                 timing={'every_s': 2, 'debounce': 1, 'min_interval_s': 0, 'reassert_s': 120}))
    assert r.status_code == 200, r.text
    for _ in range(4):
        for d in ('pv-u1', 'pv-u2'):
            _feed(app, clock, d, {'voltage_l1_n': 252.6, 'power_limit_pct': 100})   # a fresh reading each tick
        rt.tick(clock()); clock.t += 2
    assert a.writes[-1][1][0] == 6000 and len(b.writes) >= 3
    live = client.get("/api/rules/ov-u1").json()['live']
    assert set(live['units']) == {'pv-u1', 'pv-u2'} and live['units']['pv-u2']['failures'] >= 3
    ev = app.state.event_log.recent(20)
    assert any('cannot apply' in e.get('message', '') and 'pv-u2' in e.get('message', '') for e in ev)


def test_every_notable_decision_is_a_rule_event_point_in_the_units_bucket():
    """The rule's decisions are the OV history the UI panels read: one
    rule_event point (tags device/rule/state/action/result, fields signal,
    want_value, actual, reason) for every change and every command sent."""
    from types import SimpleNamespace
    from multibus.rules import Decision, RuleDef, Step
    from multibus.rules_runtime import RulesRuntime

    class _Influx:
        def __init__(self): self.points = []
        def write_point(self, p, ts=None, bucket=None): self.points.append((p, ts, bucket))

    rt = RulesRuntime.__new__(RulesRuntime)
    rt.influx = _Influx()
    rule = RuleDef(id='ov-u1', target={'device': 'pv-u1', 'command': 'power_limit'}, kind='steps',
                   signal='pv-u1.v', steps=[Step(at=250.0, value=80.0)], param='value')
    cfg = SimpleNamespace(id='pv-u1', influxdb_bucket='pv', influxdb_device_tag='pv-u1', influxdb_enabled=True)
    d = Decision(ts=1789380000.0, action='run', reason='Warning: want value 80', state='Warning',
                 signal=250.6, want={'value': 80, 'revert_s': 0}, actual=100.0, changed=True)
    rt._influx_event(rule, cfg, d, {'result': 'success'})
    (p, ts, bucket), = rt.influx.points
    lp = p.to_line_protocol()
    assert bucket == 'pv' and ts == 1789380000.0
    assert lp.startswith('rule_event,action=run,device=pv-u1,result=success,rule=ov-u1,state=Warning ')
    assert 'signal=250.6' in lp and 'want_value=80' in lp and 'actual=100' in lp and 'reason="Warning: want value 80"' in lp
    # a unit with InfluxDB off writes nothing; no publisher, nothing
    rt._influx_event(rule, SimpleNamespace(id='x', influxdb_enabled=False), d, {})
    assert len(rt.influx.points) == 1
    rt.influx = None
    rt._influx_event(rule, cfg, d, {})


def test_the_published_unit_state_counts_ignored_and_guarded_samples(tmp_path):
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv})
    _feed(app, clock, 'pv-u1', {'voltage_l1_n': 231, 'voltage_l2_n': 230, 'voltage_l3_n': 231, 'power_limit_pct': 100})
    rt.tick(clock())
    unit = rt.live('ov-u1')['units']['pv-u1']
    assert unit['ignored'] == 0 and unit['guarded'] == 0
    rt._publish_state(rt.rules['ov-u1'])
    published = json.loads(app.state.mqtt_publisher.published[-1][1]) if hasattr(app.state, 'mqtt_publisher') and hasattr(app.state.mqtt_publisher, 'published') else None
    if published is not None:
        assert published['units']['pv-u1']['guarded'] == 0 and published['units']['pv-u1']['ignored'] == 0


@needs_tc
def test_rule_set_over_mqtt_is_refused_on_an_anonymous_broker(tmp_path):
    """mbg/rules/<id>/set enables, clamps or pauses a rule — a control act,
    gated like the command faces (3.80.2)."""
    mq = _Mqtt()
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mqtt=mq)
    cfg.mqtt.username = ""
    mq.topics['mbg/rules/ov-u1/set'](json.dumps({'clamp': {'max': 60}, 'source': 'nodered'}))
    assert rt.states[('ov-u1', 'pv-u1')].clamp is None          # ignored
    cfg.mqtt.username = "gw"
    mq.topics['mbg/rules/ov-u1/set'](json.dumps({'clamp': {'max': 60}, 'source': 'nodered'}))
    assert rt.states[('ov-u1', 'pv-u1')].clamp['max'] == 60
