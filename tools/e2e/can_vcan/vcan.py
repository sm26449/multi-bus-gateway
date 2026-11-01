#!/usr/bin/env python3
# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Make, raise and lower a vcan interface with the standard library only
(rtnetlink + SIOCSIFFLAGS), for the CAN e2e inside a throwaway container
that has no iproute2. Needs CAP_NET_ADMIN in the container's own network
namespace — never run it on a host.

    python vcan.py add vcan0     # create (if missing) and bring up
    python vcan.py down vcan0
    python vcan.py up vcan0
"""
import fcntl
import os
import socket
import struct
import sys

NETLINK_ROUTE = 0
RTM_NEWLINK = 16
NLM_F_REQUEST, NLM_F_ACK, NLM_F_EXCL, NLM_F_CREATE = 0x1, 0x4, 0x200, 0x400
NLMSG_ERROR = 2
IFLA_IFNAME, IFLA_LINKINFO, IFLA_INFO_KIND = 3, 18, 1
SIOCGIFFLAGS, SIOCSIFFLAGS = 0x8913, 0x8914
IFF_UP = 0x1


def _attr(kind: int, data: bytes) -> bytes:
    raw = struct.pack('=HH', 4 + len(data), kind) + data
    return raw + b'\0' * (-len(raw) % 4)


def add(name: str) -> None:
    if os.path.exists(f'/sys/class/net/{name}'):
        return
    body = struct.pack('=BxHiII', socket.AF_UNSPEC, 0, 0, 0, 0)
    body += _attr(IFLA_IFNAME, name.encode() + b'\0')
    body += _attr(IFLA_LINKINFO, _attr(IFLA_INFO_KIND, b'vcan\0'))
    msg = struct.pack('=IHHII', 16 + len(body), RTM_NEWLINK,
                      NLM_F_REQUEST | NLM_F_ACK | NLM_F_CREATE | NLM_F_EXCL, 1, 0) + body
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_ROUTE) as s:
        s.bind((0, 0))
        s.send(msg)
        resp = s.recv(4096)
    _len, mtype = struct.unpack_from('=IH', resp)
    if mtype == NLMSG_ERROR:
        err = struct.unpack_from('=i', resp, 16)[0]
        if err:
            raise OSError(-err, f"creating {name}: {os.strerror(-err)}")


def set_up(name: str, up: bool) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        req = struct.pack('16sH14x', name.encode(), 0)
        flags = struct.unpack('16sH14x', fcntl.ioctl(s.fileno(), SIOCGIFFLAGS, req))[1]
        flags = (flags | IFF_UP) if up else (flags & ~IFF_UP)
        fcntl.ioctl(s.fileno(), SIOCSIFFLAGS, struct.pack('16sH14x', name.encode(), flags))


if __name__ == '__main__':
    cmd, iface = sys.argv[1], sys.argv[2]
    if cmd == 'add':
        add(iface)
        set_up(iface, True)
    elif cmd in ('up', 'down'):
        set_up(iface, cmd == 'up')
    else:
        raise SystemExit(__doc__)
    print(f"{iface}: {cmd}")
