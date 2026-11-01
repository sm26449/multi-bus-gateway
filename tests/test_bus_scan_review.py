# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""L3.7 safety review (2026-11-01): the bus scan hands the line back after
every request (M1), never shortens the holder's wait, never asks an address
a device has, and throws a late answer away before the line goes back (M2);
LAN probes never open a second connection to a held or foreign bridge port
(m1); the bus_unwell stop names the device (m2); one scan at a time (m3); the
unit sweep keeps its protocol and caps its wait (m4); a tapped bridge port is
refused through the bridge route (m5)."""
import socket
import struct
import threading
import time
from types import SimpleNamespace

import pytest

from tests.test_bus_scan import SERIAL, _scan, _tcp, _wait, deye_words
from tests.test_devices_api import needs_tc
from tests.test_preview import _app, _crc, _Diag, sdm_words, slave  # noqa: F401


def _serial_holder(tmp_path, slave, monkeypatch, units, timeout=1):
    """A device holding a 'serial line' (a socket to the fake slaves through
    the line's shared transport), its running connection registered."""
    from multibus import modbus_client
    from tests.test_bus_scan import fake_line
    s = slave(units=units)
    tty = tmp_path / "ttyHAT1"
    tty.write_text("")
    fake_line(monkeypatch, s.port, tty)
    cfg, client = _app(tmp_path, SERIAL.format(tty=tty).replace("timeout: 1}", f"timeout: {timeout}}}"))
    dev = cfg.get_device("rs1")
    running = modbus_client.ModbusConnection(dev.connection)
    client.app.state.registry.replace("rs1", dev, client=SimpleNamespace(
        parts=[(dev.sources[0], SimpleNamespace(connection=running))]))
    assert running.read_registers(0, 2, "input")
    return s, client, running, tty


def _serial_scan(tty, **kw):
    return {"connection": {"protocol": "rtu", "serial_port": str(tty)}, **kw}


# ── M1: the line back to the devices after every request ─────────────────────

@needs_tc
def test_the_poller_gets_the_line_between_every_two_scan_requests_identify_included(tmp_path, slave, monkeypatch):
    """On a shared serial transport (no arbiter, an unfair RLock) the device
    that holds the line reads between every two of the scan's requests —
    inside one unit's identify too."""
    s, client, running, tty = _serial_holder(tmp_path, slave, monkeypatch, {1: sdm_words(), 3: deye_words()})
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            running.read_registers(0, 2, "input")
            time.sleep(0.002)
    t = threading.Thread(target=poll, daemon=True)
    t.start()
    try:
        ans = _wait(client, _scan(client, _serial_scan(tty, **{"from": 2, "to": 4, "timeout": 0.3}))["job"])
    finally:
        stop.set()
        t.join(2)
        running.disconnect()
    assert ans["state"] == "done", ans
    assert [f["unit_id"] for f in ans["found"]] == [3]
    units = [x[1] for x in s.log]
    scan = [i for i, u in enumerate(units) if u != 1]
    assert len(scan) >= 4, units                     # unit 3: alive + its identify reads, 2 and 4 silent
    for a, b in zip(scan, scan[1:]):
        assert 1 in units[a + 1:b], (a, b, units)     # the poller read in between, every time


def _flip_after(monkeypatch, after, flip):
    from multibus import preview
    real = preview._in_turn
    n = {"sent": 0}

    def counting(mc, guard, send, deadline=None):
        out = real(mc, guard, send, deadline)
        n["sent"] += 1
        if n["sent"] == after:
            flip()
        return out
    monkeypatch.setattr(preview, "_in_turn", counting)
    return n


@needs_tc
@pytest.mark.parametrize("how", ["cancel", "standby"])
def test_a_cancel_or_a_standby_stops_the_scan_within_one_request(tmp_path, slave, monkeypatch, how):
    """Mid-identify: the request in flight, and nothing after it."""
    from multibus import preview
    s = slave(units={2: deye_words()})
    _cfg, client = _app(tmp_path)
    box = {}
    if how == "cancel":
        flip = lambda: preview.cancel(box["job"], scans=True)                           # noqa: E731
    else:
        flip = lambda: monkeypatch.setattr(preview, "_wire_allowed", lambda: False)     # noqa: E731
    real_start = preview.start

    def start(env, plan, on_done=None, job_cls=None):
        job = real_start(env, plan, on_done=on_done, job_cls=job_cls)
        box["job"] = job.id
        return job
    monkeypatch.setattr(preview, "start", start)
    n = _flip_after(monkeypatch, 2, flip)
    job = _scan(client, {"connection": _tcp(s.port), "from": 2, "to": 6, "timeout": 0.3, "confirm_open": True})
    ans = _wait(client, job["job"])
    assert n["sent"] == 2 and len(s.log) == 2, (n, s.log)
    if how == "standby":
        assert any("standby" in w for w in ans["warnings"]), ans
    else:
        assert ans["state"] == "cancelled", ans


# ── M2: the holder's wait, the addresses it has, a late answer ──────────────

@needs_tc
def test_the_holders_wait_is_never_shortened(tmp_path, slave, monkeypatch):
    from multibus import preview
    _s, client, running, tty = _serial_holder(tmp_path, slave, monkeypatch, {1: sdm_words()}, timeout=1)
    seen = []
    real = preview._in_turn

    def spy(mc, guard, send, deadline=None):
        def look(g):
            seen.append((g.inner.comm_params.timeout_connect, g.inner.transaction.comm_params.timeout_connect))
            return send(g)
        return real(mc, guard, look, deadline)
    monkeypatch.setattr(preview, "_in_turn", spy)
    try:
        ans = _wait(client, _scan(client, _serial_scan(tty, **{"from": 2, "to": 3, "timeout": 0.3}))["job"])
        assert running.client.comm_params.timeout_connect == 1
    finally:
        running.disconnect()
    assert ans["timeout"] == 1 and seen and all(a >= 1 and b >= 1 for a, b in seen), (ans, seen)


@needs_tc
def test_addresses_a_device_has_are_listed_never_asked(tmp_path, slave):
    """A unit a device of this gateway has on the bus (enabled or not):
    'already: <device>', and no question sent to it."""
    s = slave(units={1: sdm_words(), 2: sdm_words()})
    _cfg, client = _app(tmp_path, f"""
devices:
  - id: off1
    template: eastron_sdm120
    enabled: false
    connection: {{protocol: tcp, host: 127.0.0.1, port: {s.port}, unit_id: 1}}
""")
    ans = _wait(client, _scan(client, {"connection": _tcp(s.port), "from": 1, "to": 3, "timeout": 0.3,
                                       "confirm_open": True})["job"])
    found = {f["unit_id"]: f for f in ans["found"]}
    assert found[1]["device"] == "off1" and found[1]["answer"] == "known" and found[1]["asked"] is False
    assert found[2]["asked"] is True
    assert 1 not in {x[1] for x in s.log}, s.log


class RtuBus:
    """RTU over TCP (a transparent converter): each unit answers FC3 after
    its delay, in order, as on a real line — a late answer comes before the
    next question's."""

    def __init__(self, delays):
        self.delays, self.log = delays, []
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _a = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        buf = b""
        while True:
            try:
                d = c.recv(256)
            except OSError:
                return
            if not d:
                return
            buf += d
            while len(buf) >= 8:
                unit, fc = buf[0], buf[1]
                cnt = struct.unpack(">H", buf[4:6])[0]
                buf = buf[8:]
                self.log.append((unit, fc))
                if unit not in self.delays or fc not in (3, 4):
                    continue
                time.sleep(self.delays[unit])
                body = bytes([unit, fc, 2 * cnt]) + b"".join(struct.pack(">H", 100 + unit) for _ in range(cnt))
                try:
                    c.sendall(_crc(body))
                except OSError:
                    return

    def close(self):
        self.srv.close()


@needs_tc
def test_a_late_answer_never_reaches_the_poller_on_the_same_bus(tmp_path):
    """Unit 5 answers after the scan's wait: the frame is thrown away under
    the lock before the line goes back — the poller (unit 1, the same
    socket) reads its own answers, none missed, nothing mismatched."""
    from multibus.modbus_client import ModbusConnection
    bus = RtuBus({1: 0.0, 5: 0.45})
    try:
        cfg, client = _app(tmp_path, f"""
devices:
  - id: p1
    template: eastron_sdm120
    enabled: true
    connection: {{protocol: rtu-tcp, host: 127.0.0.1, port: {bus.port}, unit_id: 1, timeout: 0.3,
                  retry_attempts: 1}}
""")
        dev = cfg.get_device("p1")
        running = ModbusConnection(dev.connection)
        client.app.state.registry.replace("p1", dev, client=SimpleNamespace(
            parts=[(dev.sources[0], SimpleNamespace(connection=running))]))
        assert running.read_registers(0, 1, "holding") == [101]
        ans = _wait(client, _scan(client, {"connection": {"protocol": "rtu-tcp", "host": "127.0.0.1",
                                                          "port": bus.port}, "from": 5, "to": 5, "timeout": 0.3,
                                           "fc43": False})["job"])
        assert ans["transport"] == "shared" and ans["found"] == [], ans     # too late: not found
        fails = running.failed_reads
        got = [running.read_registers(0, 1, "holding") for _ in range(4)]
        assert got == [[101]] * 4, got
        assert running.failed_reads == fails, (running.failed_reads, running.error_counts)
        running.disconnect()
    finally:
        bus.close()


def test_drain_empties_a_serial_line_and_a_socket():
    from multibus.bus_scan import drain

    class Line:
        in_waiting, reset = 7, 0

        def reset_input_buffer(self):
            Line.reset += 1
    assert drain(SimpleNamespace(socket=Line()), 0) == 7 and Line.reset == 1
    a, b = socket.socketpair()
    a.settimeout(1.5)
    b.sendall(b"late frame")
    assert drain(SimpleNamespace(socket=a), 0.05) == 10
    assert a.gettimeout() == 1.5                      # its own timeout put back
    a.close()
    b.close()
    assert drain(SimpleNamespace(socket=None)) == 0


# ── m2, m3, m4 ──────────────────────────────────────────────────────────────

@needs_tc
def test_the_bus_unwell_stop_names_the_device(tmp_path, slave, monkeypatch):
    from multibus import preview
    _s, client, running, tty = _serial_holder(tmp_path, slave, monkeypatch, {1: sdm_words()})
    monkeypatch.setattr(preview, "_in_turn", lambda *a, **k: (None, "bus_unwell"))
    try:
        ans = _wait(client, _scan(client, _serial_scan(tty, **{"from": 2, "to": 3}))["job"])
    finally:
        running.disconnect()
    assert any("rs1 on this bus is missing answers" in w and "retry when it reads again" in w
               for w in ans["warnings"]), ans


@needs_tc
def test_one_scan_at_a_time_over_every_bus(tmp_path, slave, monkeypatch):
    """A second bus is not scanned while one is; a preview still runs."""
    from multibus import preview
    a, b = slave(units={1: sdm_words()}), slave(units={1: sdm_words()})
    _cfg, client = _app(tmp_path)
    real = preview._in_turn
    monkeypatch.setattr(preview, "_in_turn", lambda *x, **k: (time.sleep(0.2), real(*x, **k))[1])
    job = _scan(client, {"connection": _tcp(a.port), "from": 2, "to": 20, "timeout": 0.2, "confirm_open": True})
    r = client.post("/api/bus-scan", json={"connection": _tcp(b.port), "confirm_open": True})
    assert r.status_code == 409 and "one bus is scanned at a time" in r.text, r.text
    r = client.post("/api/previews", json={"connection": {**_tcp(b.port), "unit_id": 1},
                                           "template": "eastron_sdm120", "confirm_open": True})
    assert r.status_code == 202, r.text
    client.delete(f"/api/bus-scan/{job['job']}")
    _wait(client, job["job"])
    from tests.test_preview import _wait as _preview_wait
    _preview_wait(client, r.json()["job"])          # no job is left running for the next test


@needs_tc
def test_the_unit_sweep_keeps_its_protocol_and_caps_its_wait(tmp_path, monkeypatch):
    """rtu-tcp stays rtu-tcp (RTU frames to a converter); what the scan
    cannot do is refused; past the cap the job id is answered (202)."""
    from multibus import preview
    from multibus.routes import discovery_routes
    bus = RtuBus({2: 0.0})
    try:
        _cfg, client = _app(tmp_path)
        body = {"protocol": "rtu-tcp", "host": "127.0.0.1", "port": bus.port, "unit_start": 1, "unit_end": 3,
                "timeout": 0.3, "confirm_open": True}
        r = client.post("/api/discover/modbus/units", json=body)
        assert r.status_code == 200 and r.json()["protocol"] == "rtu-tcp" and r.json()["units"] == [2], r.text
        assert client.post("/api/discover/modbus/units", json={**body, "protocol": "seplos_v2"}).status_code == 422
        real = preview._in_turn
        monkeypatch.setattr(preview, "_in_turn", lambda *x, **k: (time.sleep(0.3), real(*x, **k))[1])
        monkeypatch.setattr(discovery_routes, "UNITS_WAIT_S", 0.2)
        r = client.post("/api/discover/modbus/units", json={**body, "unit_end": 20})
        assert r.status_code == 202 and r.json()["state"] == "running" and r.json()["job"], r.text
        client.delete(f"/api/bus-scan/{r.json()['job']}")
        _wait(client, r.json()["job"])
    finally:
        bus.close()


# ── m1, m5: no second connection ────────────────────────────────────────────

class _Port:
    """A TCP port that only counts who connects."""

    def __init__(self):
        self.accepts = 0
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]

        def run():
            while True:
                try:
                    c, _a = self.srv.accept()
                except OSError:
                    return
                self.accepts += 1
                c.close()
        threading.Thread(target=run, daemon=True).start()

    def close(self):
        self.srv.close()


