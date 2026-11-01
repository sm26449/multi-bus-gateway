# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Who listens on a bus (ADR-0025).

Listening is a claim on the BUS, not a property of one device: a tap, a
Seplos V2 device in listen mode or a device whose template only listens
holds the bus, because another master owns it there. Every path that would
transmit on a bus — a Test, a unit sweep, a SunSpec walk, a bridge-port
scan, a LAN discovery — asks :func:`listeners` first and refuses when
anyone is named; a save asks :func:`save_conflicts`, the same rule both
ways (a poll onto a listened bus, a listener onto a polled one).

A bus is identified by:

- a serial line: the character device itself (``major:minor``), so a tty,
  its ``/dev/serial/by-id`` link and a udev alias made as a separate node
  are one line; else the canonical path (``os.path.realpath``); a path that
  does not exist (yet) is the stripped string;
- the lower-cased ``host:port``, after a bridge is resolved.
"""
from __future__ import annotations

import os
import stat
from typing import Any, Iterable, List, Optional, Tuple

from .config import (CAN_PROTOCOLS, MODBUS_PROTOCOLS, SERIAL_PROTOCOLS, VIRTUAL_MAP_PROTOCOLS,
                     VIRTUAL_MAP_SERIAL)

# protocols whose bus is a serial line on this host when a serial port is named
_SERIAL_LINE = SERIAL_PROTOCOLS + ('rtu_tap',) + VIRTUAL_MAP_SERIAL
# what has no bus to claim
_NO_BUS = ('http', 'mqtt', 'none') + CAN_PROTOCOLS
# what a standby gateway runs as a tap (device_runtime.TAPPABLE): its partner masters the bus
_TAPPED_ON_STANDBY = ('rtu', 'rtu-tcp')


def _get(conn: Any, key: str, default: Any = '') -> Any:
    if isinstance(conn, dict):
        return conn.get(key, default)
    return getattr(conn, key, default)


def canonical_serial(path: Any) -> str:
    """One serial line, whatever name it is reached by."""
    sp = str(path or '').strip()
    if not sp:
        return ''
    try:
        st = os.stat(sp)
    except (OSError, ValueError):
        return sp
    if stat.S_ISCHR(st.st_mode):
        return f"chr:{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"
    return os.path.realpath(sp)


def bus_of(protocol: str, conn: Any) -> str:
    """The bus a connection (a raw dict or a ModbusConfig) is on, or ''
    for one that has none (HTTP, MQTT, CAN, meter-only)."""
    p = str(protocol or '').lower()
    if p in _NO_BUS:
        return ''
    sp = _get(conn, 'serial_port')
    if p in _SERIAL_LINE and str(sp or '').strip():
        return canonical_serial(sp)
    host = str(_get(conn, 'host') or '').strip().lower()
    if not host:
        return ''
    try:
        port = int(_get(conn, 'port', 0) or 0)
    except (TypeError, ValueError):
        port = 0
    return f"{host}:{port}"


def where_of(protocol: str, conn: Any) -> str:
    """A bus as a person names it: the serial port, or host:port."""
    sp = str(_get(conn, 'serial_port') or '').strip()
    if str(protocol or '').lower() in _SERIAL_LINE and sp:
        return sp
    return f"{_get(conn, 'host')}:{_get(conn, 'port')}"


def _standby() -> bool:
    from . import role
    return not role.is_active()


def _listens(protocol: str, conn: Any, tpl: Any, standby: bool = False) -> bool:
    """A source that only ever hears its bus: a tap, a virtual-map bus in
    listen mode, a register map written for listening (whatever its stored
    protocol says, the runtime starts it listening), or — on a standby
    gateway — a polled RTU source, which it runs as a tap."""
    p = str(protocol or '').lower()
    if p == 'rtu_tap' or (standby and p in _TAPPED_ON_STANDBY):
        return True
    if p in VIRTUAL_MAP_PROTOCOLS and str(_get(conn, 'mode') or 'poll').lower() == 'listen':
        return True
    if tpl is not None and p in MODBUS_PROTOCOLS + VIRTUAL_MAP_PROTOCOLS:
        from .device_template import template_listens
        return template_listens(tpl)
    return False


def _sources(dev) -> List[Tuple[int, str, Any, str]]:
    """(index, protocol, connection, template id) of each enabled source."""
    srcs = list(getattr(dev, 'sources', None) or [])
    if srcs:
        return [(i, s.protocol, s.connection, getattr(s, 'template', '') or dev.template)
                for i, s in enumerate(srcs) if getattr(s, 'enabled', True)]
    return [(0, dev.protocol, dev.connection, dev.template)]


def _tpl(templates, tid):
    return templates.get(tid) if (templates is not None and tid) else None


def source_claims(dev, templates: Optional[Any] = None,
                  standby: bool = False) -> List[Tuple[int, str, bool, str]]:
    """(source index, bus, listens, where) of each enabled source of a
    device that is on a bus."""
    out = []
    for i, proto, conn, tid in _sources(dev):
        bus = bus_of(proto, conn)
        if bus:
            out.append((i, bus, _listens(proto, conn, _tpl(templates, tid), standby), where_of(proto, conn)))
    return out


def _holders(config, bus: str, templates, exclude: Iterable[str], listening: bool,
             standby: bool) -> List[str]:
    if not bus:
        return []
    skip = set(exclude or ())
    out: List[str] = []
    for dev in config.devices:
        if not getattr(dev, 'enabled', False) or dev.id in skip or dev.id in out:
            continue
        if any(b == bus and lis == listening for _i, b, lis, _w in source_claims(dev, templates, standby)):
            out.append(dev.id)
    return out


def listeners(config, bus: str, templates: Optional[Any] = None,
              exclude: Iterable[str] = (), *, standby: Optional[bool] = None) -> List[str]:
    """The enabled devices that listen on ``bus`` (a key from :func:`bus_of`),
    in config order. ``templates`` (the template registry) lets a device
    whose template only listens count too. On a standby gateway a polled
    RTU source counts as listening (``standby`` None = this gateway's role)."""
    return _holders(config, bus, templates, exclude, True, _standby() if standby is None else standby)


def pollers(config, bus: str, templates: Optional[Any] = None,
            exclude: Iterable[str] = ()) -> List[str]:
    """The enabled devices that transmit on ``bus`` (as configured, whatever
    this gateway's role)."""
    return _holders(config, bus, templates, exclude, False, False)


def save_conflicts(config, built: Iterable[Any], templates: Optional[Any] = None,
                   exclude: Iterable[str] = ()) -> List[Tuple[Any, int, str]]:
    """What saving ``built`` (the devices a definition would build) would
    break: (device, source index, why) for a polled source on a bus another
    enabled device only listens to, and for a listening source on a bus
    another enabled device polls. ``exclude``: the ids being saved."""
    out = []
    for dev in built:
        for i, bus, listens, where in source_claims(dev, templates):
            if listens:
                ids = pollers(config, bus, templates, exclude)
                if ids:
                    out.append((dev, i, f"{where} is polled by {', '.join(ids)} — a bus is either polled or "
                                        f"listened to, never both; disable or move "
                                        f"{'it' if len(ids) == 1 else 'them'} first"))
            else:
                ids = listeners(config, bus, templates, exclude, standby=False)
                if ids:
                    out.append((dev, i, f"{where} is only listened to by {', '.join(ids)} — another master "
                                        f"owns that bus, nothing may transmit on it; disable "
                                        f"{'that device' if len(ids) == 1 else 'those devices'} first"))
    return out


def refusal(what: str, where: str, ids: List[str]) -> str:
    """Why ``what`` is not done on a listened bus, in words."""
    who = ', '.join(ids)
    return (f"{what} would transmit on {where}, a bus the gateway only listens to ({who}): another "
            f"master owns it, and a question from here would collide with it. Nothing was sent — "
            f"disable {'that device' if len(ids) == 1 else 'those devices'} first to ask on this bus")


def refusal_detail(what: str, bus: str, ids: List[str], where: str = '') -> dict:
    """The 409 body every refusing route answers with."""
    return {"errors": [refusal(what, where or bus, ids)], "listeners": list(ids), "bus": bus}
