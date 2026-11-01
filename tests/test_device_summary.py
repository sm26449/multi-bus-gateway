# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""A device's one-line connection summary and its edit locks (ADR-0031 §3,
§5).

One function (``device_summary.device_view``) gives both. The device page's
header, the device list and the bridge cards read the same words, e.g.
``Modbus RTU · /dev/ttySC1 · 9600 8E1 · unit 1 · Master``. The locks say
which fields an edit may not change and why, from ``connection_spec.edit_errors``,
the bus's line (a device sharing a serial line with others inherits it) and
the bus claims (a bus is polled or listened to, never both)."""
import pytest

from tests.test_devices_api import make_app, needs_tc


def _tty(tmp_path, name):
    p = tmp_path / name
    p.write_text("")
    return str(p)


def _fixture(tmp_path):
    t = {n: _tty(tmp_path, n) for n in ("ttySC0", "ttySC1", "ttySC2", "ttySC3")}
    return t, f"""
bridges:
  - {{id: ws, name: Panel, type: rtu_transparent, host: 192.0.2.60, ports: [{{port: 4196}}, {{port: 4197}}]}}
  - {{id: gw, name: Gateway, type: modbus_gateway, host: 192.0.2.70, ports: [{{port: 502}}]}}
devices:
  - id: deye
    template: deye_hybrid_1p
    connection: {{protocol: rtu, serial_port: {t['ttySC1']}, unit_id: 1, baudrate: 9600, parity: N}}
  - id: meter2
    enabled: false
    template: eastron_sdm630
    connection: {{protocol: rtu, serial_port: {t['ttySC1']}, unit_id: 2, baudrate: 9600, parity: N}}
  - id: pack3
    template: seplos_bms_v2_tap
    connection: {{protocol: seplos_v2, serial_port: {t['ttySC0']}, unit_id: 3, mode: listen, baudrate: 19200}}
  - id: tap
    connection: {{protocol: rtu_tap, serial_port: {t['ttySC2']}, unit_id: 4}}
  - id: asc
    connection: {{protocol: ascii, serial_port: {t['ttySC3']}, unit_id: 5, baudrate: 9600, parity: E}}
  - id: tcpdev
    connection: {{protocol: tcp, host: 192.0.2.10, port: 502, unit_id: 6}}
  - id: udpdev
    connection: {{protocol: udp, host: 192.0.2.11, port: 502, unit_id: 7}}
  - id: conv
    connection: {{protocol: rtu-tcp, host: 192.0.2.20, port: 4001, unit_id: 8}}
  - id: brpoll
    connection: {{protocol: rtu, bridge: ws, bridge_port: 4196, unit_id: 9}}
  - id: brtap
    connection: {{protocol: rtu_tap, bridge: ws, bridge_port: 4197, unit_id: 2}}
  - id: gwdev
    connection: {{protocol: tcp, bridge: gw, bridge_port: 502, unit_id: 3}}
  - id: sepnet
    template: seplos_bms_v2
    connection: {{protocol: seplos_v2-tcp, host: 192.0.2.80, port: 4001, unit_id: 0, mode: poll}}
  - id: bms-can
    template: pylontech_can_inverter
    connection: {{protocol: can, carrier: socketcan, channel: can0, bitrate: 500000}}
  - id: web
    template: fronius_solar_api_meter
    connection: {{protocol: http, url: "http://user:pw@192.0.2.5/solar_api/v1/GetMeterRealtimeData.cgi?Scope=Device&DeviceId=1&token=abc"}}
  - id: gx
    template: victron_gx_mqtt
    connection: {{protocol: mqtt, broker: gx.local, topic: "N/abc/#"}}
  - id: ups
    kind: ups
    connection: {{protocol: none}}
    meters:
      - {{role: input, device: tcpdev, power: power_active_total}}
