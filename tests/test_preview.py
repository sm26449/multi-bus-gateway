# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""L3.8 — the read preview, server side (ADR-0031 §2 Check, §5).

What must hold, each proved here:

- reads only: a guard transport refuses every Modbus function code but 3/4;
  Seplos V2 sends only its read commands; a tap and CAN only receive;
- ADR-0025: a poll on a listened bus is refused (409), an alias included; a
  listen preview joins the bus's reader and transmits nothing;
- one socket: through a running device's transport on a single-client
  bridge, taking turns (endpoint_min_gap_s kept); a foreign client on our
  bridge refuses it, named;
- the budget (preview.max_requests, preview.listen_s), one preview per bus;
- CAN only on a listen-only interface; HTTP and MQTT decode, MQTT finds the
  components;
- the plausibility qualities (the −100 °C lesson: a declared nan is "no
  value"), the identity check and the load estimate;
- operator yes, viewer no; no snapshot; an audit line; the device page's
  preview is a runtime action (ETag said, never 412)."""
import json
import math
import os
import pathlib
import socket
import struct
import threading
import time
from types import SimpleNamespace

import pytest

from tests.test_devices_api import make_app, needs_tc

ROOT = pathlib.Path(__file__).resolve().parent.parent
NONLAN = "security:\n  allow_nonlan_http_devices: true\n"


# ── a fake Modbus TCP slave that logs every function code ──────────────────

class FakeSlave:
    """Modbus TCP on 127.0.0.1. ``words``: {(table, address): word} per unit
    (``units``: {unit: words}); an address it does not hold answers ILLEGAL
    DATA ADDRESS when ``strict``, else 0. ``single``: a new client drops the
    old one (ser2net ``kickolduser``). Every request is logged."""

    def __init__(self, units=None, strict=False, single=False):
        self.units = units or {}
        self.strict, self.single = strict, single
        self.log = []                 # (t, unit, fc, address, count)
        self.connections = 0
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self._conns = []
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self._stop:
            try:
                c, _a = self._srv.accept()
            except OSError:
                return
            self.connections += 1
            if self.single:
                for old in self._conns:
                    try:
                        old.close()
                    except OSError:
                        pass
                self._conns = []
            self._conns.append(c)
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _recv(self, c, n):
        b = b""
        while len(b) < n:
            chunk = c.recv(n - len(b))
            if not chunk:
                raise OSError("closed")
            b += chunk
        return b

    def _serve(self, c):
        try:
            while True:
                head = self._recv(c, 7)
                tid, _pid, ln, unit = struct.unpack(">HHHB", head)
                pdu = self._recv(c, ln - 1)
                fc = pdu[0]
                if fc in (3, 4) and len(pdu) >= 5:
                    addr, count = struct.unpack(">HH", pdu[1:5])
                    self.log.append((time.monotonic(), unit, fc, addr, count))
                    table = "holding" if fc == 3 else "input"
                    words = self.units.get(unit)
                    if words is None:
                        continue                                  # an absent unit: silence
                    if self.strict and any((table, a) not in words for a in range(addr, addr + count)):
                        body = bytes([fc | 0x80, 2])
                    else:
                        data = b"".join(struct.pack(">H", words.get((table, a), 0) & 0xFFFF)
                                        for a in range(addr, addr + count))
                        body = bytes([fc, len(data)]) + data
                else:
                    self.log.append((time.monotonic(), unit, fc, None, None))
                    body = bytes([fc | 0x80, 1])                  # ILLEGAL FUNCTION
                c.sendall(struct.pack(">HHHB", tid, 0, len(body) + 1, unit) + body)
        except OSError:
            pass

    def fcs(self, unit=None):
        return [x[2] for x in self.log if unit is None or x[1] == unit]

    def close(self):
        self._stop = True
        self._srv.close()
        for c in self._conns:
            try:
                c.close()
            except OSError:
                pass


def _float_words(v):
    hi, lo = struct.unpack(">HH", struct.pack(">f", v))
    return hi, lo


def sdm_words(volt=230.5, amps=4.25):
    w = {}
    for addr, v in ((0, volt), (6, amps), (12, volt * amps)):
        hi, lo = _float_words(v)
        w[("input", addr)], w[("input", addr + 1)] = hi, lo
    return w


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


def _app(tmp_path, extra="", budget=None):
    pv = f"preview: {{max_requests: {budget[0]}, listen_s: {budget[1]}}}\n" if budget else ""
    return make_app(tmp_path, extra_yaml="primary_device: false\n" + NONLAN + pv + extra)


def _wait(client, job, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        r = client.get(f"/api/previews/{job}")
        assert r.status_code == 200, r.text
        d = r.json()
        if d["state"] not in ("queued", "running"):
            return d
        time.sleep(0.05)
    raise AssertionError(f"preview {job} still running")


def _start(client, body, code=202):
    # the fake slaves listen on a random port: plain tcp there is a port that
    # may be a raw serial server (N-review), so these tests confirm it
    if (body.get("connection") or {}).get("protocol") == "tcp" and "confirm_open" not in body:
        body = {**body, "confirm_open": True}
    r = client.post("/api/previews", json=body)
    assert r.status_code == code, r.text
    return r.json()


def _tcp(port, unit=1, **kw):
    return {"protocol": "tcp", "host": "127.0.0.1", "port": port, "unit_id": unit, "timeout": 1, **kw}


def _vals(ans):
    return {v["name"]: v for v in ans["values"]}


# ── the guard transport ────────────────────────────────────────────────────

class _Inner:
    def __init__(self):
        self.seen = []

    def execute(self, no_response_expected, request):
        self.seen.append(request.function_code)
        return SimpleNamespace(isError=lambda: False, registers=[0] * int(getattr(request, "count", 0) or 0))


def test_the_guard_refuses_every_function_code_but_3_and_4():
    from multibus.preview import Budget, GuardedModbusClient, GuardRefusal
    inner = _Inner()
    g = GuardedModbusClient(inner, Budget(10), max_per_read=125)
    g.read_holding_registers(0, count=2, device_id=1)
    g.read_input_registers(0, count=2, device_id=1)
    for call in (lambda: g.write_register(0, 1, device_id=1),
                 lambda: g.write_registers(0, [1, 2], device_id=1),
                 lambda: g.write_coil(0, True, device_id=1),
                 lambda: g.write_coils(0, [True], device_id=1),
                 lambda: g.read_coils(0, count=1, device_id=1),
                 lambda: g.read_discrete_inputs(0, count=1, device_id=1),
                 lambda: g.mask_write_register(address=0, and_mask=1, or_mask=0, device_id=1),
                 lambda: g.readwrite_registers(read_address=0, read_count=1, write_address=0,
                                               values=[1], device_id=1),
                 lambda: g.read_device_information(device_id=1),
                 lambda: g.diag_restart_communication(False, device_id=1)):
        with pytest.raises(GuardRefusal):
            call()
    assert inner.seen == [3, 4]
    with pytest.raises(GuardRefusal):
        g.read_holding_registers(0, count=126, device_id=1)      # more than one read may ask
    assert inner.seen == [3, 4]


def test_the_guard_keeps_the_budget():
    from multibus.preview import Budget, BudgetSpent, GuardedModbusClient
    inner = _Inner()
    b = Budget(2)
    g = GuardedModbusClient(inner, b, max_per_read=10)
    g.read_holding_registers(0, count=10, device_id=1)
    g.read_input_registers(0, count=4, device_id=1)
    with pytest.raises(BudgetSpent):
        g.read_input_registers(0, count=1, device_id=1)
    assert inner.seen == [3, 4] and b.requests == 2 and b.registers == 14
    # FC43/14 only when the template's identify asks for it
    g2 = GuardedModbusClient(inner, Budget(2), max_per_read=10, allow_fc43=True)
    g2.read_device_information(device_id=1)
    assert inner.seen[-1] == 43


# ── a poll: FC3/FC4 only, within the budget ─────────────────────────────────

@needs_tc
def test_a_poll_preview_reads_only_3_and_4_within_its_budget(tmp_path, slave):
    s = slave(units={1: sdm_words()})
    _cfg, client = _app(tmp_path, budget=(2, 10))
    job = _start(client, {"connection": _tcp(s.port), "template": "eastron_sdm630"})
    assert job["job"] and job["mode"] == "poll" and job["transport"] == "own"
    ans = _wait(client, job["job"])
    assert ans["state"] == "done", ans
    assert set(s.fcs()) <= {3, 4} and len(s.log) == 2
    assert all(c <= 120 for *_x, c in s.log)
    assert ans["budget"]["requests"] == 2 and ans["budget"]["registers"] == sum(c for *_x, c in s.log)
    assert ans["budget"]["max_requests"] == 2 and ans["budget"]["ms"] >= 0
    g = ans["groups"]
    assert g["asked"] == 2 and g["answered"] == 2 and g["total"] > 2
    v = _vals(ans)
    assert v["voltage_l1_n"]["quality"] == "ok" and abs(v["voltage_l1_n"]["value"] - 230.5) < 0.01
    assert v["voltage_l1_n"]["address"] == 0 and v["voltage_l1_n"]["unit"] == "V"
    assert any("budget" in w for w in ans["warnings"])
    assert ans["identity"]["fingerprint"] in ("none", "unknown")


@needs_tc
def test_the_budget_setting_is_read_at_each_preview(tmp_path, slave):
    s = slave(units={1: sdm_words()})
    _cfg, client = _app(tmp_path, budget=(1, 10))
    ans = _wait(client, _start(client, {"connection": _tcp(s.port), "template": "eastron_sdm120"})["job"])
    assert len(s.log) == 1 and ans["budget"]["max_requests"] == 1
    assert client.post("/api/config/preview", json={"max_requests": 3, "listen_s": 10}).status_code == 200
    ans = _wait(client, _start(client, {"connection": _tcp(s.port), "template": "eastron_sdm630"})["job"])
    assert len(s.log) == 1 + 3 and ans["budget"]["max_requests"] == 3


@needs_tc
def test_one_preview_per_bus_at_a_time_and_delete_cancels(tmp_path, monkeypatch, slave):
    from multibus import preview
    s = slave(units={1: sdm_words()})
    _cfg, client = _app(tmp_path)
    gate = threading.Event()
    real = preview._read_window

    def slow(*a, **k):
        gate.wait(5)
        return real(*a, **k)
    monkeypatch.setattr(preview, "_read_window", slow)
    first = _start(client, {"connection": _tcp(s.port), "template": "eastron_sdm630"})
    second = client.post("/api/previews", json={"connection": _tcp(s.port, unit=2), "template": "eastron_sdm630",
                                                "confirm_open": True})
    assert second.status_code == 409 and "preview" in second.text
    assert client.delete(f"/api/previews/{first['job']}").status_code == 200
    gate.set()
    ans = _wait(client, first["job"])
    assert ans["state"] == "cancelled" and len(s.log) <= 1
    # the bus is free again
    again = _start(client, {"connection": _tcp(s.port), "template": "eastron_sdm120"})
    assert _wait(client, again["job"])["state"] == "done"
    assert client.get("/api/previews/nope").status_code == 404
    assert client.delete("/api/previews/nope").status_code == 404


@needs_tc
def test_seconds_are_bounded(tmp_path, slave):
    s = slave(units={1: sdm_words()})
    _cfg, client = _app(tmp_path)
    for bad in (0, 16, "x", -1):
        r = client.post("/api/previews", json={"connection": _tcp(s.port), "template": "eastron_sdm120",
                                               "seconds": bad})
        assert r.status_code == 422 and "seconds" in r.text, (bad, r.text)
    r = client.post("/api/previews", json={"connection": _tcp(s.port), "template": "nope"})
    assert r.status_code == 422 and "template" in r.text
    assert s.log == []


# ── the shared transport and the single-client bridge ──────────────────────

SHARED = """
devices:
  - id: m1
    name: Meter one
    template: eastron_sdm120
    enabled: true
    connection: {{protocol: tcp, host: 127.0.0.1, port: {port}, unit_id: 1, timeout: 1,
                  endpoint_min_gap_s: 0.15}}
"""


@needs_tc
def test_through_the_running_devices_transport_one_socket_taking_turns(tmp_path, slave):
    from multibus.modbus_client import ModbusConnection
    s = slave(units={1: sdm_words(), 2: sdm_words(231.0, 1.0)}, single=True)
    cfg, client = _app(tmp_path, SHARED.format(port=s.port), budget=(3, 10))
    running = ModbusConnection(cfg.get_device("m1").connection)      # the device's own poller
    dev = cfg.get_device("m1")
    client.app.state.registry.replace("m1", dev, client=SimpleNamespace(
        parts=[(dev.sources[0], SimpleNamespace(connection=running))]))
    try:
        assert running.read_registers(0, 2, "input")
        assert s.connections == 1
        job = _start(client, {"connection": _tcp(s.port, unit=2), "template": "eastron_sdm630"})
        assert job["transport"] == "shared" and job["shared_with"] == "m1"
        ans = _wait(client, job["job"])
        assert ans["state"] == "done" and ans["transport"] == "shared"
        assert s.connections == 1                       # never a second socket
        mine = [x for x in s.log if x[1] == 2]
        assert len(mine) == 3 and {x[2] for x in mine} <= {3, 4}
        gaps = [b[0] - a[0] for a, b in zip(mine, mine[1:])]
        assert all(g >= 0.13 for g in gaps), gaps      # endpoint_min_gap_s kept
        assert running.read_registers(0, 2, "input")    # the device's socket still works
        assert s.connections == 1
        assert abs(_vals(ans)["voltage_l1_n"]["value"] - 231.0) < 0.01
    finally:
        running.disconnect()


BRIDGE = """
bridges:
  - {{id: br, name: Panel, type: mbg_serial_bridge, host: 127.0.0.1, ports: [{{port: {port}}}]}}
"""


class _Diag:
    def __init__(self, port, address):
        self.port, self.address = port, address

    def port_view(self, bid, port):
        if (bid, port) == ("br", self.port):
            return {"stats": {"client": {"address": self.address} if self.address else None},
                    "sampled_ts": time.time()}
        return None


@needs_tc
def test_a_foreign_client_on_our_bridge_refuses_the_preview_naming_it(tmp_path, slave):
    s = slave(units={1: sdm_words()}, single=True)
    _cfg, client = _app(tmp_path, BRIDGE.format(port=s.port))
    client.app.state.bridge_diag = _Diag(s.port, "192.168.1.50:51234")
    conn = {"protocol": "rtu-tcp", "host": "127.0.0.1", "port": s.port, "unit_id": 1}
    r = client.post("/api/previews", json={"connection": conn, "template": "eastron_sdm120"})
    assert r.status_code == 409 and "192.168.1.50:51234" in r.text, r.text
    assert s.connections == 0 and s.log == []
    # no client reported, freshly asked: the preview runs on a socket of its own
    client.app.state.bridge_diag = _Diag(s.port, None)
    job = _start(client, {"connection": {**conn, "protocol": "tcp"}, "template": "eastron_sdm120"})
    assert job["transport"] == "own"


# ── ADR-0025: a listened bus ────────────────────────────────────────────────

TAPPED = """
devices:
  - id: tap
    name: Listens
    template: eastron_sdm120
    enabled: true
    connection: {{protocol: rtu_tap, serial_port: /dev/null, baudrate: 9600, unit_id: 2}}
  - id: bridged-tap
    name: Listens through a bridge
    template: eastron_sdm120
    enabled: true
    connection: {{protocol: rtu_tap, host: 192.0.2.77, port: {port}, unit_id: 3}}
"""


@needs_tc
def test_a_poll_on_a_listened_bus_is_refused_alias_included(tmp_path, monkeypatch):
    from multibus import modbus_client, seplos_v2
    alias = tmp_path / "usb-FTDI-if00-port0"
    os.symlink("/dev/null", alias)                     # a by-id name of the same line
    _cfg, client = _app(tmp_path, TAPPED.format(port=4196))
    monkeypatch.setattr(modbus_client, "_build_client",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("a client was built")))
    monkeypatch.setattr(seplos_v2, "open_port",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("a port was opened")))
    for conn in ({"protocol": "rtu", "serial_port": "/dev/null", "unit_id": 5},
                 {"protocol": "rtu", "serial_port": str(alias), "unit_id": 5},
                 {"protocol": "rtu-tcp", "host": "192.0.2.77", "port": 4196, "unit_id": 5},
                 {"protocol": "seplos_v2", "serial_port": str(alias), "unit_id": 1, "mode": "poll"}):
        tpl = "seplos_bms_v2" if conn["protocol"] == "seplos_v2" else "eastron_sdm120"
        r = client.post("/api/previews", json={"connection": conn, "template": tpl})
        assert r.status_code == 409, (conn, r.text)
        d = r.json()["detail"]
        assert d["listeners"] in (["tap"], ["bridged-tap"]) and "Nothing was sent" in d["errors"][0]


# ── a listen preview: joins the tap, transmits nothing ──────────────────────

def _crc(b):
    from multibus.rtu_tap import crc16
    c = crc16(b)
    return b + bytes([c & 0xFF, c >> 8])


def _rtu_exchange(unit, fc, addr, words):
    req = _crc(bytes([unit, fc]) + struct.pack(">HH", addr, len(words)))
    data = b"".join(struct.pack(">H", w) for w in words)
    return req + _crc(bytes([unit, fc, len(data)]) + data)


class FakeSerial:
    """pyserial's Serial as the tap opens it: reads the stream given, counts
    every write (there must be none)."""
    opened = []
    stream = b""

    def __init__(self, port=None, **kw):
        FakeSerial.opened.append(port)
        self._out = bytearray(FakeSerial.stream)
        self.writes = 0
        self.closed = False
        self.lock = threading.Lock()

    def read(self, n):
        with self.lock:
            chunk = bytes(self._out[:64])
            del self._out[:64]
        if not chunk:
            time.sleep(0.01)
            # the master asks again: the line keeps going
            with self.lock:
                self._out.extend(FakeSerial.stream)
        return chunk

    def write(self, data):
        self.writes += 1
        raise AssertionError(f"the tap wrote {data!r}")

    def close(self):
        self.closed = True


@pytest.fixture
def fake_serial(monkeypatch):
    serial = pytest.importorskip("serial")
    from multibus import rtu_tap
    monkeypatch.setattr(serial, "Serial", FakeSerial)
    monkeypatch.setattr(rtu_tap, "_READERS", {})
    FakeSerial.opened = []
    hi, lo = _float_words(229.75)
    FakeSerial.stream = _rtu_exchange(1, 4, 0, [hi, lo]) + _rtu_exchange(9, 3, 100, [7])
    return FakeSerial


@needs_tc
def test_a_listen_preview_hears_the_tap_and_sends_nothing(tmp_path, fake_serial):
    _cfg, client = _app(tmp_path)
    conn = {"protocol": "rtu_tap", "serial_port": "/dev/fake-tap", "baudrate": 9600, "unit_id": 1}
    job = _start(client, {"connection": conn, "template": "eastron_sdm120", "seconds": 1})
    assert job["mode"] == "listen" and job["transport"] == "tap"
    ans = _wait(client, job["job"])
    assert ans["state"] == "done", ans
    assert fake_serial.opened.count("/dev/fake-tap") == 1
    v = _vals(ans)
    assert v["voltage_l1_n"]["quality"] == "ok" and abs(v["voltage_l1_n"]["value"] - 229.75) < 0.01
    assert ans["groups"]["heard"] >= 1 and ans["groups"]["total"] >= ans["groups"]["heard"]
    assert ans["budget"]["requests"] == 0 and ans["load"]["share_added"] == 0
    from multibus import rtu_tap
    r = rtu_tap._READERS[next(iter(rtu_tap._READERS))]
    assert r._thread is None and not r._clients        # the reader it opened is closed again


@needs_tc
def test_a_listen_preview_joins_the_running_tap_reader(tmp_path, fake_serial):
    from multibus import rtu_tap
    _cfg, client = _app(tmp_path)
    conn = SimpleNamespace(serial_port="/dev/fake-tap", baudrate=9600, parity="N", stopbits=1, bytesize=8,
                           host="", port=0, unit_id=9, adapter="")
    running = rtu_tap.RtuTapClient(conn, [], device_id="running")
    got = []
    running.publish_callback = lambda g, d: got.append(d)
    assert running.connect()
    try:
        body = {"connection": {"protocol": "rtu_tap", "serial_port": "/dev/fake-tap", "baudrate": 9600,
                               "unit_id": 1}, "template": "eastron_sdm120", "seconds": 1}
        ans = _wait(client, _start(client, body)["job"])
        assert ans["state"] == "done" and _vals(ans)["voltage_l1_n"]["quality"] == "ok"
        assert fake_serial.opened.count("/dev/fake-tap") == 1     # one reader, the tty opened once
        assert running._registered and running._reader._thread is not None   # still listening
    finally:
        running.disconnect()


@needs_tc
def test_a_template_written_for_listening_is_previewed_as_a_tap(tmp_path, fake_serial):
    _cfg, client = _app(tmp_path)
    conn = {"protocol": "rtu", "serial_port": "/dev/fake-tap", "baudrate": 19200, "unit_id": 2}
    job = _start(client, {"connection": conn, "template": "seplos_bms_v3_rtu_tap", "seconds": 1})
    assert job["mode"] == "listen" and job["transport"] == "tap"
    _wait(client, job["job"])


# ── Seplos V2: the capture replayed, and a poll of read commands only ──────

CAPTURE = ROOT / "tests" / "fixtures" / "seplos_v2" / "ttySC0-interpack-20261026.capture"


@pytest.fixture
def seplos_port(monkeypatch):
    from multibus import seplos_v2 as sv
    holder = {}

    def opener(c):
        if holder.get("port") is None:
            raise FileNotFoundError(c.serial_port)
        return holder["port"]
    monkeypatch.setattr(sv, "open_port", opener)
    monkeypatch.setattr(sv, "_BUSES", {})
    monkeypatch.setattr(sv, "_LISTENERS", {})
    return holder


@needs_tc
def test_a_seplos_listen_preview_from_the_capture(tmp_path, seplos_port):
    from tests.test_seplos_v2 import FakePack
    stream = b"".join(line.split()[1].encode() + b"\r" for line in CAPTURE.read_text().splitlines())
    port = FakePack(stream=stream)
    seplos_port["port"] = port
    _cfg, client = _app(tmp_path)
    conn = {"protocol": "seplos_v2", "serial_port": "/dev/fake-seplos", "baudrate": 19200, "unit_id": 1,
            "mode": "listen"}
    job = _start(client, {"connection": conn, "template": "seplos_bms_v2_tap", "seconds": 2})
    assert job["mode"] == "listen" and job["transport"] == "tap"
    ans = _wait(client, job["job"])
    assert ans["state"] == "done", ans
    v = _vals(ans)
    assert v["soc"]["quality"] == "ok" and v["soc"]["value"] == 33.4
    assert v["pack_voltage"]["value"] in (52.35, 52.36)   # the bench's own minute of drift
    assert ans["groups"]["heard"] >= 1
    assert port.written == []                           # nothing transmitted


@needs_tc
def test_a_seplos_poll_preview_asks_read_commands_only(tmp_path, seplos_port):
    from multibus import seplos_v2 as sv
    from tests.test_seplos_v2 import CELLS16, TEMPS6, FakePack, frame, telemetry_info

    def answer(req):
        if req.cid2 == sv.CMD_TELEMETRY:
            return frame(req.adr, 0, telemetry_info(CELLS16, TEMPS6, voltage=5300))
        return b""
    port = FakePack(answer=answer)
    seplos_port["port"] = port
    _cfg, client = _app(tmp_path, budget=(2, 10))
    conn = {"protocol": "seplos_v2", "serial_port": "/dev/fake-seplos", "baudrate": 19200, "unit_id": 1,
            "mode": "poll", "timeout": 1}
    job = _start(client, {"connection": conn, "template": "seplos_bms_v2"})
    assert job["mode"] == "poll"
    ans = _wait(client, job["job"])
    assert ans["state"] == "done", ans
    sent = [sv.parse(w).cid2 for w in port.written]
    assert sent and len(sent) <= 2 and set(sent) <= sv.READ_COMMANDS
    assert _vals(ans)["pack_voltage"]["value"] == 53.0
    # a pack mode not said: asked, never guessed
    r = client.post("/api/previews", json={"connection": {k: v for k, v in conn.items() if k != "mode"},
                                           "template": "seplos_bms_v2"})
    assert r.status_code == 422 and "mode" in r.text


# ── CAN ─────────────────────────────────────────────────────────────────────

CANDUMP = ROOT / "tests" / "fixtures" / "can" / "deye_seplos_v2_20261026.candump"


class FakeCarrier:
    sent = []

    def __init__(self, frames):
        self.frames = list(frames)
        self.opened = self.closed = False

    def open(self):
        self.opened = True

    def recv(self):
        if not self.frames:
            time.sleep(0.01)
            return None
        cid, ext, data = self.frames.pop(0)
        return cid, ext, False, False, data

    def close(self):
        self.closed = True


def _can_frames():
    from multibus.can_signal import parse_candump_line
    out = []
    for line in CANDUMP.read_text().splitlines()[:200]:
        p = parse_candump_line(line)
        if p:
            _t, _ch, cid, ext, data = p
            out.append((cid, ext, data))
    return out


@needs_tc
def test_can_is_previewed_only_on_a_listen_only_interface(tmp_path, monkeypatch):
    from multibus import can_client
    carriers = []
    monkeypatch.setattr(can_client, "carrier_for", lambda cfg: carriers.append(FakeCarrier(_can_frames()))
                        or carriers[-1])
    monkeypatch.setattr(can_client, "listen_only", lambda ch: False)
    _cfg, client = _app(tmp_path)
    body = {"connection": {"protocol": "can", "carrier": "socketcan", "channel": "can0", "bitrate": 500000},
            "template": "pylontech_can_inverter", "seconds": 1}
    r = client.post("/api/previews", json=body)
    assert r.status_code == 409 and "listen-only" in r.text and carriers == []
    monkeypatch.setattr(can_client, "listen_only", lambda ch: None)       # not known: not listen-only
    assert client.post("/api/previews", json=body).status_code == 409
    monkeypatch.setattr(can_client, "listen_only", lambda ch: True)
    job = _start(client, body)
    assert job["mode"] == "listen" and job["transport"] == "tap"
    ans = _wait(client, job["job"])
    assert ans["state"] == "done", ans
    v = _vals(ans)
    assert v["charge_voltage_limit"]["value"] == 57.6 and v["charge_voltage_limit"]["can_id"] == "0x351"
    assert v["soc"]["value"] == 29 and v["soc"]["quality"] == "ok"
    assert ans["groups"]["heard"] >= 3
    assert len(carriers) == 1 and carriers[0].opened and carriers[0].closed
    assert not hasattr(carriers[0], "send")


# ── HTTP ────────────────────────────────────────────────────────────────────

def test_http_test_read_returns_the_decoded_values(monkeypatch):
    from multibus.config import SelectedRegister
    from multibus.http_client import HttpClient
    regs = [SelectedRegister(address=1, name="power", label="Power", unit="W", data_type="float",
                             poll_group="realtime", json_path="Body.Data.P", scale=1.0),
            SelectedRegister(address=2, name="volt", label="Volt", unit="V", data_type="float",
                             poll_group="realtime", json_path="Body.Data.U", scale=10.0),
            SelectedRegister(address=3, name="missing", label="Missing", unit="", data_type="float",
                             poll_group="realtime", json_path="Body.Data.Nope")]
    c = HttpClient({"url": "http://192.168.1.50/x"}, regs, {})
    monkeypatch.setattr(c, "_fetch", lambda: {"Body": {"Data": {"P": 1234.5, "U": 2305}}})
    r = c.test_read()
    assert r["ok"] is True and "2/3 paths resolved" in r["message"]
    v = {x["name"]: x for x in r["values"]}
    assert v["power"]["value"] == 1234.5 and v["power"]["path"] == "Body.Data.P"
    assert v["volt"]["value"] == 230.5 and v["volt"]["raw"] == 2305
    assert v["missing"]["value"] is None and v["missing"]["raw"] is None


@needs_tc
def test_an_http_preview_decodes_the_template(tmp_path, monkeypatch):
    from multibus import http_client
    doc = {"Body": {"Data": {}}}
    monkeypatch.setattr(http_client.HttpClient, "_fetch", lambda self: doc)
    _cfg, client = _app(tmp_path)
    tpl = client.app.state.ctx.template_registry.get("fronius_solar_api_meter")
    first = next(r for r in tpl.registers if r.json_path)
    # the path as the meter answers it
    cur = doc
    parts = first.json_path.replace("]", "").replace("[", ".").split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = 42.0
    job = _start(client, {"connection": {"protocol": "http", "url": "http://192.168.1.50/solar_api/v1/x.cgi"},
                          "template": "fronius_solar_api_meter"})
    assert job["mode"] == "poll" and job["transport"] == "own"
    ans = _wait(client, job["job"])
    assert ans["state"] == "done", ans
    v = _vals(ans)[first.name]
    assert v["path"] == first.json_path and v["quality"] == "ok"
    assert any(x["quality"] == "no_answer" for x in ans["values"])


# ── MQTT, with the components ───────────────────────────────────────────────

@needs_tc
def test_an_mqtt_preview_decodes_and_finds_the_components(tmp_path, monkeypatch):
    from multibus import component_discover as cd
    from multibus import mqtt_link, preview
    from tests.test_device_components import PORTAL, _msg, _Paho
    link = mqtt_link.MqttLink(("@preview",), SimpleNamespace(broker="192.0.2.10", port=1883))
    link.client, link.connected = _Paho(), True
    closed = []
    monkeypatch.setattr(cd, "open_link", lambda conn: link)
    monkeypatch.setattr(cd, "close", lambda ln: closed.append(ln))

    def speak(seconds, cancel):
        for t, v in [(f"N/{PORTAL}/system/0/Dc/Battery/Soc", 55.5),
                     (f"N/{PORTAL}/battery/288/ProductName", "SmartShunt"),
                     (f"N/{PORTAL}/battery/288/CustomName", "House"),
                     (f"N/{PORTAL}/temperature/24/ProductName", "RuuviTag")]:
            link._on_message(link.client, None, _msg(t, v))
    monkeypatch.setattr(preview, "_sleep", speak)
    _cfg, client = _app(tmp_path)
    conn = {"protocol": "mqtt", "broker": "192.0.2.10", "port": 1883, "topic": f"N/{PORTAL}/#",
            "vars": {"portal": PORTAL}}
    job = _start(client, {"connection": conn, "template": "victron_gx_mqtt", "seconds": 2})
    assert job["mode"] == "listen" and job["transport"] == "own"
    ans = _wait(client, job["job"])
    assert ans["state"] == "done", ans
    v = _vals(ans)
    assert v["soc"]["value"] == 55.5 and v["soc"]["quality"] == "ok"
    assert v["soc"]["path"] == f"N/{PORTAL}/system/0/Dc/Battery/Soc"
    assert {(c["type"], c["instance"], c["product"]) for c in ans["components"]} >= {
        ("battery", "288", "SmartShunt"), ("temperature", "24", "RuuviTag")}
    # the template's broker keepalive, sent as the running device would, and said
    assert link.client.sent == [(f"R/{PORTAL}/keepalive", "")]
    assert any("keepalive" in w for w in ans["warnings"])
    assert closed == [link] and link._taps == []


# ── plausibility: the qualities ─────────────────────────────────────────────

def _row(**kw):
    from multibus.config import SelectedRegister
    d = dict(address=0, name="x", label="X", unit="", data_type="uint16", poll_group="normal")
    d.update(kw)
    return SelectedRegister(**d)


def test_a_declared_nan_is_no_value_the_minus_100_lesson():
    from multibus.preview import decode_row
    # Deye reg 182: raw 0 means "no BMS"; with nan: 0 it is no value, never −100 °C
    r = _row(name="battery_temperature", unit="°C", scale=10, offset=-100, nan=0)
    assert decode_row(r, [0], "little") == (None, "sentinel")
    # the same row without its nan: −100 °C is out of range, not a reading
    r2 = _row(name="battery_temperature", unit="°C", scale=10, offset=-100)
    v, q = decode_row(r2, [0], "little")
    assert v == -100 and q == "out_of_range"
    assert decode_row(r2, [1250], "little") == (25.0, "ok")


def test_the_qualities_of_a_value():
    from multibus.preview import decode_row
    f = _row(data_type="float", unit="V")
    hi, lo = _float_words(230.5)
    assert decode_row(f, [hi, lo], "big") == (230.5, "ok")
    v, q = decode_row(f, [lo, hi], "big")                  # the words swapped
    assert q == "byte_swap_suspect"
    nan_words = list(struct.unpack(">HH", struct.pack(">f", math.nan)))
    assert decode_row(f, nan_words, "big")[1] == "byte_swap_suspect"
    big = _row(data_type="uint32", unit="Wh")
    assert decode_row(big, [0xFFFF, 0xFFF0], "big")[1] in ("out_of_range", "byte_swap_suspect")
    assert decode_row(f, None, "big", error="illegal_address") == (None, "illegal_address")
    assert decode_row(f, None, "big") == (None, "no_answer")
    lim = SimpleNamespace(**{**_row(unit="%").__dict__, "min": 0, "max": 100})
    assert decode_row(lim, [150], "big") == (150, "out_of_range")
    assert decode_row(lim, [50], "big") == (50, "ok")
    en = _row(enum={0: "off", 1: "on"})
    assert decode_row(en, [1], "big") == ("on", "ok")


@needs_tc
def test_the_deye_battery_temperature_previews_as_no_value(tmp_path, slave):
    words = {("holding", a): 0 for a in range(0, 200)}
    words[("holding", 0)] = 3                                     # device type: a Deye hybrid
    words[("holding", 59)] = 2
    words[("holding", 184)] = 77
    s = slave(units={1: words})
    _cfg, client = _app(tmp_path, budget=(20, 10))
    ans = _wait(client, _start(client, {"connection": _tcp(s.port), "template": "deye_hybrid_1p"})["job"])
    v = _vals(ans)
    assert v["battery_temperature"]["value"] is None and v["battery_temperature"]["quality"] == "sentinel"
    assert ans["identity"]["fingerprint"] == "match" and ans["identity"]["template"] == "deye_hybrid_1p"


# ── identity ────────────────────────────────────────────────────────────────

@needs_tc
def test_identity_mismatch_suggests_the_template_that_fits(tmp_path, slave):
    words = {("holding", a): 0 for a in range(0, 61)}
    words[("holding", 0)] = 3
    words[("holding", 59)] = 1
    s = slave(units={1: words}, strict=True)
    _cfg, client = _app(tmp_path, budget=(4, 10))
    ans = _wait(client, _start(client, {"connection": _tcp(s.port), "template": "huawei_sun2000"})["job"])
    idn = ans["identity"]
    assert idn["template"] == "huawei_sun2000" and idn["fingerprint"] == "mismatch"
    assert idn["suggested"] == "deye_hybrid_1p"
    assert set(s.fcs()) <= {3, 4} and len(s.log) <= 4
    assert any(v["quality"] == "illegal_address" for v in ans["values"])


def test_the_sunspec_common_model_is_read_as_identity():
    from multibus.preview import sunspec_identity

    def text(s, n):
        b = s.encode().ljust(2 * n, b"\0")
        return [int.from_bytes(b[i:i + 2], "big") for i in range(0, 2 * n, 2)]
    words = [0x5375, 0x6E53, 1, 66] + text("Fronius", 16) + text("Smart Meter 63A", 16) + text("", 8) \
        + text("1.2", 8) + text("12345678", 16)
    got = sunspec_identity({("holding", 40000 + i): w for i, w in enumerate(words)})
    assert got == {"manufacturer": "Fronius", "model": "Smart Meter 63A", "version": "1.2", "serial": "12345678"}
    assert sunspec_identity({("holding", 40000): 0, ("holding", 40001): 0}) is None


# ── the load estimate ───────────────────────────────────────────────────────

def test_the_load_estimate_from_groups_line_and_interval():
    from multibus.preview import estimate_added
    # one 2-register read every second at 9600 Bd: (8 + 9) bytes × 11 bits + turnaround
    one = estimate_added([{"count": 2, "interval": 1.0}], {"baud": 9600, "serial": True})
    assert 0.02 < one < 0.06
    two = estimate_added([{"count": 2, "interval": 1.0}, {"count": 60, "interval": 10.0}],
                         {"baud": 9600, "serial": True})
    assert two > one
    assert estimate_added([{"count": 2, "interval": 1.0}], {"baud": 19200, "serial": True}) < one
    assert estimate_added([], {"baud": 9600, "serial": True}) == 0


@needs_tc
def test_the_answer_carries_the_load(tmp_path, slave):
    s = slave(units={1: sdm_words()})
    _cfg, client = _app(tmp_path)
    ans = _wait(client, _start(client, {"connection": _tcp(s.port), "template": "eastron_sdm120"})["job"])
    assert set(ans["load"]) >= {"share_now", "share_added"} and ans["load"]["share_added"] > 0


# ── roles, snapshots, audit, the device page ────────────────────────────────

@needs_tc
def test_the_operator_may_preview_the_viewer_may_not(tmp_path, monkeypatch):
    from multibus import http_client
    from tests.test_operator_role import _build_clients
    monkeypatch.setattr(http_client.HttpClient, "_fetch", lambda self: {})
    c = _build_clients(tmp_path)
    body = {"connection": {"protocol": "http", "url": "http://192.168.1.50/x"}, "template": "fronius_solar_api_meter"}
    assert c.viewer.post("/api/previews", json=body).status_code == 403
    r = c.op.post("/api/previews", json=body)
    assert r.status_code == 202, r.text
    assert c.viewer.get(f"/api/previews/{r.json()['job']}").status_code == 200
    assert c.viewer.delete(f"/api/previews/{r.json()['job']}").status_code == 403
    assert c.op.delete(f"/api/previews/{r.json()['job']}").status_code == 200
    _wait(c.op, r.json()["job"])
    dev = c.cfg.devices[0].id
    assert c.viewer.post(f"/api/devices/{dev}/preview", json={}).status_code == 403
    assert c.op.post(f"/api/devices/{dev}/preview", json={}).status_code not in (401, 403)


@needs_tc
def test_a_preview_never_schedules_a_snapshot_and_is_audited(tmp_path, slave, monkeypatch):
    s = slave(units={1: sdm_words()})
    cfg, client = _app(tmp_path, SHARED.format(port=s.port))
    got = []
    monkeypatch.setattr(client.app.state.snapshot_store, "schedule", lambda *a, **k: got.append(a))
    from multibus.modbus_client import ModbusConnection
    dev = cfg.get_device("m1")
    running = ModbusConnection(dev.connection)                  # m1 runs: the bus is held
    client.app.state.registry.replace("m1", dev, client=SimpleNamespace(
        parts=[(dev.sources[0], SimpleNamespace(connection=running))]))
    job = _start(client, {"connection": _tcp(s.port, unit=3), "template": "eastron_sdm120"})
    _wait(client, job["job"])
    r = client.post("/api/devices/m1/preview", json={"connection": {"unit_id": 4}})
    assert r.status_code == 202, r.text
    _wait(client, r.json()["job"])
    assert client.delete(f"/api/previews/{job['job']}").status_code in (200, 409)
    assert got == []
    lines = client.app.state.ctx.audit_log.recent(50)
    done = [e for e in lines if e.get("action") == "preview"]
    assert len(done) == 2 and all(e["status"] == "done" for e in done)
    assert all("127.0.0.1" in e["target"] for e in done)
    details = [json.loads(e["detail"]) for e in done]
    assert all(d["mode"] == "poll" and d["requests"] >= 1 for d in details)
    assert sorted(d.get("device") or "" for d in details) == ["", "m1"]
    # the start is in the trail too, as every mutating call is
    assert any(e.get("action") == "POST /api/previews" for e in lines)


@needs_tc
def test_the_device_page_previews_its_edited_connection(tmp_path, slave):
    s = slave(units={1: sdm_words(), 4: sdm_words(232.0, 2.0)})
    cfg, client = _app(tmp_path, SHARED.format(port=s.port))
    from multibus.modbus_client import ModbusConnection
    dev = cfg.get_device("m1")
    tag = client.get("/api/devices/m1").headers["etag"]
    running = ModbusConnection(dev.connection)                  # m1 runs: the bus is held
    client.app.state.registry.replace("m1", dev, client=SimpleNamespace(
        parts=[(dev.sources[0], SimpleNamespace(connection=running))]))
    r = client.post("/api/devices/m1/preview", json={"connection": {"unit_id": 4}},
                    headers={"If-Match": '"stale"'})
    assert r.status_code == 202, r.text
    assert r.headers["etag"] == tag and r.headers["mbg-if-match"] == "ignored"
    ans = _wait(client, r.json()["job"])
    assert ans["state"] == "done" and abs(_vals(ans)["voltage_l1_n"]["value"] - 232.0) < 0.01
    assert {x[1] for x in s.log} == {4}                  # the edited unit, never the saved one
    assert ans["transport"] == "shared"                  # the device itself holds that bus
    assert cfg.get_raw_device("m1")["connection"]["unit_id"] == 1     # nothing stored
    assert client.post("/api/devices/nope/preview", json={}).status_code == 404


@needs_tc
def test_the_device_page_previews_the_device_as_stored_its_own_address_no_clash(tmp_path, slave):
    """L3.9 (found by the e2e): the header's Preview sends no edit — the device's
    own stored connection. Judged as a save of THAT device judges it: on a
    serial bus (here RTU over TCP, two slaves) its own unit is not "already
    read by" itself; another device's unit still is."""
    rtu = ("devices:\n" + "".join(
        f"  - id: m{u}\n    name: Meter {u}\n    template: eastron_sdm120\n    enabled: false\n"
        f"    connection: {{protocol: rtu-tcp, host: 127.0.0.1, port: 1, unit_id: {u}, timeout: 1}}\n" for u in (1, 2)))
    cfg, client = _app(tmp_path, rtu)
    r = client.post("/api/devices/m1/preview", json={"confirm_open": True})
    assert r.status_code == 202, r.text
    assert r.json()["unit"] == 1 and r.json()["device"] == "m1"
    client.delete(f"/api/previews/{r.json()['job']}")
    # the edit names m2's unit: that clash is real
    r = client.post("/api/devices/m1/preview", json={"connection": {"unit_id": 2}, "confirm_open": True})
    assert r.status_code == 422 and "m2" in r.text, r.text
