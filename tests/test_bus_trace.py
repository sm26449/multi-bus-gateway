"""Frame-level bus monitor: ADU decoding, capture ring, API routes."""
import struct

import pytest

from multibus.bus_trace import BusTrace, _crc16, decode_transaction
from tests.test_devices import write_config
from tests.test_devices_api import needs_tc


# ── frame builders ──────────────────────────────────────────────────────────

def tcp_adu(tid: int, unit: int, pdu: bytes) -> bytes:
    return struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu


def rtu_adu(unit: int, pdu: bytes) -> bytes:
    body = bytes([unit]) + pdu
    return body + struct.pack("<H", _crc16(body))


REQ_FC3 = bytes([3]) + struct.pack(">HH", 19026, 4)
RSP_FC3 = bytes([3, 8]) + b"\x00" * 8


# ── decoding ────────────────────────────────────────────────────────────────

def test_decode_tcp_ok():
    m = decode_transaction("tcp", tcp_adu(7, 1, REQ_FC3), tcp_adu(7, 1, RSP_FC3))
    assert m["result"] == "ok"
    assert (m["unit"], m["tid"], m["fc"], m["addr"], m["count"]) == (1, 7, 3, 19026, 4)
    assert m["fc_name"] == "Read Holding Registers"


def test_decode_tcp_exception():
    m = decode_transaction("tcp", tcp_adu(9, 1, REQ_FC3), tcp_adu(9, 1, bytes([0x83, 0x02])))
    assert m["result"] == "exception"
    assert m["exc"] == 2 and m["exc_name"] == "ILLEGAL DATA ADDRESS"


def test_decode_no_response_and_malformed():
    assert decode_transaction("tcp", tcp_adu(1, 1, REQ_FC3), b"")["result"] == "no_response"
    assert decode_transaction("tcp", b"\x00\x01", b"")["result"] == "malformed"
    # răspuns trunchiat sub headerul MBAP
    assert decode_transaction("tcp", tcp_adu(1, 1, REQ_FC3), b"\x00\x01\x00")["result"] == "malformed"


def test_decode_tid_mismatch():
    m = decode_transaction("tcp", tcp_adu(5, 1, REQ_FC3), tcp_adu(6, 1, RSP_FC3))
    assert m["result"] == "mismatch"


def test_decode_rtu_ok_and_crc_error():
    tx, rx = rtu_adu(2, REQ_FC3), rtu_adu(2, RSP_FC3)
    assert decode_transaction("rtu", tx, rx)["result"] == "ok"
    bad = rx[:-1] + bytes([rx[-1] ^ 0xFF])
    assert decode_transaction("rtu", tx, bad)["result"] == "crc_error"


def test_decode_write_fcs():
    m = decode_transaction("tcp", tcp_adu(1, 1, bytes([6]) + struct.pack(">HH", 100, 555)),
                           tcp_adu(1, 1, bytes([6]) + struct.pack(">HH", 100, 555)))
    assert m["result"] == "ok" and m["addr"] == 100 and m["value"] == 555


# ── capture ring ────────────────────────────────────────────────────────────

class FakeClient:
    """Stands in for a pymodbus sync client: send/recv are instance-shadowable."""
    def __init__(self, reply=b""):
        self.reply = reply
    def send(self, request):
        return len(request)
    def recv(self, size):
        r, self.reply = self.reply, b""
        return r


def _transact(client, tx):
    client.send(tx)
    client.recv(1024)


def test_capture_disabled_by_default():
    tr, c = BusTrace(), FakeClient(tcp_adu(1, 1, RSP_FC3))
    tr.instrument(c, label="dev1", proto="tcp")
    _transact(c, tcp_adu(1, 1, REQ_FC3)); tr.commit(c)
    assert tr.snapshot()["entries"] == [] and tr.captured_total == 0


def test_capture_pairs_and_labels():
    tr, c = BusTrace(), FakeClient(tcp_adu(1, 1, RSP_FC3))
    tr.instrument(c, label="umg512", proto="tcp")
    tr.configure(enabled=True)
    _transact(c, tcp_adu(1, 1, REQ_FC3)); tr.commit(c)
    snap = tr.snapshot()
    assert tr.enabled and len(snap["entries"]) == 1
    e = snap["entries"][0]
    assert e["device"] == "umg512" and e["result"] == "ok" and e["seq"] == 1
    assert e["tx"] == tcp_adu(1, 1, REQ_FC3).hex() and e["latency_ms"] is not None


def test_retry_yields_separate_entries():
    """A second send with no commit in between flushes the first as no_response."""
    tr, c = BusTrace(), FakeClient()
    tr.instrument(c, label="d", proto="tcp")
    tr.configure(enabled=True)
    c.send(tcp_adu(1, 1, REQ_FC3)); c.recv(1024)      # timeout: recv gol
    c.reply = tcp_adu(2, 1, RSP_FC3)
    c.send(tcp_adu(2, 1, REQ_FC3)); c.recv(1024)      # retry reușit
    tr.commit(c)
    ents = tr.snapshot()["entries"]
    assert [e["result"] for e in ents] == ["no_response", "ok"]


