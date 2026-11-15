# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The CAN TRANSMIT seam (ADR-0037).

``can_client.py`` is receive-only by construction and says so: *any* transmission — the Virtual
BMS, a request frame — is "a separate ADR with its own safety review". This is that module. It
is the **one place** MBG sends a CAN frame, and it exists only to serve the Virtual BMS executor,
which calls it **behind an arm gate** (``multibus/outputs/vbms.py``).

Two safety properties live here:
* it **refuses to open on a listen-only interface** — you cannot transmit where the host has set
  listen-only, so an un-prepared bench can't be driven by accident;
* it only sends the exact frames handed to it; it builds nothing (``can_encode`` does that, purely).
"""
from __future__ import annotations

import logging
import socket
from typing import Optional

from .can_client import CAN_EFF_FLAG, _CAN_FRAME
from .can_link import listen_only

logger = logging.getLogger(__name__)


class SocketCanTx:
    """Transmit raw CAN frames on one SocketCAN interface. Open → send* → close."""

    def __init__(self, channel: str):
        self.channel = channel
        self._sock: Optional[socket.socket] = None

    def open(self) -> None:
        if not hasattr(socket, "AF_CAN"):
            raise OSError("this platform has no SocketCAN (AF_CAN)")
        lo = listen_only(self.channel)
        if lo:                                   # True = the host has it in listen-only
            raise OSError(f"{self.channel} is listen-only — refusing to open a CAN transmitter "
                          f"(take it out of listen-only on the host first)")
        s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        try:
            s.bind((self.channel,))
        except OSError:
            s.close()
            raise
        self._sock = s
        logger.warning("CAN transmitter open on %s (Virtual BMS)", self.channel)

    def send(self, can_id: int, data: bytes, extended: bool = False) -> None:
        if self._sock is None:
            raise OSError("CAN transmitter not open")
        cid = (can_id & 0x1FFFFFFF) | CAN_EFF_FLAG if extended else (can_id & 0x7FF)
        payload = bytes(data)[:8]
        self._sock.send(_CAN_FRAME.pack(cid, len(payload), payload.ljust(8, b"\x00")))

    def close(self) -> None:
        s, self._sock = self._sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass
            logger.info("CAN transmitter closed on %s", self.channel)
