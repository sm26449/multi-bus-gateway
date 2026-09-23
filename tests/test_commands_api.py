# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Commands from every face: API, group, HA slider, MQTT topic — one set of
gates, one verification, one audit, one read-back. The recipe is the
template's (`power_limit` on fronius_sunspec_inverter); nothing here knows a
register address."""
import json


from multibus.config import SourceConfig
from tests.test_devices import write_config
from tests.test_endpoints import needs_tc
from tests.test_commands import _Inverter

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    TestClient = None

PLANT_YAML = """
security: { allow_writes: true }
mqtt: { allow_write_entities: true, username: gw, password: pw }   # writes over MQTT need an authenticated broker
endpoints:
  - id: pv
    name: PV
    mqtt: { topic_prefix: "pv/units/${unit_id}" }
    groups:
      - id: inverters
        role: inverter
        units: [1, 2]
        mqtt: { topic_prefix: "pv/inverters/${unit_id}" }
        commands:
          - { name: power_limit }
          - { name: restore }
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

    swept: list = []            # every read-back sweep any fake driver ran

    def poll_now(self, group=None):
        _Drv.swept.append(group)
        return 1

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
def test_a_device_lists_what_it_can_be_told(tmp_path):
    """A controller discovers the command's parameters and bounds instead of
    hard-coding registers; a preset the operator did not bind is offered but
    disabled."""
    inv = _Inverter(sf=-2)
    cfg, app, client = _app(tmp_path, {'pv-u1': inv})
    r = client.get("/api/devices/pv-u1/commands")
    assert r.status_code == 200, r.text
    by = {c['name']: c for c in r.json()['commands']}
    pl = by['power_limit']
    assert pl['enabled'] is True and pl['from_template'] is True
    assert pl['mqtt_topic'] == 'pv/inverters/1/cmd/power_limit'
    value = pl['params']['value']
    assert value['min'] == 0 and value['max'] == 100 and value['unit'] == '%'
    assert pl['params']['revert_s']['default'] == 600
    assert by['restore']['alias']['command'] == 'power_limit'
    assert pl['last'] is None
    # unbind: still listed, not runnable
    app.state.registry.find('pv-u1')[1].commands = []
    r = client.get("/api/devices/pv-u1/commands")
    assert {c['name']: c['enabled'] for c in r.json()['commands']} == {'power_limit': False, 'restore': False}
    r = client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 60})
    assert r.status_code == 403 and 'not enabled' in r.json()['reason'] and inv.writes == []


@needs_tc
def test_the_api_runs_a_command_and_audits_it(tmp_path):
    inv = _Inverter(sf=-2)
    cfg, app, client = _app(tmp_path, {'pv-u1': inv})
    r = client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 60, "revert_s": 300})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['status'] == 'success' and d['before']['power_limit_pct'] == 100.0 and d['after']['power_limit_pct'] == 60.0
    assert d['scale_factors'] == {'wmaxlimpct_sf': -2} and d['frames_sent'] == 1
    assert inv.writes == [(40232, [6000, 0, 300, 0, 1])]
    # audited: who, what, before → after, which face
    a = [x for x in _audit(tmp_path) if x.get('action') == 'command'][-1]
    assert a['user'] == 'admin' and a['target'] == 'pv-u1 power_limit' and a['status'] == 'success'
    assert a['detail']['params']['value'] == 60.0 and a['detail']['via'] == 'api'
    assert a['detail']['frames'][0]['address'] == 40232
    # the last result is on the device's command list, and in the history
    last = next(c for c in client.get("/api/devices/pv-u1/commands").json()['commands'] if c['name'] == 'power_limit')['last']
    assert last['status'] == 'success' and last['by'] == 'admin'
    h = client.get("/api/commands/history?device=pv-u1").json()['history']
    assert h and h[0]['target'] == 'pv-u1 power_limit' and h[0]['detail']['params']['value'] == 60.0
    # the unit's Logs carry it too
    assert any(e.get('kind') == 'command' for e in inv.events) if hasattr(inv, 'events') else True


@needs_tc
def test_the_legacy_action_route_and_field_names_still_work(tmp_path):
    """3.72.0 spoke of `limit_pct` on `…/actions/power_limit`; Node-RED may
    still. Same command underneath."""
    inv = _Inverter(sf=-2)
    cfg, app, client = _app(tmp_path, {'pv-u1': inv})
    r = client.post("/api/devices/pv-u1/actions/power_limit", json={"limit_pct": 60, "revert_s": 300})
    assert r.status_code == 200, r.text
    assert r.json()['status'] == 'success' and inv.writes == [(40232, [6000, 0, 300, 0, 1])]


