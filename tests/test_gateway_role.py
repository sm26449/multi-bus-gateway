# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""A standby gateway: who may make it one, and what it then does not do —
no frame on a device's wire, no write by any path, no rule, no published
value, no "device down" for what it does not poll — while what only listens
goes on."""
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from multibus import role as gateway_role
from multibus.role import GatewayRole

from tests.test_devices import write_config
from tests.test_devices_api import needs_tc
from tests.test_operator_role import _build_clients

R = "/api/role"


def _token(c, name, role):
    r = c.admin.post("/api/auth/service-tokens", json={"name": name, "role": role})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


# ── who may set it ───────────────────────────────────────────────────────────

@needs_tc
def test_the_role_is_read_by_anyone_and_set_by_an_ha_token_or_an_admin(tmp_path):
    c = _build_clients(tmp_path)
    anon = TestClient(c.app, raise_server_exceptions=False)
    r = anon.get(R)
    assert r.status_code == 200 and r.json()["role"] == "active"            # a load balancer needs no login
    assert anon.put(R, json={"role": "standby", "epoch": 1}).status_code == 401
    ha = _token(c, "mbg-ha", "ha")
    for who in (c.op, c.viewer):
        assert who.put(R, json={"role": "standby", "epoch": 1}).status_code == 403
    viewer_tok = _token(c, "reader", "viewer")
    assert anon.put(R, json={"role": "standby", "epoch": 1}, headers=viewer_tok).status_code == 403
    r = anon.put(R, json={"role": "standby", "epoch": 1, "reason": "test"}, headers=ha)
    assert r.status_code == 200 and r.json()["set_by"] == "token:mbg-ha"
    assert anon.get(R).status_code == 503                                   # standby: not the one to send to
    assert anon.put(R, json={"role": "active", "epoch": 1}, headers=ha).status_code == 409   # needs a new epoch
    assert c.admin.put(R, json={"role": "active", "epoch": 2}).status_code == 200
    # an ha token reads, sets the role, raises alerts — and nothing else
    assert anon.get("/api/device-templates", headers=ha).status_code == 200
    assert anon.post("/api/devices", json={"id": "x"}, headers=ha).status_code == 403
    assert anon.put("/api/opcua", json={"enabled": True}, headers=ha).status_code == 403
    audit = c.admin.get("/api/audit?limit=20").json()
    assert "role set" in str(audit) and "role change refused" in str(audit)


@needs_tc
def test_a_service_raises_an_alert_through_the_gateway(tmp_path):
    c = _build_clients(tmp_path)
    ha = _token(c, "mbg-ha", "ha")
    anon = TestClient(c.app, raise_server_exceptions=False)
    ok = {"severity": "warning", "key": "link-tcp", "source": "mbg-ha", "message": "TCP link silent for 5 s"}
    r = anon.post("/api/alerts/fire", json=ok, headers=ha)
    assert r.status_code == 200 and r.json()["sent"] is False             # alerting is off: said so
    c.app.state.ctx.alert_mgr.enabled = True
    assert anon.post("/api/alerts/fire", json=ok, headers=ha).json()["sent"] is True
    assert "TCP link silent" in str(c.admin.get("/api/alerts").json()["alerts"])
    for bad in ({**ok, "severity": "loud"}, {**ok, "key": ""}, {**ok, "message": "x" * 600}):
        assert anon.post("/api/alerts/fire", json=bad, headers=ha).status_code == 422


@needs_tc
def test_without_login_or_api_key_the_role_cannot_be_changed(tmp_path):
    from tests.test_devices_api import make_app
    _cfg, client = make_app(tmp_path)
    r = client.put(R, json={"role": "standby", "epoch": 1})
    assert r.status_code == 403 and "authentication" in r.text


# ── what a standby does not do ───────────────────────────────────────────────

YAML = """
security: {allow_writes: true}
devices:
  - id: vfd1
    template: delta_vfd_el
    connection: {protocol: tcp, host: 192.0.2.30, port: 502, unit_id: 1}
    commands: [{name: run_at, enabled: true}]
"""


class _Cap:
    connected = True
    config = SimpleNamespace(enabled=True, topic_prefix="janitza/umg512", ha_discovery_enabled=False,
                             ha_discovery_prefix="homeassistant")

    def __init__(self):
        self.mqtt, self.influx = [], []

    def publish_register_data(self, *a, **k):
        self.mqtt.append(k.get('topic_prefix'))

    def write_register_data(self, *a, **k):
        self.influx.append(k.get('bucket'))

    def __getattr__(self, n):
        return lambda *a, **k: None


class _Client:
    """A device client that says what the gateway asked of it."""
    publish_callback = None

    def __init__(self):
        self.calls = []

    def standby(self):
        self.calls.append('standby')

    def resume(self):
        self.calls.append('resume')

    def get_stats(self):
        return {}


@needs_tc
def test_a_standby_writes_nothing_serves_nothing_and_publishes_nothing(tmp_path):
    from multibus import auth as _a
    from multibus.api import create_api
    cfg = write_config(tmp_path, extra_yaml=YAML + f"""
