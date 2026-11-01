# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
# pytest fixtures (slave, fake_serial, seplos_port) imported from a sibling test
# module are named as test parameters: ruff takes that for a redefinition
# ruff: noqa: F811
"""L3.7 — the generic bus scan and Listen for addresses (ADR-0031 §2
Address, "Many of the same"; design §3 2f; FINDINGS L3-F1).

What must hold, each proved here:

- a scan only reads: function codes ⊆ {3, 4, 43}, nothing else is built;
  FC43 only when asked;
- one socket: on a bus a device of this gateway polls, the scan rides that
  device's transport (TCP and a local serial line alike); the device keeps
  reading; the line is the device's;
- ADR-0025: refused (409) on a bus any device listens on — enabled or not,
  through an alias of the line, a bridge port, the bus id; nothing is built;
- identify: FC43, the SunSpec marker and the templates' fingerprints, the
  suggested model per unit; "already: <device>" per unit;
- the old RTU unit sweep never opens a serial client of its own (L3-F1): it
  runs as a scan job on the shared transport, the answer's shape kept;
- Listen for addresses joins the tap or the Seplos reader (or opens a
  receive-only one), never transmits, and lists the units heard with the
  master's pattern; refused on a bus a device of ours polls;
- one job per bus (a preview and a scan included), operator yes viewer no,
  an audit line."""
import json
import os
import time
from types import SimpleNamespace

import pytest

from tests.test_devices_api import needs_tc
from tests.test_preview import (CAPTURE, FakeSerial, _app, _float_words, _rtu_exchange, fake_serial,  # noqa: F401
                                sdm_words, seplos_port, slave)


