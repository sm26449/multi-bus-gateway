# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Edit bus line…, the review of L3.6: the locks of an installation's units
taken once (M1), a snapshot (M2), the plan under the locks names only locked
devices (m1), the bridge asked before the locks (m2), a bridge port already at
that line unchanged (m3), the current line for the dialog (m4), a failed
restart named with 200 (m7), a number too large refused (NIT)."""
import http.server
import json
import threading

from tests.test_bus_line import LINE, _add, _bus_id, _conn, _rtu, _two, _url
from tests.test_devices_api import make_app, needs_tc


class _FakeBridge:
    """Our serial bridge on a loopback port: /adapters says the port's line
    (serial_params), POST /adapters/<key>/serial sets it."""

    def __init__(self, line=None):
        self.posted, self.gets = [], []
        self.line = dict(line or {"baud": 9600, "parity": "N", "databits": 8, "stopbits": 1})
        me = self

        class H(http.server.BaseHTTPRequestHandler):
            def _send(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                me.gets.append(self.path)
                self._send({"adapters": [{"stable_id": "1a86:7523@1-1", "tcp_port": 7001, "available": True,
                                          "serial_params": dict(me.line)}]})

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                me.posted.append((self.path, body))
                me.line.update(body)
                self._send({"stable_id": "1a86:7523@1-1"})

            def log_message(self, *a):
                pass

        self.hs = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.hs.serve_forever, daemon=True).start()
        self.port = self.hs.server_address[1]

    def close(self):
        self.hs.shutdown()


def _bridge_app(tmp_path, fb, extra_yaml=""):
    cfg, client = make_app(tmp_path, extra_yaml=extra_yaml)
    r = client.post("/api/bridges", json={"id": "pi", "type": "mbg_serial_bridge", "host": "127.0.0.1",
                                          "control_port": fb.port, "ports": [{"port": 7001}]})
    assert r.status_code == 200, r.text
    return cfg, client


def _on_bridge(client, did, unit):
    _add(client, {"id": did, "name": did.upper(), "template": "eastron_sdm120", "enabled": False,
                  "connection": {"bridge": "pi", "bridge_port": 7001, "unit_id": unit}})


@needs_tc
def test_two_units_of_one_installation_on_a_bridge_port_do_not_refuse_themselves(tmp_path):
    """M1: two units share their installation's entry lock — taken once."""
    fb = _FakeBridge()
    try:
        _cfg, client = _bridge_app(tmp_path, fb, extra_yaml="""
endpoints:
  - id: plant
    name: Plant
    template: eastron_sdm120
    enabled: false
    connection: {protocol: rtu-tcp, host: 127.0.0.1, port: 7001, timeout: 1}
    units: [1, 2]
""")
        units = [d["id"] for d in client.get("/api/devices").json()["devices"] if d.get("endpoint_id") == "plant"]
        assert len(units) == 2
        r = client.patch(_url(_bus_id(client, units[0])), json=LINE)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "updated" and sorted(r.json()["restart"]) == sorted(units)
    finally:
        fb.close()


@needs_tc
def test_a_bus_line_change_takes_a_snapshot_and_its_dry_run_none(tmp_path, monkeypatch):
    """M2: a config change through /api/buses is snapshotted like a device's."""
    _cfg, client, bus = _two(tmp_path)
    got = []
    monkeypatch.setattr(client.app.state.snapshot_store, "schedule", lambda *a, **k: got.append(a))
    assert client.patch(_url(bus, dry=True), json=LINE).status_code == 200
    assert got == []
    assert client.patch(_url(bus), json=LINE).status_code == 200
    assert len(got) == 1 and "/api/buses/" in got[0][0]


@needs_tc
def test_a_device_joining_the_bus_between_the_check_and_the_locks_refuses(tmp_path, monkeypatch):
    """m1: the plan made under the locks names only devices whose lock is held."""
    from multibus import bus_line
    _cfg, client, bus = _two(tmp_path)
    _add(client, _rtu("m3", unit=3))
    real, calls = bus_line.plan, []

    def plan(*a, **k):
        p = real(*a, **k)
        calls.append(1)
        if len(calls) == 1:                       # the first look did not see m3
            p = dict(p, devices=[d for d in p['devices'] if d['id'] != 'm3'],
                     raws=[r for r in p['raws'] if r.get('id') != 'm3'])
        return p
    monkeypatch.setattr(bus_line, "plan", plan)
    r = client.patch(_url(bus), json=LINE)
    assert r.status_code == 409 and "m3" in r.text, r.text
    assert _conn(client, "m1")["baudrate"] == 9600


@needs_tc
def test_the_bridge_adapter_is_asked_before_the_locks_and_only_the_post_under_them(tmp_path, monkeypatch):
    """m2: GET /adapters runs with no device lock held."""
    from multibus import bridges as bridge_api
    fb = _FakeBridge()
    try:
        _cfg, client = _bridge_app(tmp_path, fb)
        _on_bridge(client, "b1", 1)
        held = []
        real_get = bridge_api.api_get

        def get(b, path, timeout=4):
            lk = client.app.state.ctx.entry_lock("b1")
            free = lk.acquire(blocking=False)
            if free:
                lk.release()
            held.append(not free)
            return real_get(b, path, timeout)
        monkeypatch.setattr(bridge_api, "api_get", get)
        r = client.patch(_url(_bus_id(client, "b1")), json=LINE)
        assert r.status_code == 200, r.text
        assert held and not any(held)
        assert len(fb.posted) == 1
    finally:
        fb.close()


