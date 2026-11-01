# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""GET /api/buses and GET /api/can-interfaces (ADR-0031 §5; design D6, D9).

``/api/buses``: one read model per known bus — its canonical id (the bus
claims' key), kind, where it is, who polls and who listens, the units in use,
the line (from the devices on it, a conflict flagged), the measured load and,
for a bridge, its type's framing and the client it reports. Read only.

``/api/can-interfaces``: the CAN interfaces of this host from sysfs, with
up/down, bitrate, listen-only and state from rtnetlink. Read only."""
import os

from tests.test_devices_api import make_app, needs_tc


def _tty(tmp_path, name):
    p = tmp_path / name
    p.write_text("")
    return str(p)


def _yaml(tty, tty2):
    return f"""
bridges:
  - {{id: ws, name: Panel, type: rtu_transparent, host: 192.0.2.60, ports: [{{port: 4196}}, {{port: 4197}}]}}
  - {{id: gw, name: Gateway, type: modbus_gateway, host: 192.0.2.70, ports: [{{port: 502}}]}}
devices:
  - {{id: deye, template: deye_hybrid_1p, connection: {{protocol: rtu, serial_port: {tty}, unit_id: 1}}}}
  - {{id: meter2, connection: {{protocol: rtu, serial_port: {tty}, unit_id: 2, baudrate: 19200}}}}
  - {{id: off3, enabled: false, connection: {{protocol: rtu, serial_port: {tty}, unit_id: 3}}}}
  - {{id: pack3, template: seplos_bms_v2_tap,
      connection: {{protocol: seplos_v2, serial_port: {tty2}, unit_id: 3, mode: listen}}}}
  - {{id: brpoll, connection: {{protocol: rtu, bridge: ws, bridge_port: 4196, unit_id: 9}}}}
  - {{id: brtap, connection: {{protocol: rtu_tap, bridge: ws, bridge_port: 4197, unit_id: 2}}}}
  - {{id: conv, connection: {{protocol: rtu-tcp, host: 192.0.2.60, port: 4196, unit_id: 10}}}}
  - {{id: tcpdev, connection: {{protocol: tcp, host: 192.0.2.10, port: 502, unit_id: 6}}}}
  - {{id: bms-can, template: pylontech_can_inverter,
      connection: {{protocol: can, carrier: socketcan, channel: can0, bitrate: 500000}}}}
  - {{id: web, connection: {{protocol: http, url: "http://192.0.2.5/x"}}}}
"""


def _buses(tmp_path):
    tty, tty2 = _tty(tmp_path, "ttySC1"), _tty(tmp_path, "ttySC0")
    cfg, client = make_app(tmp_path, extra_yaml=_yaml(tty, tty2))
    r = client.get("/api/buses")
    assert r.status_code == 200, r.text
    return cfg, client, tty, tty2, {b["id"]: b for b in r.json()["buses"]}


@needs_tc
def test_two_devices_on_a_tty_one_bus_with_its_units_and_a_line_conflict(tmp_path):
    from multibus.bus_claims import canonical_serial
    _cfg, _c, tty, _tty2, buses = _buses(tmp_path)
    b = buses[canonical_serial(tty)]
    assert b["kind"] == "serial" and b["where"] == tty
    assert b["pollers"] == ["deye", "meter2"] and b["listeners"] == []
    assert [d["id"] for d in b["devices"]] == ["deye", "meter2", "off3"]
    assert {u["unit"]: u["device"] for u in b["units_in_use"]} == {1: "deye", 2: "meter2", 3: "off3"}
    # two lines on one tty: no single line, the conflict said
    assert b["line"] is None and b["line_conflict"] is True
    assert sorted(x["line"]["baud"] for x in b["lines"]) == [9600, 19200]
    assert b["framing"] == "rtu" and b["role"] == "master" and b["role_conflict"] is False


@needs_tc
def test_a_listened_bus_names_its_listeners(tmp_path):
    from multibus.bus_claims import canonical_serial
    _cfg, _c, _tty, tty2, buses = _buses(tmp_path)
    b = buses[canonical_serial(tty2)]
    assert b["listeners"] == ["pack3"] and b["pollers"] == [] and b["role"] == "listen-only"
    assert b["line"] == {"baud": 9600, "parity": "N", "bytesize": 8, "stopbits": 1} and not b["line_conflict"]
    assert b["protocols"] == ["seplos_v2"]


@needs_tc
def test_a_bridge_port_is_one_bus_with_a_converter_at_the_same_address(tmp_path):
    _cfg, _c, _t, _t2, buses = _buses(tmp_path)
    b = buses["192.0.2.60:4196"]
    assert b["kind"] == "bridge" and b["host"] == "192.0.2.60" and b["port"] == 4196
    assert b["bridge"]["id"] == "ws" and b["bridge"]["type"] == "rtu_transparent"
    assert b["framing"] == "rtu"
    assert sorted(b["pollers"]) == ["brpoll", "conv"]
    assert b["line"] is None                       # the bridge's, not ours
    tapped = buses["192.0.2.60:4197"]
    assert tapped["listeners"] == ["brtap"] and tapped["kind"] == "bridge"
    # a declared port with nothing on it is a known bus too
    gw = buses["192.0.2.70:502"]
    assert gw["devices"] == [] and gw["bridge"]["framing"] == "mbap"


@needs_tc
def test_an_address_a_can_channel_and_nothing_for_http(tmp_path):
    _cfg, _c, _t, _t2, buses = _buses(tmp_path)
    assert buses["192.0.2.10:502"]["kind"] == "address"
    assert buses["192.0.2.10:502"]["framing"] == "mbap"
    can = buses["can:can0"]
    assert can["kind"] == "can" and can["listeners"] == ["bms-can"] and can["where"] == "can0"
    assert not any("192.0.2.5" in k for k in buses)


@needs_tc
def test_the_load_is_measured_and_none_before_anything_was(tmp_path, monkeypatch):
    from multibus import modbus_client
    calls = []

    def occ(host, port):
        calls.append((host, port))
        return {"share": 0.62, "busy_s": 37.2, "window_s": 60.0, "missed": 0, "lanes": 1}
    monkeypatch.setattr(modbus_client, "bus_occupancy", occ)
    _cfg, _c, tty, _t2, buses = _buses(tmp_path)
    assert buses["192.0.2.60:4196"]["load"]["pct"] == 62
    assert buses["192.0.2.60:4196"]["load"]["level"] == "ok"
    from multibus.bus_claims import canonical_serial
    assert buses[canonical_serial(tty)]["load"] is None        # no client measured it (tests have none)


def test_load_view_matches_the_bridge_card():
    from multibus.buses import load_view
    v = load_view({"share": 0.96, "busy_s": 57.6, "window_s": 60.0, "missed": 0}, devices=2)
    assert v["pct"] == 96 and v["level"] == "full" and v["over_capacity"] and v["capacity_left_pct"] == 4
    assert load_view(None, shares=[0.2, 0.3], devices=2)["pct"] == 50
    assert load_view(None, shares=[], devices=0) is None


@needs_tc
def test_a_bridge_reports_its_client(tmp_path):
    tty, tty2 = _tty(tmp_path, "ttySC1"), _tty(tmp_path, "ttySC0")
    _cfg, client = make_app(tmp_path, extra_yaml=_yaml(tty, tty2))

    class _Diag:
        def port_view(self, bid, port):
            if (bid, port) == ("ws", 4196):
                return {"stats": {"client": {"address": "192.0.2.99:51000"}}}
            return None
    client.app.state.bridge_diag = _Diag()
    buses = {b["id"]: b for b in client.get("/api/buses").json()["buses"]}
    assert buses["192.0.2.60:4196"]["bridge"]["client"] == "192.0.2.99:51000"
    assert buses["192.0.2.60:4197"]["bridge"].get("client") is None


# ── CAN interfaces ───────────────────────────────────────────────────────
def _net(root, name, typ, flags="0x1003", oper="up"):
    d = root / "sys" / "class" / "net" / name
    d.mkdir(parents=True)
    (d / "type").write_text(f"{typ}\n")
    (d / "flags").write_text(f"{flags}\n")
    (d / "operstate").write_text(f"{oper}\n")


def test_can_interfaces_from_a_fake_sysfs(tmp_path):
    from multibus import can_interfaces
    _net(tmp_path, "can0", 280)
    _net(tmp_path, "vcan0", 280, flags="0x1002", oper="down")
    _net(tmp_path, "eth0", 1)
    info = {"can0": {"kind": "can", "bitrate": 500000, "listen_only": True, "state": "error-active"},
            "vcan0": {"kind": "vcan", "bitrate": None, "listen_only": None, "state": None}}
    got = can_interfaces.interfaces(root=str(tmp_path), info=lambda n: info[n])
    assert [i["name"] for i in got] == ["can0", "vcan0"]
    c0, v0 = got
    assert c0 == {"name": "can0", "kind": "can", "up": True, "operstate": "up", "bitrate": 500000,
                  "listen_only": True, "state": "error-active", "devices": []}
    assert v0["up"] is False and v0["bitrate"] is None and v0["listen_only"] is None


def test_can_interfaces_without_sysfs_is_empty(tmp_path):
    from multibus import can_interfaces
    assert can_interfaces.interfaces(root=str(tmp_path / "nothing"), info=lambda n: {}) == []


def test_link_info_parses_bitrate_state_and_listen_only():
    import struct
    from multibus.can_link import parse_link_info

    def rta(kind, payload):
        ln = 4 + len(payload)
        return struct.pack("=HH", ln, kind) + payload + b"\0" * ((4 - ln % 4) % 4)
    data = (rta(1, struct.pack("=8I", 250000, 875, 0, 0, 0, 0, 0, 0))   # IFLA_CAN_BITTIMING
            + rta(4, struct.pack("=I", 1))                               # IFLA_CAN_STATE: warning
            + rta(5, struct.pack("=II", 0xFF, 0x02)))                    # IFLA_CAN_CTRLMODE
    linkinfo = rta(1, b"can\0") + rta(2, data)
    body = struct.pack("=BxHiII", 0, 0, 3, 0, 0) + rta(18, linkinfo)
    msg = struct.pack("=IHHII", 16 + len(body), 16, 0, 1, 0) + body
    assert parse_link_info(msg) == {"kind": "can", "bitrate": 250000, "listen_only": True,
                                    "state": "error-warning"}
    vbody = struct.pack("=BxHiII", 0, 0, 4, 0, 0) + rta(18, rta(1, b"vcan\0"))
    vmsg = struct.pack("=IHHII", 16 + len(vbody), 16, 0, 1, 0) + vbody
    assert parse_link_info(vmsg) == {"kind": "vcan", "bitrate": None, "listen_only": None, "state": None}


@needs_tc
def test_api_can_interfaces_names_the_devices_on_each(tmp_path, monkeypatch):
    from multibus import can_interfaces
    _net(tmp_path / "root", "can0", 280)
    monkeypatch.setattr(can_interfaces, "SYSROOT", str(tmp_path / "root"))
    monkeypatch.setattr(can_interfaces, "_link_info", lambda n: {"kind": "can", "bitrate": 500000,
                                                                  "listen_only": False,
                                                                  "state": "error-active"})
    tty, tty2 = _tty(tmp_path, "ttySC1"), _tty(tmp_path, "ttySC0")
    _cfg, client = make_app(tmp_path, extra_yaml=_yaml(tty, tty2))
    r = client.get("/api/can-interfaces")
    assert r.status_code == 200, r.text
    got = r.json()["interfaces"]
    assert got[0]["name"] == "can0" and got[0]["devices"] == ["bms-can"] and got[0]["listen_only"] is False


@needs_tc
def test_both_routes_are_open_to_a_viewer_and_read_only(tmp_path):
    tty, tty2 = _tty(tmp_path, "ttySC1"), _tty(tmp_path, "ttySC0")
    _cfg, client = make_app(tmp_path, extra_yaml=_yaml(tty, tty2))
    for path in ("/api/buses", "/api/can-interfaces"):
        assert client.get(path).status_code == 200, path
        for m in ("post", "put", "delete", "patch"):
            assert getattr(client, m)(path).status_code == 405, (m, path)
    assert os.path.exists(tty)



# ── review of l3.2a (m6, m7, NIT) ────────────────────────────────────────
@needs_tc
def test_a_bus_polled_and_listened_says_role_conflict(tmp_path):
    tty = _tty(tmp_path, "ttyS5")
    _cfg, client = make_app(tmp_path, extra_yaml=f"""
devices:
  - {{id: p, connection: {{protocol: rtu, serial_port: {tty}, unit_id: 1}}}}
  - {{id: l, connection: {{protocol: rtu_tap, serial_port: {tty}, unit_id: 2}}}}
""")
    b = next(x for x in client.get("/api/buses").json()["buses"] if x["where"] == tty)
    assert b["role_conflict"] is True and b["pollers"] == ["p"] and b["listeners"] == ["l"]


@needs_tc
def test_one_bad_device_never_hides_the_buses(tmp_path, monkeypatch):
    from multibus import buses as buses_mod
    tty, tty2 = _tty(tmp_path, "ttySC1"), _tty(tmp_path, "ttySC0")
    _cfg, client = make_app(tmp_path, extra_yaml=_yaml(tty, tty2))
    real = buses_mod._sources

    def bad(dev):
        if dev.id == "deye":
            raise RuntimeError("odd device")
        return real(dev)
    monkeypatch.setattr(buses_mod, "_sources", bad)
    r = client.get("/api/buses")
    assert r.status_code == 200
    ids = {d["id"] for b in r.json()["buses"] for d in b["devices"]}
    assert "deye" not in ids and "meter2" in ids


@needs_tc
def test_the_load_counts_every_device_that_polls_the_bus(tmp_path, monkeypatch):
    from multibus import modbus_client
    monkeypatch.setattr(modbus_client, "bus_occupancy",
                        lambda h, p: {"share": 0.1, "busy_s": 6, "window_s": 60, "missed": 0})
    tty, tty2 = _tty(tmp_path, "ttySC1"), _tty(tmp_path, "ttySC0")
    _cfg, client = make_app(tmp_path, extra_yaml=_yaml(tty, tty2) + """
  - {id: broff, enabled: false, connection: {protocol: rtu, bridge: ws, bridge_port: 4196, unit_id: 11}}
""")
    bus = {b["id"]: b for b in client.get("/api/buses").json()["buses"]}["192.0.2.60:4196"]
    # the bridge cards' rule: every device that polls the bus, enabled or not
    assert bus["load"]["devices"] == 3            # brpoll, conv, broff