def _wait(client, job, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        r = client.get(f"/api/bus-scan/{job}")
        assert r.status_code == 200, r.text
        d = r.json()
        if d["state"] not in ("queued", "running", "cancelling"):
            return d
        time.sleep(0.05)
    raise AssertionError(f"scan {job} still running")


def _scan(client, body, code=202):
    r = client.post("/api/bus-scan", json=body)
    assert r.status_code == code, r.text
    return r.json()


def _tcp(port, **kw):
    return {"protocol": "tcp", "host": "127.0.0.1", "port": port, **kw}


def fake_line(monkeypatch, port, path):
    """``_build_client`` for the serial line ``path`` only: a socket to the
    fake slaves on ``port``, each build recorded. Any other client built
    meanwhile — a connection an earlier test left behind, reconnecting — is
    built as it really is and never counted here (test isolation)."""
    from pymodbus.client import ModbusTcpClient

    from multibus import modbus_client
    real = modbus_client._build_client
    built = []

    def build(config):
        if str(getattr(config, 'serial_port', '') or '') != str(path):
            return real(config)
        built.append(config.serial_port)
        return ModbusTcpClient(host="127.0.0.1", port=port, timeout=config.timeout, retries=0)
    monkeypatch.setattr(modbus_client, "_build_client", build)
    return built


def deye_words():
    w = {("holding", a): 0 for a in range(0, 61)}
    w[("holding", 0)] = 3
    w[("holding", 59)] = 1
    return w


# ── a scan reads, and recognises ────────────────────────────────────────────

@needs_tc
def test_a_scan_reads_only_finds_the_units_and_suggests_their_model(tmp_path, slave):
    s = slave(units={1: sdm_words(), 3: deye_words()})
    _cfg, client = _app(tmp_path)
    job = _scan(client, {"connection": _tcp(s.port), "from": 1, "to": 4, "timeout": 0.3, "confirm_open": True})
    assert job["mode"] == "poll" and job["transport"] == "own" and job["total"] == 4
    ans = _wait(client, job["job"])
    assert ans["state"] == "done", ans
    found = {f["unit_id"]: f for f in ans["found"]}
    assert set(found) == {1, 3}
    assert found[3]["suggested"] == "deye_hybrid_1p" and found[3]["suggested_name"]
    assert found[1]["suggested"] is None
    assert ans["done"] == 4
    assert set(s.fcs()) <= {3, 4, 43}                 # reads, and the identification
    assert 43 in s.fcs(1)                             # FC43 asked by default…
    assert {x[1] for x in s.log} <= {1, 2, 3, 4}
    assert s.connections == 1                         # one socket for the whole scan
    assert ans["budget"]["requests"] >= 4


@needs_tc
def test_fc43_only_when_asked_and_the_range_is_bounded(tmp_path, slave):
    s = slave(units={2: sdm_words()})
    _cfg, client = _app(tmp_path)
    ans = _wait(client, _scan(client, {"connection": _tcp(s.port), "from": 2, "to": 2, "timeout": 0.3,
                                       "fc43": False, "confirm_open": True})["job"])
    assert [f["unit_id"] for f in ans["found"]] == [2]
    assert set(s.fcs()) <= {3, 4}
    for bad in ({"from": 0}, {"to": 248}, {"from": 5, "to": 4}, {"timeout": "x"}, {"timeout": 9}):
        r = client.post("/api/bus-scan", json={"connection": _tcp(s.port), "confirm_open": True, **bad})
        assert r.status_code == 422, (bad, r.text)
    # the default range is 1–32
    job = _scan(client, {"connection": _tcp(s.port), "confirm_open": True, "timeout": 0.2})
    assert job["total"] == 32
    client.delete(f"/api/bus-scan/{job['job']}")
    _wait(client, job["job"])


SHARED = """
devices:
  - id: m1
    name: Meter one
    template: eastron_sdm120
    enabled: true
    connection: {{protocol: tcp, host: 127.0.0.1, port: {port}, unit_id: 1, timeout: 1,
                  endpoint_min_gap_s: 0.05}}
"""


@needs_tc
def test_a_scan_rides_the_running_devices_transport_one_socket(tmp_path, slave):
    from multibus.modbus_client import ModbusConnection
    s = slave(units={1: sdm_words(), 2: sdm_words(231.0, 1.0)}, single=True)
    cfg, client = _app(tmp_path, SHARED.format(port=s.port))
    dev = cfg.get_device("m1")
    running = ModbusConnection(dev.connection)
    client.app.state.registry.replace("m1", dev, client=SimpleNamespace(
        parts=[(dev.sources[0], SimpleNamespace(connection=running))]))
    try:
        assert running.read_registers(0, 2, "input")
        job = _scan(client, {"connection": _tcp(s.port), "from": 1, "to": 3, "timeout": 0.3})
        assert job["transport"] == "shared" and job["shared_with"] == "m1"
        ans = _wait(client, job["job"])
        assert ans["state"] == "done", ans
        found = {f["unit_id"]: f for f in ans["found"]}
        assert set(found) == {1, 2}
        assert found[1]["device"] == "m1" and found[2]["device"] == ""     # already: m1
        assert s.connections == 1                   # never a second socket
        assert running.read_registers(0, 2, "input")
        assert s.connections == 1
        assert running.client.comm_params.timeout_connect == 1              # its timeout put back
    finally:
        running.disconnect()


SERIAL = """
devices:
  - id: rs1
    name: On the line
    template: eastron_sdm120
    enabled: true
    connection: {{protocol: rtu, serial_port: {tty}, baudrate: 9600, unit_id: 1, timeout: 1}}
"""


@needs_tc
def test_a_scan_of_a_local_serial_line_goes_through_its_shared_transport(tmp_path, slave, monkeypatch):
    """The line is held by a device: the scan is a guest on that very client
    (one client ever built), whether named by its path, an alias or the bus id."""
    from multibus import modbus_client
    s = slave(units={1: sdm_words(), 4: sdm_words(232.0, 2.0)})
    tty = tmp_path / "ttyHAT1"
    built = fake_line(monkeypatch, s.port, tty)     # the "serial line": a socket to the fake slaves
    tty.write_text("")
    alias = tmp_path / "usb-ch340-if00"
    os.symlink(tty, alias)
    cfg, client = _app(tmp_path, SERIAL.format(tty=tty))
    dev = cfg.get_device("rs1")
    running = modbus_client.ModbusConnection(dev.connection)
    client.app.state.registry.replace("rs1", dev, client=SimpleNamespace(
        parts=[(dev.sources[0], SimpleNamespace(connection=running))]))
    try:
        assert running.read_registers(0, 2, "input")
        bus = next(b for b in client.get("/api/buses").json()["buses"] if b["kind"] == "serial")
        for body in ({"bus": bus["id"]},
                     {"connection": {"protocol": "rtu", "serial_port": str(alias)}}):
            job = _scan(client, {**body, "from": 1, "to": 5, "timeout": 0.3})
            assert job["transport"] == "shared" and job["shared_with"] == "rs1", job
            ans = _wait(client, job["job"])
            assert ans["state"] == "done", ans
            assert sorted(f["unit_id"] for f in ans["found"]) == [1, 4]
        assert built == [str(tty)]                   # the line was opened once, by its device
        assert set(s.fcs()) <= {3, 4, 43}
        # a line the request names unlike its device's: refused, said
        r = client.post("/api/bus-scan", json={"connection": {"protocol": "rtu", "serial_port": str(tty),
                                                              "baudrate": 19200}})
        assert r.status_code == 422 and "baudrate" in r.text
    finally:
        running.disconnect()


# ── ADR-0025: never on a listened bus ───────────────────────────────────────

TAPPED = """
bridges:
  - {{id: br, name: Panel, type: rtu_transparent, host: 192.0.2.77, ports: [{{port: 4196}}]}}
devices:
  - id: tap
    name: Listens
    template: eastron_sdm120
    enabled: true
    connection: {{protocol: rtu_tap, serial_port: {tty}, baudrate: 9600, unit_id: 2}}
  - id: tap-off
    name: Listens, switched off
    template: eastron_sdm120
    enabled: false
    connection: {{protocol: rtu_tap, bridge: br, bridge_port: 4196, unit_id: 3}}
"""


@needs_tc
def test_a_scan_is_refused_on_a_listened_bus_disabled_and_aliases_included(tmp_path, monkeypatch):
    from multibus import discovery, modbus_client
    tty = tmp_path / "ttySC0"
    tty.write_text("")
    alias = tmp_path / "usb-FTDI-if00-port0"
    os.symlink(tty, alias)
    _cfg, client = _app(tmp_path, TAPPED.format(tty=tty))
    monkeypatch.setattr(modbus_client, "_build_client",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("a client was built")))
    assert not hasattr(discovery, "sweep_units_rtu")      # gone: nothing can open its own serial client
    buses = {b["id"]: b for b in client.get("/api/buses").json()["buses"]}
    tapped = [b for b in buses.values() if b["kind"] in ("serial", "bridge")]
    asks = [({"connection": {"protocol": "rtu", "serial_port": str(alias)}}, "tap"),
            ({"connection": {"protocol": "rtu", "serial_port": str(tty)}}, "tap"),
            ({"connection": {"protocol": "rtu-tcp", "host": "192.0.2.77", "port": 4196}}, "tap-off"),
            ({"connection": {"protocol": "rtu-tcp", "host": "192.0.2.77".upper(), "port": 4196}}, "tap-off"),
            ({"connection": {"bridge": "br", "bridge_port": 4196}}, "tap-off")]
    asks += [({"bus": b["id"]}, None) for b in tapped]
    for body, who in asks:
        r = client.post("/api/bus-scan", json={**body, "confirm_open": True})
        assert r.status_code == 409, (body, r.text)
        d = r.json()["detail"]
        assert "Nothing was sent" in d["errors"][0]
        if who:
            assert who in d["listeners"], (body, d)
    r = client.post("/api/bridges/br/ports/4196/scan", json={"confirm_open": True})
    assert r.status_code == 409 and "tap-off" in r.text
    # the old RTU sweep: the same refusal (L3-F1 — never its own client)
    r = client.post("/api/discover/modbus/units", json={"protocol": "rtu", "serial_port": str(alias),
                                                         "unit_start": 1, "unit_end": 2})
    assert r.status_code == 409 and "tap" in r.json()["detail"]["listeners"]


