# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""seplos_v2 / seplos_v2-tcp as device protocols: accepted wherever a device
protocol is, checked like a bus (one protocol, one mode, one address per pack),
built by the runtime, and never written to. No port is opened here."""
import json

import pytest

from multibus import seplos_v2 as sv
from multibus.config import Config, READ_ONLY_PROTOCOLS, VIRTUAL_MAP_PROTOCOLS, protocol_family
from tests.test_devices_api import make_app, needs_tc


@pytest.fixture(autouse=True)
def no_ports(monkeypatch):
    """Any bus the API or the runtime opens is missing — nothing real is touched."""
    def opener(c):
        raise FileNotFoundError(f"no such port {sv.bus_name(c)}")
    monkeypatch.setattr(sv, 'open_port', opener)
    monkeypatch.setattr(sv, '_BUSES', {})
    monkeypatch.setattr(sv, '_LISTENERS', {})


def pack(did, **conn):
    c = {"protocol": "seplos_v2", "serial_port": "/dev/ttyBMS0", "baudrate": 9600, "parity": "N",
         "unit_id": 1, "mode": "poll"}
    c.update(conn)
    return {"id": did, "enabled": False, "template": "seplos_bms_v2", "connection": c}


def test_the_protocol_lists():
    assert set(VIRTUAL_MAP_PROTOCOLS) == {'seplos_v2', 'seplos_v2-tcp'}
    assert set(VIRTUAL_MAP_PROTOCOLS) <= set(READ_ONLY_PROTOCOLS)
    assert protocol_family('seplos_v2-tcp') == 'seplos_v2' and protocol_family('rtu') == 'modbus'
    from multibus.device_runtime import DRIVER_PROTOCOLS
    assert set(VIRTUAL_MAP_PROTOCOLS) <= set(DRIVER_PROTOCOLS)


@needs_tc
def test_a_seplos_device_is_created_echoed_and_kept(tmp_path):
    _cfg, client = make_app(tmp_path)
    r = client.post("/api/devices", json=pack("bms-1", mode="listen"))
    assert r.status_code == 200, r.text
    dev = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "bms-1")
    assert dev["protocol"] == "seplos_v2" and dev["connection"]["mode"] == "listen"
    assert dev["serial"]["serial_port"] == "/dev/ttyBMS0"
    d = Config(str(tmp_path / "config.yaml")).get_device("bms-1")
    assert d.protocol == "seplos_v2" and d.connection.mode == "listen"
    # an edit keeps it (the device is not its own conflict)
    r = client.put("/api/devices/bms-1", json=pack("bms-1", mode="listen", unit_id=2))
    assert r.status_code == 200, r.text
    # over the network
    r = client.post("/api/devices", json={"id": "bms-net", "enabled": False, "template": "seplos_bms_v2",
                                          "connection": {"protocol": "seplos_v2-tcp", "host": "192.0.2.50",
                                                         "port": 4196, "unit_id": 0, "mode": "poll"}})
    assert r.status_code == 200, r.text
    p = client.get("/api/protocols").json()
    assert "seplos_v2" in p["device"] and "seplos_v2" in p["serial"] and "seplos_v2-tcp" in p["network"]
    assert "seplos_v2" not in p["endpoint"] and "seplos_v2" not in p["source"]


@needs_tc
def test_the_connection_is_checked(tmp_path):
    _cfg, client = make_app(tmp_path)

    def errs(body):
        r = client.post("/api/devices", json=body)
        assert r.status_code == 422, r.text
        return json.dumps(r.json())
    assert "connection.mode" in errs(pack("a", mode="master"))
    assert "connection.serial_port" in errs(pack("b", serial_port=""))
    assert "connection.unit_id" in errs(pack("c", unit_id=300))
    assert "connection.timeout" in errs(pack("d", timeout=0.2))
    assert "connection.host" in errs({"id": "e", "template": "seplos_bms_v2",
                                      "connection": {"protocol": "seplos_v2-tcp", "port": 4196}})
    assert "connection.host" in errs({"id": "f", "template": "seplos_bms_v2",
                                      "connection": {"protocol": "seplos_v2-tcp", "host": "/dev/ttyUSB0", "port": 1}})
    assert "connection.bridge" in errs({"id": "g", "template": "seplos_bms_v2",
                                        "connection": {"protocol": "seplos_v2", "bridge": "x", "bridge_port": 1}})
    # a Modbus template on a Seplos bus, and the reverse
    assert "template" in errs(pack("h") | {"template": "eastron_sdm630"})
    assert "template" in errs({"id": "i", "template": "seplos_bms_v2",
                               "connection": {"protocol": "rtu", "serial_port": "/dev/ttyX", "unit_id": 1}})


@needs_tc
def test_one_bus_one_protocol_one_mode_one_address(tmp_path):
    _cfg, client = make_app(tmp_path)
    assert client.post("/api/devices", json=pack("p1")).status_code == 200
    assert client.post("/api/devices", json=pack("p2", unit_id=2)).status_code == 200   # a second pack
    r = client.post("/api/devices", json=pack("p3", unit_id=2))
    assert r.status_code == 422 and "own address" in r.text
    r = client.post("/api/devices", json=pack("p4", unit_id=4, mode="listen"))
    assert r.status_code == 422 and "polled or listened to" in r.text
    r = client.post("/api/devices", json=pack("p5", unit_id=5, baudrate=19200))
    assert r.status_code == 422 and "same line settings" in r.text
    r = client.post("/api/devices", json={"id": "m1", "enabled": False,
                                          "connection": {"protocol": "rtu", "serial_port": "/dev/ttyBMS0", "unit_id": 9}})
    assert r.status_code == 422 and "one protocol" in r.text
    # the same on a bridge's host:port
    net = {"protocol": "seplos_v2-tcp", "host": "192.0.2.50", "port": 4196, "unit_id": 1, "mode": "poll"}
    assert client.post("/api/devices", json={"id": "n1", "enabled": False, "template": "seplos_bms_v2",
                                             "connection": net}).status_code == 200
    r = client.post("/api/devices", json={"id": "n2", "enabled": False, "template": "seplos_bms_v2",
                                          "connection": dict(net, mode="listen", unit_id=2)})
    assert r.status_code == 422 and "polled or listened to" in r.text
    r = client.post("/api/devices", json={"id": "n3", "enabled": False,
                                          "connection": {"protocol": "rtu-tcp", "host": "192.0.2.50", "port": 4196,
                                                         "unit_id": 3}})
    assert r.status_code == 422 and "one protocol" in r.text


@needs_tc
def test_test_asks_nothing_when_listening_and_says_why_when_polling(tmp_path):
    _cfg, client = make_app(tmp_path)
    r = client.post("/api/devices/test", json={"connection": pack("x", mode="listen")["connection"]}).json()
    assert r["ok"] is None and "listen-only" in r["message"]
    r = client.post("/api/devices/test", json={"connection": pack("x")["connection"]}).json()
    assert r["ok"] is False and "cannot open /dev/ttyBMS0" in r["message"]
    assert client.post("/api/devices", json=pack("bms-1")).status_code == 200
    saved = client.post("/api/devices/bms-1/test").json()
    assert saved["ok"] is False and "not reading" in saved["message"]


@needs_tc
def test_writes_are_refused(tmp_path):
    from tests.test_write_path import _app
    extra = """
