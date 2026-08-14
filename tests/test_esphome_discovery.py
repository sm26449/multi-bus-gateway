# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""ESPHome LAN discovery — the native-API (6053) unicast sweep.

The probe speaks real bytes: these tests run fake ESPHome servers on
loopback sockets (plaintext hello, noise indicator, silent close) so the
frame handling is pinned against actual socket behaviour, not mocks."""
import socket
import threading


from multibus.discovery import _esphome_hello, _pb_fields, scan_esphome

from tests.test_devices_api import make_app, needs_tc


def hello_response(name=b"hall-meter", server=b"ESPHome v2026.5.3 on esp32"):
    """A plaintext HelloResponse frame as the device would send it."""
    payload = (b"\x08\x01"                       # 1: api major = 1
               b"\x10\x0a"                       # 2: api minor = 10
               b"\x1a" + bytes([len(server)]) + server   # 3: server_info
               + b"\x22" + bytes([len(name)]) + name)    # 4: name
    return b"\x00" + bytes([len(payload)]) + b"\x02" + payload


def serve_once(behavior):
    """One-shot loopback server; returns (port, thread)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def run():
        conn, _ = srv.accept()
        try:
            conn.recv(64)                        # the HelloRequest
            behavior(conn)
        finally:
            conn.close()
            srv.close()

    th = threading.Thread(target=run, daemon=True)
    th.start()
    return port, th


def test_pb_fields_walks_hello_response():
    f = _pb_fields(hello_response()[3:])
    assert f[1] == 1 and f[2] == 10
    assert f[3] == b"ESPHome v2026.5.3 on esp32"
    assert f[4] == b"hall-meter"


def test_pb_fields_tolerates_garbage():
    assert _pb_fields(b"") == {}
    assert isinstance(_pb_fields(b"\xff\xff\x01"), dict)   # no crash


def test_hello_plaintext_device():
    port, th = serve_once(lambda c: c.sendall(hello_response()))
    r = _esphome_hello("127.0.0.1", port, timeout=2.0)
    th.join(3)
    assert r["name"] == "hall-meter"
    assert r["api_version"] == "1.10"
    assert "esp32" in r["server_info"]
    assert r["encrypted"] is False


def test_hello_noise_encrypted_device():
    port, th = serve_once(lambda c: c.sendall(b"\x01\x00\x00"))
    r = _esphome_hello("127.0.0.1", port, timeout=2.0)
    th.join(3)
    assert r["encrypted"] is True and r["name"] == ""


def test_hello_connection_dropped_counts_as_protected():
    port, th = serve_once(lambda c: None)        # accept, read, close silently
    r = _esphome_hello("127.0.0.1", port, timeout=2.0)
    th.join(3)
    assert r is not None and r["encrypted"] is True


def test_hello_closed_port_is_none():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                     # nothing listening now
    assert _esphome_hello("127.0.0.1", port, timeout=0.5) is None


def test_scan_esphome_shape():
    port, th = serve_once(lambda c: c.sendall(hello_response(b"node-a")))
    out = scan_esphome(["127.0.0.1"], port=port, timeout=2.0)
    th.join(3)
    assert out["scanned"] == 1
    assert [r["name"] for r in out["results"]] == ["node-a"]
    assert "elapsed_s" in out


# ---------------------------------------------------------------------------
# route: LAN guard + wiring
# ---------------------------------------------------------------------------

@needs_tc
def test_discover_esphome_route(tmp_path, monkeypatch):
    import multibus.discovery as disc
    _, client = make_app(tmp_path)

    seen = {}
    def fake_scan(hosts, port, timeout):
        seen.update(hosts=list(hosts), port=port, timeout=timeout)
        return {"scanned": len(hosts), "results": [], "elapsed_s": 0.1}
    monkeypatch.setattr(disc, "scan_esphome", fake_scan)

    rsp = client.post("/api/discover/esphome",
                      json={"cidr": "192.168.77.0/30", "port": 6053})
    assert rsp.status_code == 200 and rsp.json()["scanned"] == 2
    assert seen["port"] == 6053 and len(seen["hosts"]) == 2

    # public ranges are refused — the appliance cannot scan the internet
    rsp = client.post("/api/discover/esphome", json={"cidr": "8.8.8.0/30"})
    assert rsp.status_code == 422
    # malformed numbers are a clean 422
    rsp = client.post("/api/discover/esphome",
                      json={"cidr": "192.168.77.0/30", "port": "x"})
    assert rsp.status_code == 422


# ---------------------------------------------------------------------------
# LOT B — hardening (2026-07-30 audit)
# ---------------------------------------------------------------------------

def test_pb_fields_handles_long_varint_length():
    """A length-delimited field >=128 bytes uses a multi-byte varint length."""
    long_name = b"x" * 200
    buf = b"\x22" + bytes([0xC8, 0x01]) + long_name    # field 4, len 200 varint
    f = _pb_fields(buf)
    assert f[4] == long_name


def test_hello_partial_header_is_soft(monkeypatch):
    """A peer that dribbles 1 byte then closes must not raise — just be a
    non-ESPHome result, not an aborted scan."""
    port, th = serve_once(lambda c: c.sendall(b"\x00"))   # 1 of 3 header bytes
    r = _esphome_hello("127.0.0.1", port, timeout=2.0)
    th.join(3)
    assert r is not None and r["name"] == ""


def test_scan_soft_fails_one_bad_host(monkeypatch):
    import multibus.discovery as disc
    def boom(h, port, timeout):
        if h == "127.0.0.2":
            raise RuntimeError("kaboom")
        return {"host": h, "port": port, "name": h, "server_info": "",
                "api_version": "", "encrypted": False}
    monkeypatch.setattr(disc, "_esphome_hello", boom)
    out = disc.scan_esphome(["127.0.0.2", "127.0.0.1"], port=6053, timeout=0.1)
    # the good host still comes back; the bad one is dropped, not fatal
    assert [r["host"] for r in out["results"]] == ["127.0.0.1"]


def test_truncated_hello_body_not_parsed():
    """A frame that claims size N but delivers fewer bytes must NOT be decoded
    as a valid device (no false-positive name from a partial body)."""
    def behave(c):
        # header says 40-byte HelloResponse, then only 4 bytes + close
        c.sendall(b"\x00\x28\x02" + b"\x22\x02hi")
    port, th = serve_once(behave)
    r = _esphome_hello("127.0.0.1", port, timeout=2.0)
    th.join(3)
    assert r is not None and r["name"] == ""   # incomplete → no name extracted
