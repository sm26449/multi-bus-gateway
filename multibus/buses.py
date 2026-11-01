# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Every bus the gateway knows, as one read model (ADR-0031 §5; design D6).

The Add and Edit forms need, for a bus, all at once: who polls it and who
listens (ADR-0025), the units in use (the next free one), the line its
devices read it at (a new device inherits it), how busy it is, and for a
bridge its framing and the client it reports. :func:`buses` builds that from
the configuration, the bus claims and the measured wire time. Read only.

A bus is keyed by the bus claims' canonical id (:func:`bus_claims.bus_of`):
a serial line by its character device (``chr:major:minor``) or real path,
an address by its lower-cased ``host:port`` (so a converter reached by its
address and a bridge record naming the same port are one bus), and a CAN
interface by ``can:<name>``. Kinds: ``serial`` (a port here), ``bridge``
(a port of a bridge record), ``address`` (any other host:port) and ``can``.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import modbus_client

logger = logging.getLogger(__name__)

BUSY_HIGH, BUSY_FULL = 0.70, 0.95
_FRAMING_OF = {'rtu': 'rtu', 'rtu-tcp': 'rtu', 'rtu_tap': 'rtu', 'ascii': 'ascii', 'ascii-tcp': 'ascii',
               'tcp': 'mbap', 'udp': 'mbap'}
_TYPE_FRAMING = {'rtu': 'rtu', 'ascii': 'ascii', 'modbus_tcp': 'mbap'}


def load_view(occ: Optional[Dict[str, Any]], shares: Optional[List[Optional[float]]] = None,
              devices: int = 0) -> Optional[Dict[str, Any]]:
    """One value per bus, as the bridge cards show it: the wire time of its
    lane(s) over the last minute (``modbus_client.bus_occupancy``), else the
    sum of its devices' own wire shares. Over capacity is a state (turns
    given up, or ≥95 %), never a percentage above 100. None before anything
    was measured."""
    if occ is None:
        got = [s for s in (shares or []) if s is not None]
        if not got:
            return None
        occ = {'share': min(1.0, sum(got)), 'busy_s': None, 'window_s': None, 'missed': 0}
    share = occ['share']
    pct = int(round(100 * share))
    over = occ.get('missed', 0) > 0 or share >= BUSY_FULL
    return {'pct': pct,
            'level': 'full' if over else 'high' if share >= BUSY_HIGH else 'ok',
            'over_capacity': over,
            'busy_s': occ.get('busy_s'), 'window_s': occ.get('window_s'),
            'missed_turns': occ.get('missed', 0),
            'devices': devices,
            'capacity_left_pct': max(0, 100 - pct)}


def _sources(dev) -> List[Tuple[int, str, Any, str]]:
    from .bus_claims import _sources as claim_sources     # one notion of "a device's sources"
    return claim_sources(dev)


def _line(conn) -> Dict[str, Any]:
    return {'baud': int(getattr(conn, 'baudrate', 9600) or 9600),
            'parity': str(getattr(conn, 'parity', 'N') or 'N').upper(),
            'bytesize': int(getattr(conn, 'bytesize', 8) or 8),
            'stopbits': int(getattr(conn, 'stopbits', 1) or 1)}


def _bridge_ports(config) -> Dict[str, Tuple[Dict[str, Any], int]]:
    """host:port → (bridge record, port) of every declared bridge port."""
    out = {}
    for b in getattr(config, 'bridges', None) or []:
        host = str(b.get('host', '') or '').strip().lower()
        for p in b.get('ports') or []:
            try:
                port = int(p.get('port'))
            except (TypeError, ValueError):
                continue
            if host:
                out[f"{host}:{port}"] = (b, port)
    return out


def _bridge_view(b: Dict[str, Any], port: int, diag) -> Dict[str, Any]:
    from .bridges import TYPES
    t = TYPES.get(str(b.get('type') or ''), {})
    out = {'id': b.get('id'), 'name': b.get('name', ''), 'type': b.get('type'),
           'framing': _TYPE_FRAMING.get(t.get('framing', 'rtu'), 'rtu'), 'port': port, 'client': None}
    if diag is not None:
        try:
            v = diag.port_view(b.get('id'), port) or {}
            client = (v.get('stats') or {}).get('client') or {}
            out['client'] = client.get('address') if isinstance(client, dict) else None
        except Exception:  # noqa: BLE001 — a diagnosis never fails the list
            out['client'] = None
    return out


def _new_bus(bid: str, kind: str, where: str) -> Dict[str, Any]:
    return {'id': bid, 'kind': kind, 'where': where, 'path': None, 'host': None, 'port': None,
            'bridge': None, 'protocols': [], 'framing': None, 'devices': [], 'pollers': [],
            'listeners': [], 'units_in_use': [], 'role': None, 'line': None, 'line_conflict': False,
            'lines': [], 'role_conflict': False, 'load': None}


