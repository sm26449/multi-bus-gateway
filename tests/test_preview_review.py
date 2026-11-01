# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""L3.8 safety review — the read preview (L3.8-F5 …).

- M5: a poll is refused when ANY device, enabled or not, listens on the bus;
- M1: one silent unit costs one request; the shared line is handed back
  between the preview's turns; pymodbus' own disconnect counter is never
  spent by a preview;
- M2: a held bus is read through the holder's own running connection (its
  lane, m6) — never a copy; a holder that does not share its transport
  refuses;
- M3: whether another program holds a bridge port is present / absent /
  unknown; unknown refuses unless ``confirm_open`` (audited), on every
  path that opens a connection of its own;
- M4: a reader is found by the bus's identity, whatever name reaches it;
- m7 standby mid-job; m8 at most a few previews at once (429); m9 the
  holder's line said, an edited line refused on a shared bus;
- the nits: the HTTP retry counts, a keepalive that would write, unit 0 on a
  serial bus, a refusal audited, CAN's listen-only asked again once open."""
import os
import socket
import struct
import threading
import time
from types import SimpleNamespace

import pytest

from tests.test_devices_api import needs_tc
from tests.test_preview import (FakeCarrier, FakeSlave, _app, _can_frames, _start, _tcp, _wait,
                                sdm_words)


def _crc(b):
    from multibus.rtu_tap import crc16
    c = crc16(b)
    return b + bytes([c & 0xFF, c >> 8])


class FakeRtuBridge:
    """A serial bus behind a transparent bridge (RTU frames over TCP): units
    in ``units`` answer, any other is silent. Every request is logged."""

    def __init__(self, units):
        self.units = units
        self.log = []
        self.connections = 0
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(4)
        self.port = self._srv.getsockname()[1]
        self._conns = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _a = self._srv.accept()
            except OSError:
                return
            self.connections += 1
            self._conns.append(c)
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        buf = b""
        try:
            while True:
                chunk = c.recv(256)
                if not chunk:
                    return
                buf += chunk
                while len(buf) >= 8:
                    req, buf = buf[:8], buf[8:]
                    unit, fc = req[0], req[1]
                    addr, count = struct.unpack(">HH", req[2:6])
                    self.log.append((time.monotonic(), unit, fc, addr, count))
                    words = self.units.get(unit)
                    if words is None or fc not in (3, 4):
                        continue
                    table = "holding" if fc == 3 else "input"
                    data = b"".join(struct.pack(">H", words.get((table, a), 0)) for a in range(addr, addr + count))
                    c.sendall(_crc(bytes([unit, fc, len(data)]) + data))
        except OSError:
            return

    def close(self):
        self._srv.close()
        for c in self._conns:
            try:
                c.close()
            except OSError:
                pass


@pytest.fixture
def rtu_bridge():
    made = []

    def make(units):
        b = FakeRtuBridge(units)
        made.append(b)
        return b
    yield make
    for b in made:
        b.close()


@pytest.fixture
def slave():
    made = []

    def make(**kw):
        s = FakeSlave(**kw)
        made.append(s)
        return s
    yield make
    for s in made:
        s.close()


def _run_holder(client, did):
    """The device's running acquisition, as the registry holds it: its own
    ModbusConnection inside its client."""
    from multibus.modbus_client import ModbusConnection
    reg = client.app.state.registry
    _i, dev, _c = reg.find(did)
    mc = ModbusConnection(dev.connection, trace_label=did)
    src = (dev.sources or [None])[0]
    reg.replace(did, dev, client=SimpleNamespace(parts=[(src, SimpleNamespace(connection=mc))]))
    return mc


class _Poller:
    """The device's poller: one read every ``every`` s on its connection."""

    def __init__(self, mc, every=0.05):
        self.mc, self.every = mc, every
        self.times, self.ok = [], 0
        self._stop = threading.Event()
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self):
        while not self._stop.is_set():
            if self.mc.read_registers(0, 2, "input"):
                self.ok += 1
            self.times.append(time.monotonic())
            self._stop.wait(self.every)

    def stop(self):
        self._stop.set()
        self.t.join(5)