devices:
  - id: bms
    enabled: false
    template: seplos_bms_v2
    connection: {protocol: seplos_v2, serial_port: /dev/ttyBMS0, unit_id: 1}
"""
    _cfg, client = _app(tmp_path, auth=True, allow_writes=True, extra_yaml=extra)
    r = client.post("/api/devices/bms/write", json={"address": 0x30, "value": 1, "register_type": "holding"})
    assert r.status_code == 400 and "read-only" in r.text


def test_the_runtime_builds_the_seplos_client(tmp_path):
    from types import SimpleNamespace

    from multibus.config import ModbusConfig
    from multibus.device_runtime import driver_for
    from multibus.device_template import TemplateRegistry
    tr = TemplateRegistry()
    src = SimpleNamespace(protocol='seplos_v2', template='seplos_bms_v2', id='default',
                          connection=ModbusConfig(protocol='seplos_v2', serial_port='/dev/ttyBMS9', unit_id=3,
                                                  mode='listen'))
    dev = SimpleNamespace(id='bms', template='seplos_bms_v2', sources=[src], component_of='')
    drv = driver_for(None, tr, dev, src, [], {}, False)
    assert isinstance(drv, sv.SeplosV2Client) and drv.mode == 'listen' and drv.unit_id == 3
    assert not hasattr(drv, 'write_value')


def test_a_listened_to_pack_reports_on_the_tap_group():
    """Its values arrive pushed (group 'tap'), so its calculated fields match
    every group there, like rtu_tap's; a polled pack keeps its own groups."""
    from types import SimpleNamespace

    from multibus.calc_engine import CalcEngine
    from multibus.config import ModbusConfig

    def dev(mode):
        c = ModbusConfig(protocol='seplos_v2', serial_port='/dev/ttyBMS0', mode=mode)
        return SimpleNamespace(id='bms', protocol='seplos_v2', connection=c,
                               sources=[SimpleNamespace(protocol='seplos_v2', connection=c)])
    for mode, want in (('listen', 'tap'), ('poll', None), ('', None)):
        cfg = SimpleNamespace(get_device=lambda _id, d=dev(mode): d)
        assert CalcEngine(cfg, lambda _d: None, lambda: (None, None))._push_group('bms') == want, mode


