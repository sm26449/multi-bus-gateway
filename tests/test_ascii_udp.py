# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Modbus ASCII (on a serial port, or tunnelled through TCP) and Modbus over
UDP (3.91): read end to end through the same connection code as RTU/TCP,
validated like them, and decoded by the bus monitor."""
import os
import socket
import struct
import threading
import time

import pytest

from multibus.bus_trace import decode_transaction
from multibus.config import ModbusConfig
from multibus.modbus_client import _TRANSPORTS, ModbusConnection
from tests.test_devices_api import make_app, needs_tc

REGS = {0: 0x1234, 1: 0x5678}


def _lrc(b: bytes) -> int:
    return (-sum(b)) & 0xFF


def _answer_pdu(unit: int, pdu: bytes) -> bytes:
    """An FC3 answer from the REGS table."""
    fc, addr, count = pdu[0], *struct.unpack(">HH", pdu[1:5])
    data = b"".join(struct.pack(">H", REGS.get(addr + i, 0)) for i in range(count))
    return bytes([unit, fc, len(data)]) + data


def _ascii_frame(raw: bytes) -> bytes:
    return b":" + (raw + bytes([_lrc(raw)])).hex().upper().encode() + b"\r\n"


def _ascii_reply(buf: bytes):
    """One whole ':…CRLF' request in buf → (reply, rest), else (None, buf)."""
    i, j = buf.find(b":"), buf.find(b"\r\n")
    if i < 0 or j < 0:
        return None, buf
    raw = bytes.fromhex(buf[i + 1:j].decode())
    unit, pdu = raw[0], raw[1:-1]
    return _ascii_frame(_answer_pdu(unit, pdu)), buf[j + 2:]


def _read(cfg: ModbusConfig):
    _TRANSPORTS.clear()
    c = ModbusConnection(cfg, trace_label="t")
    try:
        return c.read_registers(0, 2, "holding")
    finally:
        c.disconnect()
        _TRANSPORTS.clear()


def test_udp_reads_through_the_same_connection_code():
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    stop = threading.Event()

    def serve():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                req, peer = srv.recvfrom(256)
            except socket.timeout:
                continue
            tid, _p, _l, unit = struct.unpack(">HHHB", req[:7])
            body = _answer_pdu(unit, req[7:])[1:]
            srv.sendto(struct.pack(">HHHB", tid, 0, len(body) + 1, unit) + body, peer)
    threading.Thread(target=serve, daemon=True).start()
    try:
        words = _read(ModbusConfig(host="127.0.0.1", port=port, unit_id=1, timeout=2, retry_attempts=1,
                                   retry_delay=0, protocol="udp"))
        assert words == [0x1234, 0x5678]
    finally:
        stop.set()
        srv.close()


def test_ascii_through_a_tcp_socket_reads():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        c, _ = srv.accept()
        buf = b""
        with c:
            c.settimeout(3)
            try:
                while True:
                    chunk = c.recv(256)
                    if not chunk:
                        return
                    buf += chunk
                    out, buf = _ascii_reply(buf)
                    if out:
                        c.sendall(out)
            except OSError:
                return
    threading.Thread(target=serve, daemon=True).start()
    try:
        words = _read(ModbusConfig(host="127.0.0.1", port=port, unit_id=7, timeout=2, retry_attempts=1,
                                   retry_delay=0, protocol="ascii-tcp"))
        assert words == [0x1234, 0x5678]
    finally:
        srv.close()


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs a pty")
def test_ascii_on_a_serial_port_reads():
    import tty
    master, slave = os.openpty()
    tty.setraw(slave)
    path = os.ttyname(slave)
    stop = threading.Event()

    def serve():
        buf = b""
        while not stop.is_set():
            try:
                chunk = os.read(master, 256)
            except OSError:
                return
            buf += chunk
            out, buf = _ascii_reply(buf)
            if out:
                os.write(master, out)
    threading.Thread(target=serve, daemon=True).start()
    try:
        words = _read(ModbusConfig(serial_port=path, unit_id=2, timeout=2, retry_attempts=1,
                                   retry_delay=0, protocol="ascii", baudrate=9600))
        assert words == [0x1234, 0x5678]
    finally:
        stop.set()
        os.close(master)
        os.close(slave)


def test_the_monitor_decodes_ascii_frames_and_checks_the_lrc():
    req = _ascii_frame(bytes([1, 3, 0, 0, 0, 2]))
    rsp = _ascii_frame(_answer_pdu(1, bytes([3, 0, 0, 0, 2])))
    m = decode_transaction("ascii", req, rsp)
    assert (m["result"], m["unit"], m["fc"], m["addr"], m["count"]) == ("ok", 1, 3, 0, 2)
    bad = rsp[:-4] + b"00\r\n"                                  # wrong LRC
    assert decode_transaction("ascii", req, bad)["result"] == "crc_error"


@needs_tc
def test_ascii_and_udp_devices_are_validated_like_their_kin(tmp_path):
    cfg, client = make_app(tmp_path)
    dev = lambda did, conn: {"id": did, "template": "fronius_smart_meter_65a", "enabled": False,  # noqa: E731
                             "connection": conn}
    r = client.post("/api/devices", json=dev("a1", {"protocol": "ascii", "serial_port": "/dev/ttyA", "unit_id": 1}))
    assert r.status_code == 200, r.text
    assert next(d for d in cfg.devices if d.id == "a1").protocol == "ascii"
    assert client.post("/api/devices", json=dev("a2", {"protocol": "ascii", "serial_port": "/dev/ttyA",
                                                       "unit_id": 2})).status_code == 200   # a second slave
    r = client.post("/api/devices", json=dev("r1", {"protocol": "rtu", "serial_port": "/dev/ttyA", "unit_id": 3}))
    assert r.status_code == 422 and "one framing" in r.text                                 # RTU on an ASCII line
    r = client.post("/api/devices", json=dev("a3", {"protocol": "ascii", "serial_port": "/dev/ttyA", "unit_id": 1}))
    assert r.status_code == 422 and "unit 1" in r.text
    r = client.post("/api/devices", json=dev("u0", {"protocol": "udp", "unit_id": 1}))
    assert r.status_code == 422 and "host: required for Modbus UDP" in r.text
    assert client.post("/api/devices", json=dev("u1", {"protocol": "udp", "host": "192.0.2.5", "port": 502,
                                                       "unit_id": 1})).status_code == 200
    # TCP to the same host:port is another conversation, not a clash
    assert client.post("/api/devices", json=dev("t1", {"protocol": "tcp", "host": "192.0.2.5", "port": 502,
                                                       "unit_id": 1})).status_code == 200
    # ASCII through a bridge: a kind of bridge that declares ascii framing
    from multibus import bridges
    t = dict(bridges.TYPES["rtu_transparent"], id="ascii_conv", framing="ascii")
    bridges.TYPES["ascii_conv"] = t
    try:
        assert client.post("/api/bridges", json={"id": "asc", "name": "ASCII box", "type": "ascii_conv",
                                                 "host": "192.0.2.70", "ports": [{"port": 4196}]}).status_code == 200
        r = client.post("/api/devices", json=dev("b1", {"bridge": "asc", "bridge_port": 4196, "unit_id": 4}))
        assert r.status_code == 200, r.text
        assert next(d for d in cfg.devices if d.id == "b1").protocol == "ascii-tcp"
    finally:
        bridges.TYPES.pop("ascii_conv", None)


def test_udp_and_tcp_to_one_host_do_not_share_a_socket():
    _TRANSPORTS.clear()
    u = ModbusConnection(ModbusConfig(host="192.0.2.9", port=502, unit_id=1, protocol="udp"))
    t = ModbusConnection(ModbusConfig(host="192.0.2.9", port=502, unit_id=2, protocol="tcp"))
    a = ModbusConnection(ModbusConfig(serial_port="/dev/ttyQ", unit_id=1, protocol="ascii"))
    try:
        assert u._tp is not t._tp
        assert a.bus == "/dev/ttyQ" and u.bus == "192.0.2.9:502"
    finally:
        _TRANSPORTS.clear()
    time.sleep(0)