@needs_tc
def test_a_dry_run_reads_but_never_writes(tmp_path):
    inv = _Inverter(sf=-1)
    cfg, app, client = _app(tmp_path, {'pv-u1': inv})
    cfg.security.allow_writes = False                     # a dry run needs no write permission
    r = client.post("/api/devices/pv-u1/commands/power_limit/dry-run", json={"value": 42.5})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['status'] == 'dry_run' and inv.writes == [] and inv.reads >= 1
    assert d['frames'] == [{'address': 40232, 'type': 'holding', 'words': [425, 0, 600, 0, 1],
                            'registers': ['power_limit_pct', 'power_limit_win_s', 'power_limit_revert_s',
                                          'power_limit_ramp_s', 'power_limit_enabled'],
                            'values': {'power_limit_pct': 42.5, 'power_limit_win_s': 0, 'power_limit_revert_s': 600,
                                       'power_limit_ramp_s': 0, 'power_limit_enabled': 1}}]
    assert d['guard'] == {'controls_model_id': 123, 'wmaxlimpct_sf': -1}
    # a dry run is not audited as a write, and leaves no "last result"
    assert not [x for x in _audit(tmp_path) if x.get('action') == 'command']


@needs_tc
def test_gates_hold_for_commands_too(tmp_path):
    inv = _Inverter()
    cfg, app, client = _app(tmp_path, {'pv-u1': inv})
    cfg.security.allow_writes = False
    r = client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 50})
    assert r.status_code == 403 and inv.writes == []
    cfg.security.allow_writes = True
    r = client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 150})
    assert r.status_code == 422 and 'value' in r.json()['reason'] and inv.writes == []
    r = client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 50, "bogus": 1})
    assert r.status_code == 422 and 'bogus' in r.json()['reason'] and inv.writes == []
    r = client.post("/api/devices/pv-u1/commands/no_such", json={"value": 50})
    assert r.status_code == 404
    # a unit with no Modbus source cannot be written, and says so
    r = client.post("/api/devices/pv-u2/commands/power_limit", json={"value": 50})
    assert r.status_code == 409 and 'Modbus' in r.json()['reason']
    # write-locked device
    app.state.registry.find('pv-u1')[1].write_locked = True
    r = client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 50})
    assert r.status_code == 403 and 'write-locked' in r.json()['reason']


@needs_tc
def test_a_refusing_guard_is_rejected_and_audited(tmp_path):
    inv = _Inverter(model_id=7)                           # not model 123 → the recipe does not apply
    cfg, app, client = _app(tmp_path, {'pv-u1': inv})
    r = client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 50})
    assert r.status_code == 422 and r.json()['status'] == 'rejected' and inv.writes == []
    a = [x for x in _audit(tmp_path) if x.get('action') == 'command'][-1]
    assert a['status'] == 'rejected' and 'controls_model_id' in a['detail']['reason']


@needs_tc
def test_the_group_route_does_every_inverter_and_reports_each(tmp_path):
    a, b = _Inverter(sf=-2), _Inverter(sf=-1, model_id=7)      # b is not model 123
    cfg, app, client = _app(tmp_path, {'pv-u1': a, 'pv-u2': b})
    r = client.post("/api/endpoints/pv/groups/inverters/commands/power_limit", json={"value": 70})
    assert r.status_code == 200, r.text
    d = r.json()
    by = {u['device']: u for u in d['units']}
    assert by['pv-u1']['status'] == 'success' and a.writes[0][1][0] == 7000
    assert by['pv-u2']['status'] == 'rejected' and b.writes == []
    assert d['ok'] is False
    # the legacy group route too
    r = client.post("/api/endpoints/pv/groups/inverters/actions/power_limit", json={"limit_pct": 65})
    assert r.status_code == 200 and a.writes[-1][1][0] == 6500


