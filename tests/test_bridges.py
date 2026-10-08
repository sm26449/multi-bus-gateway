# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Bridges: boxes a serial bus is reached through, several slaves per bus,
devices following their bridge, and telling a transparent converter from a
Modbus TCP gateway by asking it."""
import socket
import struct
import threading

import pytest

from multibus.bridges import TYPES, resolve_connection, validate_bridge
from multibus.routes.bridges import _crc16
from tests.test_devices_api import make_app, needs_tc


def test_the_three_kinds_are_described_by_data():
    assert {"mbg_serial_bridge", "rtu_transparent", "modbus_gateway"} <= set(TYPES)
    assert TYPES["rtu_transparent"]["framing"] == "rtu"
    assert TYPES["modbus_gateway"]["framing"] == "modbus_tcp"
    assert all(t.get("setup") for t in TYPES.values())


def test_a_device_on_a_bridge_gets_host_port_and_protocol_from_it():
    bridges = [{"id": "ws", "type": "rtu_transparent", "host": "192.0.2.60",
                "ports": [{"port": 4196}]},
               {"id": "gw", "type": "modbus_gateway", "host": "192.0.2.61",
                "max_connections": 3, "ports": [{"port": 502}]}]
    a = resolve_connection({"bridge": "ws", "unit_id": 2}, bridges)
    assert (a["host"], a["port"], a["protocol"]) == ("192.0.2.60", 4196, "rtu-tcp")
    b = resolve_connection({"bridge": "gw", "bridge_port": 502, "unit_id": 5}, bridges)
    assert (b["protocol"], b["max_connections"]) == ("tcp", 3)
    gone = {"bridge": "nope", "host": "x"}
    assert resolve_connection(gone, bridges) == gone


def test_a_converter_declares_its_buses_and_errors_are_words():
    assert validate_bridge({"id": "ws", "type": "rtu_transparent", "host": "192.0.2.60",
                            "ports": [{"port": 4196}]}) == []
    errs = " | ".join(validate_bridge({"id": "WS", "type": "x", "host": "", "ports": []}))
    for frag in ("id:", "type:", "host:"):
        assert frag in errs
    assert any("add at least one" in e for e in validate_bridge(
        {"id": "ws", "type": "rtu_transparent", "host": "192.0.2.60"}))


@needs_tc
def test_several_slaves_on_one_bus_and_devices_follow_their_bridge(tmp_path):
    cfg, client = make_app(tmp_path)
    r = client.post("/api/bridges", json={"id": "ws", "name": "Panel", "type": "rtu_transparent",
                                          "host": "192.0.2.60", "ports": [{"port": 4196, "label": "bus A"}]})
    assert r.status_code == 200, r.text
    dev = lambda did, unit: {"id": did, "template": "fronius_smart_meter_65a", "enabled": False,  # noqa: E731
                             "connection": {"bridge": "ws", "bridge_port": 4196, "unit_id": unit}}
    assert client.post("/api/devices", json=dev("m1", 1)).status_code == 200
    assert client.post("/api/devices", json=dev("m2", 2)).status_code == 200      # same bus, another slave
    r = client.post("/api/devices", json=dev("m3", 2))
    assert r.status_code == 422 and "unit 2" in r.text                            # same slave twice
    d = {x.id: x for x in cfg.devices}
    assert (d["m1"].protocol, d["m1"].connection.host, d["m1"].connection.port) == ("rtu-tcp", "192.0.2.60", 4196)
    b = client.get("/api/bridges/ws").json()
    assert [x["unit_id"] for x in b["ports"][0]["devices"]] == [1, 2]
    assert b["state"]["status"] == "unknown"           # disabled devices: nothing to judge by
    # the box moves: its devices follow
    r = client.put("/api/bridges/ws", json={"name": "Panel", "type": "rtu_transparent",
                                            "host": "192.0.2.70", "ports": [{"port": 4196}]})
    assert r.status_code == 200
    assert {x.id: x.connection.host for x in cfg.devices if x.id in ("m1", "m2")} == \
        {"m1": "192.0.2.70", "m2": "192.0.2.70"}
    # a bridge in use cannot go
    assert client.delete("/api/bridges/ws").status_code == 409
    # a port the converter does not have is refused
    r = client.post("/api/devices", json={**dev("m4", 4), "connection": {
        "bridge": "ws", "bridge_port": 9999, "unit_id": 4}})
    assert r.status_code == 422 and "not a port" in r.text


@needs_tc
def test_our_bridge_gets_a_token_and_a_ready_command(tmp_path):
    cfg, client = make_app(tmp_path)
    r = client.post("/api/bridges", json={"id": "pi", "type": "mbg_serial_bridge", "host": "192.0.2.50"})
    assert r.status_code == 200, r.text
    assert r.json()["has_token"] is True and "token" not in r.json()
    setup = client.get("/api/bridges/pi/setup").json()
    assert setup["token"] and f"BRIDGE_TOKEN='{setup['token']}'" in setup["docker_run"]
    # editing without a token keeps the one made
    client.put("/api/bridges/pi", json={"type": "mbg_serial_bridge", "host": "192.0.2.51"})
    assert cfg.get_raw_bridge("pi")["token"] == setup["token"]


# ── telling the two converter modes apart ───────────────────────────────────

def _serve(handler):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)

    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            with c:
                c.settimeout(2)
                try:
                    data = c.recv(256)
                except OSError:
                    continue
                out = handler(data)
                if out:
                    c.sendall(out)
    threading.Thread(target=loop, daemon=True).start()
    return srv


def _transparent(data):          # a slave behind a transparent converter
    if len(data) == 8 and data[1] == 3:
        body = bytes([data[0], 3, 2, 0x12, 0x34])
        return body + struct.pack("<H", _crc16(body))
    return b""


def _gateway(data):              # a Modbus TCP gateway
    if len(data) >= 12:
        tid, _p, _l, unit, fn = struct.unpack(">HHHBB", data[:8])
        return struct.pack(">HHHBBBH", tid, 0, 5, unit, 3, 2, 0x1234)
    return b""


@needs_tc
@pytest.mark.parametrize("kind,handler,speaks", [("rtu_transparent", _transparent, "rtu"),
                                                 ("modbus_gateway", _gateway, "modbus_tcp")])
def test_probe_tells_transparent_from_gateway_and_says_when_the_type_is_wrong(tmp_path, kind, handler, speaks):
    srv = _serve(handler)
    port = srv.getsockname()[1]
    try:
        cfg, client = make_app(tmp_path)
        # declared as the OTHER kind on purpose
        wrong = "modbus_gateway" if kind == "rtu_transparent" else "rtu_transparent"
        client.post("/api/bridges", json={"id": "box", "type": wrong, "host": "127.0.0.1",
                                          "ports": [{"port": port}]})
        r = client.post("/api/bridges/box/probe", json={"port": port, "unit_id": 3}).json()
        assert r["speaks"] == speaks, r
        assert ("GATEWAY" if speaks == "modbus_tcp" else "TRANSPARENT") in r["verdict"]
        client.put("/api/bridges/box", json={"type": kind, "host": "127.0.0.1", "ports": [{"port": port}]})
        r = client.post("/api/bridges/box/probe", json={"port": port, "unit_id": 3}).json()
        assert r["verdict"].startswith("It speaks the way this bridge is set up"), r
    finally:
        srv.close()


@needs_tc
def test_renumbering_a_bus_takes_its_devices_and_a_used_bus_cannot_vanish(tmp_path):
    import yaml
    cfg, client = make_app(tmp_path)
    client.post("/api/bridges", json={"id": "ws", "type": "rtu_transparent", "host": "192.0.2.60",
                                      "ports": [{"port": 4196}, {"port": 4197}]})
    for did, unit in (("da", 1), ("db", 2)):
        assert client.post("/api/devices", json={"id": did, "template": "fronius_smart_meter_65a",
            "enabled": False, "connection": {"bridge": "ws", "bridge_port": 4196, "unit_id": unit}}).status_code == 200
    # only the reference is saved
    saved = yaml.safe_load(cfg.config_path.read_text())["devices"]
    assert all(set(d["connection"]) <= {"bridge", "bridge_port", "unit_id", "timeout"} for d in saved)
    r = client.put("/api/bridges/ws", json={"type": "rtu_transparent", "host": "192.0.2.60",
                                            "ports": [{"port": 5000}, {"port": 4197}]})
    assert r.status_code == 200, r.text
    assert {d.id: d.connection.port for d in cfg.devices if d.id in ("da", "db")} == {"da": 5000, "db": 5000}
    r = client.put("/api/bridges/ws", json={"type": "rtu_transparent", "host": "192.0.2.60",
                                            "ports": [{"port": 4197}]})
    assert r.status_code == 409 and ":5000" in r.text


@needs_tc
def test_one_unit_twice_on_a_gateway_bus_is_refused_too(tmp_path):
    cfg, client = make_app(tmp_path)
    client.post("/api/bridges", json={"id": "gw", "type": "modbus_gateway", "host": "192.0.2.61",
                                      "ports": [{"port": 502}]})
    body = lambda did: {"id": did, "template": "fronius_smart_meter_65a", "enabled": False,  # noqa: E731
                        "connection": {"bridge": "gw", "bridge_port": 502, "unit_id": 2}}
    assert client.post("/api/devices", json=body("g1")).status_code == 200
    r = client.post("/api/devices", json=body("g2"))
    assert r.status_code == 422 and "unit 2" in r.text


# ── phase 2: scan a bus, its budget, moving devices, export / import ───────

def _rtu_bus(regs_by_unit):
    """A transparent converter: raw RTU over TCP, several slaves; FC3 only,
    an exception for an address a slave does not have."""
    def handler(data):
        if len(data) < 8:
            return b""
        unit, fn, addr, cnt = struct.unpack(">BBHH", data[:6])
        regs = regs_by_unit.get(unit)
        if regs is None:
            return b""                                    # no such slave: silence
        if fn != 3 or any((addr + i) not in regs for i in range(cnt)):
            body = bytes([unit, fn | 0x80, 2])
        else:
            vals = b"".join(struct.pack(">H", regs[addr + i]) for i in range(cnt))
            body = bytes([unit, 3, len(vals)]) + vals
        return body + struct.pack("<H", _crc16(body))
    return handler


def _serve_many(handler):
    """Like _serve, but answers every frame on a connection (a scan asks many)."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)

    def conn(c):
        with c:
            c.settimeout(5)
            while True:
                try:
                    data = c.recv(256)
                except OSError:
                    return
                if not data:
                    return
                out = handler(data)
                if out:
                    c.sendall(out)

    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=conn, args=(c,), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()
    return srv