# ── review 2026-10-24 (FINDINGS SV2-R1..R8) ─────────────────────────────────

@pytest.fixture
def opened(monkeypatch):
    """Records every bus the code tries to open (and refuses it)."""
    calls = []

    def opener(c):
        calls.append(sv.bus_name(c))
        raise FileNotFoundError(f"no such port {sv.bus_name(c)}")
    monkeypatch.setattr(sv, 'open_port', opener)
    return calls


@needs_tc
def test_r1_test_never_transmits_on_a_bus_another_device_holds(tmp_path, opened):
    _cfg, client = make_app(tmp_path)
    # a listening pack on the inter-pack bus: Test in poll mode must not ask there
    assert client.post("/api/devices", json=pack("lp", mode="listen")).status_code == 200
    r = client.post("/api/devices/test", json={"connection": pack("x", unit_id=5)["connection"]}).json()
    assert r["ok"] is False and "listened to" in r["message"], r
    # a Modbus RTU line: no Seplos frame onto it
    assert client.post("/api/devices", json={"id": "m1", "enabled": False, "connection": {
        "protocol": "rtu", "serial_port": "/dev/ttyMB0", "unit_id": 1}}).status_code == 200
    r = client.post("/api/devices/test", json={"connection": pack("x", serial_port="/dev/ttyMB0")["connection"]}).json()
    assert r["ok"] is False and "rtu" in r["message"], r
    # a tap through the network on that host:port
    assert client.post("/api/devices", json={"id": "t1", "enabled": False, "connection": {
        "protocol": "rtu_tap", "host": "192.168.7.7", "port": 4196, "unit_id": 1}}).status_code in (200, 422)
    # a host off the LAN is blocked like a Modbus probe
    r = client.post("/api/devices/test", json={"connection": {
        "protocol": "seplos_v2-tcp", "host": "8.8.8.8", "port": 4196, "unit_id": 1, "mode": "poll"}}).json()
    assert r["ok"] is False and "blocked" in r["message"], r
    assert opened == []                               # not one port opened
    # a polled pack on the same line: Test goes through the same transport
    assert client.post("/api/devices", json=pack("pp", serial_port="/dev/ttyBMS1")).status_code == 200
    r = client.post("/api/devices/test", json={"connection": pack("x", serial_port="/dev/ttyBMS1", unit_id=4)["connection"]}).json()
    assert r["ok"] is False and "cannot open /dev/ttyBMS1" in r["message"] and opened == ["/dev/ttyBMS1"]


