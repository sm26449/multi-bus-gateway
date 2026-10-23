"""Commands through an MQTT source: the source's
publishing, keepalive and last values; the run's verdicts; the template's
write side; the Victron GX template."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from multibus import mqtt_input as mi
from multibus.commands import mqtt_payload, parse_command_def, run_mqtt_command
from multibus.config import SelectedRegister
from multibus.device_template import TemplateRegistry, parse_template, validate_template, extra_errors

PORTAL = "c0ffee000001"


# ── a source with a fake broker link ─────────────────────────────────────────

class _Paho:
    """What MqttInputClient needs of paho: publish, with what was sent."""
    def __init__(self):
        self.sent = []
        self.rc = 0

    def publish(self, topic, payload, qos=0, retain=False):
        self.sent.append((topic, payload, qos, retain))
        return SimpleNamespace(rc=self.rc)


def _victron_source(tpl, **cfg):
    regs = [SelectedRegister(address=r.address, name=r.name, label=r.label, unit=r.unit,
                             data_type=r.data_type, poll_group='normal', json_path=r.json_path,
                             topic=r.topic, enum=r.enum)
            for r in tpl.registers]
    src = mi.MqttInputClient({'broker': 'gx', 'topic': '', 'vars': {'portal': PORTAL, 'vebus': '276'},
                              'keepalive': tpl.protocol.get('keepalive'), **cfg}, regs)
    src._client, src.connected = _Paho(), True
    src.publish_callback = lambda pg, data: None
    return src


def _say(src, path, value, **extra):
    src._on_message(None, None, SimpleNamespace(topic=f"N/{PORTAL}/{path}",
                                                payload=json.dumps({'value': value, **extra}).encode(),
                                                retain=False))


@pytest.fixture(scope='module')
def gx():
    t = TemplateRegistry().get('victron_gx_mqtt')
    assert t is not None
    return t


def _cmd(t, name):
    return parse_command_def(name, t.commands[name])


def _regs(t):
    return {r.name: r for r in t.registers}


def _answer_writes(src, path_of):
    """A GX: what is published on W/… comes back on N/… a moment later."""
    orig = src.publish

    def publish(topic, payload):
        ok = orig(topic, payload)
        if ok[0] and topic.startswith('W/'):
            v = json.loads(payload)['value']
            threading.Timer(0.05, lambda: _say(src, path_of(topic), v)).start()
        return ok
    src.publish = publish


# ── the source ───────────────────────────────────────────────────────────────

def test_a_command_is_published_qos0_never_retained_and_never_on_a_wildcard(gx):
    src = _victron_source(gx)
    assert src.publish("W/x/a", "1") == (True, '')
    assert src._client.sent[-1] == ("W/x/a", "1", 0, False)
    assert src.publish("W/+/a", "1")[0] is False
    src.connected = False
    ok, why = src.publish("W/x/a", "1")
    assert not ok and 'not connected' in why


def test_the_keepalive_asks_for_everything_first_then_stays_quiet(gx):
    src = _victron_source(gx)
    src._on_connect(src._client, None, None, 0)
    topic, payload, _q, retain = src._client.sent[-1]
    assert topic == f"R/{PORTAL}/keepalive" and payload == "" and not retain
    src.send_keepalive()
    assert json.loads(src._client.sent[-1][1]) == {"keepalive-options": ["suppress-republish"]}
    assert src.get_stats()['keepalives_sent'] == 2


def test_a_connection_can_turn_the_templates_keepalive_off(gx):
    from multibus.device_runtime import driver_for
    dev = SimpleNamespace(template='victron_gx_mqtt', id='gx')
    reg = TemplateRegistry()
    on = SimpleNamespace(protocol='mqtt', template='', mqtt_in={'broker': 'b', 'topic': 't'})
    off = SimpleNamespace(protocol='mqtt', template='', mqtt_in={'broker': 'b', 'topic': 't', 'keepalive': False})
    cfg = SimpleNamespace(mqtt=SimpleNamespace(broker='b', port=1883, username='', password='', tls=False))
    assert driver_for(cfg, reg, dev, on, [], {}, True).keepalive['topic'] == 'R/{portal}/keepalive'
    assert driver_for(cfg, reg, dev, off, [], {}, True).keepalive is None


def test_keepalive_validation():
    assert mi.keepalive_errors(False) == []
    assert mi.keepalive_errors({'topic': 'R/x/keepalive', 'every_s': 30}) == []
    assert mi.keepalive_errors({'topic': 'R/+/keepalive'})
    assert mi.keepalive_errors({'topic': 'R/x/keepalive', 'every_s': 1})
    assert mi.keepalive_errors({'payload': 'x'})


def test_real_gx_payloads_decode(gx):
    """As the GX sends them: a setting carries min/max next to its value,
    DVCC is a JSON boolean, an enum arrives as its code."""
    src = _victron_source(gx)
    _say(src, "settings/0/Settings/CGwacs/AcPowerSetPoint", -15711.0, max=1000000.0, min=-1000000.0)
    _say(src, "system/0/Control/Dvcc", True)
    _say(src, "settings/0/Settings/CGwacs/Hub4Mode", 2)
    _say(src, "system/0/Dc/Battery/Soc", 99.0)
    r = _regs(gx)
    assert src.last(r['ess_setpoint'].address)[0] == -15711.0
    assert src.last(r['dvcc'].address)[0] == 'On'
    assert src.last(r['ess_mode'].address)[0] == 'Optimized, no phase compensation'
    assert src.last(r['soc'].address)[0] == 99.0


def test_null_clears_the_value_a_command_reads(gx):
    src = _victron_source(gx)
    r = _regs(gx)
    _say(src, "vebus/276/Ac/ActiveIn/Connected", 1)
    assert src.last(r['grid_connected'].address) is not None
    _say(src, "vebus/276/Ac/ActiveIn/Connected", None)
    assert src.last(r['grid_connected'].address) is None


def test_last_values_come_after_the_store_and_are_dropped_on_a_register_swap(gx):
    src = _victron_source(gx)
    seen = []
    src.publish_callback = lambda pg, data: seen.append(src.last(next(iter(data))))
    _say(src, "system/0/Dc/Battery/Soc", 50.0)
    assert seen == [None]                             # the store was told first
    assert src.last(_regs(gx)['soc'].address)[0] == 50.0
    src.update_registers(src.registers)
    assert src.last(_regs(gx)['soc'].address) is None


def test_a_source_is_alive_by_its_traffic_not_by_each_value(gx):
    src = _victron_source(gx, stale_after_s=60)
    assert not src.alive()                            # nothing yet
    _say(src, "system/0/Dc/Battery/Soc", 50.0)
    assert src.alive()
    src.last_msg_mono -= 61
    assert not src.alive()


# ── the run ──────────────────────────────────────────────────────────────────

def _ready(gx, **cfg):
    src = _victron_source(gx, **cfg)
    _say(src, "settings/0/Settings/CGwacs/Hub4Mode", 2)
    _say(src, "system/0/Control/Dvcc", True)
    _say(src, "settings/0/Settings/CGwacs/AcPowerSetPoint", 20.0)
    _say(src, "settings/0/Settings/SystemSetup/MaxChargeCurrent", 420)
    return src


def _gx_path(topic):
    return topic.split('/', 2)[2]


def test_a_setpoint_is_published_and_verified_on_its_read_back(gx):
    src = _ready(gx)
    _answer_writes(src, _gx_path)
    res = run_mqtt_command(_cmd(gx, 'ess_setpoint'), {'value': -12000}, src, _regs(gx))
    assert res['status'] == 'success', res
    assert res['frames'] == [{'topic': f"W/{PORTAL}/settings/0/Settings/CGwacs/AcPowerSetPoint",
                              'payload': '{"value": -12000}', 'registers': ['ess_setpoint'],
                              'values': {'ess_setpoint': -12000}}]
    assert res['before'] == {'ess_setpoint': 20.0} and res['after'] == {'ess_setpoint': -12000}
    assert res['frames_sent'] == 1


def test_a_value_already_set_is_not_written_again(gx):
    src = _ready(gx)
    res = run_mqtt_command(_cmd(gx, 'max_charge_current'), {'value': 420}, src, _regs(gx))
    assert res['status'] == 'success' and res['already_set'] is True and res['frames_sent'] == 0
    assert src._client.sent == []


def test_the_mode_guard_refuses_external_control(gx):
    src = _ready(gx)
    _say(src, "settings/0/Settings/CGwacs/Hub4Mode", 3)
    res = run_mqtt_command(_cmd(gx, 'ess_setpoint'), {'value': -5000}, src, _regs(gx))
    assert res['status'] == 'rejected' and 'guard' in res['reason'] and src._client.sent == []


def test_a_guard_without_a_current_value_writes_nothing(gx):
    src = _victron_source(gx)
    _say(src, "system/0/Dc/Battery/Soc", 50.0)       # alive, but no Hub4Mode yet
    res = run_mqtt_command(_cmd(gx, 'ess_setpoint'), {'value': -5000}, src, _regs(gx))
    assert res['status'] == 'rejected' and 'ess_mode has no current value' in res['reason']


def test_a_silent_source_writes_nothing(gx):
    src = _ready(gx)
    src.last_msg_mono -= 3600
    res = run_mqtt_command(_cmd(gx, 'ess_setpoint'), {'value': -5000}, src, _regs(gx))
    assert res['status'] == 'rejected' and 'sent nothing lately' in res['reason']


def test_a_register_the_device_does_not_read_is_named(gx):
    src = _ready(gx)
    src.registers = [r for r in src.registers if r.name != 'ess_mode']
    res = run_mqtt_command(_cmd(gx, 'ess_setpoint'), {'value': -5000}, src, _regs(gx))
    assert res['status'] == 'rejected' and 'ess_mode not read by this device' in res['reason']


def test_no_read_back_is_unverified_and_a_wrong_one_is_a_mismatch(gx):
    cmd = _cmd(gx, 'ess_setpoint')
    cmd.settle_s = 0.3
    src = _ready(gx)
    res = run_mqtt_command(cmd, {'value': -5000}, src, _regs(gx))
    assert res['status'] == 'unverified' and res['frames_sent'] == 1
    src = _ready(gx)
    _answer_writes(src, _gx_path)
    real = src.publish
    src.publish = lambda t, p: real(t, json.dumps({'value': -4000}))   # the GX clamps it
    res = run_mqtt_command(cmd, {'value': -5000}, src, _regs(gx))
    assert res['status'] == 'mismatch' and 'expected -5000' in res['reason']


def test_an_old_value_republished_meanwhile_is_not_the_verdict(gx):
    """A keepalive republish of the old value right after the publish, then
    the real answer: success, not mismatch."""
    src = _ready(gx)
    orig = src.publish

    def publish(topic, payload):
        ok = orig(topic, payload)
        threading.Timer(0.02, lambda: _say(src, "settings/0/Settings/CGwacs/AcPowerSetPoint", 20.0)).start()
        threading.Timer(0.15, lambda: _say(src, "settings/0/Settings/CGwacs/AcPowerSetPoint", -5000)).start()
        return ok
    src.publish = publish
    res = run_mqtt_command(_cmd(gx, 'ess_setpoint'), {'value': -5000}, src, _regs(gx))
    assert res['status'] == 'success', res


def test_a_failed_publish_is_an_error_and_a_dry_run_publishes_nothing(gx):
    src = _ready(gx)
    res = run_mqtt_command(_cmd(gx, 'ess_setpoint'), {'value': -5000}, src, _regs(gx), dry_run=True)
    assert res['status'] == 'dry_run' and res['frames'][0]['payload'] == '{"value": -5000}'
    assert src._client.sent == []
    src._client.rc = 4
    res = run_mqtt_command(_cmd(gx, 'ess_setpoint'), {'value': -5000}, src, _regs(gx))
    assert res['status'] == 'error' and 'nothing went out' in res['reason']


def test_a_write_topic_the_gateway_would_hear_is_refused(gx):
    src = _ready(gx)
    regs = _regs(gx)
    cmd = _cmd(gx, 'ess_setpoint')
    res = run_mqtt_command(cmd, {'value': -5000}, src, regs, forbidden_topics=[f"W/{PORTAL}/#"])
    assert res['status'] == 'rejected' and 'command topic of this gateway' in res['reason']
    loop = SimpleNamespace(**{**vars(regs['ess_setpoint']), 'write_topic': regs['ess_setpoint'].topic})
    res = run_mqtt_command(cmd, {'value': -5000}, src, {**regs, 'ess_setpoint': loop})
    assert res['status'] == 'rejected' and 'one this device reads' in res['reason']
    assert src._client.sent == []


def test_the_payload_template():
    reg = SimpleNamespace(name='r', scale=1.0, offset=0.0, scale_from='', enum=None, write_payload='')
    assert mqtt_payload(reg, 12.0, {}) == '12'
    assert mqtt_payload(reg, 12.5, {}) == '12.5'
    reg.write_payload = '{"value": ${value}}'
    assert mqtt_payload(reg, -3, {}) == '{"value": -3}'
    sw = SimpleNamespace(name='relay', scale=1.0, offset=0.0, scale_from='', enum={'0': 'OFF', '1': 'ON'},
                         write_payload='{"state": "${label}"}')
    assert mqtt_payload(sw, 1, {}) == '{"state": "ON"}'
    assert mqtt_payload(sw, 'OFF', {}) == '{"state": "OFF"}'
    with pytest.raises(ValueError):
        mqtt_payload(sw, 'MAYBE', {})
    const = SimpleNamespace(name='restart', scale=1.0, offset=0.0, scale_from='', enum=None,
                            write_payload='Restart')
    assert mqtt_payload(const, 1, {}) == 'Restart'
    txt = SimpleNamespace(name='t', scale=1.0, offset=0.0, scale_from='', enum=None, write_payload='"${value}"')
    assert mqtt_payload(txt, 'a"b', {}) == '"a\\"b"'
    scaled = SimpleNamespace(name='s', scale=10.0, offset=0.0, scale_from='', enum=None, write_payload='')
    assert mqtt_payload(scaled, 2.5, {}) == '25'


# ── the template ─────────────────────────────────────────────────────────────

def _tpl(commands, regs_extra=None, transports=('mqtt',)):
    return {'device_template': {
        'schema_version': 1, 'id': 't_mq', 'name': 'T', 'vendor': 'V', 'model': 'M', 'version': '1',
        'protocol': {'transports': list(transports)},
        'registers': [{'address': 1, 'name': 'sp', 'data_type': 'float', 'topic': 'a/sp',
                       **(regs_extra or {})}],
        'commands': commands}}


def test_an_mqtt_command_needs_a_write_topic():
    cmds = {'set': {'params': {'value': {'required': True}}, 'writes': [{'register': 'sp', 'value': '${value}'}]}}
    errs = extra_errors(_tpl(cmds))
    assert any('has no write_topic' in e for e in errs)
    assert not any('write_topic' in e for e in extra_errors(_tpl(cmds, {'write_topic': 'a/sp/set'})))
    assert any('no + or #' in e for e in extra_errors(_tpl(cmds, {'write_topic': 'a/+/set'})))


def test_the_write_side_survives_a_save():
    t = parse_template(_tpl({}, {'write_topic': 'a/sp/set', 'write_payload': '{"v": ${value}}'}))
    back = t.to_dict()['device_template']['registers'][0]
    assert back['write_topic'] == 'a/sp/set' and back['write_payload'] == '{"v": ${value}}'


def test_a_templates_keepalive_is_checked():
    bad = _tpl({})
    bad['device_template']['protocol']['keepalive'] = {'topic': 'R/#'}
    assert any('protocol.keepalive' in e for e in validate_template(bad))


def test_the_victron_template_loads_whole(gx):
    assert validate_template({'device_template': json.loads(json.dumps(gx.to_dict()['device_template']))}) == []
    assert set(gx.commands) == {'ess_setpoint', 'max_charge_current', 'max_charge_power',
                                'max_discharge_power', 'max_feed_in', 'min_soc'}
    assert all(not c.get('safe') for c in gx.commands.values())          # the site sets it
    assert all(ch.get('min_interval_s') == 60 for ch in gx.channels.values())
    src = _victron_source(gx)
    assert f"N/{PORTAL}/vebus/276/Ac/ActiveIn/Connected" in src.subscriptions()
    assert not any('{' in t for t in src.subscriptions())


# ── through the API: a GX of its own, every face and gate ────────────────────

GX_YAML = f"""
security: {{ allow_writes: true }}
devices:
  - id: gx
    name: Victron GX
    template: victron_gx_mqtt
    connection: {{ protocol: mqtt, broker: gx.local, topic: "N/{PORTAL}/#",
                   vars: {{ portal: "{PORTAL}", vebus: "276" }}, keepalive: false }}
    commands:
      - {{ name: ess_setpoint, safe: {{ value: 0 }} }}
      - {{ name: max_charge_current }}