@needs_tc
def test_lan_probes_never_open_a_second_connection_to_a_held_or_foreign_bridge_port(tmp_path):
    """The CIDR sweep skips, and the SunSpec walk refuses, a host:port a
    device of ours polls, and our serial bridge's port with a foreign client
    — or with none it can name, unless confirm_open. Nothing connects."""
    held, foreign = _Port(), _Port()
    try:
        _cfg, client = _app(tmp_path, f"""
bridges:
  - {{id: br, name: Panel, type: mbg_serial_bridge, host: 127.0.0.1, ports: [{{port: {foreign.port}}}]}}
devices:
  - id: held1
    template: eastron_sdm120
    enabled: true
    connection: {{protocol: tcp, host: 127.0.0.1, port: {held.port}, unit_id: 1}}
""")
        r = client.post("/api/discover/modbus/scan", json={"cidr": "127.0.0.1/32", "port": held.port, "timeout": 0.2})
        assert r.status_code == 200 and r.json()["scanned"] == 0, r.text
        assert r.json()["skipped"][0]["pollers"] == ["held1"]
        r = client.post("/api/discover/sunspec", json={"host": "127.0.0.1", "port": held.port})
        assert r.status_code == 409 and "held1" in r.text, r.text
        diag = _Diag(foreign.port, "192.168.1.50:51234")
        diag.port_view = lambda bid, p: ({"stats": {"client": {"address": "192.168.1.50:51234"}},
                                          "sampled_ts": time.time()} if (bid, p) == ("br", foreign.port) else None)
        client.app.state.bridge_diag = diag
        r = client.post("/api/discover/modbus/scan", json={"cidr": "127.0.0.1/32", "port": foreign.port,
                                                           "timeout": 0.2})
        assert r.status_code == 200 and r.json()["scanned"] == 0, r.text
        assert r.json()["skipped"][0]["client"] == "192.168.1.50:51234"
        assert client.post("/api/discover/sunspec", json={"host": "127.0.0.1", "port": foreign.port}).status_code == 409
        client.app.state.bridge_diag = None                     # nobody can say: refused unless confirmed
        r = client.post("/api/discover/sunspec", json={"host": "127.0.0.1", "port": foreign.port})
        assert r.status_code == 409 and r.json()["detail"]["client"] == "unknown", r.text
        r = client.post("/api/discover/modbus/scan", json={"cidr": "127.0.0.1/32", "port": foreign.port,
                                                           "timeout": 0.2})
        assert r.json()["scanned"] == 0 and r.json()["skipped"][0]["client"] == "unknown", r.text
        assert held.accepts == 0 and foreign.accepts == 0
    finally:
        held.close()
        foreign.close()


@needs_tc
def test_a_tapped_bridge_port_is_refused_through_the_bridge_route_nothing_connects(tmp_path):
    port = _Port()
    try:
        _cfg, client = _app(tmp_path, f"""
bridges:
  - {{id: br, name: Panel, type: rtu_transparent, host: 127.0.0.1, ports: [{{port: {port.port}}}]}}
devices:
  - id: brtap
    template: eastron_sdm120
    enabled: true
    connection: {{protocol: rtu_tap, bridge: br, bridge_port: {port.port}, unit_id: 2}}
""")
        for body in ({}, {"confirm_open": True}):
            r = client.post(f"/api/bridges/br/ports/{port.port}/scan", json=body)
            assert r.status_code == 409 and "brtap" in r.text, r.text
        time.sleep(0.2)
        assert port.accepts == 0
    finally:
        port.close()