def test_r1_the_driver_probe_refuses_a_listened_to_bus(monkeypatch):
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(sv, 'open_port', lambda c: calls.append(c) or (_ for _ in ()).throw(OSError('x')))
    c = SimpleNamespace(protocol='seplos_v2', serial_port='/dev/ttyL', host='', port=0, baudrate=9600,
                        parity='N', stopbits=1, bytesize=8, unit_id=1, mode='poll')
    lst = sv.listener_for(c)
    lst._clients[2] = object()                        # a pack listened to on that port
    r = sv.probe(c)
    assert r['ok'] is False and 'listened to' in r['message'] and calls == []


@needs_tc
def test_r2_a_bridge_scan_never_transmits_on_a_seplos_bus(tmp_path):
    _cfg, client = make_app(tmp_path)
    assert client.post("/api/bridges", json={"id": "ws", "name": "Panel", "type": "rtu_transparent",
                                             "host": "192.0.2.60", "ports": [{"port": 4196}]}).status_code == 200
    assert client.post("/api/devices", json={"id": "bms", "enabled": False, "template": "seplos_bms_v2",
                                             "connection": {"protocol": "seplos_v2-tcp", "host": "192.0.2.60",
                                                            "port": 4196, "unit_id": 1, "mode": "listen"}}).status_code == 200
    r = client.post("/api/bridges/ws/ports/4196/scan", json={})
    assert r.status_code == 409 and "bms" in r.text
    r = client.post("/api/bridges/ws/probe", json={"port": 4196, "unit_id": 1})
    assert r.status_code == 409 and "bms" in r.text


def test_r3_an_unknown_mode_never_transmits(monkeypatch, caplog):
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(sv, 'open_port', lambda c: calls.append(c))
    c = SimpleNamespace(protocol='seplos_v2', serial_port='/dev/ttyQ', host='', port=0, baudrate=9600,
                        parity='N', stopbits=1, bytesize=8, unit_id=1, mode='master', timeout=1,
                        retry_attempts=1, retry_delay=0, stale_after_s=30)
    cl = sv.SeplosV2Client(c, [], {}, device_id='bad')
    assert cl.connect() is False
    cl.start_polling()
    assert cl._thread is None and calls == []
    assert cl.data_health()['status'] == 'down' and 'mode' in cl.data_health()['reason']
    assert any('mode' in r.getMessage() for r in caplog.records if r.levelname == 'ERROR')


@needs_tc
def test_r4_a_new_device_and_a_test_need_a_chosen_mode_a_stored_one_keeps_poll(tmp_path):
    from tests.test_write_path import _app
    extra = """
devices:
  - id: old
    enabled: false
    template: seplos_bms_v2
    connection: {protocol: seplos_v2, serial_port: /dev/ttyOLD, unit_id: 1}
"""
    _cfg, client = _app(tmp_path, extra_yaml=extra)
    body = pack("new")
    del body["connection"]["mode"]
    r = client.post("/api/devices", json=body)
    assert r.status_code == 422 and "connection.mode" in r.text
    r = client.post("/api/devices/test", json={"connection": body["connection"]})
    assert r.status_code == 422 and "connection.mode" in r.text
    # the stored device without a mode: an edit and its Test keep poll
    r = client.put("/api/devices/old", json={"id": "old", "enabled": False, "template": "seplos_bms_v2",
                                             "name": "renamed", "connection": {}})
    assert r.status_code == 200, r.text
    r = client.post("/api/devices/test", json={"device_id": "old", "connection": {"unit_id": 2}})
    assert r.status_code == 200 and "connection.mode" not in r.text
    # a Modbus device moved onto Seplos must say how
    r = client.post("/api/devices", json={"id": "mb", "enabled": False, "connection": {
        "protocol": "rtu", "serial_port": "/dev/ttyM", "unit_id": 1}})
    assert r.status_code == 200, r.text
    r = client.put("/api/devices/mb", json={"id": "mb", "enabled": False, "template": "seplos_bms_v2",
                                           "connection": {"protocol": "seplos_v2", "serial_port": "/dev/ttyM",
                                                          "unit_id": 1}})
    assert r.status_code == 422 and "connection.mode" in r.text


