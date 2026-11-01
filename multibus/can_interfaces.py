# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The CAN interfaces of this host (ADR-0031 §5; design D9).

An interface is a CAN interface when sysfs says its link type is CAN
(``/sys/class/net/<name>/type`` = 280, ARPHRD_CAN: a controller or a vcan).
Each one comes with whether it is up (``flags``), its ``operstate``, and
from rtnetlink (:func:`can_link.link_info`) its bitrate, listen-only flag and
controller state. The Add form's CAN step reads it for its interface picker
and the listen-only gate. Read only: nothing is opened on a bus.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

from . import can_link

# where /sys is read from: "/" here, a fake tree in the tests
SYSROOT = "/"
ARPHRD_CAN = 280
_IFF_UP = 0x1


def _link_info(name: str) -> Dict[str, Any]:
    return can_link.link_info(name)


def _read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="ascii", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def interfaces(root: Optional[str] = None, info: Optional[Callable[[str], Dict[str, Any]]] = None,
               devices: Optional[Dict[str, List[str]]] = None) -> List[Dict[str, Any]]:
    """``[{name, kind, up, operstate, bitrate, listen_only, state, devices}]``
    sorted by name. ``devices``: interface → the device ids reading it."""
    root = SYSROOT if root is None else root
    info = info or _link_info
    base = os.path.join(root, "sys", "class", "net")
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    out = []
    for name in names:
        d = os.path.join(base, name)
        if _read(os.path.join(d, "type")) != str(ARPHRD_CAN):
            continue
        flags = _read(os.path.join(d, "flags"))
        try:
            up: Optional[bool] = bool(int(flags, 16) & _IFF_UP) if flags else None
        except ValueError:
            up = None
        li = info(name) or {}
        out.append({"name": name, "kind": li.get("kind"), "up": up,
                    "operstate": _read(os.path.join(d, "operstate")),
                    "bitrate": li.get("bitrate"), "listen_only": li.get("listen_only"),
                    "state": li.get("state"), "devices": list((devices or {}).get(name, []))})
    return out


def devices_by_channel(config) -> Dict[str, List[str]]:
    """interface → the CAN devices (any state) that read it."""
    from .config import CAN_PROTOCOLS
    out: Dict[str, List[str]] = {}
    for dev in getattr(config, "devices", None) or []:
        if str(getattr(dev, "protocol", "") or "").lower() not in CAN_PROTOCOLS:
            continue
        can = dict(getattr((getattr(dev, "sources", None) or [None])[0], "can", None) or {})
        out.setdefault(str(can.get("channel") or "can0"), []).append(dev.id)
    return out
