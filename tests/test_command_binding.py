# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Command bindings and per-device channel pace (ADR-0019) through the app,
and the dispatcher as every face meets it (ADR-0018): two GX on one
template at 5 s and 60 s, the dead-man's `safe`, renewals that change
nothing, every MQTT refusal on cmd/result with its id, lanes that do not
wait for each other. The EMS acceptance of pv-stack-ems' request
mbg-command-binding-2026-10-25 is a test here."""
import copy
import json
import threading
import time

import pytest

from multibus.command_dispatch import Job
from tests.test_mqtt_commands import PORTAL, _answer_writes, _gx_path, _ready, _say
from tests.test_commands_api import _Mqtt, _app as _plant_app
from tests.test_commands import _Inverter
from tests.test_endpoints import needs_tc

GX_YAML = f"""
security: {{ allow_writes: true }}
mqtt: {{ allow_write_entities: true, username: gw, password: pw }}
devices:
  - id: gx
    name: Victron GX
    template: victron_gx_mqtt
    connection: {{ protocol: mqtt, broker: gx.local, topic: "N/{PORTAL}/#",
                   vars: {{ portal: "{PORTAL}", vebus: "276" }}, keepalive: false }}
    commands:
      - {{ name: ess_setpoint, enabled: true }}
      - {{ name: max_charge_current, enabled: true }}
  - id: gx2
    name: Victron GX two
    template: victron_gx_mqtt
    connection: {{ protocol: mqtt, broker: gx2.local, topic: "N/{PORTAL}/#",
                   vars: {{ portal: "{PORTAL}", vebus: "276" }}, keepalive: false }}
    commands:
      - {{ name: ess_setpoint, enabled: true }}
"""


@pytest.fixture(scope='module')
def gx():
    from multibus.device_template import TemplateRegistry
    return TemplateRegistry().get('victron_gx_mqtt')


class _Gx:
    """Two GX of one template, each read through its own MQTT source, the
    gateway's broker a fake, the rules' clock ours."""

    def __init__(self, tmp_path, gx, extra='', operator=False):
        from fastapi.testclient import TestClient
        from multibus import auth as _authmod
        from multibus.api import create_api
        from tests.test_devices import write_config
        self.cfg = write_config(tmp_path, extra_yaml=GX_YAML + extra)
        self.cfg.ui.auth_enabled = True
        self.cfg.ui.auth_username = "admin"
        self.cfg.ui.auth_password = _authmod.hash_password("pw")
        if operator:
            self.cfg.ui.operator_username, self.cfg.ui.operator_password = "ops", _authmod.hash_password("op")
        self.src = {'gx': _ready(gx), 'gx2': _ready(gx)}
        for s in self.src.values():
            _answer_writes(s, _gx_path)
        self.mq = _Mqtt()
        self.mq.unregister_command = lambda t: self.mq.topics.pop(t, None)
        devices = [(d, self.src.get(d.id)) for d in self.cfg.devices]
        self.app, _ = create_api(self.cfg, None, self.mq, None, devices=devices)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.client.post("/api/auth/login", json={"username": "admin", "password": "pw"})
        self.t = [1000.0]
        rt = self.app.state.rules_runtime
        rt._clock = rt._mono = lambda: self.t[0]
        self.rt = rt

    # ── faces ──
    def mqtt(self, dev, name, value=None, **body):
        if value is not None:
            body['value'] = value
        self.mq.topics[f"mbg/devices/{dev}/cmd/{name}"](json.dumps(body))

    def results(self, dev='gx'):
        return [json.loads(p) for t, p, _r in self.mq.sent if t == f"mbg/devices/{dev}/cmd/result"]

    def result(self, id_, dev='gx', timeout=8.0):
        end = time.time() + timeout
        while time.time() < end:
            got = [r for r in self.results(dev) if r.get('id') == id_]
            if got:
                return got[-1]
            time.sleep(0.01)
        raise AssertionError(f"no cmd/result with id {id_!r}: {self.results(dev)}")

    def writes(self, dev='gx'):
        return [json.loads(p)['value'] for t, p, *_ in self.src[dev]._client.sent if t.startswith('W/')]

    def listing(self, dev='gx'):
        return {c['name']: c for c in self.client.get(f"/api/devices/{dev}/commands").json()['commands']}

    def block_lane(self, dev='gx'):
        """Hold the device's wire with a command of our own."""
        started, release = threading.Event(), threading.Event()

        def run(job):
            started.set()
            release.wait(10)
            return 200, {'status': 'success'}
        d = self.cfg.get_device(dev)
        lane = self.app.state.lane_key(d, self.src[dev])
        self.app.state.command_dispatch.submit(Job(lane=lane, device='blocker', channel='x', command='hold',
                                                   params={}, run=run))
        assert started.wait(3)
        return release


def _put(h, path, body):
    return h.client.put(path, json=body)


# ── the EMS acceptance (pv-stack-ems mbg-command-binding-2026-10-25) ─────────