BRIDGED = """
devices:
  - id: m1
    name: Meter one
    template: eastron_sdm120
    enabled: true
    connection: {{protocol: rtu-tcp, host: 127.0.0.1, port: {port}, unit_id: 1, timeout: 1{extra}}}
"""


# ── M5: any device that listens, enabled or not ─────────────────────────────

@needs_tc
def test_a_disabled_listener_still_refuses_a_poll(tmp_path, monkeypatch):
    from multibus import modbus_client
    alias = tmp_path / "by-id-port0"
    os.symlink("/dev/null", alias)
    _cfg, client = _app(tmp_path, """
devices:
  - id: tap-off
    name: A tap switched off
    template: eastron_sdm120
    enabled: false
    connection: {protocol: rtu_tap, serial_port: /dev/null, unit_id: 2}
""")
    monkeypatch.setattr(modbus_client, "_build_client",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("a client was built")))
    r = client.post("/api/previews", json={"connection": {"protocol": "rtu", "serial_port": str(alias),
                                                          "unit_id": 5}, "template": "eastron_sdm120"})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["listeners"] == ["tap-off"] and "tap-off" in r.text


# ── M1: one silent unit, one request; the line handed back ─────────────────

@needs_tc
def test_a_silent_unit_costs_one_request_and_the_poller_keeps_its_rhythm(tmp_path, rtu_bridge):
    hi, lo = struct.unpack(">HH", struct.pack(">f", 230.0))
    b = rtu_bridge({1: {("input", 0): hi, ("input", 1): lo}})
    _cfg, client = _app(tmp_path, BRIDGED.format(port=b.port, extra=""), budget=(4, 10))
    mc = _run_holder(client, "m1")
    poller = _Poller(mc, every=0.1)
    try:
        time.sleep(0.3)
        job = _start(client, {"connection": {"protocol": "rtu-tcp", "host": "127.0.0.1", "port": b.port,
                                             "unit_id": 9}, "template": "eastron_sdm630"})
        assert job["transport"] == "shared"
        ans = _wait(client, job["job"])
        time.sleep(0.3)
    finally:
        poller.stop()
    assert len([x for x in b.log if x[1] == 9]) == 1                 # one request, then it stopped
    assert ans["state"] == "done" and ans["budget"]["requests"] == 1
    assert any("no answer from unit 9" in w for w in ans["warnings"]), ans["warnings"]
    assert b.connections == 1                                         # the line never reopened
    gaps = [y - x for x, y in zip(poller.times, poller.times[1:])]
    assert max(gaps) < 1.0 + 0.6, gaps                                # one timeout at most, then its turn
    tm = mc.client.transaction
    assert tm.count_until_disconnect == tm.max_until_disconnect       # the preview's timeout was not counted
    assert mc.read_registers(0, 2, "input")


@needs_tc
def test_the_poller_gets_the_line_between_two_preview_requests(tmp_path, rtu_bridge):
    hi, lo = struct.unpack(">HH", struct.pack(">f", 230.0))
    words = {("input", a): 0 for a in range(0, 400)}
    words[("input", 0)], words[("input", 1)] = hi, lo
    b = rtu_bridge({1: words, 2: words})
    _cfg, client = _app(tmp_path, BRIDGED.format(port=b.port, extra=""), budget=(3, 10))
    mc = _run_holder(client, "m1")
    poller = _Poller(mc, every=0.01)
    try:
        time.sleep(0.2)
        ans = _wait(client, _start(client, {"connection": {"protocol": "rtu-tcp", "host": "127.0.0.1",
                                                           "port": b.port, "unit_id": 2},
                                            "template": "eastron_sdm630"})["job"])
    finally:
        poller.stop()
    assert ans["state"] == "done"
    order = [x[1] for x in b.log]
    idx = [i for i, u in enumerate(order) if u == 2]
    assert len(idx) == 3
    for a, z in zip(idx, idx[1:]):
        assert 1 in order[a + 1:z], order                             # the poller had a turn in between


# ── M2 / m6: the holder's own connection, never a copy ─────────────────────