@needs_tc
def test_the_old_rtu_unit_sweep_runs_on_the_shared_transport_l3_f1(tmp_path, slave, monkeypatch):
    from multibus import discovery
    s = slave(units={2: sdm_words()})
    built = fake_line(monkeypatch, s.port, "/dev/fake-line")
    assert not hasattr(discovery, "sweep_units_rtu") and not hasattr(discovery, "sweep_units_tcp")
    _cfg, client = _app(tmp_path)
    r = client.post("/api/discover/modbus/units", json={"protocol": "rtu", "serial_port": "/dev/fake-line",
                                                         "unit_start": 1, "unit_end": 3, "timeout": 0.3})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["protocol"] == "rtu" and d["units"] == [2] and d["job"]
    assert built == ["/dev/fake-line"] and set(s.fcs()) <= {3, 4, 43}
    # TCP keeps its shape too
    r = client.post("/api/discover/modbus/units", json={"protocol": "tcp", "host": "127.0.0.1", "port": s.port,
                                                         "unit_start": 2, "unit_end": 2, "timeout": 0.3,
                                                         "confirm_open": True})
    assert r.status_code == 200 and r.json()["units"] == [2], r.text


@needs_tc
def test_a_seplos_poll_bus_is_not_scanned_and_can_is_not_a_bus_to_scan(tmp_path):
    _cfg, client = _app(tmp_path)
    r = client.post("/api/bus-scan", json={"connection": {"protocol": "seplos_v2", "serial_port": "/dev/x",
                                                          "mode": "poll"}})
    assert r.status_code == 422 and "Listen" in r.text
    r = client.post("/api/bus-scan", json={"connection": {"protocol": "can", "channel": "can0"}})
    assert r.status_code == 422
    assert client.post("/api/bus-scan", json={"bus": "nope"}).status_code == 404
    assert client.post("/api/bus-scan", json={}).status_code == 422


