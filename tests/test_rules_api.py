# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Rules from the outside: the API, the runtime ticking against the live
store, the commands it runs, ownership, MQTT state, release to safe."""
import json

import pytest


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
    # phase 3: the inverter's power channel combines by min — a second armed
    # limit is welcome (the lowest goes out); refusals at equal priority are
    # for channels decided by priority (tests/test_rules_arbiter.py)
    r = client.post("/api/rules", json=dict(OV, id='ov-u1-bis', mode='armed'))
    assert r.status_code == 200, r.text
    client.delete("/api/rules/ov-u1-bis")
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
    # deleting an armed rule that holds a limit restores the safe values — in
    # the background, the answer says which units (2026-10-20: 19 s blocked)
    r = client.delete("/api/rules/ov-u1")
    assert r.status_code == 200 and r.json()['releasing'] == ['pv-u1']
    assert rt.wait_released(10)
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
    import time as _time
    for _ in range(4):
        for d in ('pv-u1', 'pv-u2'):
            _feed(app, clock, d, {'voltage_l1_n': 252.6, 'power_limit_pct': 100})   # a fresh reading each tick
        rt.tick(clock()); clock.t += 2
        _time.sleep(0.2)          # a failing device is not waited for: its answer lands by the next tick
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


@needs_tc
def test_override_is_bounded_and_a_rejected_command_does_not_pause_the_rule(tmp_path):
    """F-09 (3.83.0): override_s used to be unbounded (a rule silenced for
    years) and the rule was paused BEFORE the command was validated, so a
    rejected command still disarmed the safety rule."""
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    r = client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 50, "override_s": 10**12})
    assert r.status_code == 409 and inv.writes == []
    r = client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 50, "override_s": "nan"})
    assert r.status_code == 409 and inv.writes == []
    # a valid override with an invalid value: rejected, and the rule stays armed
    r = client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 500, "override_s": 60})
    assert r.status_code == 422, r.text
    assert inv.writes == [] and rt.owner_of('pv-u1', 'power_limit') == 'ov-u1'
    assert not client.get("/api/rules/ov-u1").json()['live']['units']['pv-u1'].get('paused_until')
    # the MQTT face is bounded the same way
    assert rt.override('ov-u1', 10**12, who='t', via='mqtt') and rt.owner_of('pv-u1', 'power_limit') == 'ov-u1'
    assert rt.override('ov-u1', float('nan'), who='t', via='mqtt')


@needs_tc
def test_mqtt_set_cannot_enable_a_rule_and_clamps_are_bounded(tmp_path):
    """F-74 (3.83.0): the broker may disable a rule but never enable one
    (enabling an armed rule is arming it); a clamp must be a finite,
    non-negative number with a bounded expiry."""
    inv = _Inverter(sf=-2)
    mq = _Mqtt()
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mqtt=mq, mode='armed')
    setter = mq.topics['mbg/rules/ov-u1/set']
    setter(json.dumps({'enabled': False, 'source': 'nodered'}))
    assert client.get("/api/rules/ov-u1").json()['enabled'] is False
    setter(json.dumps({'enabled': True, 'source': 'nodered'}))
    assert client.get("/api/rules/ov-u1").json()['enabled'] is False    # refused
    for bad in ({'clamp': {'max': 'nan'}}, {'clamp': {'max': -5}}, {'clamp': {'max': 60, 'expires_s': 1e12}},
                {'clamp': {'max': float('inf')}} if False else {'clamp': {'max': 'inf'}}):
        setter(json.dumps(bad))
        assert not rt.states[('ov-u1', 'pv-u1')].clamp, bad
    setter(json.dumps({'clamp': {'max': 60, 'expires_s': 3600}}))
    assert rt.states[('ov-u1', 'pv-u1')].clamp['max'] == 60


@needs_tc
def test_a_failed_release_to_safe_is_reported_as_failed(tmp_path):
    """F-10 (3.83.0): deleting an armed rule whose safe recipe cannot be
    written must not log 'released to safe'."""
    inv = _Inverter(sf=-2, write_ok=False)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    # make the rule believe it moved the unit away from safe
    rt.states[('ov-u1', 'pv-u1')].commanded = {'value': 50}
    r = client.delete("/api/rules/ov-u1")
    assert r.status_code == 200
    assert rt.wait_released(10)
    ev = [e for e in app.state.event_log.recent(50) if 'ov-u1' in str(e)] if hasattr(app.state, 'event_log') else []
    texts = " ".join(str(e) for e in ev)
    assert "FAILED" in texts and "released to safe" not in texts.replace("release to safe FAILED", "")


@needs_tc
def test_rules_export_then_import_preview_apply_and_never_arrive_armed(tmp_path):
    import yaml
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv})
    r = client.get("/api/rules/export")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    doc = yaml.safe_load(r.text)
    assert [x["id"] for x in doc["rules"]] == ["ov-u1"]
    assert client.get("/api/rules/export?ids=nope").status_code == 404
    # the file says armed; an import never arms
    doc["rules"][0]["mode"] = "armed"
    text = yaml.safe_dump(doc)
    assert client.delete("/api/rules/ov-u1").status_code == 200
    prev = client.post("/api/rules/import", json={"yaml": text}).json()
    assert prev["applied"] is False and prev["rules"][0]["status"] == "new"
    assert prev["rules"][0]["was_armed"] is True
    assert "ov-u1" not in rt.rules                              # preview wrote nothing
    done = client.post("/api/rules/import", json={"yaml": text, "apply": True}).json()
    assert done["rules"][0]["status"] == "created" and rt.rules["ov-u1"].mode == "shadow"
    # again: it exists — refused unless replace is ticked
    again = client.post("/api/rules/import", json={"yaml": text, "apply": True}).json()
    assert again["rules"][0]["status"] == "exists" and again["ok"] == 0
    rep = client.post("/api/rules/import", json={"yaml": text, "apply": True, "replace": True}).json()
    assert rep["rules"][0]["status"] == "replaced"
    # a rule whose target does not exist here is shown as invalid, with why
    other = dict(doc["rules"][0], id="ov-u9", target={"device": "pv-u9", "command": "power_limit"})
    bad = client.post("/api/rules/import", json={"yaml": yaml.safe_dump([other])}).json()
    assert bad["rules"][0]["status"] == "invalid" and bad["rules"][0]["errors"]
    # garbage is a 422, not a crash
    assert client.post("/api/rules/import", json={"yaml": "rules: 3"}).status_code == 422


@needs_tc
def test_a_trim_rule_is_saved_previewed_and_nudges_the_device_once_armed(tmp_path):
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv})
    client.delete("/api/rules/ov-u1")
    trim = {'id': 'trim-u1', 'mode': 'shadow', 'target': {'device': 'pv-u1', 'command': 'power_limit'},
            'kind': 'trim', 'signal': 'pv-u1.voltage_l1_n', 'above': 250, 'below': 240,
            'step': 5, 'min': 20, 'max': 100, 'params': {'revert_s': 0},
            'timing': {'every_s': 2, 'debounce': 1, 'min_interval_s': 0, 'reassert_s': 0}}
    r = client.post("/api/rules", json=trim)
    assert r.status_code == 200, r.text
    assert client.get("/api/rules").json()['rules'][0]['kind'] == 'trim'
    _feed(app, clock, 'pv-u1', {'voltage_l1_n': 230, 'power_limit_pct': 100})
    p = client.post("/api/rules/validate", json=trim).json()
    assert p['state'] == 'lowering' and p['want']['value'] == 95       # from the read-back 100, one step down
    assert client.post("/api/rules/trim-u1/mode", json={'mode': 'armed'}).status_code == 200
    _feed(app, clock, 'pv-u1', {'voltage_l1_n': 230, 'power_limit_pct': 100})
    rt.tick()
    assert inv.mem[40232] == 9500                                         # 95 % at SF -2


@needs_tc
def test_a_rule_on_the_clock_alone_is_never_stale_and_one_rule_reads_anothers_counters(tmp_path):
    import json as _json

    from multibus import clock
    inv = _Inverter(sf=-2)
    cfg, app, client, clock_, rt = _rule_app(tmp_path, {'pv-u1': inv})
    client.delete("/api/rules/ov-u1")
    base = {'mode': 'shadow', 'target': {'device': 'pv-u1', 'command': 'power_limit'}, 'kind': 'condition',
            'params': {'revert_s': 0}, 'timing': {'every_s': 2, 'debounce': 1, 'min_interval_s': 0, 'reassert_s': 0}}
    clock.set_timezone('UTC')
    clock.set_now(lambda: 12 * 3600.0)                                   # 12:00
    try:
        sched = dict(base, id='midday', when='between("10:00", "16:00")',
                     then={'params': {'value': 60}}, **{'else': {'params': {'value': 100}}}, off_value=100)
        assert client.post("/api/rules", json=sched).status_code == 200
        _feed(app, clock_, 'pv-u1', {'power_limit_pct': 100})
        rt.tick()
        assert rt.states[('midday', 'pv-u1')].state == 'true'            # not stale: the clock has no age
        clock_.t += 120
        rt.tick()
        assert rt._rule_stat('midday', 'starts_today') == 1
        assert rt._rule_stat('midday', 'run_today_h') == pytest.approx(120 / 3600, abs=1e-3)
        reader = dict(base, id='quota', target={'device': 'pv-u1', 'command': 'restore'},
                      when='run_today_h("midday") > 1', then={'params': {}}, **{'else': None})
        r = client.post("/api/rules/validate", json=reader)
        assert r.status_code == 200 and 'run_today_h' not in str(r.json().get('errors') or '')
        # the counters are kept in rules_state.json and come back after a reload
        rt._save_state()
        saved = _json.loads((tmp_path / "rules_state.json").read_text())
        assert saved['midday:pv-u1']['counters']['starts_today'] == 1
        rt.states.clear()
        rt.load()
        assert rt.states[('midday', 'pv-u1')].starts_today == 1
    finally:
        clock.set_now(None)


@needs_tc
def test_a_schedule_runs_on_the_clock_and_a_force_comes_and_goes_over_api_and_mqtt(tmp_path):
    from multibus import clock
    inv = _Inverter(sf=-2)
    mq = _Mqtt()
    cfg, app, client, clock_, rt = _rule_app(tmp_path, {'pv-u1': inv}, mqtt=mq)
    client.delete("/api/rules/ov-u1")
    clock.set_timezone('UTC')
    clock.set_now(lambda: 4 * 86400 + 12 * 3600.0)                         # a Monday, 12:00 UTC
    try:
        sched = {'id': 'noon-cap', 'mode': 'shadow', 'target': {'device': 'pv-u1', 'command': 'power_limit'},
                 'kind': 'schedule', 'params': {'revert_s': 0},
                 'schedule': [{'days': 'all', 'at': '11:00', 'value': 70}, {'days': 'all', 'at': '15:00', 'value': 100}],
                 'timing': {'every_s': 2, 'debounce': 1, 'min_interval_s': 0, 'reassert_s': 0}}
        clock_.t = 4 * 86400 + 12 * 3600.0                                  # the runtime's clock, the same noon
        assert client.post("/api/rules", json=sched).status_code == 200
        p = client.post("/api/rules/validate", json=sched).json()
        assert p['state'] == 'every day 11:00' and p['want']['value'] == 70 and not p['stale']
        _feed(app, clock_, 'pv-u1', {'power_limit_pct': 100})
        rt.tick()
        assert rt.states[('noon-cap', 'pv-u1')].state == 'every day 11:00'
        # forcing: over the API, then ended over MQTT
        r = client.post("/api/rules/noon-cap/force", json={'value': 50, 'seconds': 600})
        assert r.status_code == 200 and r.json()['rule']['live']['units']['pv-u1']['forced']['params'] == {'value': 50.0}
        clock_.t += 2
        rt.tick()
        assert rt.states[('noon-cap', 'pv-u1')].want['value'] == 50
        assert client.post("/api/rules/noon-cap/force", json={'value': 50, 'seconds': 99999}).status_code == 422
        rt._save_state()
        rt.states.clear()
        rt.load()
        assert rt.states[('noon-cap', 'pv-u1')].forced['params'] == {'value': 50.0}   # kept across a reload
        mq.topics['mbg/rules/noon-cap/set'](json.dumps({'force': None, 'source': 'hmi'}))
        assert rt.states[('noon-cap', 'pv-u1')].forced is None
        a = [x for x in _audit(tmp_path) if x['action'].startswith('rule force')]
        assert [x['action'] for x in a] == ['rule force', 'rule force ended'] and a[-1]['user'] == 'hmi'
    finally:
        clock.set_now(None)


@needs_tc
def test_an_armed_group_trim_starts_from_the_read_back_and_moves_its_units_together(tmp_path):
    """2026-10-20 on the plant (an export cap on four inverters): shadow
    minutes over the cap walked the trim down to min, arming sent 20 % to
    every unit, the units then drifted apart (39.7 / 27.2 / 33.1 / 37.8 %),
    and disarming held the API for 19 s while the safe writes went out."""
    import time as _time
    a, b = _Inverter(sf=-2), _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': a, 'pv-u2': b})
    cap = dict(id='cap', label='cap', mode='shadow', kind='trim',
               target={'endpoint': 'pv', 'group': 'inverters', 'command': 'power_limit'},
               signal='300 - pv-u1.voltage_l1_n', signal_unit='V under 300',
               below=0, above=30, step=10, min=20, max=100, params={'revert_s': 0},
               stale_after_s=60, timing={'every_s': 2, 'debounce': 1, 'min_interval_s': 0, 'reassert_s': 0})
    assert client.post("/api/rules", json=cap).status_code == 200

    def tick(n=1):
        for _ in range(n):
            for d, inv in (('pv-u1', a), ('pv-u2', b)):
                _feed(app, clock, d, {'voltage_l1_n': 320, 'power_limit_pct': inv.mem[40232] / 100})
            rt.tick(clock()); clock.t += 2
            _time.sleep(0.05)

    tick(10)                                                       # over the cap, in shadow
    live = client.get("/api/rules/cap").json()['live']
    assert {u['want']['value'] for u in live['units'].values()} == {90}   # one step from 100, not min
    assert not a.writes and not b.writes
    assert client.post("/api/rules/cap/mode", json={'mode': 'armed'}).status_code == 200
    tick(1)
    assert a.writes[-1][1][0] == 9000 and b.writes[-1][1][0] == 9000     # from the read-back
    tick(6)
    wa, wb = (live['units'][u]['want']['value'] for live in [client.get("/api/rules/cap").json()['live']] for u in ('pv-u1', 'pv-u2'))
    assert wa == wb < 90 and a.mem[40232] == b.mem[40232]                 # together
    t0 = _time.monotonic()
    r = client.post("/api/rules/cap/mode", json={'mode': 'shadow'})
    assert r.status_code == 200 and sorted(r.json()['releasing']) == ['pv-u1', 'pv-u2']
    assert _time.monotonic() - t0 < 2
    assert rt.wait_released(10)
    assert a.mem[40232] == 10000 and b.mem[40232] == 10000


@needs_tc
def test_a_disabled_rule_says_so_on_the_broker(tmp_path):
    """2026-10-20: an armed export cap disabled over MQTT mid-curtail kept its
    retained state at "lowering" with a want — a rule that is no longer
    evaluated must not go on saying what it last decided."""
    inv = _Inverter(sf=-2)
    mq = _Mqtt()
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mqtt=mq, mode='armed')
    for _ in range(3):
        _feed(app, clock, 'pv-u1', {'voltage_l1_n': 252.6, 'voltage_l2_n': 230, 'voltage_l3_n': 231, 'power_limit_pct': 100})
        rt.tick(clock()); clock.t += 2
    def last_state():
        return json.loads([x for x in mq.sent if x[0] == 'mbg/rules/ov-u1/state'][-1][1])
    assert last_state()['state'] != 'normal' and last_state()['units']['pv-u1']['want']['value'] == 60
    mq.topics['mbg/rules/ov-u1/set'](json.dumps({'enabled': False, 'source': 'ems'}))
    assert rt.wait_released(10)
    rt.tick(clock())
    s = last_state()
    assert s['enabled'] is False and s['state'] == 'disabled' and s['signal'] is None
    assert s['units']['pv-u1'] == {**s['units']['pv-u1'], 'state': 'disabled', 'want': None}
    n = len([x for x in mq.sent if x[0] == 'mbg/rules/ov-u1/state'])
    rt.tick(clock()); rt.tick(clock())
    assert len([x for x in mq.sent if x[0] == 'mbg/rules/ov-u1/state']) == n     # said once, not every tick


@needs_tc
def test_a_save_that_does_not_name_mode_or_enabled_keeps_them(tmp_path):
    # an EMS sends what it changes: leaving `mode` out must not disarm the
    # rule (and release its target), leaving `enabled` out must not enable it
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    body = {k: v for k, v in OV.items() if k not in ('mode', 'enabled', 'id')}
    r = client.put("/api/rules/ov-u1", json=dict(body, label='renamed'))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['rule']['mode'] == 'armed' and d['releasing'] == []
    assert rt.rules['ov-u1'].mode == 'armed' and rt.rules['ov-u1'].label == 'renamed'
    # disabled stays disabled when a save does not say otherwise
    assert client.post("/api/rules/ov-u1/enable", json={'enabled': False}).status_code == 200
    r = client.put("/api/rules/ov-u1", json=dict(body, label='again'))
    assert r.status_code == 200, r.text
    assert rt.rules['ov-u1'].enabled is False and rt.rules['ov-u1'].mode == 'armed'
    # and an explicit mode still works
    r = client.put("/api/rules/ov-u1", json=dict(body, mode='shadow'))
    assert r.status_code == 200 and rt.rules['ov-u1'].mode == 'shadow'


@needs_tc
def test_one_broken_rule_in_the_file_does_not_stop_the_others(tmp_path):
    # a hand edit (target: true) used to make the whole load raise, so every
    # rule — the armed protection ones too — stopped; now that rule alone is
    # in error, the others load and keep working
    import yaml
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    path = tmp_path / "rules.yaml"
    doc = yaml.safe_load(path.read_text())
    rules = doc['rules'] if isinstance(doc, dict) else doc
    rules.append({'id': 'bad', 'label': 'hand-edited', 'kind': 'steps', 'target': True,
                  'signal': 'pv-u1.voltage_l1_n', 'steps': [{'at': 250, 'want': {'value': 80}}]})
    rules.append('not even a mapping')
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    rt.load()
    assert 'ov-u1' in rt.rules and rt.rules['ov-u1'].mode == 'armed' and 'ov-u1' not in rt.errors
    assert 'bad' in rt.errors
    listed = {r['id']: r for r in client.get("/api/rules").json()['rules']}
    assert 'ov-u1' in listed
    # the broken one is shown (so it can be fixed) and never acts
    assert 'bad' in listed and listed['bad']['mode'] == 'shadow' and listed['bad']['enabled'] is False
    assert listed['bad']['live']['error']
    # and it can be deleted from the UI / API
    assert client.delete("/api/rules/bad").status_code == 200
    assert 'bad' not in rt.rules and 'ov-u1' in rt.rules and rt.rules['ov-u1'].mode == 'armed'


@needs_tc
def test_an_entry_that_is_not_a_rule_alerts_and_survives_a_ui_save(tmp_path):
    # review RL-LOAD: a mis-indented hand edit (a list where a rule goes) was
    # dropped by the store before the runtime saw it — no alert — and the
    # next UI save rewrote the file without it
    import yaml
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    fired = []
    rt.alert_mgr = type('A', (), {'fire': lambda self, sev, key, src, msg: fired.append(key)})()
    path = tmp_path / "rules.yaml"
    doc = yaml.safe_load(path.read_text())
    doc['rules'].append([{'id': 'ov-u2', 'label': 'mis-indented'}])
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    rt.load()
    assert 'rules:entry' in fired and rt.rules['ov-u1'].mode == 'armed'
    # a save from the UI keeps the operator's entry as it was
    r = client.put("/api/rules/ov-u1", json=dict(OV, mode='armed', label='renamed'))
    assert r.status_code == 200, r.text
    rules = yaml.safe_load(path.read_text())['rules']
    assert [{'id': 'ov-u2', 'label': 'mis-indented'}] in rules
    assert next(x for x in rules if isinstance(x, dict) and x['id'] == 'ov-u1')['label'] == 'renamed'


@needs_tc
def test_a_rule_that_could_not_be_loaded_is_repaired_by_a_put(tmp_path):
    import yaml
    inv = _Inverter(sf=-2)
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': inv}, mode='armed')
    path = tmp_path / "rules.yaml"
    doc = yaml.safe_load(path.read_text())
    doc['rules'].append(dict(OV, id='ov-u2', mode='armed', target=True))
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    rt.load()
    assert 'ov-u2' in rt.errors
    # its state says error, not that the operator turned it off
    lv = client.get("/api/rules").json()['rules']
    assert next(x for x in lv if x['id'] == 'ov-u2')['live']['error']
    published = []
    pub = type('C', (), {'publish': lambda self, t, p, **k: published.append((t, p))})()
    rt.mqtt = type('M', (), {'connected': True, 'client': pub})()
    rt._last_pub.pop('ov-u2', None)
    rt._publish_state(rt.rules['ov-u2'])
    import json as _json
    bodies = [_json.loads(p) for t, p in published if t.endswith('ov-u2/state')]
    assert bodies and bodies[-1]['state'] == 'error' and bodies[-1]['error']
    rt.mqtt = None
    r = client.put("/api/rules/ov-u2", json=dict(OV, id='ov-u2', mode='shadow'))
    assert r.status_code == 200, r.text
    assert 'ov-u2' not in rt.errors and rt.rules['ov-u2'].mode == 'shadow'