@needs_tc
def test_a_lease_runs_the_safe_recipe_when_the_controller_goes_quiet(tmp_path):
    """The gateway's own dead-man: a controller that limited and then died
    must not leave the plant throttled — when the lease it asked for runs
    out, the gateway runs the command's `safe` parameters itself."""
    inv = _Inverter(sf=-2)
    cfg, app, client = _app(tmp_path, {'pv-u1': inv})
    r = client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 40, "lease_s": 5})
    assert r.status_code == 200 and inv.writes[-1][1][0] == 4000
    mgr = app.state.lease_manager
    lease = next(l for l in mgr.snapshot() if l['device'] == 'pv-u1')
    assert lease['address'] == 40232 and 0 < lease['remaining_s'] <= 5
    # fire the revert as the dead-man would (the lease is still current)
    mgr._leases[('pv-u1', 'holding', 40232)]['revert'](lambda: True)
    assert inv.writes[-1][1] == [10000, 0, 0, 0, 0]          # safe: 100 %, revert 0, enable cleared
    vias = [x['detail']['via'] for x in _audit(tmp_path) if x.get('action') == 'command']
    assert vias[-1] == 'lease-revert'
    # a write of the safe value clears any lease
    client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 40, "lease_s": 5})
    client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 100})
    assert not [l for l in mgr.snapshot() if l['device'] == 'pv-u1']


@needs_tc
def test_the_ha_slider_and_the_mqtt_topic_go_through_the_same_command(tmp_path):
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
    assert res['by'] == 'ov' and res['via'] == 'mqtt'
    # the last applied command is retained for a controller that restarts
    topic, payload, retain = [x for x in mq.sent if x[0].endswith('/cmd/power_limit/state')][-1]
    assert topic == 'pv/inverters/1/cmd/power_limit/state' and retain is True
    assert json.loads(payload)['params']['value'] == 55.0
    # the legacy collector's envelope (command, device_id) is tolerated: Node-RED's OV node speaks it
    mq.topics['pv/inverters/1/cmd/power_limit']('{"command": "set_power_limit", "device_id": 1, "limit_pct": 65, "revert_timeout": 0, "ramp_time": 10, "source": "nodered-ov"}')
    assert inv.writes[-1][1] == [6500, 0, 0, 10, 1]
    mq.topics['pv/inverters/1/cmd/restore']('{"command": "restore_power_limit", "device_id": 1, "source": "nodered-ov"}')
    assert inv.writes[-1][1] == [10000, 0, 0, 0, 0]
    # a bare number is the value
    mq.topics['pv/inverters/1/cmd/power_limit']('70')
    assert inv.writes[-1][1] == [7000, 0, 600, 0, 1]
    # restore (an alias: power_limit with its parameters filled in)
    mq.topics['pv/inverters/1/cmd/restore']('')
    assert inv.writes[-1][1] == [10000, 0, 0, 0, 0]
    # the group topic reaches every unit of the group
    mq.topics['pv/inverters/cmd/power_limit']('{"value": 90}')
    assert inv.writes[-1][1] == [9000, 0, 600, 0, 1]
    # the HA number entity for power_limit_pct is the command, not a bare write
    from multibus.device_template import TemplateRegistry
    reg = next(r for r in TemplateRegistry().get('fronius_sunspec_inverter').registers if r.name == 'power_limit_pct')
    app.state.mqtt_write_command('pv-u1', reg, '80')
    assert inv.writes[-1][1] == [8000, 0, 600, 0, 1]
    # audited with the face
    vias = [x['detail']['via'] for x in _audit(tmp_path) if x.get('action') == 'command']
    assert vias[-7:] == ['mqtt'] * 6 + ['ha']
    # writes over MQTT off → the topic is ignored, nothing on the wire
    n = len(inv.writes)
    cfg.mqtt.allow_write_entities = False
    mq.topics['pv/inverters/1/cmd/power_limit']('20')
    assert len(inv.writes) == n