@needs_tc
def test_a_held_bus_is_read_through_the_holders_own_connection(tmp_path, slave, monkeypatch):
    from multibus import modbus_client, preview
    s = slave(units={1: sdm_words(), 2: sdm_words(231.0, 1.0)}, single=True)
    _cfg, client = _app(tmp_path, f"""
devices:
  - id: m1
    name: Meter one
    template: eastron_sdm120
    enabled: true
    connection: {{protocol: tcp, host: 127.0.0.1, port: {s.port}, unit_id: 1, timeout: 1, max_connections: 2}}
""", budget=(2, 10))
    mc = _run_holder(client, "m1")
    assert mc.read_registers(0, 2, "input")
    made = []
    real = modbus_client.ModbusConnection.__init__

    def counting(self, *a, **k):
        made.append(a)
        real(self, *a, **k)
    monkeypatch.setattr(modbus_client.ModbusConnection, "__init__", counting)
    ans = _wait(client, _start(client, {"connection": _tcp(s.port, unit=2), "template": "eastron_sdm120"})["job"])
    assert ans["state"] == "done" and ans["transport"] == "shared" and made == []
    assert abs({v["name"]: v for v in ans["values"]}["voltage_l1_n"]["value"] - 231.0) < 0.01
    assert s.connections == 1 and mc.lane == 1                         # the holder's lane (unit 1 of 2)
    assert {x[1] for x in s.log} == {1, 2}
    assert preview is not None


@needs_tc
def test_a_holder_that_does_not_share_its_transport_refuses(tmp_path, slave):
    s = slave(units={1: sdm_words()})
    _cfg, client = _app(tmp_path, f"""
devices:
  - id: m1
    name: Meter one
    template: eastron_sdm120
    enabled: true
    connection: {{protocol: tcp, host: 127.0.0.1, port: {s.port}, unit_id: 1, timeout: 1, share_transport: false}}
""")
    _run_holder(client, "m1")
    r = client.post("/api/previews", json={"connection": _tcp(s.port, unit=2), "template": "eastron_sdm120"})
    assert r.status_code == 409 and "share_transport" in r.text and "m1" in r.text, r.text
    assert s.log == []


# ── M3: another program on the bridge — present, absent, unknown ───────────

MBG_BRIDGE = """
bridges:
  - {{id: br, name: Panel, type: {kind}, host: 127.0.0.1, ports: [{{port: {port}}}]}}
"""


class _Diag:
    def __init__(self, port, client=None, age=0.0, fail=False, known=True):
        self.port, self.client, self.age, self.fail, self.known = port, client, age, fail, known

    def port_view(self, bid, port):
        if self.fail:
            raise RuntimeError("diag broke")
        if (bid, port) != ("br", self.port) or not self.known:
            return None
        return {"stats": {"client": {"address": self.client} if self.client else None},
                "sampled_ts": time.time() - self.age}


def _bridged(port, **kw):
    return {"connection": {"protocol": "rtu-tcp", "host": "127.0.0.1", "port": port, "unit_id": 1, "timeout": 1},
            "template": "eastron_sdm120", **kw}


@needs_tc
def test_our_bridge_says_present_absent_or_unknown(tmp_path, slave):
    s = slave(units={1: sdm_words()})
    _cfg, client = _app(tmp_path, MBG_BRIDGE.format(kind="mbg_serial_bridge", port=s.port))
    client.app.state.bridge_diag = _Diag(s.port, client="192.168.1.50:51234")
    r = client.post("/api/previews", json=_bridged(s.port, confirm_open=True))
    assert r.status_code == 409 and "192.168.1.50:51234" in r.text      # present: refused, even confirmed
    for diag, why in ((_Diag(s.port, known=False), "not asked"), (_Diag(s.port, age=3600), "old"),
                      (_Diag(s.port, fail=True), "could not")):
        client.app.state.bridge_diag = diag
        r = client.post("/api/previews", json=_bridged(s.port))
        assert r.status_code == 409, r.text
        d = r.json()["detail"]
        assert d["client"] == "unknown" and "confirm_open" in d["errors"][0] and why in d["errors"][0], d
        assert "disconnect" in d["errors"][0]
    assert s.connections == 0
    client.app.state.bridge_diag = _Diag(s.port)                          # absent, fresh: it runs
    ans = _wait(client, _start(client, _bridged(s.port))["job"])
    assert ans["transport"] == "own"