# ── Listen for addresses: never transmits ───────────────────────────────────

@needs_tc
def test_listen_for_addresses_hears_the_units_from_a_tap_replay(tmp_path, fake_serial):
    from multibus import modbus_client, rtu_tap
    _cfg, client = _app(tmp_path)
    job = _scan(client, {"connection": {"protocol": "rtu", "serial_port": "/dev/fake-tap", "baudrate": 9600},
                         "mode": "listen", "seconds": 1})
    assert job["mode"] == "listen" and job["transport"] == "tap"
    ans = _wait(client, job["job"])
    assert ans["state"] == "done", ans
    found = {f["unit_id"]: f for f in ans["found"]}
    assert set(found) == {1, 9}
    assert found[1]["answered"] >= 1 and "FC4 @0 x2" in found[1]["pattern"]
    assert found[9]["asked"] >= 1 and "FC3 @100 x1" in found[9]["pattern"]
    assert ans["budget"]["requests"] == 0 and ans["heard"]["frames"] > 0
    assert fake_serial.opened.count("/dev/fake-tap") == 1
    r = rtu_tap._READERS[next(iter(rtu_tap._READERS))]
    assert r._thread is None and not r._observers           # the reader it opened is closed again
    assert modbus_client._TRANSPORTS == {} or all(k for k in modbus_client._TRANSPORTS)


@needs_tc
def test_listen_joins_a_running_tap_and_is_the_only_discovery_on_a_listened_bus(tmp_path, fake_serial):
    from multibus import rtu_tap
    tty = "/dev/fake-tap"
    _cfg, client = _app(tmp_path, f"""
devices:
  - id: tapper
    template: eastron_sdm120
    enabled: true
    connection: {{protocol: rtu_tap, serial_port: {tty}, baudrate: 9600, unit_id: 9}}
""")
    conn = SimpleNamespace(serial_port=tty, baudrate=9600, parity="N", stopbits=1, bytesize=8,
                           host="", port=0, unit_id=9, adapter="")
    running = rtu_tap.RtuTapClient(conn, [], device_id="tapper")
    running.publish_callback = lambda g, d: None
    assert running.connect()
    try:
        bus = next(b for b in client.get("/api/buses").json()["buses"] if b["kind"] == "serial")
        assert client.post("/api/bus-scan", json={"bus": bus["id"]}).status_code == 409   # Scan: refused
        ans = _wait(client, _scan(client, {"bus": bus["id"], "mode": "listen", "seconds": 1})["job"])
        assert ans["state"] == "done", ans
        found = {f["unit_id"]: f for f in ans["found"]}
        assert set(found) == {1, 9} and found[9]["device"] == "tapper"
        assert fake_serial.opened.count(tty) == 1            # one reader, the tty opened once
        assert running._reader._thread is not None           # still listening
    finally:
        running.disconnect()