@needs_tc
def test_a_revert_timer_arms_a_read_back_after_it_fires(tmp_path, monkeypatch):
    """The inverter reverts on its own when revert_s elapses — no write from the
    gateway, so nothing would re-read the hourly controls block; the retained
    limit said 95 for up to an hour (seen live 2026-09-14). A timer sweeps the
    read-back group just after the device-side timer, through the unit's
    Modbus part."""
    import multibus.api as apimod
    armed = []

    class _Timer:
        def __init__(self, interval, fn, args=()):
            armed.append((interval, fn, args))
        def start(self): pass
        def cancel(self): pass
    monkeypatch.setattr(apimod.threading, 'Timer', _Timer)
    _Drv.swept.clear()
    inv = _Inverter(sf=-2)
    cfg, app, client = _app(tmp_path, {'pv-u1': inv})
    r = client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 95, "revert_s": 120})
    assert r.status_code == 200 and r.json()['status'] == 'success'
    mine = [a for a in armed if getattr(a[1], '__name__', '') == '_reread_after_revert']
    assert [a[0] for a in mine] == [125.0]                  # revert_s + the first offset
    assert _Drv.swept == ['controls']                       # the write's own read-back
    interval, fn, args = mine[0]
    assert args[:3] == ('pv-u1', 'power_limit', 'controls')
    # the series chains: each sweep arms the next at the remaining offsets
    seen = [interval]
    while True:
        fn(*args)
        nxt = [a for a in armed if getattr(a[1], '__name__', '') == '_reread_after_revert'][len(seen):]
        if not nxt:
            break
        interval, fn, args = nxt[0]; seen.append(interval)
    assert seen == [125.0, 15.0, 40.0, 120.0]               # 5, 20, 60, 180 s past the mark
    assert _Drv.swept == ['controls'] * 5                   # the write's, then four more
    # a command without a revert timer arms nothing
    armed.clear()
    r = client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 100, "revert_s": 0})
    assert r.status_code == 200
    assert not [a for a in armed if getattr(a[1], '__name__', '') == '_reread_after_revert']


def test_a_new_command_replaces_the_pending_read_back_series(tmp_path, monkeypatch):
    """Node-RED's OV re-sends the limit every 30–60 s while a step holds; each
    command used to arm its own four-sweep series on top of the previous ones
    (1681 sweeps for 514 commands on 2026-09-15). A new command restarts the
    inverter's revert timer, so only the latest series is worth running: the
    pending one is cancelled and a callback of a superseded series is a no-op."""
    import multibus.api as apimod
    armed, cancelled = [], []

    class _Timer:
        def __init__(self, interval, fn, args=()):
            self.fn, self.args = fn, args
            armed.append(self)
        def start(self): pass
        def cancel(self): cancelled.append(self)
    monkeypatch.setattr(apimod.threading, 'Timer', _Timer)
    _Drv.swept.clear()
    inv = _Inverter(sf=-2)
    cfg, app, client = _app(tmp_path, {'pv-u1': inv})
    for value in (80, 70, 80):
        r = client.post("/api/devices/pv-u1/commands/power_limit", json={"value": value, "revert_s": 600})
        assert r.status_code == 200 and r.json()['status'] == 'success'
    def series_timers():          # the lease arms timers of its own; only the read-back series count
        return [t for t in armed if getattr(t.fn, '__name__', '') == '_reread_after_revert']
    series = series_timers()
    # each command cancelled the previous series (the dead-man lease cancels its own timers too)
    assert len(series) == 3 and [t for t in cancelled if t in series] == series[:2]
    assert [t.args[4] for t in series] == [1, 2, 3]              # generations
    assert _Drv.swept == ['controls'] * 3                        # only the writes' own read-backs so far
    # a callback of the superseded series does nothing and arms nothing
    series[0].fn(*series[0].args)
    assert _Drv.swept == ['controls'] * 3 and len(series_timers()) == 3
    # the current series runs and chains at the same generation
    series[2].fn(*series[2].args)
    assert _Drv.swept == ['controls'] * 4
    assert series_timers()[-1].args[4] == 3 and len(series_timers()) == 4


@needs_tc
def test_an_anonymous_broker_gets_no_writes_over_mqtt(tmp_path):
    """With allow_write_entities on, a broker publish IS a hardware write —
    so the gateway refuses to act on it while its own broker session is
    anonymous (3.80.0): broker ACLs are the only authentication a publish
    carries."""
    mq = _Mqtt()
    inv = _Inverter(sf=-2)
    cfg, app, client = _app(tmp_path, {'pv-u1': inv}, mqtt=mq)
    cfg.mqtt.username = ""
    before = len(inv.writes)
    mq.topics['pv/inverters/1/cmd/power_limit']('{"limit_pct": 55, "source": "ov"}')
    assert len(inv.writes) == before                    # ignored, nothing written
    cfg.mqtt.username = "gw"
    mq.topics['pv/inverters/1/cmd/power_limit']('{"limit_pct": 55, "source": "ov"}')
    assert len(inv.writes) > before                     # authenticated broker: acted on