"""


@pytest.fixture
def app(tmp_path):
    t, yml = _fixture(tmp_path)
    cfg, client = make_app(tmp_path, extra_yaml=yml)
    by = {d["id"]: d for d in client.get("/api/devices").json()["devices"]}
    return cfg, client, t, by


def _head(text, n):
    return " · ".join(text.split(" · ")[:n])


# ── the one-liner, per matrix cell ───────────────────────────────────────
@needs_tc
def test_every_device_carries_a_summary_and_locks(app):
    _cfg, _client, _t, by = app
    for did, d in by.items():
        assert isinstance(d.get("summary"), dict), did
        assert isinstance(d.get("summary", {}).get("text"), str) and d["summary"]["text"], did
        assert isinstance(d.get("locks"), list), did
        for lk in d["locks"]:
            assert set(lk) >= {"field", "code", "text"}, (did, lk)


@needs_tc
def test_summary_text_for_each_matrix_cell(app):
    _cfg, _client, t, by = app
    s = {k: v["summary"]["text"] for k, v in by.items()}
    assert _head(s["deye"], 5) == f"Modbus RTU · {t['ttySC1']} · 9600 8N1 · unit 1 · Master"
    assert _head(s["asc"], 5) == f"Modbus ASCII · {t['ttySC3']} · 9600 8E1 · unit 5 · Master"
    assert _head(s["tap"], 5) == f"Modbus RTU · {t['ttySC2']} · 9600 8N1 · unit 4 · Listen-only"
    assert _head(s["pack3"], 5) == f"Seplos V2 · {t['ttySC0']} · 19200 8N1 · pack 3 · Listen-only"
    assert _head(s["tcpdev"], 4) == "Modbus TCP · 192.0.2.10:502 · unit 6 · Master"
    assert _head(s["udpdev"], 4) == "Modbus UDP · 192.0.2.11:502 · unit 7 · Master"
    assert _head(s["conv"], 4) == "Modbus RTU over TCP · 192.0.2.20:4001 · unit 8 · Master"
    assert _head(s["brpoll"], 4) == "Modbus RTU · bridge ws port 4196 · unit 9 · Master"
    assert _head(s["brtap"], 4) == "Modbus RTU · bridge ws port 4197 · unit 2 · Listen-only"
    assert _head(s["gwdev"], 4) == "Modbus TCP · bridge gw port 502 · unit 3 · Master"
    assert _head(s["sepnet"], 4) == "Seplos V2 over TCP · 192.0.2.80:4001 · pack 0 · Master"
    assert s["bms-can"] == "CAN · can0 · 500 kbit/s · Listen-only"
    assert _head(s["web"], 2) == "HTTP/JSON · 192.0.2.5/solar_api/v1/GetMeterRealtimeData.cgi"
    assert s["gx"] == "MQTT/JSON · gx.local · N/abc/#"
    assert s["ups"] == "Measured by its meters"
    assert s["umg512"].startswith("Modbus TCP · 192.168.1.207:502 · unit 1 · Master")


@needs_tc
def test_summary_never_carries_credentials(app):
    _cfg, _client, _t, by = app
    web = by["web"]["summary"]
    assert "pw" not in str(web) and "token" not in str(web) and "user" not in str(web)
    assert web["endpoint_path"] == "/solar_api/v1/GetMeterRealtimeData.cgi"


@needs_tc
def test_summary_structured_fields(app):
    _cfg, _client, t, by = app
    d = by["deye"]["summary"]
    assert d["port"] == "serial" and d["reach"] == "here" and d["protocol"] == "modbus"
    assert d["framing"] == "rtu" and d["role"] == "master"
    assert d["line"] == {"baud": 9600, "parity": "N", "bytesize": 8, "stopbits": 1}
    assert d["where"] == t["ttySC1"] and d["unit"] == 1 and d["address_kind"] == "unit"
    b = by["brpoll"]["summary"]
    assert b["line"] is None and b["bridge"] == {"id": "ws", "port": 4196, "type": "rtu_transparent",
                                                   "framing": "rtu", "host": "192.0.2.60"}
    assert by["pack3"]["summary"]["role"] == "listen-only"
    assert by["pack3"]["summary"]["address_kind"] == "pack"
    assert by["bms-can"]["summary"]["role"] == "listen-only"
    assert by["web"]["summary"]["role"] is None and by["gx"]["summary"]["role"] is None
    assert by["ups"]["summary"]["port"] is None


def test_summary_text_with_intervals_is_one_line():
    from multibus.device_summary import summary_text
    s = {"port": "serial", "reach": "here", "protocol": "modbus", "framing": "rtu", "udp": False,
         "where": "/dev/ttySC1", "line": {"baud": 9600, "parity": "E", "bytesize": 8, "stopbits": 1},
         "unit": 1, "address_kind": "unit", "role": "master", "poll_s": [2, 10]}
    assert summary_text(s) == "Modbus RTU · /dev/ttySC1 · 9600 8E1 · unit 1 · Master · 2 s/10 s"
    assert summary_text(dict(s, poll_s=[5, 5])).endswith("· Master · 5 s")
    assert summary_text(dict(s, poll_s=[0.5, 1])).endswith("· Master · 0.5 s/1 s")


# ── the locks ────────────────────────────────────────────────────────────
def _locks(by, did):
    return {(lk["field"], lk["code"]): lk["text"] for lk in by[did]["locks"]}


@needs_tc
def test_a_device_sharing_a_serial_line_inherits_it_locked(app):
    _cfg, _client, _t, by = app
    lk = _locks(by, "deye")
    assert ("connection.line", "line_shared") in lk
    assert lk[("connection.line", "line_shared")].startswith("set by the bus: meter2")
    assert "9600 8N1" in lk[("connection.line", "line_shared")]
    # and the other way round (a disabled device holds the line too: a save checks it)
    assert ("connection.line", "line_shared") in _locks(by, "meter2")
    # alone on its line: editable in place
    assert not any(f == "connection.line" for f, _c in _locks(by, "asc"))


@needs_tc
def test_a_line_set_elsewhere_is_locked(app):
    _cfg, _client, _t, by = app
    assert ("connection.line", "line_bridge") in _locks(by, "brpoll")
    assert ("connection.line", "line_remote") in _locks(by, "conv")
    assert not any(f == "connection.line" for f, _c in _locks(by, "tcpdev"))


@needs_tc
def test_the_protocol_class_is_locked_with_edit_errors_words(app):
    _cfg, _client, _t, by = app
    from multibus.connection_spec import METER_ONLY_TEXT, PRIMARY_TEXT
    assert _locks(by, "umg512")[("connection.protocol", "primary")] == PRIMARY_TEXT
    assert _locks(by, "ups")[("connection.protocol", "meter_only")] == METER_ONLY_TEXT
    txt = _locks(by, "tcpdev")[("connection.protocol", "protocol_fixed")]
    assert "fixed after creation" in txt and "Modbus" in txt


@needs_tc
def test_the_role_is_locked_by_the_template_the_protocol_and_the_bus(app):
    _cfg, _client, _t, by = app
    assert ("connection.role", "template_listens") in _locks(by, "pack3")
    assert ("connection.role", "can_listen") in _locks(by, "bms-can")
    # a device alone on its bus chooses its role
    assert not any(f == "connection.role" for f, _c in _locks(by, "asc"))


@needs_tc
def test_the_role_is_locked_by_another_device_on_the_bus(tmp_path):
    tty = _tty(tmp_path, "ttyS9")
    _cfg, client = make_app(tmp_path, extra_yaml=f"""
