# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Edit bus line… (ADR-0031 §4, L3.6): ``PATCH /api/buses/{bus}/line``.

A bus has one line. A device on a shared serial bus inherits it and cannot
change it alone; this route changes it for every device on the bus at once,
in one config write and one audit record, and restarts them. A dry run lists
them and changes nothing. Through our serial bridge the line is the bridge
port's, set with the bridge's own API in the same operation; a converter or
another bridge is set on the box. Refused while a write to one of the
devices is in flight. Admin only.

Also: the matrix the connection step is tested on in Node
(``tests/fixtures/conn_step_protocols.json``) is the one the server serves.
"""
import http.server
import json
import pathlib
import threading
from urllib.parse import quote


from tests.test_devices_api import make_app, needs_tc

FIX = pathlib.Path(__file__).parent / "fixtures"
LINE = {"baudrate": 19200, "parity": "E", "bytesize": 8, "stopbits": 1}


def _rtu(did, port='/dev/ttyQA1', unit=1, **conn):
    return {"id": did, "name": did.upper(), "template": "eastron_sdm120", "enabled": False,
            "connection": {"protocol": "rtu", "serial_port": port, "baudrate": 9600, "parity": "N",
                           "stopbits": 1, "bytesize": 8, "unit_id": unit, **conn}}


def _add(client, body):
    r = client.post("/api/devices", json=body)
    assert r.status_code == 200, r.text


def _bus_id(client, did):
    for b in client.get("/api/buses").json()["buses"]:
        if any(d["id"] == did for d in b["devices"]):
            return b["id"]
    raise AssertionError(f"no bus holds {did}")


def _url(bus, dry=False):
    return f"/api/buses/{quote(bus, safe='')}/line" + ("?dry_run=true" if dry else "")


def _conn(client, did):
    return client.get(f"/api/devices/{did}").json()["device"]["connection"]


def _two(tmp_path):
    cfg, client = make_app(tmp_path)
    _add(client, _rtu("m1", unit=1))
    _add(client, _rtu("m2", unit=2))
    return cfg, client, _bus_id(client, "m1")


@needs_tc
def test_the_dry_run_lists_every_device_that_changes_and_restarts_and_stores_nothing(tmp_path):
    cfg, client, bus = _two(tmp_path)
    tags = {d: client.get(f"/api/devices/{d}").headers["etag"] for d in ("m1", "m2")}
    r = client.patch(_url(bus, dry=True), json=LINE)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "valid" and d["kind"] == "serial" and d["where"] == "/dev/ttyQA1"
    assert d["line"] == {"from": "9600 8N1", "to": "19200 8E1"}
    assert [x["id"] for x in d["devices"]] == ["m1", "m2"]
    assert all(x["from"] == "9600 8N1" and x["to"] == "19200 8E1" and x["restart"] for x in d["devices"])
    assert d["restart"] == ["m1", "m2"]
    assert _conn(client, "m1")["baudrate"] == 9600 and _conn(client, "m2")["parity"] == "N"
    assert {d: client.get(f"/api/devices/{d}").headers["etag"] for d in ("m1", "m2")} == tags


@needs_tc
def test_the_line_is_applied_to_every_device_on_the_bus_and_they_restart(tmp_path, monkeypatch):
    cfg, client, bus = _two(tmp_path)
    _add(client, _rtu("other", port='/dev/ttyQA2', unit=1))          # another bus: untouched
    restarted = []
    ctx = client.app.state.ctx
    real = ctx.restart_devices
    monkeypatch.setattr(ctx, "restart_devices", lambda ids: restarted.append(list(ids)) or real(ids))
    r = client.patch(_url(bus), json=LINE)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "updated" and sorted(d["restarted"]) == ["m1", "m2"]
    assert restarted == [["m1", "m2"]]
    for did in ("m1", "m2"):
        c = _conn(client, did)
        assert (c["baudrate"], c["parity"], c["bytesize"], c["stopbits"]) == (19200, "E", 8, 1), c
    assert _conn(client, "other")["baudrate"] == 9600
    # stored: a reload reads the new line
    from multibus.config import Config
    again = Config(str(cfg.config_path))
    assert {d.id: d.connection.baudrate for d in again.devices if d.id in ("m1", "m2", "other")} == \
        {"m1": 19200, "m2": 19200, "other": 9600}
    # the same line again: nothing to do
    r2 = client.patch(_url(_bus_id(client, "m1")), json=LINE)
    assert r2.status_code == 200 and r2.json()["status"] == "unchanged" and r2.json()["restarted"] == []


@needs_tc
def test_one_audit_record_for_the_whole_bus(tmp_path):
    cfg, client, bus = _two(tmp_path)
    log = cfg.config_path.parent / "audit.jsonl"
    before = log.read_text().splitlines() if log.exists() else []
    assert client.patch(_url(bus), json=LINE).status_code == 200
    new = [json.loads(x) for x in log.read_text().splitlines()[len(before):]]
    assert len(new) == 1, new
    assert new[0]["action"].startswith("PATCH /api/buses/") and new[0]["action"].endswith("/line")
    assert new[0]["status"] == "ok"


@needs_tc
def test_the_line_is_checked(tmp_path):
    _cfg, client, bus = _two(tmp_path)
    r = client.patch(_url(bus), json={**LINE, "parity": "X"})
    assert r.status_code == 422 and "parity" in r.json()["detail"]["fields"]
    r = client.patch(_url(bus), json={"baudrate": 19200})
    assert r.status_code == 422 and "parity" in r.json()["detail"]["fields"]
    r = client.patch(_url(bus), json={**LINE, "baudrate": "fast"})
    assert r.status_code == 422 and "baudrate" in r.json()["detail"]["fields"]
    assert _conn(client, "m1")["baudrate"] == 9600


@needs_tc
def test_an_unknown_bus_is_404_and_a_bus_without_a_serial_line_is_refused(tmp_path):
    _cfg, client = make_app(tmp_path)
    assert client.patch(_url("/dev/ttyNONE"), json=LINE).status_code == 404
    _add(client, {"id": "tcp1", "name": "T", "template": "eastron_sdm120", "enabled": False,
                  "connection": {"protocol": "tcp", "host": "192.0.2.7", "port": 502, "unit_id": 1}})
    r = client.patch(_url(_bus_id(client, "tcp1")), json=LINE)
    assert r.status_code == 422 and "no serial line" in r.text


@needs_tc
def test_a_converter_is_set_on_the_box(tmp_path):
    _cfg, client = make_app(tmp_path)
    _add(client, {"id": "conv", "name": "C", "template": "eastron_sdm120", "enabled": False,
                  "connection": {"protocol": "rtu-tcp", "host": "192.0.2.8", "port": 4001, "unit_id": 3}})
    r = client.patch(_url(_bus_id(client, "conv"), dry=True), json=LINE)
    assert r.status_code == 422 and "set on the box" in r.text
    # a transparent bridge record too: the gateway cannot set its line
    r = client.post("/api/bridges", json={"id": "ws", "type": "rtu_transparent", "host": "192.0.2.9",
                                          "ports": [{"port": 4196}]})
    assert r.status_code == 200, r.text
    _add(client, {"id": "onws", "name": "W", "template": "eastron_sdm120", "enabled": False,
                  "connection": {"bridge": "ws", "bridge_port": 4196, "unit_id": 4}})
    r = client.patch(_url(_bus_id(client, "onws")), json=LINE)
    assert r.status_code == 422 and "set on the box" in r.text


@needs_tc
def test_refused_while_a_write_to_one_of_its_devices_is_in_flight(tmp_path):
    _cfg, client, bus = _two(tmp_path)
    lk = client.app.state.ctx.entry_lock("m2")
    assert lk.acquire(blocking=False)
    try:
        r = client.patch(_url(bus), json=LINE)
        assert r.status_code == 409 and "m2" in r.text
        assert _conn(client, "m1")["baudrate"] == 9600            # nothing applied, not even m1
    finally:
        lk.release()
    # the locks it took are given back: the next try goes through
    assert client.patch(_url(bus), json=LINE).status_code == 200
    assert client.app.state.ctx.entry_lock("m1").acquire(blocking=False)
    client.app.state.ctx.entry_lock("m1").release()


@needs_tc
def test_a_unit_of_an_installation_is_refused_with_where_to_set_it(tmp_path):
    _cfg, client = make_app(tmp_path, extra_yaml="""