"""


def _gx_app(tmp_path, gx, mqtt=None):
    from fastapi.testclient import TestClient
    from multibus import auth as _authmod
    from multibus.api import create_api
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml=GX_YAML)
    cfg.ui.auth_enabled = True
    cfg.ui.auth_username = "admin"
    cfg.ui.auth_password = _authmod.hash_password("pw")
    src = _ready(gx)
    _answer_writes(src, _gx_path)
    devices = [(d, src if d.id == 'gx' else None) for d in cfg.devices]
    app, _ = create_api(cfg, None, mqtt, None, devices=devices)
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/api/auth/login", json={"username": "admin", "password": "pw"})
    return cfg, app, client, src


def _audit(tmp_path):
    return [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines() if x.strip()]


def test_a_gx_command_runs_through_the_api_with_audit_and_event(tmp_path, gx):
    cfg, app, client, src = _gx_app(tmp_path, gx)
    cmds = {c['name']: c for c in client.get("/api/devices/gx/commands").json()['commands']}
    assert cmds['ess_setpoint']['enabled'] and not cmds['min_soc']['enabled']
    assert cmds['ess_setpoint']['safe'] == {'value': 0}          # the site's release value
    r = client.post("/api/devices/gx/commands/ess_setpoint", json={"value": -8000})
    assert r.status_code == 200 and r.json()['status'] == 'success', r.text
    assert src._client.sent[-1][0] == f"W/{PORTAL}/settings/0/Settings/CGwacs/AcPowerSetPoint"
    rec = [a for a in _audit(tmp_path) if a.get('action') == 'command'][-1]
    assert rec['target'] == 'gx ess_setpoint' and rec['status'] == 'success'
    ev = client.get("/api/devices/gx/events").json()['events']
    assert ev and ev[0]['kind'] == 'command' and 'ess_setpoint' in ev[0]['message']
    # a disabled command is refused, a dry run previews it
    assert client.post("/api/devices/gx/commands/min_soc", json={"value": 20}).status_code == 403
    d = client.post("/api/devices/gx/commands/min_soc/dry-run", json={"value": 20}).json()
    assert d['status'] == 'dry_run' and d['frames'][0]['payload'] == '{"value": 20}'


def test_a_standby_gateway_does_not_command_the_gx(tmp_path, gx):
    """Neither from the API nor from a rule: the GX belongs to the active one."""
    cfg, app, client, src = _gx_app(tmp_path, gx)
    app.state.role.set('standby', 1, by='test')
    assert client.post("/api/devices/gx/commands/ess_setpoint", json={"value": -8000}).status_code == 503
    code, res = app.state.run_command(cfg.get_device('gx'), src, 'ess_setpoint', {'value': -8000},
                                      who='rule', via='rule:test')
    assert code == 503 and src._client.sent == []
    # the keepalive is a read request: a standby still asks
    src.keepalive = mi.parse_keepalive({'topic': 'R/{portal}/keepalive', 'payload': ''})
    assert src.send_keepalive(first=True) and src._client.sent[-1][0] == f"R/{PORTAL}/keepalive"


def test_each_mqtt_source_has_its_own_command_lock(tmp_path, gx):
    cfg, app, client, src = _gx_app(tmp_path, gx)
    other = _ready(gx)
    assert app.state.cmd_lock_for(src) is app.state.cmd_lock_for(src)
    assert app.state.cmd_lock_for(src) is not app.state.cmd_lock_for(other)


def test_the_mqtt_face_is_subscribed_for_a_device_of_its_own(tmp_path, gx):
    from tests.test_commands_api import _Mqtt
    m = _Mqtt()
    m.unregister_command = lambda t: m.topics.pop(t, None)
    cfg, app, client, src = _gx_app(tmp_path, gx, mqtt=m)
    prefix = cfg.get_device('gx').mqtt_topic_prefix
    assert f"{prefix}/cmd/ess_setpoint" in m.topics
    assert f"{prefix}/cmd/min_soc" not in m.topics                  # not enabled
    # a rebuild leaves the topics it did not make (a sequence's) alone
    m.topics['mbg/sequences/s1/run'] = lambda p: None
    app.state.sync_command_topics()
    assert 'mbg/sequences/s1/run' in m.topics and f"{prefix}/cmd/ess_setpoint" in m.topics


def test_home_assistant_cannot_raw_write_a_gx_setting(tmp_path, gx, caplog):
    """A device read over MQTT is written through its commands only."""
    from tests.test_commands_api import _Mqtt
    m = _Mqtt()
    m.unregister_command = lambda t: m.topics.pop(t, None)
    m.set_command_write_handler = lambda fn: setattr(m, 'handler', fn)
    cfg, app, client, src = _gx_app(tmp_path, gx, mqtt=m)
    cfg.mqtt.allow_write_entities = True
    reg = next(r for r in gx.registers if r.name == 'max_charge_power')   # writable, no enabled command
    cfg.mqtt.username, cfg.mqtt.password = 'gw', 'pw'                  # past the broker gate
    with caplog.at_level('WARNING'):
        m.handler('gx', reg, '5000')
    assert 'written only through an enabled command' in caplog.text, caplog.text
    assert [s for s in src._client.sent if s[0].startswith('W/')] == []


def test_a_trim_rule_commands_the_gx_spaced_by_its_channel(tmp_path, gx):
    """Shadow decides and publishes nothing; armed publishes, verifies, and
    the template's channel keeps a minute between two writes."""
    cfg, app, client, src = _gx_app(tmp_path, gx)
    t = [1000.0]
    clock = lambda: t[0]                                                  # noqa: E731
    rt = app.state.rules_runtime
    rt._clock = rt._mono = clock
    by_name = {r.name: r for r in gx.registers}

    def feed(**values):
        store = app.state.registry.ensure_store('gx')
        for name, v in values.items():
            store[int(by_name[name].address)] = {'value': v, 'name': name, 'ts': clock() - 1,
                                                 'mono': clock() - 1, 'interval': 20}
    rule = {'id': 'absorb', 'mode': 'shadow', 'target': {'device': 'gx', 'command': 'ess_setpoint'},
            'kind': 'trim', 'signal': 'gx.soc', 'above': 90, 'below': 80,
            'step': 1000, 'min': -20000, 'max': 0,
            'timing': {'every_s': 5, 'debounce': 1, 'min_interval_s': 0, 'reassert_s': 0}}
    r = client.post("/api/rules", json=rule)
    assert r.status_code == 200, r.text
    # SOC below `below` lowers the setpoint one step at a time (more export)
    feed(soc=70.0, ess_setpoint=-5000.0)
    rt.tick()
    assert src._client.sent == []                                         # shadow
    assert client.post("/api/rules/absorb/mode", json={'mode': 'armed'}).status_code == 200

    def writes():
        return [s for s in src._client.sent if s[0].startswith('W/')]

    def wait_writes(n):
        deadline = time.time() + 5
        while len(writes()) < n and time.time() < deadline:
            time.sleep(0.05)
        return writes()
    t[0] += 5
    feed(soc=70.0, ess_setpoint=-5000.0)
    rt.tick()
    sent = wait_writes(1)
    assert len(sent) == 1 and json.loads(sent[0][1])['value'] == -6000   # one step from the read-back
    # the next tick wants another step: the channel holds it for a minute
    t[0] += 5
    feed(soc=70.0, ess_setpoint=-6000.0)
    rt.tick()
    time.sleep(0.3)
    assert len(writes()) == 1
    last = client.get("/api/rules").json()['rules'][0]['live']['last']
    assert 'min_interval_s' in last['reason'], last
    t[0] += 60
    feed(soc=70.0, ess_setpoint=-6000.0)
    rt.tick()
    assert len(wait_writes(2)) == 2 and json.loads(writes()[-1][1])['value'] == -7000