@needs_tc
def test_an_unknown_bus_needs_confirm_open_and_it_is_audited(tmp_path, slave):
    s = slave(units={1: sdm_words()})
    _cfg, client = _app(tmp_path, MBG_BRIDGE.format(kind="rtu_transparent", port=s.port))
    r = client.post("/api/previews", json=_bridged(s.port))              # a converter that cannot say
    assert r.status_code == 409 and r.json()["detail"]["client"] == "unknown"
    ans = _wait(client, _start(client, _bridged(s.port, confirm_open=True))["job"])
    assert ans["state"] == "done"
    lines = [e for e in client.app.state.ctx.audit_log.recent(50) if e.get("action") == "preview"]
    assert any('"confirm_open": true' in (e.get("detail") or "") for e in lines), lines
    assert any((e.get("status") or "").startswith("refused (409)") for e in lines), lines


@needs_tc
@pytest.mark.parametrize("conn,tpl", [
    ({"protocol": "rtu-tcp", "unit_id": 1}, "eastron_sdm120"),
    ({"protocol": "ascii-tcp", "unit_id": 1}, "eastron_sdm120"),
    ({"protocol": "seplos_v2-tcp", "unit_id": 1, "mode": "poll"}, "seplos_bms_v2"),
    ({"protocol": "seplos_v2-tcp", "unit_id": 1, "mode": "listen"}, "seplos_bms_v2_tap"),
])
def test_every_connection_of_its_own_to_an_undeclared_bus_needs_confirm(tmp_path, conn, tpl):
    _cfg, client = _app(tmp_path)
    body = {"connection": {**conn, "host": "127.0.0.1", "port": 9}, "template": tpl, "seconds": 1}
    r = client.post("/api/previews", json=body)
    assert r.status_code == 409 and r.json()["detail"]["client"] == "unknown", r.text
    r = client.post("/api/previews", json={**body, "confirm_open": True})
    assert r.status_code == 202, r.text
    _wait(client, r.json()["job"])


@needs_tc
def test_a_tap_of_its_own_through_a_bridge_needs_confirm(tmp_path):
    """A tap reads the bridge's port: opening it is a connection to the
    bridge too (a single-client one drops its other client)."""
    from multibus import rtu_tap
    _cfg, client = _app(tmp_path, MBG_BRIDGE.format(kind="rtu_transparent", port=9))
    body = {"connection": {"protocol": "rtu_tap", "bridge": "br", "bridge_port": 9, "unit_id": 1},
            "template": "eastron_sdm120", "seconds": 1}
    r = client.post("/api/previews", json=body)
    assert r.status_code == 409 and r.json()["detail"]["client"] == "unknown", r.text
    assert not any(k.startswith("tcp:127.0.0.1") and v._thread for k, v in rtu_tap._READERS.items())
    r = client.post("/api/previews", json={**body, "confirm_open": True})
    assert r.status_code == 202 and r.json()["transport"] == "tap", r.text
    _wait(client, r.json()["job"])


@needs_tc
def test_plain_modbus_tcp_needs_confirm_off_port_502(tmp_path, slave):
    """Port 502 is a Modbus TCP server (many clients); any other port reached
    as tcp may be a raw serial server that serves one client (N-review)."""
    s = slave(units={1: sdm_words()})
    _cfg, client = _app(tmp_path)
    body = {"connection": _tcp(s.port), "template": "eastron_sdm120"}
    r = client.post("/api/previews", json=body)
    assert r.status_code == 409 and r.json()["detail"]["client"] == "unknown", r.text
    assert s.log == []
    assert client.post("/api/previews", json={**body, "confirm_open": True}).status_code == 202
    r = client.post("/api/previews", json={"connection": {**_tcp(502), "host": "127.0.0.1"},
                                           "template": "eastron_sdm120"})
    assert r.status_code == 202, r.text


# ── M4: one reader per bus, whatever name reaches it ───────────────────────