def test_the_ems_acceptance(tmp_path, gx):
    h = _Gx(tmp_path, gx)
    tpl_before = json.dumps(gx.channels, sort_keys=True)
    r = _put(h, "/api/devices/gx/commands/ess_setpoint",
             {'enabled': True, 'faces': {'api': True, 'mqtt': True, 'ha': True}, 'lease_s': 90, 'safe': {'value': 0}})
    assert r.status_code == 200, r.text
    r = _put(h, "/api/devices/gx/channels/ess_setpoint", {'min_interval_s': 5})
    assert r.status_code == 200, r.text
    r = _put(h, "/api/devices/gx/commands/max_charge_current", {'enabled': True, 'lease_s': 90, 'safe': {'value': 420}})
    assert r.status_code == 200, r.text
    by = h.listing()
    es = by['ess_setpoint']
    assert es['enabled'] is True and es['faces']['mqtt'] is True
    assert es['lease_s'] == 90 and es['safe'] == {'value': 0} and es['safe_from'] == 'device'
    assert es['min_interval_s'] == 5 and es['template_min_interval_s'] == 60 and es['channel'] == 'ess_setpoint'
    mc = by['max_charge_current']
    assert mc['enabled'] is True and mc['lease_s'] == 90 and mc['safe'] == {'value': 420}
    assert mc['min_interval_s'] == 60
    # a command 4 s after the previous one is held, at 5 s it runs
    h.mqtt('gx', 'ess_setpoint', -1000, id='a1', source='ems-io')
    assert h.result('a1')['status'] == 'success'
    h.t[0] += 4
    h.mqtt('gx', 'ess_setpoint', -2000, id='a2', source='ems-io')
    res = h.result('a2')
    assert res['status'] == 'rejected' and res['held'] is True and 0 < res['retry_after_s'] <= 1
    h.t[0] += 1
    h.mqtt('gx', 'ess_setpoint', -2000, id='a3', source='ems-io')
    assert h.result('a3')['status'] == 'success'
    assert h.writes() == [-1000, -2000]
    # nothing changed for any other device or the template
    assert h.listing('gx2')['ess_setpoint']['min_interval_s'] == 60
    assert json.dumps(gx.channels, sort_keys=True) == tpl_before
    import yaml
    raw = {d['id']: d for d in yaml.safe_load((tmp_path / "config.yaml").read_text())['devices']}
    assert raw['gx']['channels'] == {'ess_setpoint': {'min_interval_s': 5}}
    assert 'channels' not in raw['gx2']


# ── pace per device ──────────────────────────────────────────────────────────

def test_two_devices_on_one_template_keep_their_own_pace(tmp_path, gx):
    h = _Gx(tmp_path, gx, extra="")
    assert _put(h, "/api/devices/gx/channels/ess_setpoint", {'min_interval_s': 5}).status_code == 200
    for dev in ('gx', 'gx2'):
        r = h.client.post(f"/api/devices/{dev}/commands/ess_setpoint", json={'value': -100})
        assert r.status_code == 200, r.text
    h.t[0] += 5
    assert h.client.post("/api/devices/gx/commands/ess_setpoint", json={'value': -200}).status_code == 200
    r = h.client.post("/api/devices/gx2/commands/ess_setpoint", json={'value': -200})
    assert r.status_code == 409 and r.json()['held'] is True and r.json()['retry_after_s'] == 55
    # null removes the override: the template's again
    assert _put(h, "/api/devices/gx/channels/ess_setpoint", {'min_interval_s': None}).status_code == 200
    assert h.listing()['ess_setpoint']['min_interval_s'] == 60


def test_a_channel_override_is_keyed_by_a_name_and_an_unknown_one_is_refused(tmp_path, gx):
    h = _Gx(tmp_path, gx)
    r = _put(h, "/api/devices/gx/channels/no_such_channel", {'min_interval_s': 5})
    assert r.status_code == 422 and 'no_such_channel' in r.text
    assert _put(h, "/api/devices/gx/channels/ess_setpoint", {'combine': 'min'}).status_code == 422
    assert _put(h, "/api/devices/gx/channels/ess_setpoint", {'min_interval_s': -1}).status_code == 422
    # one stored for a channel the template no longer has is kept, listed as orphan
    dev = h.app.state.registry.find('gx')[1]
    dev.channels = {'gone_channel': {'min_interval_s': 3}}
    h.app.state.commands_changed()
    body = h.client.get("/api/devices/gx/commands").json()
    assert body['orphan_channels'] == ['gone_channel']


def test_the_device_routes_refuse_an_installation_unit_and_the_group_carries_them(tmp_path):
    extra = """
        channels:
          power: { min_interval_s: 7 }
"""
    import tests.test_commands_api as tca
    yaml_text = tca.PLANT_YAML.replace("        commands:\n          - { name: power_limit }",
                                       extra.rstrip('\n') + "\n        commands:\n          - { name: power_limit, lease_s: 60 }")
    old = tca.PLANT_YAML
    tca.PLANT_YAML = yaml_text
    try:
        cfg, app, client = _plant_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    finally:
        tca.PLANT_YAML = old
    r = client.put("/api/devices/pv-u1/commands/power_limit", json={'enabled': True, 'lease_s': 30})
    assert r.status_code == 422 and 'group' in r.text
    r = client.put("/api/devices/pv-u1/channels/power", json={'min_interval_s': 3})
    assert r.status_code == 422 and 'group' in r.text
    pl = {c['name']: c for c in client.get("/api/devices/pv-u1/commands").json()['commands']}['power_limit']
    assert pl['min_interval_s'] == 7 and pl['channel'] == 'power' and pl['lease_s'] == 60
    assert pl['safe_from'] == 'template'
    # what an editor shows beside the fields: the template's own, the key a
    # save goes under, the group's override (the UI lot of ADR-0019)
    assert pl['template_safe'] == {'value': 100, 'revert_s': 0}
    assert pl['channel_key'] == 'power' and pl['channel_override'] == {'min_interval_s': 7}
    assert pl['template_channel'].get('combine') == 'min'
    grp = next(g for g in client.get("/api/endpoints/pv").json()['groups'] if g['id'] == 'inverters')
    assert grp['channels'] == {'power': {'min_interval_s': 7}}
    gpl = next(c for c in grp['commands'] if c['name'] == 'power_limit')
    assert gpl['lease_s'] == 60 and gpl['channel_key'] == 'power' and gpl['template_safe'] == {'value': 100, 'revert_s': 0}