@needs_tc
def test_the_bridge_port_already_at_that_line_is_unchanged_and_restarts_nothing(tmp_path, monkeypatch):
    """m3: the port's current line (from /adapters) equal → unchanged; m4:
    the current line said for the dialog."""
    fb = _FakeBridge({"baud": 19200, "parity": "E", "databits": 8, "stopbits": 1})
    try:
        _cfg, client = _bridge_app(tmp_path, fb)
        _on_bridge(client, "b1", 1)
        restarted = []
        monkeypatch.setattr(client.app.state.ctx, "restart_devices", lambda ids: restarted.append(ids) or ids)
        bus = _bus_id(client, "b1")
        d = client.patch(_url(bus, dry=True), json=LINE).json()
        assert d["status"] == "unchanged" and d["line"]["from"] == "19200 8E1" and d["restart"] == [], d
        r = client.patch(_url(bus), json=LINE)
        assert r.status_code == 200 and r.json()["status"] == "unchanged" and r.json()["restarted"] == []
        assert fb.posted == [] and restarted == []
        cur = client.get(_url(bus)).json()
        assert cur["line"] == {"baudrate": 19200, "parity": "E", "bytesize": 8, "stopbits": 1} and cur["source"] == "bridge"
    finally:
        fb.close()


@needs_tc
def test_the_current_line_of_a_bus(tmp_path):
    """m4: the line the dialog starts from — the devices' when they agree,
    else unknown (never a guessed 9600 8N1)."""
    _cfg, client, bus = _two(tmp_path)
    cur = client.get(_url(bus)).json()
    assert cur["line"] == {"baudrate": 9600, "parity": "N", "bytesize": 8, "stopbits": 1} and cur["source"] == "devices"
    # a stored config whose devices disagree (a save refuses it now)
    (tmp_path / "x").mkdir()
    _c2, c2 = make_app(tmp_path / "x", extra_yaml="""
devices:
  - {id: d1, template: eastron_sdm120, enabled: false, connection: {protocol: rtu, serial_port: /dev/ttyQA8, baudrate: 9600, unit_id: 1}}
  - {id: d2, template: eastron_sdm120, enabled: false, connection: {protocol: rtu, serial_port: /dev/ttyQA8, baudrate: 19200, unit_id: 2}}
""")
    cur = c2.get(_url(_bus_id(c2, "d1"))).json()
    assert cur["line"] is None and cur["source"] == "unknown"
    r = client.post("/api/bridges", json={"id": "pi", "type": "mbg_serial_bridge", "host": "127.0.0.1",
                                          "control_port": 9, "ports": [{"port": 7001, "adapter": "x"}]})
    assert r.status_code == 200
    _on_bridge(client, "b1", 1)
    cur = client.get(_url(_bus_id(client, "b1"))).json()
    assert cur["line"] is None and cur["source"] == "unknown"


@needs_tc
def test_a_restart_that_fails_after_the_write_says_so_with_200(tmp_path, monkeypatch):
    """m7: the line is stored; a failed restart is named, never a 500."""
    _cfg, client, bus = _two(tmp_path)

    def boom(ids):
        raise RuntimeError("no serial port")
    monkeypatch.setattr(client.app.state.ctx, "restart_devices", boom)
    r = client.patch(_url(bus), json=LINE)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "updated" and r.json()["restart_failed"] == ["m1", "m2"]
    assert _conn(client, "m1")["baudrate"] == 19200
    monkeypatch.setattr(client.app.state.ctx, "restart_devices", lambda ids: ["m1"])
    r = client.patch(_url(bus), json={**LINE, "baudrate": 38400})
    assert r.status_code == 200 and r.json()["restart_failed"] == ["m2"]


@needs_tc
def test_a_number_too_large_is_refused_not_a_500(tmp_path):
    _cfg, client, bus = _two(tmp_path)
    r = client.patch(_url(bus), json={**LINE, "baudrate": "1e999"})
    assert r.status_code == 422 and "baudrate" in r.json()["detail"]["fields"]


@needs_tc
def test_a_bus_says_which_of_its_devices_are_an_installations_units(tmp_path):
    """m5: the form hides Edit bus line… on a bus an installation's unit is on."""
    _cfg, client = make_app(tmp_path, extra_yaml="""
endpoints:
  - id: plant
    name: Plant
    template: seplos_bms_v3_rtu_tap
    enabled: false
    connection: {protocol: rtu_tap, serial_port: /dev/ttyQA3, baudrate: 9600}
    units: [1]
""")
    unit = next(d["id"] for d in client.get("/api/devices").json()["devices"] if d.get("endpoint_id") == "plant")
    bus = next(b for b in client.get("/api/buses").json()["buses"] if any(d["id"] == unit for d in b["devices"]))
    row = next(d for d in bus["devices"] if d["id"] == unit)
    assert row["installation"] == "plant" and row["component_of"] is None