def test_readers_are_keyed_by_the_bus_identity(tmp_path, monkeypatch):
    from multibus import rtu_tap, seplos_v2
    monkeypatch.setattr(rtu_tap, "_READERS", {})
    monkeypatch.setattr(seplos_v2, "_LISTENERS", {})
    monkeypatch.setattr(seplos_v2, "_BUSES", {})
    alias = tmp_path / "usb-FTDI-port0"
    os.symlink("/dev/null", alias)

    def c(**kw):
        d = dict(serial_port="", host="", port=0, baudrate=9600, parity="N", stopbits=1, bytesize=8,
                 unit_id=1, adapter="", protocol="rtu_tap", mode="listen")
        d.update(kw)
        return SimpleNamespace(**d)
    assert rtu_tap.reader_for(c(serial_port="/dev/null")) is rtu_tap.reader_for(c(serial_port=str(alias)))
    assert rtu_tap.reader_for(c(host="Bridge.LAN", port=4196)) is rtu_tap.reader_for(c(host="bridge.lan", port=4196))
    sp = dict(protocol="seplos_v2")
    assert seplos_v2.listener_for(c(serial_port="/dev/null", **sp)) is \
        seplos_v2.listener_for(c(serial_port=str(alias), **sp))
    assert seplos_v2.bus_for(c(serial_port="/dev/null", **sp)) is seplos_v2.bus_for(c(serial_port=str(alias), **sp))
    tcp = dict(protocol="seplos_v2-tcp")
    assert seplos_v2.listener_for(c(host="Bridge.LAN", port=4197, **tcp)) is \
        seplos_v2.listener_for(c(host="bridge.lan", port=4197, **tcp))


# ── m7: a standby stops the job ────────────────────────────────────────────

@needs_tc
def test_a_standby_mid_job_stops_the_preview(tmp_path, slave, monkeypatch):
    from multibus import role
    s = slave(units={1: sdm_words()})
    _cfg, client = _app(tmp_path, budget=(4, 10))
    calls = {"n": 0}

    def flip():
        # the job's own asks: active for its first request, standby after
        if threading.current_thread().name.startswith("preview-"):
            calls["n"] += 1
            return calls["n"] <= 1
        return True
    monkeypatch.setattr(role, "is_active", flip)
    ans = _wait(client, _start(client, {"connection": _tcp(s.port), "template": "eastron_sdm630"})["job"])
    assert len(s.log) == 1 and any("standby" in w for w in ans["warnings"]), (s.log, ans["warnings"])


# ── m8: at most a few previews at once ─────────────────────────────────────

@needs_tc
def test_previews_at_once_are_capped_with_429(tmp_path, slave, monkeypatch):
    from multibus import preview
    a, b = slave(units={1: sdm_words()}), slave(units={1: sdm_words()})
    monkeypatch.setattr(preview, "MAX_JOBS", 1)
    gate = threading.Event()
    real = preview._read_window
    monkeypatch.setattr(preview, "_read_window", lambda *x, **k: (gate.wait(5), real(*x, **k))[1])
    _cfg, client = _app(tmp_path)
    first = _start(client, {"connection": _tcp(a.port), "template": "eastron_sdm120"})
    r = client.post("/api/previews", json={"connection": _tcp(b.port), "template": "eastron_sdm120",
                                           "confirm_open": True})
    assert r.status_code == 429 and r.headers.get("retry-after"), r.text
    gate.set()
    _wait(client, first["job"])
    assert client.post("/api/previews", json={"connection": _tcp(b.port), "template": "eastron_sdm120",
                                              "confirm_open": True}).status_code == 202


# ── m9: the holder's line, said; an edited line refused ────────────────────