# ── the binding: lease and safe ──────────────────────────────────────────────

def test_lease_without_safe_is_refused(tmp_path, gx):
    h = _Gx(tmp_path, gx)
    r = _put(h, "/api/devices/gx/commands/max_charge_current", {'enabled': True, 'lease_s': 90})
    assert r.status_code == 422 and 'safe' in r.text
    # a run with a lease and no safe says the dead-man is not armed
    r = h.client.post("/api/devices/gx/commands/max_charge_current", json={'value': 100, 'lease_s': 30})
    assert r.status_code == 200 and r.json()['lease_armed'] is False and 'safe' in r.json()['lease_reason']


def test_safe_out_of_bounds_is_refused(tmp_path, gx):
    h = _Gx(tmp_path, gx)
    r = _put(h, "/api/devices/gx/commands/ess_setpoint", {'lease_s': 90, 'safe': {'value': 999999}})
    assert r.status_code == 422 and 'maximum' in r.text
    r = _put(h, "/api/devices/gx/commands/ess_setpoint", {'safe': {'nope': 1}})
    assert r.status_code == 422


def test_off_keeps_the_binding(tmp_path, gx):
    h = _Gx(tmp_path, gx)
    assert _put(h, "/api/devices/gx/commands/ess_setpoint",
                {'lease_s': 90, 'safe': {'value': 0}}).status_code == 200
    assert _put(h, "/api/devices/gx/commands/ess_setpoint", {'enabled': False}).status_code == 200
    es = h.listing()['ess_setpoint']
    assert es['enabled'] is False and es['lease_s'] == 90 and es['safe'] == {'value': 0}
    r = _put(h, "/api/devices/gx/commands/no_such", {'enabled': True})
    assert r.status_code == 404


def test_the_binding_route_is_admin_only(tmp_path, gx):
    from fastapi.testclient import TestClient
    h = _Gx(tmp_path, gx, operator=True)
    c = TestClient(h.app, raise_server_exceptions=False)
    assert c.post("/api/auth/login", json={"username": "ops", "password": "op"}).status_code == 200
    assert c.put("/api/devices/gx/commands/ess_setpoint", json={'lease_s': 5, 'safe': {'value': 0}}).status_code == 403
    assert c.put("/api/devices/gx/channels/ess_setpoint", json={'min_interval_s': 1}).status_code == 403


def test_a_lease_reverts_to_safe_without_renewal(tmp_path, gx):
    h = _Gx(tmp_path, gx)
    assert _put(h, "/api/devices/gx/commands/ess_setpoint", {'lease_s': 90, 'safe': {'value': 0}}).status_code == 200
    r = h.client.post("/api/devices/gx/commands/ess_setpoint", json={'value': -3000})
    assert r.status_code == 200 and r.json()['lease_armed'] is True
    mgr = h.app.state.lease_manager
    key = next(k for k in mgr._leases if k[0] == 'gx')
    assert 89 <= mgr._leases[key]['expiry'] - time.monotonic() <= 90
    mgr._leases[key]['expiry'] = time.monotonic() - 1           # 90 s and nobody renewed
    mgr._sweep()
    end = time.time() + 8
    while h.writes()[-1:] != [0] and time.time() < end:
        time.sleep(0.02)
    assert h.writes() == [-3000, 0]


def test_a_same_value_renewal_rearms_the_lease_and_does_not_stamp_the_channel(tmp_path, gx):
    h = _Gx(tmp_path, gx)
    assert _put(h, "/api/devices/gx/commands/ess_setpoint", {'lease_s': 90, 'safe': {'value': 0}}).status_code == 200
    assert _put(h, "/api/devices/gx/channels/ess_setpoint", {'min_interval_s': 5}).status_code == 200
    h.mqtt('gx', 'ess_setpoint', -1000, id='r1')
    assert h.result('r1')['status'] == 'success'
    mgr = h.app.state.lease_manager
    key = next(k for k in mgr._leases if k[0] == 'gx')
    gen = mgr._leases[key]['gen']
    h.t[0] += 2                                                  # inside the interval
    h.mqtt('gx', 'ess_setpoint', -1000, id='r2')
    res = h.result('r2')
    assert res['status'] == 'success' and res['already_set'] is True and res['frames_sent'] == 0
    assert mgr._leases[key]['gen'] > gen                         # the dead-man renewed
    assert h.rt.channels[('gx', 'ess_setpoint')].last_write == 1000.0
    h.t[0] += 3                                                  # 5 s after the WRITE, not the renewal
    h.mqtt('gx', 'ess_setpoint', -1500, id='r3')
    assert h.result('r3')['status'] == 'success'
    assert h.writes() == [-1000, -1500]


def test_pending_500_then_100_while_the_device_holds_100_ends_at_100(tmp_path, gx):
    """Review blocker 1: "already set" is judged against the slot, not the
    reading alone — the pending 500 is superseded, and 100 is what the
    device holds: nothing is written, and it does not end at 500."""
    h = _Gx(tmp_path, gx)
    _say(h.src['gx'], "settings/0/Settings/CGwacs/AcPowerSetPoint", 100.0)
    release = h.block_lane()
    h.mqtt('gx', 'ess_setpoint', 500, id='p500')
    h.mqtt('gx', 'ess_setpoint', 100, id='p100')
    assert h.result('p500')['status'] == 'superseded'
    release.set()
    res = h.result('p100')
    assert res['status'] == 'success' and res['already_set'] is True
    assert h.writes() == []
    assert h.listing()['ess_setpoint']['counters']['superseded'] == 1


# ── results over MQTT ────────────────────────────────────────────────────────