devices:
  - {{id: a, connection: {{protocol: rtu, serial_port: {tty}, unit_id: 1}}}}
  - {{id: b, connection: {{protocol: rtu, serial_port: {tty}, unit_id: 2}}}}
  - {{id: t1, connection: {{protocol: rtu_tap, host: 192.0.2.9, port: 4001, unit_id: 1}}}}
  - {{id: t2, connection: {{protocol: rtu_tap, host: 192.0.2.9, port: 4001, unit_id: 2}}}}
""")
    by = {d["id"]: d for d in client.get("/api/devices").json()["devices"]}
    lk = _locks(by, "a")
    assert lk[("connection.role", "role_bus_polled")].startswith("polled by b")
    lk = _locks(by, "t1")
    assert lk[("connection.role", "role_bus_listened")].startswith("listened to by t2")


@needs_tc
def test_get_one_device_has_the_same_summary_and_locks_as_the_list(app):
    _cfg, client, _t, by = app
    one = client.get("/api/devices/deye").json()["device"]
    assert one["summary"] == by["deye"]["summary"] and one["locks"] == by["deye"]["locks"]


# ── review of l3.2a (M1, m1, m4, m5, NIT) ────────────────────────────────
_UNIT_AND_COMPONENT = """
endpoints:
  - id: pv
    name: PV
    template: eastron_sdm630
    enabled: false
    connection: {protocol: rtu-tcp, host: 192.0.2.70, port: 4001}
    units: [{unit_id: 1}, {unit_id: 2}]
devices:
  - id: gx
    name: Victron GX
    template: victron_gx_mqtt
    enabled: false
    connection: {protocol: mqtt, broker: 192.0.2.10, topic: 'N/abc/#', vars: {portal: abc}}
    components:
      - {type: battery, instance: 288, name: SmartShunt}