@needs_tc
def test_the_holders_line_is_said_and_an_edited_line_is_refused(tmp_path, rtu_bridge):
    b = rtu_bridge({1: {}, 4: {}})
    cfg, client = _app(tmp_path, BRIDGED.format(port=b.port, extra=", baudrate: 19200"))
    _run_holder(client, "m1")
    job = _start(client, {"connection": {"protocol": "rtu-tcp", "host": "127.0.0.1", "port": b.port,
                                         "unit_id": 4}, "template": "eastron_sdm120"})
    ans = _wait(client, job["job"])
    assert ans["shared_line"]["baudrate"] == 19200 and ans["shared_line"]["timeout"] == 1
    assert any("m1" in w and "timeout" in w for w in ans["warnings"])
    for edit in ({"unit_id": 4, "baudrate": 9600}, {"unit_id": 4, "timeout": 3}, {"unit_id": 4, "parity": "E"}):
        r = client.post("/api/devices/m1/preview", json={"connection": edit})
        assert r.status_code == 422 and "m1" in r.text, (edit, r.text)
    assert client.post("/api/devices/m1/preview", json={"connection": {"unit_id": 4}}).status_code == 202


# ── the nits ───────────────────────────────────────────────────────────────

@needs_tc
def test_the_http_retry_counts_in_the_budget(tmp_path, monkeypatch):
    from multibus import http_client
    calls = []
    monkeypatch.setattr(http_client.HttpClient, "_fetch", lambda self: calls.append(1) or {})
    _cfg, client = _app(tmp_path, budget=(1, 10))
    ans = _wait(client, _start(client, {"connection": {"protocol": "http", "url": "http://192.168.1.50/x"},
                                        "template": "fronius_solar_api_meter"})["job"])
    assert len(calls) == 1 and ans["budget"]["requests"] == 1


@needs_tc
def test_a_keepalive_that_would_write_is_refused(tmp_path):
    from tests.test_device_components import PORTAL
    _cfg, client = _app(tmp_path)
    conn = {"protocol": "mqtt", "broker": "192.0.2.10", "port": 1883, "topic": f"N/{PORTAL}/#",
            "vars": {"portal": PORTAL},
            "keepalive": {"topic": "W/{portal}/settings/0/Settings/CGwacs/AcPowerSetPoint", "payload": "0",
                          "every_s": 30}}
    r = client.post("/api/previews", json={"connection": conn, "template": "victron_gx_mqtt"})
    assert r.status_code == 422 and "keepalive" in r.text, r.text


@needs_tc
@pytest.mark.parametrize("proto", ["rtu", "rtu-tcp"])
def test_unit_0_is_a_broadcast_and_refused_on_a_serial_bus(tmp_path, proto):
    _cfg, client = _app(tmp_path)
    conn = ({"protocol": "rtu", "serial_port": "/dev/fake-rtu", "unit_id": 0} if proto == "rtu"
            else {"protocol": "rtu-tcp", "host": "127.0.0.1", "port": 9, "unit_id": 0})
    r = client.post("/api/previews", json={"connection": conn, "template": "eastron_sdm120", "confirm_open": True})
    assert r.status_code == 422 and "broadcast" in r.text, r.text


@needs_tc
def test_can_listen_only_is_asked_again_once_the_socket_is_open(tmp_path, monkeypatch):
    from multibus import can_client
    carriers = []
    monkeypatch.setattr(can_client, "carrier_for", lambda cfg: carriers.append(FakeCarrier(_can_frames()))
                        or carriers[-1])
    said = iter([True, False])
    monkeypatch.setattr(can_client, "listen_only", lambda ch: next(said, False))
    _cfg, client = _app(tmp_path)
    body = {"connection": {"protocol": "can", "carrier": "socketcan", "channel": "can0", "bitrate": 500000},
            "template": "pylontech_can_inverter", "seconds": 1}
    ans = _wait(client, _start(client, body)["job"])
    assert ans["state"] == "failed" and "listen-only" in ans["error"]
    assert carriers[0].closed and len(carriers[0].frames) == len(_can_frames())     # nothing read


# ── second review: N1 … N5 ─────────────────────────────────────────────────