def test_every_mqtt_refusal_is_published_with_its_id(tmp_path, gx):
    h = _Gx(tmp_path, gx)
    # held
    h.mqtt('gx', 'ess_setpoint', -10, id='h1')
    assert h.result('h1')['status'] == 'success'
    h.mqtt('gx', 'ess_setpoint', -20, id='h2')
    r = h.result('h2')
    assert r['status'] == 'rejected' and r['held'] is True and r['retry_after_s'] == 60
    # a bad parameter
    h.mqtt('gx', 'ess_setpoint', 10 ** 9, id='b1')
    assert 'maximum' in h.result('b1')['reason']
    # not enabled (a group topic, or a binding turned off meanwhile)
    h.app.state.mqtt_command(('gx',), 'min_soc', json.dumps({'value': 20, 'id': 'n1'}))
    assert 'not enabled' in h.result('n1')['reason']
    # write-locked
    h.app.state.registry.find('gx')[1].write_locked = True
    h.mqtt('gx', 'max_charge_current', 50, id='w1')
    assert 'write-locked' in h.result('w1')['reason']
    h.app.state.registry.find('gx')[1].write_locked = False
    # writes over MQTT off at the gateway
    h.cfg.mqtt.allow_write_entities = False
    h.mqtt('gx', 'max_charge_current', 50, id='g1')
    r = h.result('g1')
    assert r['status'] == 'rejected' and 'allow_write_entities' in r['reason']
    assert all(x.get('command') for x in h.results())
    # an id longer than 64 characters is not echoed blindly
    h.cfg.mqtt.allow_write_entities = True
    h.mqtt('gx', 'max_charge_current', 50, id='x' * 65)
    end = time.time() + 3
    while not any('id' in (x.get('reason') or '') for x in h.results()) and time.time() < end:
        time.sleep(0.01)
    assert any('id' in (x.get('reason') or '') for x in h.results())


def test_a_standby_publishes_nothing(tmp_path, gx):
    h = _Gx(tmp_path, gx)
    h.app.state.role.set('standby', 1, by='test')
    n = len(h.mq.sent)
    h.mqtt('gx', 'ess_setpoint', -10, id='s1')
    time.sleep(0.2)
    assert [s for s in h.mq.sent[n:] if '/cmd/' in s[0]] == [] and h.writes() == []


def test_the_retained_state_is_only_what_was_applied(tmp_path, gx):
    h = _Gx(tmp_path, gx)
    h.mqtt('gx', 'ess_setpoint', -10, id='ok')
    assert h.result('ok')['status'] == 'success'
    states = lambda: [p for t, p, r in h.mq.sent if t == 'mbg/devices/gx/cmd/ess_setpoint/state' and r]  # noqa: E731
    assert len(states()) == 1
    _say(h.src['gx'], "settings/0/Settings/CGwacs/Hub4Mode", 3)          # the guard refuses now
    h.t[0] += 61
    h.mqtt('gx', 'ess_setpoint', -20, id='guard')
    r = h.result('guard')
    assert r['status'] == 'rejected' and 'guard' in r['reason']
    assert len(states()) == 1 and json.loads(states()[-1])['status'] == 'success'


# ── lanes and spending at execution ──────────────────────────────────────────

def test_a_slow_device_does_not_delay_another_lane(tmp_path, gx):
    """CMD-Q-1: the MQTT worker only submits, and the other GX's wire is its own."""
    h = _Gx(tmp_path, gx)
    release = h.block_lane('gx')
    t0 = time.time()
    h.mqtt('gx', 'ess_setpoint', -10, id='slow')
    h.mqtt('gx2', 'ess_setpoint', -10, id='fast')
    assert time.time() - t0 < 0.5                       # the handler did not wait for the blocked lane
    assert h.result('fast', dev='gx2')['status'] == 'success'
    assert not [r for r in h.results('gx') if r.get('id') == 'slow']
    release.set()
    assert h.result('slow')['status'] == 'success'


def test_the_lane_key_is_stable_across_a_client_swap(tmp_path, gx):
    h = _Gx(tmp_path, gx)
    d = h.cfg.get_device('gx')
    k1 = h.app.state.lane_key(d, h.src['gx'])
    k2 = h.app.state.lane_key(d, _ready(gx))
    assert k1 == k2 and k1.startswith('mqtt:gx')
    assert h.app.state.cmd_lock_for(h.src['gx'], d) is h.app.state.cmd_lock_for(_ready(gx), d)
    assert k1 != h.app.state.lane_key(h.cfg.get_device('gx2'), h.src['gx2'])


def test_pause_and_the_rate_token_are_spent_only_at_execution(tmp_path, gx):
    h = _Gx(tmp_path, gx)
    h.cfg.security.write_rate_limit_per_s = 1.0                   # one token
    paused = []
    h.rt.owner_of = lambda dev, cmd, now=None: 'trim'
    h.rt.safety_holder = lambda dev, cmd: None
    h.rt.pause_channel = lambda dev, cmd, secs, who, via: paused.append((dev, cmd, secs))
    release = h.block_lane()
    h.mqtt('gx', 'ess_setpoint', -500, id='p1', override_s=60)
    h.mqtt('gx', 'ess_setpoint', -600, id='p2', override_s=60)
    assert h.result('p1')['status'] == 'superseded'
    release.set()
    assert h.result('p2')['status'] == 'success'               # the superseded one spent no token
    assert paused == [('gx', 'ess_setpoint', 60.0)]