def test_the_device_form_sees_and_edits_the_keepalive_never_the_password(tmp_path, gx):
    """The form shows the variables and the keepalive (its own and the
    template's) but never the password; it can set its own keepalive, turn it
    off, or go back to the template's, and the stored password survives."""
    import yaml
    cfg, app, client, src = _gx_app(tmp_path, gx)
    dev = next(d for d in client.get("/api/devices").json()['devices'] if d['id'] == 'gx')
    c = dev['connection']
    assert c['vars'] == {'portal': PORTAL, 'vebus': '276'}
    assert c['keepalive'] is False and c['template_keepalive']['topic'] == 'R/{portal}/keepalive'
    raw = lambda: [d for d in yaml.safe_load((tmp_path / "config.yaml").read_text())['devices'] if d['id'] == 'gx'][0]['connection']  # noqa: E731
    body = {'id': 'gx', 'name': 'Victron GX', 'template': 'victron_gx_mqtt', 'enabled': True,
            'connection': {'protocol': 'mqtt', 'broker': 'gx.local', 'port': 1883, 'topic': f"N/{PORTAL}/#",
                           'username': 'u', 'password': 'secret', 'tls': False, 'vars': {'portal': PORTAL, 'vebus': '276'},
                           'keepalive': False}}
    assert client.put("/api/devices/gx", json=body).status_code == 200
    # the form sends a blank password (keep the stored one) and its own keepalive
    body['connection'].update(password='', keepalive={'topic': 'R/{portal}/keepalive', 'payload': '', 'every_s': 600})
    assert client.put("/api/devices/gx", json=body).status_code == 200
    assert raw()['keepalive']['every_s'] == 600 and raw()['password'] == 'secret'
    shown = next(d for d in client.get("/api/devices").json()['devices'] if d['id'] == 'gx')['connection']
    assert shown['password'] == '******' and shown['keepalive']['every_s'] == 600
    assert client.put("/api/devices/gx", json={**body, 'connection': {**body['connection'], 'keepalive': None}}).status_code == 200
    assert 'keepalive' not in raw()                                       # back to the template's
    assert client.put("/api/devices/gx", json={**body, 'connection': {**body['connection'], 'keepalive': {'topic': 'R/#'}}}).status_code == 422
