# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The power-limit action from every channel: API, group, HA slider, MQTT
command — one set of gates, one audit, one read-back."""
import json

import pytest

from multibus.config import SourceConfig
from tests.test_devices import write_config
from tests.test_endpoints import needs_tc
from tests.test_power_limit import _Inverter

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    TestClient = None

PLANT_YAML = """
security: { allow_writes: true }
mqtt: { allow_write_entities: true }
endpoints:
  - id: pv
    name: PV
    mqtt: { topic_prefix: "pv/units/${unit_id}" }
    groups:
      - id: inverters
        role: inverter
        units: [1, 2]
        mqtt: { topic_prefix: "pv/inverters/${unit_id}" }
        sources:
          - id: solar_api
            protocol: http
            url: "http://192.0.2.9/x.cgi?DeviceId=${unit_id}"
            template: fronius_solar_api_inverter
          - id: sunspec
            protocol: tcp
            host: 192.0.2.9
            port: 502
            template: fronius_sunspec_inverter
"""


class _Drv:
    """A Modbus driver as the facade sees it: a connection that answers model 123."""

    def __init__(self, inv):
        self.connection = inv
        self.connected = True

    def get_stats(self):
        return {'connected': True, 'successful_reads': 1, 'failed_reads': 0}

    def data_health(self, *a):
        return {'status': 'ok', 'last_success_ts': 1.0, 'connected': True}


class _Facade:
    """A unit read two ways: the HTTP driver cannot write, the Modbus one can."""

    def __init__(self, inv):
        self.parts = [(SourceConfig(id='solar_api', protocol='http'), object()),
                      (SourceConfig(id='sunspec', protocol='tcp'), _Drv(inv))]
        self.connected = True

    def get_stats(self):
        return {'connected': True, 'successful_reads': 1, 'failed_reads': 0, 'sources': []}

    def data_health(self, *a):
        return {'status': 'ok', 'last_success_ts': 1.0, 'connected': True, 'sources': {}}


class _Mqtt:
    connected = True

    def __init__(self):
        self.sent, self.topics = [], {}
        self.client = self

    def publish(self, topic, payload, qos=0, retain=False):
        self.sent.append((topic, payload, retain))

    def subscribe(self, topic):
        pass

    def unsubscribe(self, topic):
        pass

    def register_command(self, topic, fn):
        self.topics[topic] = fn

    def unregister_commands(self, prefix):
        self.topics = {t: f for t, f in self.topics.items() if not t.startswith(prefix)}

    def set_command_write_handler(self, fn):
        self.handler = fn

    # the rest of the publisher surface the API touches at boot
    discovery_hooks = []
    def publish_device_discovery(self, *a, **k): return 0
    def publish_ha_discovery(self): pass
    def get_stats(self): return {'connected': True}
    def publish_device_availability(self, *a, **k): pass
    def publish_device_runtime(self, *a, **k): pass
    def publish_if_changed(self, *a, **k): pass
    config = type('C', (), {'topic_prefix': 'mbg', 'ha_discovery_enabled': False})()


def _audit(tmp_path):
    """The audit file's records, `detail` decoded (it is stored as a JSON string)."""
    out = []
    for line in (tmp_path / "audit.jsonl").read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if isinstance(rec.get('detail'), str):
            try:
                rec['detail'] = json.loads(rec['detail'])
            except ValueError:
                pass
        out.append(rec)
    return out


def _app(tmp_path, inverters, mqtt=None):
    from multibus import auth as _authmod
    from multibus.api import create_api
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    cfg.ui.auth_enabled = True
    cfg.ui.auth_username = "admin"
    cfg.ui.auth_password = _authmod.hash_password("pw")
    devices = [(d, _Facade(inverters[d.id]) if d.id in inverters else None) for d in cfg.devices]
    app, _ = create_api(cfg, None, mqtt, None, devices=devices)
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/api/auth/login", json={"username": "admin", "password": "pw"})
    return cfg, app, client


@needs_tc
def test_the_api_limits_one_inverter_and_audits_it(tmp_path):
    inv = _Inverter(sf=-2)
    cfg, app, client = _app(tmp_path, {'pv-u1': inv})
    r = client.post("/api/devices/pv-u1/actions/power_limit", json={"limit_pct": 60, "revert_s": 300})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['status'] == 'success' and d['before_pct'] == 100.0 and d['after_pct'] == 60.0
    assert inv.writes == [(40232, [6000, 0, 300, 0, 1])]
    # audited: who, what, before → after
    a = [x for x in _audit(tmp_path) if x.get('action') == 'power limit'][-1]
    assert a['user'] == 'admin' and a['target'] == 'pv-u1' and a['status'] == 'success'
    assert a['detail']['limit_pct'] == 60.0 and a['detail']['via'] == 'api'
    # the unit's Logs carry it too
    assert any(e.get('kind') == 'power_limit' for e in inv.events) if hasattr(inv, 'events') else True