def test_opcua_and_group_commands_go_through_the_dispatcher(tmp_path, gx):
    h = _Gx(tmp_path, gx)
    seen = []
    disp = h.app.state.command_dispatch
    real = disp.submit
    disp.submit = lambda job: (seen.append((job.device, job.command, job.kind)), real(job))[1]
    code, res = h.app.state.opcua._run('gx', 'ess_setpoint', {'value': -50}, 'admin')
    assert code == 200 and res['status'] == 'success'
    assert seen == [('gx', 'ess_setpoint', 'command')]


@needs_tc
def test_a_group_command_submits_every_unit_then_waits(tmp_path):
    a, b = _Inverter(sf=-2), _Inverter(sf=-2)
    cfg, app, client = _plant_app(tmp_path, {'pv-u1': a, 'pv-u2': b})
    seen = []
    disp = app.state.command_dispatch
    real = disp.submit
    disp.submit = lambda job: (seen.append(job.device), real(job))[1]
    r = client.post("/api/endpoints/pv/groups/inverters/commands/power_limit", json={"value": 70})
    assert r.status_code == 200 and r.json()['ok'] is True
    assert sorted(seen) == ['pv-u1', 'pv-u2']


def test_the_opcua_server_runs_commands_on_its_own_executor():
    from multibus.opcua import server as srv
    assert hasattr(srv.OpcUaServer, '_command_executor')


# ── a Modbus device: "already set" only by the template's opt-in ─────────────

@needs_tc
def test_a_modbus_command_is_skipped_only_by_skip_if_set_and_a_fresh_reading(tmp_path, monkeypatch):
    from multibus.config import store_key
    inv = _Inverter(sf=-2)
    cfg, app, client = _plant_app(tmp_path, {'pv-u1': inv})
    tpl = app.state.template_registry.get('fronius_sunspec_inverter')
    monkeypatch.setitem(tpl.commands, 'limit_word', {
        'params': {'value': {'min': 0, 'max': 100, 'required': True}},
        'writes': [{'register': 'power_limit_pct', 'value': '${value}'}], 'skip_if_set': True})
    app.state.registry.find('pv-u1')[1].commands.append({'name': 'limit_word'})
    app.state.commands_changed()
    reg = next(r for r in tpl.registers if r.name == 'power_limit_pct')
    store = app.state.registry.ensure_store('pv-u1')
    store[store_key(reg.address, 'holding')] = {'value': 60.0, 'name': reg.name, 'ts': time.time(),
                                                'mono': time.monotonic(), 'interval': 10}
    r = client.post("/api/devices/pv-u1/commands/limit_word", json={'value': 60})
    assert r.status_code == 200 and r.json()['already_set'] is True and inv.writes == []
    # a reading older than two poll periods is not trusted: written
    store[store_key(reg.address, 'holding')] = {**store[store_key(reg.address, 'holding')],
                                                'mono': time.monotonic() - 25}
    r = client.post("/api/devices/pv-u1/commands/limit_word", json={'value': 60})
    assert r.status_code == 200 and not r.json().get('already_set') and len(inv.writes) == 1
    # without the opt-in (power_limit: it re-writes its revert timer) always written
    n = len(inv.writes)
    store[store_key(reg.address, 'holding')] = {**store[store_key(reg.address, 'holding')],
                                                'value': 60.0, 'mono': time.monotonic()}
    client.post("/api/devices/pv-u1/commands/power_limit", json={'value': 60, 'revert_s': 0})
    assert len(inv.writes) == n + 1


def test_a_template_says_coalesce_and_skip_if_set_plainly():
    from multibus.device_template import extra_errors
    from tests.test_mqtt_commands import _tpl
    cmds = {'set': {'params': {'value': {'required': True}, 'revert_s': {'min': 0, 'max': 600}},
                    'writes': [{'register': 'sp', 'value': '${value}'}], 'coalesce': 'no'}}
    t = _tpl(cmds, {'write_topic': 'a/sp/set'})
    assert any('coalesce must be true or false' in e for e in extra_errors(t))
    cmds['set'].update(coalesce=False, skip_if_set=True)
    assert any('skip_if_set cannot go with' in e for e in extra_errors(t))
    t['device_template']['channels'] = {'c1': {'max_age_s': 0}}
    assert any('max_age_s 1..86400' in e for e in extra_errors(t))


# ── review 2 (CR2) ───────────────────────────────────────────────────────────

def _yaml_app(tmp_path, yaml_text, inverters):
    """The plant app of test_commands_api on another config."""
    import tests.test_commands_api as tca
    old = tca.PLANT_YAML
    tca.PLANT_YAML = yaml_text
    try:
        return _plant_app(tmp_path, inverters)
    finally:
        tca.PLANT_YAML = old


GX3 = """
  - id: gx3
    name: Victron GX three
    template: victron_gx_mqtt
    connection: { protocol: mqtt, broker: gx3.local, topic: "N/x/#", keepalive: false }
    commands:
      - { name: max_charge_current, enabled: true, lease_s: 60 }
"""


def test_turning_a_command_off_is_never_refused_by_what_is_stored(tmp_path, gx):
    """CR2-1 (proof ported from the review): a stored lease without a safe
    value made the …/enabled route refuse to DISABLE the command."""
    h = _Gx(tmp_path, gx, extra=GX3)
    r = _put(h, "/api/devices/gx3/commands/max_charge_current/enabled", {"enabled": False})
    assert r.status_code == 200, r.json()
    assert _put(h, "/api/devices/gx3/commands/max_charge_current", {"enabled": True}).status_code == 200
    assert _put(h, "/api/devices/gx3/commands/max_charge_current", {"faces": {"mqtt": False}}).status_code == 200
    # what the request changes is judged: the lease again, without a safe
    r = _put(h, "/api/devices/gx3/commands/max_charge_current", {"lease_s": 30})
    assert r.status_code == 422 and 'safe' in r.text