def test_r5_the_poll_loop_survives_an_error_and_a_bad_address_does_not_start(monkeypatch):
    import time
    from types import SimpleNamespace
    from multibus.config import PollGroup
    c = SimpleNamespace(protocol='seplos_v2', serial_port='/dev/ttyR5', host='', port=0, baudrate=9600,
                        parity='N', stopbits=1, bytesize=8, unit_id=1, mode='poll', timeout=1,
                        retry_attempts=1, retry_delay=0, stale_after_s=30)
    reg = SimpleNamespace(address=0x20, name='cells', register_type='input', poll_group='realtime',
                          data_type='uint16', scale=1, offset=0, nan=None, monotonic=False, enum=None,
                          bits=None, mask=None, shift=None, scale_from='')
    cl = sv.SeplosV2Client(c, [reg], {'realtime': PollGroup(interval=0.05)}, device_id='r5')
    n = {'calls': 0}

    def flaky(cid2):
        n['calls'] += 1
        if n['calls'] == 1:
            raise RuntimeError('boom')
        return False
    monkeypatch.setattr(cl, 'poll_once', flaky)
    cl._registered = True
    cl.start_polling()
    deadline = time.time() + 3
    while time.time() < deadline and n['calls'] < 3:
        time.sleep(0.02)
    alive = cl._thread.is_alive()
    cl.disconnect()
    assert n['calls'] >= 3 and alive and cl.stats.loop_errors == 1
    bad = sv.SeplosV2Client(SimpleNamespace(**{**vars(c), 'unit_id': 300}), [], {}, device_id='r5b')
    assert bad.connect() is False and 'unit_id' in bad.data_health()['reason']


@needs_tc
def test_r6_a_write_is_refused_only_when_no_source_can_write(tmp_path):
    from tests.test_write_path import _app
    extra = """
devices:
  - id: mixed
    enabled: false
    sources:
      - {id: bus, protocol: rtu_tap, serial_port: /dev/ttyT, unit_id: 1, template: fronius_smart_meter_65a}
      - {id: net, protocol: tcp, host: 192.0.2.9, unit_id: 1, template: fronius_smart_meter_65a}
  - id: taponly
    enabled: false
    template: fronius_smart_meter_65a
    connection: {protocol: rtu_tap, serial_port: /dev/ttyT2, unit_id: 1}
"""
    _cfg, client = _app(tmp_path, auth=True, allow_writes=True, extra_yaml=extra)
    r = client.post("/api/devices/mixed/write", json={"address": 1, "value": 1, "register_type": "holding",
                                                      "unguarded": True, "data_type": "uint16"})
    assert "read-only" not in r.text, r.text
    r = client.post("/api/devices/taponly/write", json={"address": 1, "value": 1, "register_type": "holding"})
    assert r.status_code == 400 and "read-only" in r.text


def test_r7_a_listening_seplos_source_keeps_running_on_a_standby():
    from types import SimpleNamespace
    from multibus import role
    from multibus.config import ModbusConfig
    src = lambda mode: SimpleNamespace(protocol='seplos_v2', connection=ModbusConfig(protocol='seplos_v2', mode=mode))  # noqa: E731
    assert role.source_listens(src('listen'))
    assert not role.source_listens(src('poll')) and not role.source_listens(src(''))
    assert role.source_listens(SimpleNamespace(protocol='rtu_tap', connection=ModbusConfig()))
    assert not role.source_listens(SimpleNamespace(protocol='rtu', connection=ModbusConfig()))
