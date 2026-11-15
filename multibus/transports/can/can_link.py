# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""What the kernel says about a CAN interface (rtnetlink, standard library
only): today, whether its controller is in listen-only mode — the second
guarantee beside the driver having no transmit path, set by the host
(``ip link set can0 type can bitrate 500000 listen-only on``).

A query to the kernel on a netlink socket; nothing here touches the bus.
Kept out of can_client.py so that module has no send call of any kind.
"""
from __future__ import annotations

import socket
import struct
from typing import Optional

# rtnetlink, to read the interface's own listen-only flag (no iproute2)
_RTM_GETLINK, _RTM_NEWLINK, _NLM_F_REQUEST = 18, 16, 0x1
_IFLA_LINKINFO, _IFLA_INFO_KIND, _IFLA_INFO_DATA, _IFLA_CAN_CTRLMODE = 18, 1, 2, 5
_CAN_CTRLMODE_LISTENONLY = 0x02


def _rtattrs(buf: bytes, off: int, end: int):
    while off + 4 <= end:
        ln, kind = struct.unpack_from('=HH', buf, off)
        if ln < 4:
            return
        yield kind & 0x7FFF, buf[off + 4:off + ln]
        off += (ln + 3) & ~3


def parse_listen_only(resp: bytes) -> Optional[bool]:
    """An RTM_NEWLINK answer → is the CAN controller in listen-only mode?
    None when the link is not a CAN controller (vcan, a bridge's virtual
    interface) or says nothing about it."""
    if len(resp) < 32 or struct.unpack_from('=H', resp, 4)[0] != _RTM_NEWLINK:
        return None
    end = min(len(resp), struct.unpack_from('=I', resp, 0)[0])
    for kind, data in _rtattrs(resp, 32, end):
        if kind != _IFLA_LINKINFO:
            continue
        for k2, d2 in _rtattrs(data, 0, len(data)):
            if k2 != _IFLA_INFO_DATA:
                continue
            for k3, d3 in _rtattrs(d2, 0, len(d2)):
                if k3 == _IFLA_CAN_CTRLMODE and len(d3) >= 8:
                    return bool(struct.unpack_from('=II', d3)[1] & _CAN_CTRLMODE_LISTENONLY)
    return None


_IFLA_CAN_BITTIMING, _IFLA_CAN_STATE = 1, 4
# enum can_state (linux/can/netlink.h)
CAN_STATES = ('error-active', 'error-warning', 'error-passive', 'bus-off', 'stopped', 'sleeping')


def parse_link_info(resp: bytes) -> dict:
    """An RTM_NEWLINK answer → ``{kind, bitrate, listen_only, state}``: the
    link's kind (``can``, ``vcan``…), and for a CAN controller its bitrate,
    listen-only flag and state (``error-active`` … ``bus-off``). A key the
    answer does not carry is None."""
    out = {'kind': None, 'bitrate': None, 'listen_only': None, 'state': None}
    if len(resp) < 32 or struct.unpack_from('=H', resp, 4)[0] != _RTM_NEWLINK:
        return out
    end = min(len(resp), struct.unpack_from('=I', resp, 0)[0])
    for kind, data in _rtattrs(resp, 32, end):
        if kind != _IFLA_LINKINFO:
            continue
        for k2, d2 in _rtattrs(data, 0, len(data)):
            if k2 == _IFLA_INFO_KIND:
                out['kind'] = d2.split(b'\0', 1)[0].decode('ascii', 'replace') or None
            if k2 != _IFLA_INFO_DATA:
                continue
            for k3, d3 in _rtattrs(d2, 0, len(d2)):
                if k3 == _IFLA_CAN_CTRLMODE and len(d3) >= 8:
                    out['listen_only'] = bool(struct.unpack_from('=II', d3)[1] & _CAN_CTRLMODE_LISTENONLY)
                elif k3 == _IFLA_CAN_BITTIMING and len(d3) >= 4:
                    out['bitrate'] = struct.unpack_from('=I', d3)[0] or None
                elif k3 == _IFLA_CAN_STATE and len(d3) >= 4:
                    st = struct.unpack_from('=I', d3)[0]
                    out['state'] = CAN_STATES[st] if st < len(CAN_STATES) else None
    return out


def _getlink(channel: str) -> Optional[bytes]:
    """The kernel's RTM_NEWLINK answer for one interface, or None."""
    try:
        idx = socket.if_nametoindex(channel)
        req = struct.pack('=BxHiII', 0, 0, idx, 0, 0)
        msg = struct.pack('=IHHII', 16 + len(req), _RTM_GETLINK, _NLM_F_REQUEST, 1, 0) + req
        with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0) as s:
            s.settimeout(1.0)
            s.bind((0, 0))
            s.sendto(msg, (0, 0))      # a kernel query on a netlink socket — not the CAN bus
            return s.recv(65536)
    except (OSError, ValueError, AttributeError, struct.error):
        return None


def listen_only(channel: str) -> Optional[bool]:
    """The interface's listen-only flag as the kernel holds it (the second
    guarantee, set by the host), or None when unknown."""
    resp = _getlink(channel)
    try:
        return parse_listen_only(resp) if resp is not None else None
    except struct.error:
        return None


def link_info(channel: str) -> dict:
    """``{kind, bitrate, listen_only, state}`` of one interface, as the
    kernel holds them (each None when unknown)."""
    resp = _getlink(channel)
    try:
        return parse_link_info(resp) if resp is not None else parse_link_info(b'')
    except struct.error:
        return parse_link_info(b'')


_IFF_UP = 0x1


def link_state(channel: str) -> dict:
    """Whether the interface exists in this process's network namespace and
    whether it is administratively up — an index lookup and sysfs, no socket
    opened. ``up`` is None when sysfs does not say."""
    try:
        socket.if_nametoindex(channel)
    except (OSError, ValueError):
        return {'exists': False, 'up': None}
    try:
        with open(f'/sys/class/net/{channel}/flags') as f:
            return {'exists': True, 'up': bool(int(f.read().strip(), 16) & _IFF_UP)}
    except (OSError, ValueError):
        return {'exists': True, 'up': None}