def test_the_execution_timeout_counts_the_recipe(tmp_path, gx):
    """CR2-3: three transactions was the assumption; a recipe with more
    reads and frames ran past it and was answered 504 while it still ran."""
    from types import SimpleNamespace
    from multibus.commands import parse_command_def
    h = _Gx(tmp_path, gx)
    cmd = parse_command_def('x', {'guard': [{'read': 'a'}] * 2, 'writes': [{'register': 'a', 'value': 1}] * 5,
                                  'verify': [{'read': 'a'}] * 3, 'settle_s': 1})
    drv = SimpleNamespace(connection=SimpleNamespace(config=SimpleNamespace(timeout=1, retry_attempts=0)))
    assert h.app.state.exec_timeout({'cmd': cmd, 'mq': None, 'drv': drv}) == 16 * 1 + 1.0


@needs_tc
def test_a_revert_still_running_is_in_flight_not_failed(tmp_path):
    """CR2-3: a revert whose wait ran out while it still ran (504) was read
    as a failure — the dead-man fired it again and logged an ERROR."""
    inv = _Inverter(sf=-2)
    cfg, app, client = _plant_app(tmp_path, {'pv-u1': inv})
    assert client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 40, "lease_s": 60}).status_code == 200
    disp = app.state.command_dispatch
    real = disp.wait_for
    disp.wait_for = lambda job, timeout=None: (504, {'status': 'error', 'reason': 'still running'})
    try:
        app.state.lease_manager._leases[('pv-u1', 'command', 40232)]['revert'](lambda: True)   # does not raise
    finally:
        disp.wait_for = real
    assert inv.writes[-1][1] == [10000, 0, 0, 0, 0]


EP2_YAML = """
security: { allow_writes: true }
endpoints:
  - id: pv2
    name: PV two
    template: fronius_sunspec_inverter
    mqtt: { topic_prefix: "pv2/units/${unit_id}" }
    connection: { protocol: tcp, host: 192.0.2.10, port: 502 }
    units: [1]
    channels:
      power: { min_interval_s: 9 }
    commands:
      - { name: power_limit }
"""


@needs_tc
def test_an_installation_without_groups_keeps_its_channels(tmp_path, monkeypatch):
    """CR2-4: the units of an installation without groups read its top-level
    `channels`, but its save never wrote them — lost on the next edit — and
    the unit routes pointed to a group that does not exist."""
    import tests.test_commands_api as tca
    monkeypatch.setattr(tca._Facade, 'disconnect', lambda self: None, raising=False)   # the save restarts the unit
    cfg, app, client = _yaml_app(tmp_path, EP2_YAML, {'pv2-u1': _Inverter(sf=-2)})
    pl = {c['name']: c for c in client.get("/api/devices/pv2-u1/commands").json()['commands']}['power_limit']
    assert pl['min_interval_s'] == 9
    r = client.put("/api/devices/pv2-u1/channels/power", json={'min_interval_s': 3})
    assert r.status_code == 422 and 'on the installation itself' in r.text
    body = copy.deepcopy(cfg.get_raw_endpoint('pv2'))
    body['channels'] = {'power': {'min_interval_s': 4}}
    r = client.put("/api/endpoints/pv2", json=body)
    assert r.status_code == 200, r.text
    assert cfg.get_raw_endpoint('pv2')['channels'] == {'power': {'min_interval_s': 4}}
    body['name'] = 'Renamed'
    body.pop('channels')                                       # a form that does not speak of them keeps them
    assert client.put("/api/endpoints/pv2", json=body).status_code == 200
    assert cfg.get_raw_endpoint('pv2')['channels'] == {'power': {'min_interval_s': 4}}
    body['channels'] = {'nope': {'min_interval_s': 4}}
    assert client.put("/api/endpoints/pv2", json=body).status_code == 422
    # the save restarted the unit on a real Modbus client: stopped here, or
    # its reconnect loop runs on into the next tests' patched time.sleep
    for _d, cl in app.state.registry.pairs():
        if hasattr(cl, 'disconnect'):
            cl.disconnect()


@needs_tc
def test_a_stored_safe_does_not_block_an_unrelated_save(tmp_path):
    """CR2-5: a safe the template's bounds no longer allow, stored before,
    refused every later save of the installation."""
    import tests.test_commands_api as tca
    yaml_text = tca.PLANT_YAML.replace("          - { name: power_limit }",
                                       "          - { name: power_limit, safe: { value: 999 } }")
    cfg, app, client = _yaml_app(tmp_path, yaml_text, {'pv-u1': _Inverter(sf=-2)})
    body = copy.deepcopy(cfg.get_raw_endpoint('pv'))
    body['name'] = 'Renamed'
    r = client.put("/api/endpoints/pv", json=body)
    assert r.status_code == 200, r.text
    body['groups'][0]['commands'][0]['safe'] = {'value': 998}         # changed: judged
    assert client.put("/api/endpoints/pv", json=body).status_code == 422


def test_a_rule_write_behaves_as_before_until_the_rules_lot(tmp_path, gx):
    """CR2-6: a rule's write that finds the value already set still stamps
    the channel and spends the write-rate token, as before 3.93 (the export
    cap on the GX); the renewal shortcut is for outside writers only."""
    h = _Gx(tmp_path, gx)
    dev, src = h.cfg.get_device('gx'), h.src['gx']
    code, res = h.app.state.run_command(dev, src, 'ess_setpoint', {'value': 20}, who='rule', via='rule:cap')
    assert code == 200 and res['already_set'] is True
    assert h.rt.channels[('gx', 'ess_setpoint')].last_write == 1000.0
    h.cfg.security.write_rate_limit_per_s = 1.0
    h.app.state.run_command(dev, src, 'ess_setpoint', {'value': 20}, who='rule', via='rule:cap2')
    code, res = h.app.state.run_command(dev, src, 'ess_setpoint', {'value': 20}, who='rule', via='rule:cap2')
    assert code == 429


