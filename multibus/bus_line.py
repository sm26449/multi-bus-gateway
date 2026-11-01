# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Edit bus line… (ADR-0031 §4, design §11.1, L3.6).

A serial bus has one line (baud, parity, data bits, stop bits). A device on
a bus other devices share inherits it and may not change it alone (the
``line_shared`` lock); this changes it for every device on the bus at once.

- :func:`clean` checks the line asked for (all four keys).
- :func:`plan` says what would change: for a serial port of this host, the
  new stored entry of each device on it; for a port of our serial bridge
  (``serial_config: api``), the bridge port to set and the devices to
  restart. A converter, a transparent bridge or a Modbus TCP gateway keep
  their line on the box (refused: "set on the box"); a bus with no serial
  line (Ethernet, CAN) is refused; so is a unit of an installation (its
  installation's connection says the line) and a component.

The route (``routes/buses.py``) holds every device's entry lock and the
device-mutation lock, writes the entries in one config write, sets the
bridge port, and restarts the devices.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

LINE_KEYS = ('baudrate', 'parity', 'bytesize', 'stopbits')
_NET_ONLY = ('tcp', 'udp')


class Refused(Exception):
    def __init__(self, status: int, errors: List[str], fields: Optional[Dict[str, str]] = None, **extra):
        super().__init__('; '.join(errors))
        self.status = status
        self.detail = {'errors': errors, **({'fields': fields} if fields else {}), **extra}


def text(line: Optional[Dict[str, Any]]) -> str:
    if not line:
        return ''
    return f"{line.get('baudrate', line.get('baud'))} {line['bytesize']}{line['parity']}{line['stopbits']}"


def clean(payload: Any) -> Dict[str, Any]:
    """The line asked for, as stored ({baudrate, parity, bytesize, stopbits}),
    or :class:`Refused` (422, each field with its words)."""
    from .template_facts import protocol_hint_errors
    if not isinstance(payload, dict):
        raise Refused(422, ["the body is {baudrate, parity, bytesize, stopbits}"])
    fields: Dict[str, str] = {}
    line: Dict[str, Any] = {}
    for k in LINE_KEYS:
        if k not in payload or payload[k] in (None, ''):
            fields[k] = 'required'
            continue
        v = payload[k]
        if k == 'parity':
            line[k] = str(v).strip().upper()
        else:
            try:
                if isinstance(v, bool) or float(v) != int(float(v)):
                    raise ValueError
                line[k] = int(float(v))
            except (TypeError, ValueError, OverflowError):
                fields[k] = 'an integer'
    if not fields:
        for e in protocol_hint_errors({'line': line}):
            path, _sep, msg = e.partition(': ')
            fields[path.rsplit('.', 1)[-1]] = msg
    if fields:
        raise Refused(422, [f"{k}: {v}" for k, v in fields.items()], fields)
    return line


def find(config, templates, bus_id: str) -> Optional[Dict[str, Any]]:
    from . import buses
    return next((b for b in buses.buses(config, templates) if b['id'] == bus_id), None)


def _set_line(conn: Dict[str, Any], line: Dict[str, Any]) -> bool:
    before = {k: conn.get(k) for k in LINE_KEYS}
    conn.update(line)
    return before != {k: conn.get(k) for k in LINE_KEYS}


def _device_line(dev) -> Optional[Dict[str, Any]]:
    c = getattr(dev, 'connection', None)
    if c is None:
        return None
    return {'baudrate': int(c.baudrate or 9600), 'parity': str(c.parity or 'N').upper(),
            'bytesize': int(c.bytesize or 8), 'stopbits': int(c.stopbits or 1)}


def _members(config, bus: Dict[str, Any]) -> List[Any]:
    out, seen = [], set()
    for d in bus.get('devices') or []:
        if d['id'] in seen:
            continue
        seen.add(d['id'])
        dev = config.get_device(d['id'])
        if dev is not None:
            out.append(dev)
    return out


def plan(config, templates, bus_id: str, line: Dict[str, Any]) -> Dict[str, Any]:
    """What setting ``line`` on bus ``bus_id`` changes (nothing is stored)."""
    from .bridges import TYPES
    from .bus_claims import bus_of
    bus = find(config, templates, bus_id)
    if bus is None:
        raise Refused(404, [f"bus {bus_id!r} not found (GET /api/buses lists them)"])
    kind = bus['kind']
    where = bus.get('where') or bus_id
    protos = set(bus.get('protocols') or [])
    if kind == 'can' or (kind == 'address' and protos and protos <= set(_NET_ONLY)):
        raise Refused(422, [f"{where} has no serial line: its devices are reached over "
                            f"{'CAN' if kind == 'can' else 'Ethernet'}"])
    devs = _members(config, bus)
    out: Dict[str, Any] = {'bus': bus_id, 'kind': kind, 'where': where, 'bridge': None,
                           'line': {'from': text(_from_bus(bus)) or None, 'to': text(line)},
                           'devices': [], 'raws': []}
    if kind in ('address', 'bridge'):
        rec = bus.get('bridge') or {}
        t = TYPES.get(str(rec.get('type') or ''), {}) if kind == 'bridge' else {}
        if kind == 'address' or t.get('serial_config') != 'api':
            box = (f"the bridge '{rec.get('name') or rec.get('id')}'" if kind == 'bridge'
                   else f"the converter at {where}")
            raise Refused(422, [f"{where}: the line is set on the box — {box} keeps its own serial settings, "
                                "the gateway cannot change them"])
        raw_b = config.get_raw_bridge(str(rec.get('id'))) or {}
        entry = next((p for p in raw_b.get('ports') or [] if str(p.get('port')) == str(bus.get('port'))), {})
        out['bridge'] = {'id': rec.get('id'), 'name': rec.get('name') or rec.get('id'), 'port': bus.get('port'),
                         'adapter': entry.get('adapter') or None}
        out['line']['from'] = None                    # the bridge has it; asked only when set
        for dev in devs:
            out['devices'].append({'id': dev.id, 'name': dev.name or dev.id, 'from': None, 'to': text(line),
                                   'changed': True, 'restart': True})
        return out
    # a serial port of this host: every device's own entry carries the line
    refused = []
    for dev in devs:
        if getattr(dev, 'endpoint_id', ''):
            refused.append(f"{dev.id} is a unit of the installation '{dev.endpoint_id}': its line is set on "
                           "the installation's connection — edit it there")
            continue
        if getattr(dev, 'component_of', ''):
            refused.append(f"{dev.id} is a component of '{dev.component_of}' — edit that device")
            continue
        raw = copy.deepcopy(config.get_raw_device(dev.id) or {})
        touched = changed = False
        conns = [raw.get('connection')] + list(raw.get('sources') or [])
        for c in conns:
            if isinstance(c, dict) and bus_of(str(c.get('protocol') or 'tcp'), c) == bus_id:
                touched = True
                changed = _set_line(c, line) or changed
        if not touched:
            refused.append(f"{dev.id}: its entry does not name {where} (it reaches it another way)")
            continue
        out['devices'].append({'id': dev.id, 'name': dev.name or dev.id, 'from': text(_device_line(dev)),
                               'to': text(line), 'changed': changed, 'restart': changed})
        if changed:
            out['raws'].append(raw)
    if refused:
        raise Refused(422, refused)
    if not devs:
        raise Refused(422, [f"{where}: no device is on this bus — a device alone sets its line on its own connection"])
    return out


def _from_bus(bus: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ln = bus.get('line')
    return {'baudrate': ln['baud'], 'parity': ln['parity'], 'bytesize': ln['bytesize'],
            'stopbits': ln['stopbits']} if ln else None


def _from_params(sp: Any) -> Optional[Dict[str, Any]]:
    """A bridge adapter's ``serial_params`` as a stored line, or None."""
    if not isinstance(sp, dict):
        return None
    try:
        return {'baudrate': int(sp['baud']), 'parity': str(sp['parity']).upper(),
                'bytesize': int(sp['databits']), 'stopbits': int(sp['stopbits'])}
    except (KeyError, TypeError, ValueError):
        return None


def bridge_state(config, bridge: Dict[str, Any]) -> Dict[str, Any]:
    """Our bridge's port as the bridge says it: ``{key, line, error}`` —
    the adapter serving the port (recorded, else the one on that TCP port
    now) and its current line. Asked BEFORE any device lock is taken (a
    network call never runs under them); unreachable: the recorded key, the
    line unknown."""
    from . import bridges as bridge_api
    b = config.get_raw_bridge(str(bridge['id'])) or {}
    key = bridge.get('adapter')
    try:
        got = bridge_api.api_get(b, "/adapters")
    except Exception as e:  # noqa: BLE001 — unreachable: said by the caller
        return {'key': key, 'line': None, 'error': str(e)}
    ads = [a for a in got.get('adapters') or [] if isinstance(a, dict)]
    hit = (next((a for a in ads if key and a.get('stable_id') == key), None)
           or next((a for a in ads if str(a.get('tcp_port')) == str(bridge.get('port'))), None))
    return {'key': key or (hit or {}).get('stable_id'), 'line': _from_params((hit or {}).get('serial_params')),
            'error': None}


def with_bridge_line(p: Dict[str, Any], current: Optional[Dict[str, Any]], line: Dict[str, Any]) -> Dict[str, Any]:
    """A bridge plan told the port's current line: nothing changes (and
    nothing restarts) when it is the line asked for."""
    same = current == line
    p = dict(p, line=dict(p['line'], **{'from': text(current) or None}))
    p['devices'] = [dict(d, **{'from': text(current) or None, 'changed': not same, 'restart': not same})
                    for d in p['devices']]
    p['unchanged'] = same
    return p


def current(config, templates, bus_id: str) -> Dict[str, Any]:
    """The line a bus has now, for the Edit bus line dialog: ``{line,
    source}`` — the devices' (``devices``) when they agree, our bridge
    port's (``bridge``), else ``unknown`` (never a guess)."""
    from .bridges import TYPES
    bus = find(config, templates, bus_id)
    if bus is None:
        raise Refused(404, [f"bus {bus_id!r} not found (GET /api/buses lists them)"])
    out = {'bus': bus_id, 'kind': bus['kind'], 'line': None, 'source': 'unknown'}
    if bus['kind'] == 'serial' and bus.get('line'):
        out.update(line=_from_bus(bus), source='devices')
    elif bus['kind'] == 'bridge' and bus.get('bridge'):
        rec = bus['bridge']
        if TYPES.get(str(rec.get('type') or ''), {}).get('serial_config') == 'api':
            raw_b = config.get_raw_bridge(str(rec.get('id'))) or {}
            entry = next((x for x in raw_b.get('ports') or [] if str(x.get('port')) == str(bus.get('port'))), {})
            st = bridge_state(config, {'id': rec.get('id'), 'port': bus.get('port'), 'adapter': entry.get('adapter')})
            if st['line']:
                out.update(line=st['line'], source='bridge')
    return out


def view(p: Dict[str, Any], status: str) -> Dict[str, Any]:
    """What the route answers for a plan (the raws stay inside)."""
    restart = [d['id'] for d in p['devices'] if d['restart']]
    return {'status': status, 'bus': p['bus'], 'kind': p['kind'], 'where': p['where'], 'line': p['line'],
            'bridge': ({k: v for k, v in p['bridge'].items() if k != 'adapter'} if p['bridge'] else None),
            'devices': p['devices'], 'restart': restart}