@needs_tc
def test_listen_for_seplos_pack_addresses_from_the_capture(tmp_path, seplos_port):
    from tests.test_seplos_v2 import FakePack
    stream = b"".join(line.split()[1].encode() + b"\r" for line in CAPTURE.read_text().splitlines())
    port = FakePack(stream=stream)
    seplos_port["port"] = port
    _cfg, client = _app(tmp_path)
    job = _scan(client, {"connection": {"protocol": "seplos_v2", "serial_port": "/dev/fake-seplos",
                                        "baudrate": 19200}, "mode": "listen", "seconds": 2})
    ans = _wait(client, job["job"])
    assert ans["state"] == "done", ans
    found = {f["unit_id"]: f for f in ans["found"]}
    assert set(found) == {1, 2}
    assert found[1]["answered"] >= 1 and any("0x5A" in p for p in found[1]["pattern"])
    assert port.written == []                         # nothing transmitted


@needs_tc
def test_listen_is_refused_on_a_bus_a_device_of_ours_polls(tmp_path, slave):
    s = slave(units={1: sdm_words()})
    _cfg, client = _app(tmp_path, SHARED.format(port=s.port))
    r = client.post("/api/bus-scan", json={"connection": {"protocol": "rtu-tcp", "host": "127.0.0.1",
                                                          "port": s.port}, "mode": "listen"})
    assert r.status_code == 409 and "m1" in r.text and "Scan" in r.text
    assert s.connections == 0


# ── one job per bus, roles, audit ───────────────────────────────────────────

@needs_tc
def test_one_job_per_bus_a_preview_included_and_delete_cancels(tmp_path, slave, monkeypatch):
    s = slave(units={1: sdm_words()})
    _cfg, client = _app(tmp_path)
    from multibus import preview
    real = preview._in_turn

    def slow(*a, **k):
        time.sleep(0.2)
        return real(*a, **k)
    monkeypatch.setattr(preview, "_in_turn", slow)
    job = _scan(client, {"connection": _tcp(s.port), "from": 1, "to": 30, "timeout": 0.2, "confirm_open": True})
    r = client.post("/api/bus-scan", json={"connection": _tcp(s.port), "confirm_open": True})
    assert r.status_code == 409 and "already running" in r.text
    r = client.post("/api/previews", json={"connection": {**_tcp(s.port), "unit_id": 1},
                                           "template": "eastron_sdm120", "confirm_open": True})
    assert r.status_code == 409 and "scan" in r.text
    assert client.delete(f"/api/bus-scan/{job['job']}").json()["cancelled"] is True
    ans = _wait(client, job["job"])
    assert ans["state"] == "cancelled" and ans["done"] < 30
    assert client.get("/api/previews/" + job["job"]).status_code == 404     # a scan is not a preview
    assert client.get("/api/bus-scan/nope").status_code == 404


@needs_tc
def test_the_operator_may_scan_the_viewer_may_not_and_each_scan_is_audited(tmp_path, slave):
    from tests.test_operator_role import _build_clients
    s = slave(units={1: sdm_words()})
    c = _build_clients(tmp_path)
    body = {"connection": _tcp(s.port), "from": 1, "to": 1, "timeout": 0.3, "confirm_open": True}
    assert c.viewer.post("/api/bus-scan", json=body).status_code == 403
    r = c.op.post("/api/bus-scan", json=body)
    assert r.status_code == 202, r.text
    assert c.viewer.get(f"/api/bus-scan/{r.json()['job']}").status_code == 200
    assert c.viewer.delete(f"/api/bus-scan/{r.json()['job']}").status_code == 403
    _wait(c.op, r.json()["job"])
    lines = c.op.app.state.ctx.audit_log.recent(50)
    done = [e for e in lines if e.get("action") == "bus-scan"]
    assert done and done[-1]["status"] == "done"
    d = json.loads(done[-1]["detail"])
    assert d["mode"] == "poll" and d["confirm_open"] is True and d["found"] == [1] and d["requests"] >= 1


@pytest.mark.parametrize("fc", [5, 6, 15, 16, 8])
def test_the_scan_guard_refuses_every_write(fc):
    from pymodbus.pdu import ModbusPDU

    from multibus.preview import Budget, GuardedModbusClient, GuardRefusal
    g = GuardedModbusClient(SimpleNamespace(execute=lambda *a: (_ for _ in ()).throw(AssertionError("sent"))),
                            Budget(10), allow_fc43=True)
    req = ModbusPDU()
    req.function_code = fc
    with pytest.raises(GuardRefusal):
        g.execute(False, req)
