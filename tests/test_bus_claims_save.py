# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The bus claim is one rule for saving and for transmitting (ADR-0025,
review of L0): a polled connection is refused on a bus a device only
listens to, and a listener on a bus another device polls — for every
source of a device and every group and unit of an installation. The LAN
bridge discovery, the runtime's shared readers and transports, a standby
gateway and a multi-source Test follow the same identity of a bus."""
import os
from types import SimpleNamespace

from tests.test_bus_claims import _app, _ttys
from tests.test_devices_api import make_app, needs_tc


def _files(tmp_path, *names):
    out = []
    for n in names:
        p = tmp_path / n
        p.write_text("")
        out.append(str(p))
    return out


def _errs(r):
    assert r.status_code == 422, (r.status_code, r.text)
    return " | ".join(r.json()["detail"]["errors"])


# ── M1: saving asks the same claim, both ways ────────────────────────────
@needs_tc
def test_a_polled_device_is_refused_on_a_line_a_later_source_listens_to(tmp_path):
    (tty,) = _files(tmp_path, "ttyX")
    _cfg, client = make_app(tmp_path, extra_yaml=f"""
devices:
  - id: multi
    enabled: true
    sources:
      - {{ id: web, protocol: http, url: "http://192.0.2.5/x", template: fronius_solar_api_meter }}
      - {{ id: mq, protocol: mqtt, topic: "a/b", template: tasmota_generic }}
      - {{ id: wire, protocol: rtu_tap, serial_port: {tty}, unit_id: 1, template: eastron_sdm120 }}
""")
    r = client.post("/api/devices", json={"id": "m9", "enabled": False, "template": "eastron_sdm120",
                                          "connection": {"protocol": "rtu", "serial_port": tty, "unit_id": 9}})
    assert "multi" in _errs(r) and "connection.protocol" in r.json()["detail"]["fields"]


@needs_tc
def test_a_listener_is_refused_on_a_line_another_device_polls(tmp_path):
    tty_q, tty_p = _files(tmp_path, "ttyQ", "ttyP")
    _cfg, client = make_app(tmp_path, extra_yaml=f"""
devices:
  - id: multi2
    enabled: true
    sources:
      - {{ id: web, protocol: http, url: "http://192.0.2.5/x", template: fronius_solar_api_meter }}
      - {{ id: wire, protocol: rtu, serial_port: {tty_q}, unit_id: 1, template: eastron_sdm120 }}
  - id: poller
    enabled: true
    template: eastron_sdm120
    connection: {{ protocol: rtu, serial_port: {tty_p}, unit_id: 1 }}
""")
    r = client.post("/api/devices", json={"id": "t9", "enabled": False, "template": "eastron_sdm120",
                                          "connection": {"protocol": "rtu_tap", "serial_port": tty_q, "unit_id": 9}})
    assert "multi2" in _errs(r)
    # an installation that listens on a polled line, too
    r = client.post("/api/endpoints", json={"id": "bank", "template": "eastron_sdm120", "enabled": False,
                                            "connection": {"protocol": "rtu_tap", "serial_port": tty_p},
                                            "units": [2, 3]})
    assert "poller" in _errs(r)


@needs_tc
def test_a_polled_device_is_refused_next_to_a_device_whose_template_listens(tmp_path):
    (tty,) = _files(tmp_path, "ttyV3")
    _cfg, client = make_app(tmp_path, extra_yaml=f"""
devices:
  - id: v3
    enabled: true
    template: seplos_bms_v3_rtu_tap
    connection: {{ protocol: rtu, serial_port: {tty}, unit_id: 1 }}
""")
    r = client.post("/api/devices", json={"id": "m9", "enabled": False, "template": "eastron_sdm120",
                                          "connection": {"protocol": "rtu", "serial_port": tty, "unit_id": 9}})
    assert "v3" in _errs(r)


@needs_tc
def test_an_installation_polling_a_tapped_bridge_port_is_refused(tmp_path):
    _cfg, client, *_ = _app(tmp_path)
    r = client.post("/api/endpoints", json={"id": "plant", "template": "eastron_sdm120", "enabled": False,
                                            "connection": {"protocol": "rtu-tcp", "host": "192.0.2.60",
                                                           "port": 4196},
                                            "units": [3, 4]})
    assert "brtap" in _errs(r)
    # …and through its third source
    r = client.post("/api/endpoints", json={
        "id": "plant", "template": "eastron_sdm120", "enabled": False,
        "connection": {"protocol": "tcp", "host": "192.0.2.99", "port": 502}, "units": [3],
        "sources": [{"id": "a", "protocol": "tcp", "host": "192.0.2.99", "port": 502},
                    {"id": "b", "protocol": "tcp", "host": "192.0.2.98", "port": 502},
                    {"id": "c", "protocol": "rtu-tcp", "host": "192.0.2.60", "port": 4196}]})
    e = _errs(r)
    assert "brtap" in e and "sources[2]" in e


@needs_tc
def test_an_installation_with_a_listening_template_over_tcp_is_refused(tmp_path):
    _cfg, client = make_app(tmp_path)
    r = client.post("/api/endpoints", json={"id": "bank", "template": "seplos_bms_v3_rtu_tap", "enabled": False,
                                            "connection": {"protocol": "tcp", "host": "192.0.2.80", "port": 502},
                                            "units": [1, 2]})
    assert "seplos_bms_v3_rtu_tap" in _errs(r)
    r = client.post("/api/endpoints", json={
        "id": "bank", "enabled": False, "connection": {"protocol": "tcp", "host": "192.0.2.80", "port": 502},
        "groups": [{"id": "packs", "template": "seplos_bms_v3_rtu_tap", "units": [1, 2],
                    "connection": {"protocol": "tcp", "host": "192.0.2.80", "port": 502}}]})
    assert "groups[0]" in _errs(r)


@needs_tc
def test_an_unrelated_edit_of_the_listener_itself_is_not_refused(tmp_path):
    _cfg, client, *_ = _app(tmp_path)
    r = client.put("/api/devices/brtap", json={"name": "Bridge tap"})
    assert r.status_code == 200, r.text


# ── M2: the bridge LAN discovery does not knock on a listened port ───────
@needs_tc
def test_bridge_discovery_skips_a_listened_host_and_port(tmp_path, monkeypatch):
    import multibus.bridges as br
    asked = []
    monkeypatch.setattr(br, "_port_open", lambda h, p, t: asked.append((h, p)) or False)
    _cfg, client = make_app(tmp_path, extra_yaml="""