@needs_tc
def test_a_holder_without_a_live_connection_refuses_modbus(tmp_path, slave):
    """N1: the device that holds the bus is starting (or reconnecting): the
    preview never opens a connection of its own a starting device could
    inherit — 409, retry in a moment."""
    s = slave(units={1: sdm_words()})
    _cfg, client = _app(tmp_path, f"""
devices:
  - id: m1
    name: Meter one
    template: eastron_sdm120
    enabled: true
    connection: {{protocol: tcp, host: 127.0.0.1, port: {s.port}, unit_id: 1, timeout: 1}}
""")
    r = client.post("/api/previews", json={"connection": _tcp(s.port, unit=2), "template": "eastron_sdm120",
                                           "confirm_open": True})
    assert r.status_code == 409 and "busy starting" in r.text and "m1" in r.text, r.text
    assert s.connections == 0 and s.log == []


SEPLOS_HELD = """
devices:
  - id: pack1
    name: Pack one
    template: seplos_bms_v2
    enabled: true
    connection: {protocol: seplos_v2, serial_port: /dev/fake-seplos, baudrate: 19200, unit_id: 1, mode: poll,
                 timeout: 1}
"""


@pytest.fixture
def seplos_port(monkeypatch):
    from multibus import seplos_v2 as sv
    from tests.test_seplos_v2 import CELLS16, TEMPS6, FakePack, frame, telemetry_info

    def answer(req):
        if req.cid2 == sv.CMD_TELEMETRY:
            return frame(req.adr, 0, telemetry_info(CELLS16, TEMPS6, voltage=5300))
        return b""
    port = FakePack(answer=answer)
    monkeypatch.setattr(sv, "open_port", lambda c: port)
    monkeypatch.setattr(sv, "_BUSES", {})
    monkeypatch.setattr(sv, "_LISTENERS", {})
    return port


@needs_tc
def test_a_seplos_holder_without_a_live_bus_refuses(tmp_path, seplos_port):
    _cfg, client = _app(tmp_path, SEPLOS_HELD)
    r = client.post("/api/previews", json={"connection": {"protocol": "seplos_v2", "serial_port": "/dev/fake-seplos",
                                                          "baudrate": 19200, "unit_id": 2, "mode": "poll"},
                                           "template": "seplos_bms_v2"})
    assert r.status_code == 409 and "busy starting" in r.text and "pack1" in r.text, r.text
    assert seplos_port.written == []


@needs_tc
def test_a_seplos_poll_on_a_held_bus_keeps_the_holders_line(tmp_path, seplos_port):
    """N3: an edited line is refused like Modbus m9; the answer says the line."""
    from multibus import seplos_v2 as sv
    cfg, client = _app(tmp_path, SEPLOS_HELD)
    dev = cfg.get_device("pack1")
    holder = sv.SeplosV2Client(dev.connection, [], {}, device_id="pack1")
    assert holder.connect()                                    # the running device: the bus is open, held
    try:
        for edit in ({"unit_id": 2, "baudrate": 9600}, {"unit_id": 2, "timeout": 3}, {"unit_id": 2, "parity": "E"}):
            r = client.post("/api/devices/pack1/preview", json={"connection": edit})
            assert r.status_code == 422 and "pack1" in r.text, (edit, r.text)
        job = _start(client, {"connection": {"protocol": "seplos_v2", "serial_port": "/dev/fake-seplos",
                                             "baudrate": 19200, "unit_id": 2, "mode": "poll"},
                              "template": "seplos_bms_v2"})
        assert job["transport"] == "shared" and job["shared_with"] == "pack1"
        ans = _wait(client, job["job"])
        assert ans["shared_line"]["baudrate"] == 19200 and ans["shared_line"]["timeout"] == 1
        assert sv._BUSES[sv.bus_key(dev.connection)].users == 1 and holder.connected   # its bus stays open
    finally:
        holder.disconnect()


class _FakeTm:
    def __init__(self, count, mx=3):
        self.count_until_disconnect, self.max_until_disconnect = count, mx


class _FakeClient:
    def __init__(self, count, fail=False):
        self.transaction = _FakeTm(count)
        self.fail, self.sent = fail, 0

    def execute(self, no_response_expected, request):
        self.sent += 1
        if self.fail:
            self.transaction.count_until_disconnect -= 1
            raise TimeoutError("no answer")
        return SimpleNamespace(isError=lambda: False, information={0: b"Acme", 1: b"Meter"})