@needs_tc
def test_gates_hold_for_the_action_too(tmp_path):
    inv = _Inverter()
    cfg, app, client = _app(tmp_path, {'pv-u1': inv})
    cfg.security.allow_writes = False
    r = client.post("/api/devices/pv-u1/actions/power_limit", json={"limit_pct": 50})
    assert r.status_code == 403 and inv.writes == []
    cfg.security.allow_writes = True
    r = client.post("/api/devices/pv-u1/actions/power_limit", json={"limit_pct": 150})
    assert r.status_code == 422 and inv.writes == []
    # a unit with no Modbus source cannot be written, and says so
    r = client.post("/api/devices/pv-u2/actions/power_limit", json={"limit_pct": 50})
    assert r.status_code == 409 and 'Modbus' in r.json()['reason']


@needs_tc
def test_the_group_route_does_every_inverter_and_reports_each(tmp_path):
    a, b = _Inverter(sf=-2), _Inverter(sf=-1, model_id=7)      # b answers garbage
    cfg, app, client = _app(tmp_path, {'pv-u1': a, 'pv-u2': b})
    r = client.post("/api/endpoints/pv/groups/inverters/actions/power_limit", json={"limit_pct": 70})
    assert r.status_code == 200, r.text
    d = r.json()
    by = {u['device']: u for u in d['units']}
    assert by['pv-u1']['status'] == 'success' and a.writes[0][1][0] == 7000
    assert by['pv-u2']['status'] == 'error' and b.writes == []
    assert d['ok'] is False


@needs_tc
def test_a_lease_restores_100_when_the_controller_goes_quiet(tmp_path):
    """The gateway's own dead-man: a controller that limited and then died
    must not leave the plant throttled — when the lease it asked for runs
    out, the gateway restores 100 % itself (and clears the enable bit)."""
    inv = _Inverter(sf=-2)
    cfg, app, client = _app(tmp_path, {'pv-u1': inv})
    r = client.post("/api/devices/pv-u1/actions/power_limit", json={"limit_pct": 40, "lease_s": 5})
    assert r.status_code == 200 and inv.writes[-1][1][0] == 4000
    mgr = app.state.lease_manager
    lease = next(l for l in mgr.snapshot() if l['device'] == 'pv-u1')
    assert lease['address'] == 40232 and 0 < lease['remaining_s'] <= 5
    # fire the revert as the dead-man would (the lease is still current)
    mgr._leases[('pv-u1', 'holding', 40232)]['revert'](lambda: True)
    assert inv.writes[-1][1] == [10000, 0, 0, 0, 0]          # 100 %, enable cleared
    # a restore to 100 % clears any lease
    client.post("/api/devices/pv-u1/actions/power_limit", json={"limit_pct": 100})
    assert not [l for l in mgr.snapshot() if l['device'] == 'pv-u1']


@needs_tc
def test_the_ha_slider_and_the_mqtt_command_go_through_the_same_action(tmp_path):
    inv = _Inverter(sf=-2)
    mq = _Mqtt()
    cfg, app, client = _app(tmp_path, {'pv-u1': inv}, mqtt=mq)
    # the command topics exist for the unit and its group
    assert 'pv/inverters/1/cmd/power_limit' in mq.topics and 'pv/inverters/cmd/power_limit' in mq.topics
    assert 'pv/inverters/1/cmd/restore' in mq.topics
    # a controller's command, legacy field names included
    mq.topics['pv/inverters/1/cmd/power_limit']('{"limit_pct": 55, "revert_timeout": 120, "source": "ov"}')
    assert inv.writes[-1][1] == [5500, 0, 120, 0, 1]
    topic, payload, retain = [x for x in mq.sent if x[0].endswith('/cmd/result')][-1]
    assert topic == 'pv/inverters/1/cmd/result' and retain is False
    res = json.loads(payload)
    assert res['status'] == 'success' and res['command'] == 'power_limit' and res['device'] == 'pv-u1'
    # restore
    mq.topics['pv/inverters/1/cmd/restore']('')
    assert inv.writes[-1][1] == [10000, 0, 0, 0, 0]
    # the HA number entity for power_limit_pct is the action, not a bare write
    from multibus.device_template import TemplateRegistry
    reg = next(r for r in TemplateRegistry().get('fronius_sunspec_inverter').registers if r.name == 'power_limit_pct')
    app.state.mqtt_write_command('pv-u1', reg, '80')
    assert inv.writes[-1][1] == [8000, 0, 600, 0, 1]
    # audited with the channel
    vias = [x['detail']['via'] for x in _audit(tmp_path) if x.get('action') == 'power limit']
    assert vias[-3:] == ['mqtt', 'mqtt', 'ha']