devices:
  - id: sv2net
    enabled: true
    connection: { protocol: seplos_v2-tcp, host: 192.0.2.77, port: 4196, unit_id: 1, mode: listen }
""")
    r = client.post("/api/bridges/discover", json={"cidr": "192.0.2.77/32"})
    assert r.status_code == 200, r.text
    assert ("192.0.2.77", 4196) not in asked and asked                # the other ports are asked
    assert r.json()["skipped"] == [{"host": "192.0.2.77", "port": 4196, "listeners": ["sv2net"]}]


# ── minors 1-4 ───────────────────────────────────────────────────────────
def test_a_char_device_is_known_by_its_major_and_minor(monkeypatch):
    import stat
    from multibus import bus_claims
    real = os.stat
    nodes = {"/dev/ttySEPLOS": (188, 0), "/dev/ttyUSB0": (188, 0), "/dev/ttyUSB1": (188, 1)}

    def fake(p, *a, **k):
        if p in nodes:
            return SimpleNamespace(st_mode=stat.S_IFCHR | 0o660, st_rdev=os.makedev(*nodes[p]))
        return real(p, *a, **k)
    monkeypatch.setattr(os, "stat", fake)
    assert bus_claims.canonical_serial("/dev/ttySEPLOS") == bus_claims.canonical_serial("/dev/ttyUSB0")
    assert bus_claims.canonical_serial("/dev/ttyUSB0") != bus_claims.canonical_serial("/dev/ttyUSB1")


def test_an_alias_shares_the_tap_reader_and_the_serial_transport(tmp_path):
    from multibus import rtu_tap
    from multibus.config import ModbusConfig
    from multibus.modbus_client import ModbusConnection
    tty, alias, _tty2 = _ttys(tmp_path)
    c = lambda sp: SimpleNamespace(serial_port=sp, host="", port=0)  # noqa: E731
    assert rtu_tap._reader_key(c(alias)) == rtu_tap._reader_key(c(tty))
    a = ModbusConnection(ModbusConfig(protocol="rtu", serial_port=alias, unit_id=1))
    b = ModbusConnection(ModbusConfig(protocol="rtu", serial_port=tty, unit_id=2))
    assert a._tp is b._tp


@needs_tc
def test_on_a_standby_gateway_a_polled_rtu_source_listens(tmp_path):
    from multibus import role
    from multibus.bus_claims import listeners
    (tty,) = _files(tmp_path, "ttyR")
    cfg, client = make_app(tmp_path, extra_yaml=f"""
devices:
  - id: rtu1
    enabled: true
    template: eastron_sdm120
    connection: {{ protocol: rtu, serial_port: {tty}, unit_id: 1 }}
""")
    tr = client.app.state.template_registry
    assert listeners(cfg, tty, tr) == []
    role.install(SimpleNamespace(active=False))
    try:
        assert listeners(cfg, tty, tr) == ["rtu1"]
    finally:
        role.install(None)


@needs_tc
def test_a_listening_source_does_not_abort_a_multi_source_test(tmp_path):
    (tty,) = _files(tmp_path, "ttyV3")
    _cfg, client = make_app(tmp_path, extra_yaml=f"""
devices:
  - id: two
    enabled: true
    sources:
      - {{ id: net, protocol: tcp, host: 192.0.2.9, port: 502, unit_id: 1, timeout: 0.2, template: eastron_sdm120 }}
      - {{ id: wire, protocol: rtu, serial_port: {tty}, unit_id: 1, template: seplos_bms_v3_rtu_tap }}
""")
    r = client.post("/api/devices/two/test")
    assert r.status_code == 200, r.text
    rows = {x["source"]: x for x in r.json()["sources"]}
    assert rows["wire"]["ok"] in (None, False) and "listen" in rows["wire"]["message"]
