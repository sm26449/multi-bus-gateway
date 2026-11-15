# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The CAN transmit seam (ADR-0037 inc.3): SocketCanTx packs frames correctly, refuses a
listen-only interface, and is the only module besides the executor that may send."""
import socket

import pytest

from multibus.transports.can import can_tx
from multibus.transports.can.can_client import CAN_EFF_FLAG, _CAN_FRAME

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_CAN"), reason="no SocketCAN on this platform")


class FakeSock:
    def __init__(self):
        self.sent, self.bound, self.closed = [], None, False
    def bind(self, addr):
        self.bound = addr
    def send(self, b):
        self.sent.append(b)
    def close(self):
        self.closed = True


@pytest.fixture
def fs(monkeypatch):
    s = FakeSock()
    monkeypatch.setattr(can_tx, "listen_only", lambda ch: False)
    monkeypatch.setattr(socket, "socket", lambda *a, **k: s)
    return s


def test_send_packs_a_standard_frame(fs):
    tx = can_tx.SocketCanTx("can0")
    tx.open()
    assert fs.bound == ("can0",)
    tx.send(0x351, bytes.fromhex("400220030807D001"))
    cid, dlc, data = _CAN_FRAME.unpack(fs.sent[0])
    assert cid == 0x351 and dlc == 8 and data.hex().upper() == "400220030807D001"
    tx.close()
    assert fs.closed


def test_short_data_is_padded_to_8(fs):
    tx = can_tx.SocketCanTx("can0")
    tx.open()
    tx.send(0x35C, b"\x00\x00")
    _cid, dlc, data = _CAN_FRAME.unpack(fs.sent[0])
    assert dlc == 2 and len(data) == 8 and data[2:] == b"\x00" * 6


def test_extended_id_sets_the_eff_flag(fs):
    # HV profiles use 29-bit ids
    tx = can_tx.SocketCanTx("can0")
    tx.open()
    tx.send(0x4210, b"\x01\x02", extended=True)
    cid, _dlc, _data = _CAN_FRAME.unpack(fs.sent[0])
    assert cid & CAN_EFF_FLAG and (cid & 0x1FFFFFFF) == 0x4210


def test_refuses_to_open_a_listen_only_interface(monkeypatch):
    monkeypatch.setattr(can_tx, "listen_only", lambda ch: True)
    with pytest.raises(OSError, match="listen-only"):
        can_tx.SocketCanTx("can0").open()


def test_send_before_open_raises(monkeypatch):
    monkeypatch.setattr(can_tx, "listen_only", lambda ch: False)
    with pytest.raises(OSError, match="not open"):
        can_tx.SocketCanTx("can0").send(0x351, b"\x00")