ui: {{auth: {{enabled: true, username: boss, password: "{_a.hash_password('pw')}"}}}}
""")
    cap = _Cap()
    clients = {d.id: _Client() for d in cfg.devices}
    app, _ = create_api(cfg, None, cap, cap, devices=[(d, clients[d.id]) for d in cfg.devices])
    admin = TestClient(app, raise_server_exceptions=False)
    admin.post("/api/auth/login", json={"username": "boss", "password": "pw"})
    reg = SimpleNamespace(name="_V1", label="L1", unit="V", mqtt_enabled=True, influxdb_enabled=True)

    def poll():
        clients["vfd1"].publish_callback("realtime", {19000: {"value": 230.0, "register": reg}})

    poll()
    assert cap.mqtt and cap.influx                                    # active: published
    assert admin.put(R, json={"role": "standby", "epoch": 1}).status_code == 200
    for _ in range(50):                                               # the listener runs off the request
        if clients["vfd1"].calls:
            break
        time.sleep(0.05)
    assert clients["vfd1"].calls == ['standby']
    cap.mqtt.clear(); cap.influx.clear()
    poll()
    assert cap.mqtt == [] and cap.influx == []                        # heard, kept, not published
    assert app.state.registry.store_for("vfd1")                       # …but kept
    r = admin.post("/api/devices/vfd1/commands/run_at", json={"params": {"hz": 25}})
    assert r.status_code == 503 and "standby" in r.text
    r = admin.post("/api/devices/vfd1/write", json={"address": 8193, "value": 1})
    assert r.status_code == 503
    for path in ("/api/meters", "/api/devices/vfd1/test", "/api/discover/modbus/scan", "/api/query/register"):
        r = admin.get(path) if path == "/api/meters" else admin.post(path, json={})
        assert r.status_code == 503, path
    h = admin.get("/health")
    assert h.status_code == 200 and h.json()["role"]["role"] == "standby"
    assert admin.put(R, json={"role": "active", "epoch": 2}).status_code == 200
    for _ in range(50):
        if 'resume' in clients["vfd1"].calls:
            break
        time.sleep(0.05)
    assert clients["vfd1"].calls == ['standby', 'resume']
    poll()
    assert cap.mqtt                                                   # publishing again


def test_a_standby_puts_no_frame_on_the_wire(tmp_path):
    from multibus import modbus_client as mc
    from multibus.config import ModbusConfig
    opened = []

    class _C:
        def is_socket_open(self): return True
        def connect(self): opened.append(1); return True
        def close(self): pass
        def write_registers(self, *a, **k): raise AssertionError("a standby wrote")
        def read_holding_registers(self, *a, **k): raise AssertionError("a standby read")
    conn = mc.ModbusConnection(ModbusConfig(host="192.0.2.78", port=502, unit_id=1))
    conn._new_client = _C
    r = GatewayRole(str(tmp_path / 'role.json'))
    gateway_role.install(r)
    r.set('standby', 1, by='test')
    assert conn.write(10, values=[1]) == (False, gateway_role.STANDBY_WRITE)
    assert conn.connect() is False and conn._ensure_connected() is False and opened == []
    # the poll path too: a poller started on a standby (a register list saved
    # there reloads its pollers) reads nothing and opens no socket
    assert conn.read_registers(0, 2) is None and opened == []
    r.set('active', 2, by='test')
    assert conn.connect() is True and opened == [1]
    mc._TRANSPORTS.clear()


def test_a_standby_keeps_only_the_sources_that_listen(tmp_path):
    from multibus.multi_source import MultiSourceClient

    class _Drv:
        def __init__(self): self.log = []
        def connect(self): self.log.append('connect'); return True
        def start_polling(self): self.log.append('poll')
        def disconnect(self): self.log.append('stop')
    tap, mq, tcp = _Drv(), _Drv(), _Drv()
    parts = [(SimpleNamespace(id='tap', protocol='rtu_tap'), tap), (SimpleNamespace(id='mq', protocol='mqtt'), mq),
             (SimpleNamespace(id='tcp', protocol='tcp'), tcp)]
    client = MultiSourceClient('d1', parts)
    r = GatewayRole(str(tmp_path / 'role.json'))
    gateway_role.install(r)
    r.set('standby', 1, by='test')
    client.connect(); client.start_polling()                         # booting as a standby
    assert tap.log == ['connect', 'poll'] and mq.log == ['connect', 'poll'] and tcp.log == []
    r.set('active', 2, by='test')
    client.resume()
    assert tcp.log == ['connect', 'poll'] and tap.log == ['connect', 'poll']   # the tap was never stopped
    r.set('standby', 3, by='test')
    client.standby()
    assert tcp.log[-1] == 'stop' and tap.log[-1] == 'poll'


def test_rules_wait_on_a_standby_without_a_stall_alarm(tmp_path):
    from multibus.rules_runtime import RulesRuntime
    rt = RulesRuntime.__new__(RulesRuntime)
    import threading
    rt._stop = threading.Event()
    rt._mono = time.monotonic
    rt._period = lambda: 0.02
    ticks = []
    rt.tick = lambda: ticks.append(1)
    r = GatewayRole(str(tmp_path / 'role.json'))
    gateway_role.install(r)
    r.set('standby', 1, by='test')
    t = threading.Thread(target=rt._loop, daemon=True)
    t.start()
    time.sleep(0.2)
    assert ticks == [] and time.monotonic() - rt.__dict__['_last_tick_mono'] < 0.1
    r.set('active', 2, by='test')
    time.sleep(0.2)
    rt._stop.set(); t.join(1)
    assert ticks


def test_a_standby_starts_no_sequence(tmp_path):
    from multibus.sequences import SequenceRunner, parse_sequence
    seq = parse_sequence({'id': 's1', 'steps': [{'wait_s': 0}]})
    runner = SequenceRunner(run_command=lambda *a, **k: (200, {}), resolver_factory=lambda *a: (lambda n: None))
    r = GatewayRole(str(tmp_path / 'role.json'))
    gateway_role.install(r)
    r.set('standby', 1, by='test')
    code, res = runner.start(seq, {}, who='t', via='api')
    assert code == 503 and 'standby' in res['reason']


def test_the_publisher_announces_nothing_from_a_standby(tmp_path):
    from multibus.config import MQTTConfig
    from multibus.mqtt_publisher import MQTTPublisher
    pub = MQTTPublisher(MQTTConfig(enabled=False), [])
    sent = []
    pub._publish = lambda *a, **k: sent.append(a) or True
    pub.connected = True
    r = GatewayRole(str(tmp_path / 'role.json'))
    gateway_role.install(r)
    r.set('standby', 1, by='test')
    assert pub.publish_ha_discovery() == 0
    pub.publish_device_availability("mbg/devices/x", False)
    assert sent == []


@needs_tc
def test_starting_as_a_standby_drops_old_leases_instead_of_reverting_them(tmp_path):
    import json
    from tests.test_devices_api import make_app
    (tmp_path / 'role.json').write_text(json.dumps({'role': 'standby', 'epoch': 4, 'since': 1.0, 'set_by': 'ha'}))
    (tmp_path / 'write_leases.json').write_text(json.dumps([{
        'device': 'umg512', 'register_type': 'holding', 'address': 100, 'data_type': 'uint16',
        'scale': 1.0, 'safe_value': 0, 'lease_ms': 1000}]))
    _cfg, client = make_app(tmp_path)
    assert client.get(R).json()['role'] == 'standby'
    assert json.loads((tmp_path / 'write_leases.json').read_text() or '[]') in ([], {'leases': []})


# ── companion services in the menu ───────────────────────────────────────────

def test_modules_are_checked_and_shown_only_once_signed_in(tmp_path):
    from multibus.config import Config
    cfg = write_config(tmp_path, extra_yaml="""