class _FakeMc:
    def __init__(self, client):
        self.client = client

    def transaction(self, label=None):
        import contextlib

        @contextlib.contextmanager
        def turn():
            yield self.client
        return turn()


def test_fc43_has_the_read_guards():
    """N2: FC43 as a read: nothing on a client already missing answers, the
    client's silence count put back, nothing past the deadline."""
    from multibus.preview import Budget, GuardedModbusClient, _fc43

    def g():
        return GuardedModbusClient(None, Budget(5), allow_fc43=True)
    unwell = _FakeClient(2)
    assert _fc43(_FakeMc(unwell), g(), 1) is None and unwell.sent == 0
    silent = _FakeClient(3, fail=True)
    assert _fc43(_FakeMc(silent), g(), 1) is None and silent.sent == 1
    assert silent.transaction.count_until_disconnect == 3
    late = _FakeClient(3)
    assert _fc43(_FakeMc(late), g(), 1, deadline=time.monotonic() - 1) is None and late.sent == 0
    ok = _FakeClient(3)
    assert _fc43(_FakeMc(ok), g(), 1) == {"vendor": "Acme", "product": "Meter"}


@needs_tc
def test_a_stuck_request_ends_the_job_and_frees_its_slot(tmp_path, slave, monkeypatch):
    """N4: a wedged production lock: the job ends at its wall time, its bus
    and its slot are free; the lock itself is never touched; the abandoned
    worker's late answer is ignored."""
    from multibus import preview
    monkeypatch.setattr(preview, "POLL_SLACK_S", 0.2)
    monkeypatch.setattr(preview, "WALL_MARGIN_S", 0.5)
    s = slave(units={1: sdm_words(), 2: sdm_words()})
    _cfg, client = _app(tmp_path, f"""
devices:
  - id: m1
    name: Meter one
    template: eastron_sdm120
    enabled: true
    connection: {{protocol: tcp, host: 127.0.0.1, port: {s.port}, unit_id: 1, timeout: 1}}
""", budget=(1, 10))
    mc = _run_holder(client, "m1")
    held, release = threading.Event(), threading.Event()

    def wedge():
        with mc.lock:                       # the device's transaction, stuck
            held.set()
            release.wait(30)
    threading.Thread(target=wedge, daemon=True).start()
    assert held.wait(5)
    t0 = time.monotonic()
    job = _start(client, {"connection": _tcp(s.port, unit=2), "template": "eastron_sdm120"})
    ans = _wait(client, job["job"], timeout=15)
    took = time.monotonic() - t0
    try:
        assert ans["state"] == "failed" and "did not finish" in ans["error"], ans
        assert took < 8, took
        assert preview._BUSY == {} and all(j.finished for j in preview._JOBS.values())
    finally:
        release.set()
    time.sleep(1.5)                                 # the abandoned worker runs on, then ends
    again = client.get(f"/api/previews/{job['job']}").json()
    assert again["state"] == "failed" and again["values"] == []
    assert mc.read_registers(0, 2, "input")        # the device's lock and socket still work


@needs_tc
def test_a_local_tty_held_by_another_process_needs_confirm(tmp_path, monkeypatch):
    from multibus import modbus_client, serial_inventory
    monkeypatch.setattr(serial_inventory, "inventory", lambda *a, **k: {"ports": [
        {"name": "ttyS7", "path": "/dev/ttyS7", "by_id": [], "aliases": [], "base_state": "busy",
         "processes": ["minicom"], "reason": "open in minicom"}], "hidden": 0})
    monkeypatch.setattr(modbus_client, "_build_client",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("a client was built")))
    _cfg, client = _app(tmp_path)
    body = {"connection": {"protocol": "rtu", "serial_port": "/dev/ttyS7", "unit_id": 1},
            "template": "eastron_sdm120"}
    r = client.post("/api/previews", json=body)
    assert r.status_code == 409 and "minicom" in r.text and r.json()["detail"]["client"] == "unknown", r.text
    monkeypatch.setattr(modbus_client, "_build_client", lambda *a, **k: (_ for _ in ()).throw(OSError("no tty")))
    assert client.post("/api/previews", json={**body, "confirm_open": True}).status_code == 202