@needs_tc
def test_scanning_a_bus_finds_its_slaves_and_recognises_a_fingerprinted_one(tmp_path):
    import time as _t
    srv = _serve_many(_rtu_bus({3: {0: 1}, 7: {0: 5, 11: 731}}))   # unit 7 = Fronius 65A (model 731)
    port = srv.getsockname()[1]
    try:
        cfg, client = make_app(tmp_path)
        client.post("/api/bridges", json={"id": "ws", "type": "rtu_transparent", "host": "127.0.0.1",
                                          "ports": [{"port": port}]})
        job = client.post(f"/api/bridges/ws/ports/{port}/scan", json={"from": 1, "to": 10, "timeout": 0.2}).json()["job"]
        for _ in range(100):
            st = client.get(f"/api/bus-scan/{job}").json()
            if st["state"] != "running":
                break
            _t.sleep(0.1)
        assert st["state"] == "done", st
        found = {f["unit_id"]: f for f in st["found"]}
        assert set(found) == {3, 7}
        assert found[7]["suggested"] == "fronius_smart_meter_65a"
        assert found[3]["suggested"] is None and found[3]["matches"] == []
    finally:
        srv.close()


@needs_tc
def test_a_device_moves_to_another_bus_keeping_its_identity_and_bridges_travel(tmp_path):
    import yaml
    cfg, client = make_app(tmp_path)
    client.post("/api/bridges", json={"id": "ws", "type": "rtu_transparent", "host": "192.0.2.60",
                                      "ports": [{"port": 4196}, {"port": 4197}]})
    body = {"id": "m1", "template": "fronius_smart_meter_65a", "enabled": False,
            "connection": {"bridge": "ws", "bridge_port": 4196, "unit_id": 1}}
    assert client.post("/api/devices", json=body).status_code == 200
    prefix = next(d for d in cfg.devices if d.id == "m1").mqtt_topic_prefix
    r = client.put("/api/devices/m1", json={**body, "connection": {"bridge": "ws", "bridge_port": 4197, "unit_id": 1}})
    assert r.status_code == 200, r.text
    d = next(d for d in cfg.devices if d.id == "m1")
    assert (d.connection.port, d.mqtt_topic_prefix) == (4197, prefix)     # moved; same topics
    # export, then import on another gateway
    text = client.get("/api/bridges/export").text
    assert "ws" in text
    (tmp_path / "gw2").mkdir()
    _cfg2, client2 = make_app(tmp_path / "gw2")
    prev = client2.post("/api/bridges/import", json={"yaml": text}).json()
    assert prev["bridges"][0]["status"] == "new" and _cfg2.get_raw_bridge("ws") is None
    done = client2.post("/api/bridges/import", json={"yaml": text, "apply": True}).json()
    assert done["bridges"][0]["status"] == "created" and _cfg2.get_raw_bridge("ws")["host"] == "192.0.2.60"
    # back home: replacing it with a file that drops a bus a device uses is refused
    one_bus = yaml.safe_load(text)
    one_bus["bridges"][0]["ports"] = [{"port": 4196}]
    r = client.post("/api/bridges/import", json={"yaml": yaml.safe_dump(one_bus), "replace": True}).json()
    assert r["bridges"][0]["status"] == "invalid" and "4197" in r["bridges"][0]["errors"][0]