def test_a_disabled_device_writes_nothing_that_waited(tmp_path, gx):
    """CR2-8: disabling a device left its waiting commands in the lane."""
    h = _Gx(tmp_path, gx)
    release = h.block_lane()
    h.mqtt('gx', 'ess_setpoint', -77, id='w')
    body = {'id': 'gx', 'name': 'Victron GX', 'template': 'victron_gx_mqtt', 'enabled': False,
            'connection': {'protocol': 'mqtt', 'broker': 'gx.local', 'port': 1883, 'topic': f"N/{PORTAL}/#",
                           'vars': {'portal': PORTAL, 'vebus': '276'}, 'keepalive': False}}
    assert h.client.put("/api/devices/gx", json=body).status_code == 200
    release.set()
    h.app.state.command_dispatch.drain()
    assert h.writes() == []
    assert h.app.state.command_dispatch.lanes() == {} or not any(
        v['pending'] for v in h.app.state.command_dispatch.lanes().values())


def test_one_device_two_config_objects_one_memo_entry(tmp_path, gx):
    """CR2-9: the registry's and the config's copies of one device each
    rebuilt the command list and cleared the rules' channel cache."""
    import dataclasses
    h = _Gx(tmp_path, gx)
    a = h.app.state.registry.find('gx')[1]
    b = dataclasses.replace(a)
    first = h.app.state.commands_for(a)
    h.rt.__dict__.setdefault('_chan_cache', {})['sentinel'] = 1
    second = h.app.state.commands_for(b)
    assert [c['def'] for c in first] == [c['def'] for c in second]
    assert all(x['def'] is y['def'] for x, y in zip(first, second))
    assert h.rt._chan_cache.get('sentinel') == 1


def test_the_rules_channel_cache_cleared_meanwhile_is_no_keyerror(tmp_path, gx):
    """CR2-9: `cache[key] = …; return cache[key]` raised KeyError when the
    API cleared the cache from another thread in between."""
    class _Forgets(dict):
        def __setitem__(self, k, v):
            pass                                          # cleared at once, as by another thread
    h = _Gx(tmp_path, gx)
    h.rt.__dict__['_chan_cache'] = _Forgets()
    got = h.rt._chan(h.cfg.get_device('gx'), 'ess_setpoint')
    assert got is not None and got[0] == 'ess_setpoint'


def test_a_component_shares_its_parents_wire(tmp_path, gx):
    """CR2-10: a component reads through its parent's MQTT link; it got a
    lane of its own and could write beside its parent."""
    from types import SimpleNamespace
    h = _Gx(tmp_path, gx)
    parent, comp = _ready(gx), _ready(gx)
    parent.link_name = comp.link_name = 'gx'
    assert h.app.state.lane_key(SimpleNamespace(id='gx-bat'), comp) == \
        h.app.state.lane_key(h.cfg.get_device('gx'), parent)


def test_a_dry_run_takes_and_echoes_the_id(tmp_path, gx):
    """CR2-11 (live black-box test): the preview refused `id` as an unknown
    parameter while the run took it."""
    h = _Gx(tmp_path, gx)
    r = h.client.post("/api/devices/gx/commands/ess_setpoint/dry-run", json={'value': 1, 'id': 'x'})
    assert r.status_code == 200 and r.json()['status'] == 'dry_run' and r.json()['id'] == 'x', r.text


def test_every_published_result_carries_its_params(tmp_path, gx):
    """CR2-12 (live black-box test): a client without ids matches results by
    command and value; refusals at admission carried no params."""
    h = _Gx(tmp_path, gx)
    h.mqtt('gx', 'ess_setpoint', -10)
    h.app.state.command_dispatch.drain()
    h.mqtt('gx', 'ess_setpoint', -20)                              # held
    h.mqtt('gx', 'ess_setpoint', 10 ** 9)                          # out of range
    h.app.state.mqtt_command(('gx',), 'min_soc', json.dumps({'value': 20}))   # not enabled
    release = h.block_lane()
    h.t[0] += 61
    h.mqtt('gx', 'ess_setpoint', -30)
    h.mqtt('gx', 'ess_setpoint', -40)                              # supersedes -30
    release.set()
    h.app.state.command_dispatch.drain()
    got = [(r['command'], r['status'], (r.get('params') or {}).get('value')) for r in h.results()]
    assert ('ess_setpoint', 'success', -10) in got, h.results()
    assert ('ess_setpoint', 'rejected', -20) in got
    assert ('ess_setpoint', 'rejected', 10 ** 9) in got
    assert ('min_soc', 'rejected', 20) in got
    assert ('ess_setpoint', 'superseded', -30) in got
    assert all('params' in r for r in h.results()), h.results()


@needs_tc
def test_a_partial_safe_is_laid_over_the_templates(tmp_path):
    """CR2-13 (UI lot): a binding's safe {value: 50} replaced the template's
    whole safe — revert_s fell back to its default 600 and the dead-man
    left an inverter timer running."""
    import tests.test_commands_api as tca
    yaml_text = tca.PLANT_YAML.replace("          - { name: power_limit }",
                                       "          - { name: power_limit, safe: { value: 50 } }")
    inv = _Inverter(sf=-2)
    cfg, app, client = _yaml_app(tmp_path, yaml_text, {'pv-u1': inv})
    pl = {c['name']: c for c in client.get("/api/devices/pv-u1/commands").json()['commands']}['power_limit']
    assert pl['safe'] == {'value': 50, 'revert_s': 0}
    assert pl['safe_from'] == 'binding+template'
    assert pl['safe_from_keys'] == {'value': 'device', 'revert_s': 'template'}
    assert client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 40, "lease_s": 60}).status_code == 200
    app.state.lease_manager._leases[('pv-u1', 'command', 40232)]['revert'](lambda: True)
    assert inv.writes[-1][1][:3] == [5000, 0, 0]                  # 50 %, and no device timer