"""


@needs_tc
def test_an_installation_unit_and_a_component_are_locked_whole_first(tmp_path):
    _cfg, client = make_app(tmp_path, extra_yaml=_UNIT_AND_COMPONENT)
    devs = client.get("/api/devices").json()["devices"]
    unit = next(d for d in devs if d.get("endpoint_id") == "pv")
    comp = next(d for d in devs if d.get("component_of") == "gx")
    lu, lc = unit["locks"], comp["locks"]
    assert lu[0]["field"] == "device" and lu[0]["code"] == "installation_unit"
    assert lu[0]["installation"] == "pv" and "edit the endpoint instead" in lu[0]["text"]
    assert lc[0]["field"] == "device" and lc[0]["code"] == "component" and lc[0]["parent"] == "gx"
    assert lc[0]["text"] == f"'{comp['id']}' is a component of 'gx' — manage it from that device"
    for lk in lu + lc:
        assert "Edit bus line" not in lk["text"], lk
        assert lk["field"] != "connection.role", lk
    line = [lk for lk in lu if lk["field"] == "connection.line"]
    assert line and line[0]["code"] == "line_installation" and "'pv'" in line[0]["text"]
    assert [lk["field"] for lk in lc] == ["device"]


@needs_tc
def test_the_bus_claims_are_built_once_per_list(tmp_path, monkeypatch):
    from multibus import bus_claims
    _t, yml = _fixture(tmp_path)
    _cfg, client = make_app(tmp_path, extra_yaml=yml)
    calls = []
    real = bus_claims.source_claims

    def counting(dev, *a, **kw):
        calls.append(dev.id)
        return real(dev, *a, **kw)
    monkeypatch.setattr(bus_claims, "source_claims", counting)
    n = len(client.get("/api/devices").json()["devices"])
    # each device once for its own bus, the index once: linear, not n²
    assert len(calls) <= 2 * n + 1, (len(calls), n)


@needs_tc
def test_the_shared_line_lock_quotes_the_other_devices_and_their_disagreement(tmp_path):
    tty = _tty(tmp_path, "ttyS7")
    _cfg, client = make_app(tmp_path, extra_yaml=f"""
devices:
  - {{id: a, connection: {{protocol: rtu, serial_port: {tty}, unit_id: 1, baudrate: 9600}}}}
  - {{id: b, enabled: false, connection: {{protocol: rtu, serial_port: {tty}, unit_id: 2, baudrate: 19200}}}}
  - {{id: c, enabled: false, connection: {{protocol: rtu, serial_port: {tty}, unit_id: 3, baudrate: 19200}}}}
""")
    by = {d["id"]: d for d in client.get("/api/devices").json()["devices"]}
    txt = _locks(by, "a")[("connection.line", "line_shared")]
    assert txt.startswith("set by the bus: b, c read it at 19200 8N1")
    assert "disagree" in txt
    txt_b = _locks(by, "b")[("connection.line", "line_shared")]
    assert "a reads it at 9600 8N1" in txt_b and "c reads it at 19200 8N1" in txt_b


@needs_tc
def test_a_bad_url_port_and_a_failing_summary_never_hide_the_rest(tmp_path, monkeypatch):
    _cfg, client = make_app(tmp_path, extra_yaml="""
devices:
  - {id: web, connection: {protocol: http, url: "http://192.0.2.5:99999/x"}}
  - {id: tcpdev, connection: {protocol: tcp, host: 192.0.2.10, port: 502, unit_id: 6}}
""")
    by = {d["id"]: d for d in client.get("/api/devices").json()["devices"]}
    assert by["web"]["summary"]["text"].startswith("HTTP/JSON · 192.0.2.5")
    from multibus import device_summary

    def boom(*a, **kw):
        raise RuntimeError("x")
    monkeypatch.setattr(device_summary, "summarize", boom)
    by = {d["id"]: d for d in client.get("/api/devices").json()["devices"]}
    assert by["tcpdev"]["summary"] is None
    assert any(lk["code"] == "protocol_fixed" for lk in by["tcpdev"]["locks"])


@needs_tc
def test_mqtt_on_the_gateways_broker_says_so(tmp_path):
    _cfg, client = make_app(tmp_path, extra_yaml="""
devices:
  - {id: z, connection: {protocol: mqtt, broker: "@gateway", topic: "zigbee2mqtt/x"}}
""")
    by = {d["id"]: d for d in client.get("/api/devices").json()["devices"]}
    assert by["z"]["summary"]["text"] == "MQTT/JSON · gateway broker · zigbee2mqtt/x"