modules:
  - {id: ha, name: Redundancy, icon: shield-check, url: /ha/, health: /ha/api/health, description: Two gateways, one in charge.}
  - {id: nodes, name: ESP nodes, icon: usb-plug, url: "https://gw.example/nodes/"}
  - {id: Bad Id, name: x, url: /x/}
  - {id: evil, name: x, url: "javascript:alert(1)"}
  - {id: ha, name: Again, url: /again/}
""")
    assert [m['id'] for m in cfg.modules] == ['ha', 'nodes']
    assert cfg.modules[0]['health'] == '/ha/api/health' and cfg.modules[0]['description'].startswith('Two')
    cfg.save_yaml_config()
    assert [m['url'] for m in Config(str(tmp_path / 'config.yaml')).modules] == ['/ha/', 'https://gw.example/nodes/']


@needs_tc
def test_the_menu_gets_the_modules_after_login(tmp_path):
    c = _build_clients(tmp_path)
    c.cfg.modules = [{'id': 'ha', 'name': 'Redundancy', 'icon': 'shield-check', 'url': '/ha/'}]
    anon = TestClient(c.app, raise_server_exceptions=False)
    assert anon.get("/api/auth/status").json()["modules"] == []
    assert c.viewer.get("/api/auth/status").json()["modules"][0]["url"] == "/ha/"


@needs_tc
def test_the_esp_nodes_service_set_the_old_way_is_a_module(tmp_path):
    c = _build_clients(tmp_path)
    c.cfg.ui.nodes_url = '/nodes/'
    mods = c.viewer.get("/api/auth/status").json()["modules"]
    assert [(m['id'], m['url']) for m in mods] == [('nodes', '/nodes/')]
    c.cfg.modules = [{'id': 'esp', 'name': 'ESP nodes', 'icon': 'usb-plug', 'url': '/nodes/'}]
    assert len(c.viewer.get("/api/auth/status").json()["modules"]) == 1        # declared: not twice