VFD = """
  - id: vfd
    name: VFD
    template: delta_vfd_el
    enabled: false
    connection: { protocol: tcp, host: 192.0.2.20, port: 502, unit_id: 1 }
    channels:
      set_frequency: { min_interval_s: 3 }
      motor: { min_interval_s: 7 }
"""


def test_one_channel_one_override_key(tmp_path, gx):
    """CR2-14 (UI lot): `motor` and `set_frequency` both name the VFD's motor;
    which override won depended on the file's order. The declared name
    wins at load, the other is a conflict; a second key is refused."""
    h = _Gx(tmp_path, gx, extra=VFD)
    body = h.client.get("/api/devices/vfd/commands").json()
    run = {c['name']: c for c in body['commands']}['run']
    assert run['min_interval_s'] == 7 and run['channel_key'] == 'motor'
    assert body['channel_conflicts'] == ['set_frequency']
    # stored only under the member name: a second key for the channel is refused
    h.app.state.registry.find('vfd')[1].channels = {'set_frequency': {'min_interval_s': 3}}
    h.app.state.commands_changed()
    h.cfg._raw_devices = [dict(d, channels={'set_frequency': {'min_interval_s': 3}}) if d.get('id') == 'vfd' else d
                          for d in h.cfg._raw_devices]
    r = _put(h, "/api/devices/vfd/channels/motor", {'min_interval_s': 9})
    assert r.status_code == 422 and "'set_frequency'" in r.text
    assert _put(h, "/api/devices/vfd/channels/set_frequency", {'min_interval_s': 9}).status_code == 200


# ── the lease is the outside controller's dead-man (RD-1, RD-2) ─────────────

def _leased_plant(tmp_path):
    import tests.test_commands_api as tca
    yaml_text = tca.PLANT_YAML.replace("          - { name: power_limit }",
                                       "          - { name: power_limit, lease_s: 90 }")
    inv = _Inverter(sf=-2)
    cfg, app, client = _yaml_app(tmp_path, yaml_text, {'pv-u1': inv})
    return cfg, app, client, inv


def _leases(app, dev='pv-u1'):
    return [x for x in app.state.lease_manager.snapshot() if x['device'] == dev]


@needs_tc
def test_a_rules_write_never_arms_the_bindings_lease(tmp_path):
    """RD-1: with lease_s on the Fronius power_limit binding, an OV curtail
    armed the dead-man — 90 s later it put 100 % back over the rule."""
    cfg, app, client, inv = _leased_plant(tmp_path)
    dev = cfg.get_device('pv-u1')
    code, res = app.state.run_command(dev, app.state.registry.find('pv-u1')[2], 'power_limit',
                                      {'value': 60}, who='rule', via='rule:ov-u1')
    assert code == 200 and inv.writes[-1][1][0] == 6000
    assert _leases(app) == [] and not res.get('lease_armed')
    # an outside controller's write still arms it, as it always did
    assert client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 50}).json()['lease_armed'] is True
    assert len(_leases(app)) == 1


@needs_tc
def test_a_rules_write_ends_an_outside_controllers_lease(tmp_path):
    """RD-2: an EMS lease outlived a later rule write on the same command;
    its revert would then write the safe value over the rule's curtail."""
    cfg, app, client, inv = _leased_plant(tmp_path)
    assert client.post("/api/devices/pv-u1/commands/power_limit",
                       json={"value": 50, "lease_s": 60}).json()['lease_armed'] is True
    mgr = app.state.lease_manager
    key = ('pv-u1', 'command', 40232)
    assert key in mgr._leases
    app.state.run_command(cfg.get_device('pv-u1'), app.state.registry.find('pv-u1')[2], 'power_limit',
                          {'value': 60}, who='rule', via='rule:ov-u1')
    assert key not in mgr._leases and _leases(app) == []
    n = len(inv.writes)
    mgr._sweep()                                              # nothing left to fire
    assert len(inv.writes) == n and inv.writes[-1][1][0] == 6000


@needs_tc
def test_a_rules_write_ends_every_lease_on_its_channel(tmp_path, monkeypatch):
    """RD-2, the whole channel: an outside lease taken through ANOTHER
    command of the rule's channel (keyed on that command's own register)
    survived the rule's write."""
    cfg, app, client, inv = _leased_plant(tmp_path)
    tpl = app.state.template_registry.get('fronius_sunspec_inverter')
    monkeypatch.setitem(tpl.commands, 'limit_window', {
        'params': {'value': {'min': 0, 'max': 600, 'required': True}}, 'channel': 'power',
        'writes': [{'register': 'power_limit_win_s', 'value': '${value}'}]})
    app.state.commands_changed()
    other = next(r for r in tpl.registers if r.name == 'power_limit_win_s')
    mgr = app.state.lease_manager
    mgr.arm('pv-u1', 'command', int(other.address), 60_000, lambda is_current: None,
            meta={'device': 'pv-u1', 'command': 'limit_window'})
    app.state.run_command(cfg.get_device('pv-u1'), app.state.registry.find('pv-u1')[2], 'power_limit',
                          {'value': 60}, who='rule', via='rule:ov-u1')
    assert ('pv-u1', 'command', int(other.address)) not in mgr._leases