endpoints:
  - id: plant
    name: Plant
    template: seplos_bms_v3_rtu_tap
    enabled: false
    connection:
      protocol: rtu_tap
      serial_port: /dev/ttyQA3
      baudrate: 9600
    units: [1, 2]
""")
    unit = next(d["id"] for d in client.get("/api/devices").json()["devices"] if d.get("endpoint_id") == "plant")
    r = client.patch(_url(_bus_id(client, unit), dry=True), json=LINE)
    assert r.status_code == 422 and "plant" in r.text and "installation" in r.text


@needs_tc
def test_admin_only(tmp_path):
    from tests.test_operator_role import _build_clients
    c = _build_clients(tmp_path)
    r = c.admin.post("/api/devices", json=_rtu("m1"))
    assert r.status_code == 200, r.text
    bus = _bus_id(c.admin, "m1")
    assert c.op.patch(_url(bus, dry=True), json=LINE).status_code == 403
    assert c.viewer.patch(_url(bus), json=LINE).status_code == 403
    assert c.admin.patch(_url(bus, dry=True), json=LINE).status_code == 200


@needs_tc
def test_through_our_serial_bridge_the_port_is_set_with_the_bridge_api_and_its_devices_restart(tmp_path, monkeypatch):
    posted = []

    class Bridge(http.server.BaseHTTPRequestHandler):
        def _send(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            self._send({"version": "3.98.0", "adapters": [
                {"stable_id": "1a86:7523@1-1", "tcp_port": 7001, "available": True, "kind": "usb"}]})

        def do_POST(self):  # noqa: N802
            posted.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self._send({"stable_id": "1a86:7523@1-1", "serial_text": "19200 8E1"})

        def log_message(self, *a):
            pass

    hs = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
    threading.Thread(target=hs.serve_forever, daemon=True).start()
    try:
        cfg, client = make_app(tmp_path)
        r = client.post("/api/bridges", json={"id": "pi", "type": "mbg_serial_bridge", "host": "127.0.0.1",
                                              "control_port": hs.server_address[1], "ports": [{"port": 7001}]})
        assert r.status_code == 200, r.text
        _add(client, {"id": "b1", "name": "B1", "template": "eastron_sdm120", "enabled": False,
                      "connection": {"bridge": "pi", "bridge_port": 7001, "unit_id": 1}})
        _add(client, {"id": "b2", "name": "B2", "template": "eastron_sdm120", "enabled": False,
                      "connection": {"bridge": "pi", "bridge_port": 7001, "unit_id": 2}})
        bus = _bus_id(client, "b1")
        d = client.patch(_url(bus, dry=True), json=LINE)
        assert d.status_code == 200, d.text
        assert d.json()["kind"] == "bridge" and [x["id"] for x in d.json()["devices"]] == ["b1", "b2"]
        assert d.json()["bridge"]["id"] == "pi" and d.json()["bridge"]["port"] == 7001
        assert posted == []                                           # a dry run asks the bridge nothing
        restarted = []
        ctx = client.app.state.ctx
        real = ctx.restart_devices
        monkeypatch.setattr(ctx, "restart_devices", lambda ids: restarted.append(list(ids)) or real(ids))
        r = client.patch(_url(bus), json=LINE)
        assert r.status_code == 200, r.text
        assert posted == [("/adapters/1a86%3A7523%401-1/serial",
                           {"baud": 19200, "parity": "E", "databits": 8, "stopbits": 1})]
        assert r.json()["status"] == "updated" and sorted(r.json()["restarted"]) == ["b1", "b2"]
        assert restarted == [["b1", "b2"]]
        # the devices hold no line of their own: the bridge has it
        assert "baudrate" not in cfg.get_raw_device("b1")["connection"]
    finally:
        hs.shutdown()


@needs_tc
def test_the_bridge_unreachable_restarts_nothing(tmp_path, monkeypatch):
    _cfg, client = make_app(tmp_path)
    r = client.post("/api/bridges", json={"id": "pi", "type": "mbg_serial_bridge", "host": "127.0.0.1",
                                          "control_port": 9, "ports": [{"port": 7001, "adapter": "x"}]})
    assert r.status_code == 200, r.text
    _add(client, {"id": "b1", "name": "B1", "template": "eastron_sdm120", "enabled": False,
                  "connection": {"bridge": "pi", "bridge_port": 7001, "unit_id": 1}})
    restarted = []
    monkeypatch.setattr(client.app.state.ctx, "restart_devices", lambda ids: restarted.append(ids))
    r = client.patch(_url(_bus_id(client, "b1")), json=LINE)
    assert r.status_code == 502 and "bridge" in r.text and restarted == []


@needs_tc
def test_the_connection_step_is_tested_on_the_matrix_the_server_serves(tmp_path):
    _cfg, client = make_app(tmp_path)
    p = client.get("/api/protocols").json()
    fx = json.loads((FIX / "conn_step_protocols.json").read_text())
    assert fx["matrix"]["device"] == p["matrix"]["device"]
    assert fx["invalid"] == p["invalid"]
    assert fx["labels"] == p["labels"]