def test_chunked_recv_accumulates():
    tr, c = BusTrace(), FakeClient()
    tr.instrument(c, label="d", proto="tcp")
    tr.configure(enabled=True)
    full = tcp_adu(3, 1, RSP_FC3)
    c.send(tcp_adu(3, 1, REQ_FC3))
    c.reply = full[:7]; c.recv(7)                      # întâi headerul MBAP
    c.reply = full[7:]; c.recv(1024)                   # apoi restul
    tr.commit(c)
    assert tr.snapshot()["entries"][0]["rx"] == full.hex()


def test_ring_capacity_and_incremental_read():
    tr, c = BusTrace(capacity=100), FakeClient()
    tr.instrument(c, label="d", proto="tcp")
    tr.configure(enabled=True, capacity=100)
    for i in range(150):
        c.send(tcp_adu(i, 1, REQ_FC3)); tr.commit(c)
    snap = tr.snapshot(after=0, limit=1000)
    assert len(snap["entries"]) == 100 and snap["captured_total"] == 150
    assert snap["entries"][0]["seq"] == 51
    inc = tr.snapshot(after=148, limit=1000)
    assert [e["seq"] for e in inc["entries"]] == [149, 150]


def test_device_filter_and_clear():
    tr = BusTrace()
    a, b = FakeClient(), FakeClient()
    tr.instrument(a, label="a", proto="tcp"); tr.instrument(b, label="b", proto="tcp")
    tr.configure(enabled=True)
    a.send(tcp_adu(1, 1, REQ_FC3)); tr.commit(a)
    b.send(tcp_adu(1, 1, REQ_FC3)); tr.commit(b)
    assert [e["device"] for e in tr.snapshot(device="b")["entries"]] == ["b"]
    tr.configure(clear=True)
    assert tr.snapshot()["entries"] == []


def test_instrument_idempotent():
    tr, c = BusTrace(), FakeClient()
    tr.instrument(c, label="d", proto="tcp")
    first = c.send
    tr.instrument(c, label="d", proto="tcp")
    assert c.send is first


# ── API routes ──────────────────────────────────────────────────────────────

@pytest.fixture
def api_client(tmp_path):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from multibus.api import create_api
    cfg = write_config(tmp_path)
    fake = SimpleNamespace(publish_callback=None)
    app, _ = create_api(cfg, fake, None, None,
                        devices=[(d, fake) for d in cfg.devices])
    return TestClient(app, raise_server_exceptions=False)


@needs_tc
def test_api_roundtrip(api_client):
    from multibus.bus_trace import trace
    trace.configure(enabled=False, clear=True)
    r = api_client.get("/api/bus-trace")
    assert r.status_code == 200 and r.json()["enabled"] is False

    r = api_client.post("/api/bus-trace/config", json={"enabled": True, "capacity": 500})
    assert r.json()["enabled"] is True and r.json()["capacity"] == 500

    r = api_client.post("/api/bus-trace/config", json={"enabled": False, "clear": True})
    assert r.json()["enabled"] is False
    assert api_client.get("/api/bus-trace").json()["entries"] == []


# ── register probe (endianness workbench) ───────────────────────────────────

def test_interpret_words_float_orders():
    from multibus.routes.diagnostics import interpret_words
    # 50.0 float32 big-endian = 0x42480000 → words [0x4248, 0x0000]
    out = interpret_words([0x4248, 0x0000])
    by_type = {i["type"]: i["orders"] for i in out}
    assert by_type["float"]["abcd"] == 50.0
    assert by_type["float"]["cdab"] != 50.0          # word-swap reads other bits
    assert by_type["uint32"]["abcd"] == 0x42480000
    assert by_type["uint32"]["cdab"] == 0x00004248
    assert by_type["uint16"]["abcd"] == 0x4248
    assert by_type["int16"]["abcd"] == 0x4248
    # NaN devine string, nu număr (JSON)
    nan_words = [0x7FC0, 0x0000]
    nan_out = {i["type"]: i["orders"] for i in interpret_words(nan_words)}
    v = nan_out["float"]["abcd"]
    assert isinstance(v, str) and "nan" in v.lower()


def test_interpret_words_counts():
    from multibus.routes.diagnostics import interpret_words
    types1 = {i["type"] for i in interpret_words([1])}
    assert types1 == {"uint16", "int16"}
    types4 = {i["type"] for i in interpret_words([1, 2, 3, 4])}
    assert {"double", "uint64", "int64", "float"} <= types4


@needs_tc
def test_probe_api_on_fake_device(api_client):
    # dispozitivul fake din fixture nu are .connection → 400, nu 500
    r = api_client.post("/api/diagnostics/probe",
                        json={"device": "umg512", "address": 19026})
    assert r.status_code == 400
    r = api_client.post("/api/diagnostics/probe",
                        json={"device": "nope", "address": 0})
    assert r.status_code == 404
    r = api_client.post("/api/diagnostics/probe",
                        json={"device": "umg512", "address": "abc"})
    assert r.status_code == 400