def buses(config, templates=None, *, share_of: Optional[Callable[[str], Optional[float]]] = None,
          diag=None) -> List[Dict[str, Any]]:
    """Every known bus (see the module's words). ``share_of(device id)``: a
    device's measured wire share (its client's ``bus_occupancy``); ``diag``:
    the bridge diagnostics poller (``port_view``), for the client a bridge
    reports."""
    from .bus_claims import _SERIAL_LINE as SERIAL_LINE, bus_of, source_claims
    from .config import CAN_PROTOCOLS
    declared = _bridge_ports(config)
    out: Dict[str, Dict[str, Any]] = {}
    polled_conns: Dict[str, List[Any]] = {}
    polling: Dict[str, List[str]] = {}      # every device that polls a bus, enabled or not
    serial_lines: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}

    def bus_for(bid, kind, where):
        if bid not in out:
            out[bid] = _new_bus(bid, kind, where)
        return out[bid]

    def add(dev) -> None:
            # who sets its line when not itself: its installation, its parent
            owner = {'installation': getattr(dev, 'endpoint_id', '') or None,
                     'component_of': getattr(dev, 'component_of', '') or None}
            enabled = bool(getattr(dev, 'enabled', False))
            try:
                claims = {i: lis for i, _b, lis, _w in source_claims(dev, templates, standby=False)}
            except Exception:  # noqa: BLE001
                claims = {}
            for i, proto, conn, _tid in _sources(dev):
                proto = str(proto or '').lower()
                if proto in CAN_PROTOCOLS:
                    src = (getattr(dev, 'sources', None) or [None])[i] if getattr(dev, 'sources', None) else None
                    ch = str((getattr(src, 'can', None) or {}).get('channel') or 'can0')
                    b = bus_for(f"can:{ch}", 'can', ch)
                    b['protocols'] = sorted(set(b['protocols']) | {proto})
                    b['devices'].append({'id': dev.id, 'name': getattr(dev, 'name', ''), 'enabled': enabled, **owner,
                                         'unit': None, 'protocol': proto, 'role': 'listen-only', 'line': None})
                    if enabled and dev.id not in b['listeners']:
                        b['listeners'].append(dev.id)
                    continue
                bid = bus_of(proto, conn)
                if not bid:
                    continue
                sp = str(getattr(conn, 'serial_port', '') or '').strip()
                serial = bool(sp) and proto in SERIAL_LINE      # bus_of's own rule
                if serial:
                    b = bus_for(bid, 'serial', sp)
                    b['path'] = b['path'] or sp
                else:
                    hit = declared.get(bid)
                    b = bus_for(bid, 'bridge' if hit else 'address', bid)
                    b['host'], b['port'] = str(conn.host), int(conn.port)
                listens = claims.get(i, False)
                b['protocols'] = sorted(set(b['protocols']) | {proto})
                b['framing'] = b['framing'] or _FRAMING_OF.get(proto)
                unit = getattr(conn, 'unit_id', None)
                line = _line(conn) if serial else None
                b['devices'].append({'id': dev.id, 'name': getattr(dev, 'name', ''), 'enabled': enabled, **owner,
                                     'unit': unit, 'protocol': proto,
                                     'role': 'listen-only' if listens else 'master', 'line': line})
                if unit is not None:
                    b['units_in_use'].append({'unit': unit, 'device': dev.id})
                if serial:
                    serial_lines.setdefault(bid, []).append((dev.id, line))
                if enabled:
                    lst = b['listeners'] if listens else b['pollers']
                    if dev.id not in lst:
                        lst.append(dev.id)
                    if not listens:
                        polled_conns.setdefault(bid, []).append((dev.id, conn))
                if not listens and dev.id not in polling.setdefault(bid, []):
                    polling[bid].append(dev.id)
    for dev in getattr(config, 'devices', None) or []:
        try:
            add(dev)
        except Exception:  # noqa: BLE001 — one odd device never hides the buses
            logger.warning("bus list: device %s skipped", getattr(dev, 'id', '?'), exc_info=True)
    # a declared bridge port is a known bus, devices or not
    for bid, (br, port) in declared.items():
        b = bus_for(bid, 'bridge', bid)
        b['kind'] = 'bridge'
        b['host'], b['port'] = str(br.get('host', '')), port
        b['bridge'] = _bridge_view(br, port, diag)
        b['framing'] = b['bridge']['framing']
    for bid, b in out.items():
        b['units_in_use'].sort(key=lambda u: (u['unit'] is None, u['unit']))
        b['role'] = 'listen-only' if b['listeners'] else 'master' if b['pollers'] else None
        # polled and listened at once: a save refuses that now, a stored config may hold it
        b['role_conflict'] = bool(b['listeners'] and b['pollers'])
        lines = serial_lines.get(bid) or []
        distinct: List[Dict[str, Any]] = []
        for did, ln in lines:
            hit = next((x for x in distinct if x['line'] == ln), None)
            if hit is None:
                distinct.append({'line': ln, 'devices': [did]})
            else:
                hit['devices'].append(did)
        if len(distinct) == 1:
            b['line'] = distinct[0]['line']
        elif len(distinct) > 1:
            b['line_conflict'], b['lines'] = True, distinct
        if b['kind'] != 'can':
            b['load'] = _load(polled_conns.get(bid, []), share_of, len(polling.get(bid, [])))
    return list(out.values())


def _load(polled: List[Tuple[str, Any]], share_of, devices: int) -> Optional[Dict[str, Any]]:
    """The load of a bus from its enabled pollers' lanes (else their own
    shares); ``devices`` is the bridge cards' count: every device that polls
    the bus, enabled or not."""
    occ = None
    for _did, conn in polled:
        host = getattr(conn, 'host', None)
        if host:
            occ = modbus_client.bus_occupancy(host, int(conn.port))
            break
    shares = [share_of(did) for did, _c in polled] if (occ is None and share_of) else []
    return load_view(occ, shares, devices=devices)
