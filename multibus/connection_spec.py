# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""What a stored connection means, read out on its axes (ADR-0026).

A stored ``connection.protocol`` packs four things into one string: the port
the device has, how the gateway reaches it, the language it speaks and
whether the gateway asks or only listens (FINDINGS ADD-3). This module reads
them out — it never changes what is stored:

- ``spec_of(conn, bridges, bridge_types)`` → ``{port, reach, protocol,
  framing, udp, mode, writable, dangling}`` plus two private keys that make
  the read exact: ``params`` (every stored key the axes do not say, as
  stored) and ``implicit`` (axis keys the stored form leaves to their
  default — a ``protocol`` left out is Modbus TCP, a Seplos ``mode`` left
  out is poll);
- ``conn_of(spec)`` → the legacy dict back. ``conn_of(spec_of(x)) == x``
  for every stored shape. It is the only place outside the runtime that
  knows the stored strings;
- ``MATRIX[context]`` → the valid cells per editor (device, source,
  endpoint, group), each marked ``today``, ``new`` or ``not_yet`` with the
  legacy connection stub it is saved as. It never widens what the API
  accepts;
- ``template_speaks(tpl)`` → the protocol a template's map is read by,
  derived from ``protocol.transports`` (no new template key);
- ``edit_errors(old, new, device)`` → the edit locks, in one place.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

PORTS = ('serial', 'ethernet', 'can', 'http', 'mqtt')     # 'unknown': a stored string nothing reads
REACHES = ('here', 'bridge', 'address', 'socketcan')
PROTOCOLS = ('modbus', 'seplos_v2', 'can_signals', 'json_http', 'json_mqtt')
FRAMINGS = ('rtu', 'ascii', 'mbap')
MODES = ('poll', 'listen')
# what the API shows of a spec (the private keys make the round trip exact)
PUBLIC_KEYS = ('port', 'reach', 'protocol', 'framing', 'udp', 'mode', 'writable', 'dangling')

# a bridge type's framing → the spec's framing
_TYPE_FRAMING = {'rtu': 'rtu', 'ascii': 'ascii', 'modbus_tcp': 'mbap'}

LABELS = {
    'port': {'serial': 'RS-485 / serial', 'ethernet': 'Ethernet / Wi-Fi', 'can': 'CAN',
             'http': 'Web API (HTTP)', 'mqtt': 'MQTT'},
    'reach': {'here': 'A port on this host', 'bridge': 'Through a bridge',
              'address': 'At a network address', 'socketcan': 'SocketCAN interface'},
    'protocol': {'modbus': 'Modbus', 'seplos_v2': 'Seplos V2', 'can_signals': 'CAN signals',
                 'json_http': 'HTTP / JSON', 'json_mqtt': 'MQTT / JSON'},
    'framing': {'rtu': 'RTU', 'ascii': 'ASCII', 'mbap': 'Modbus TCP (MBAP)'},
    # the role on the bus (L3-F14): the gateway polls, or another master
    # polls and the gateway is a passive monitor (never 'slave': it answers nothing)
    'mode': {'poll': 'Master', 'listen': 'Listen-only'},
}


def _axes(port=None, reach=None, protocol=None, framing=None, udp=False, mode=None,
          dangling=False) -> Dict[str, Any]:
    writable = (protocol == 'modbus' and mode == 'poll') or protocol == 'json_mqtt'
    return {'port': port, 'reach': reach, 'protocol': protocol, 'framing': framing,
            'udp': bool(udp), 'mode': mode, 'writable': writable, 'dangling': bool(dangling)}


def _stub(axes: Dict[str, Any]) -> Dict[str, Any]:
    """The legacy keys the axes alone say — what a form sends for that cell
    (everything else — host, unit, line, the bridge's id — is the device's
    own). The only place that knows the stored strings."""
    port, reach, proto = axes.get('port'), axes.get('reach'), axes.get('protocol')
    framing, mode = axes.get('framing'), axes.get('mode')
    if port is None and proto is None:
        return {'protocol': 'none'}                # measured by its meters (not a connection)
    if proto == 'modbus':
        if reach == 'bridge':
            # the bridge's type says the framing; only listening is stored
            return {'protocol': 'rtu_tap'} if mode == 'listen' else {}
        if mode == 'listen':
            return {'protocol': 'rtu_tap'}
        if framing == 'mbap':
            return {'protocol': 'udp' if axes.get('udp') else 'tcp'}
        base = 'ascii' if framing == 'ascii' else 'rtu'
        return {'protocol': base if reach == 'here' else f"{base}-tcp"}
    if proto == 'seplos_v2':
        return {'protocol': 'seplos_v2' if reach == 'here' else 'seplos_v2-tcp',
                'mode': mode or 'poll'}
    if proto == 'can_signals':
        return {'protocol': 'can'}
    if proto == 'json_http':
        return {'protocol': 'http'}
    if proto == 'json_mqtt':
        return {'protocol': 'mqtt'}
    return {}


def _read_axes(conn: Dict[str, Any], bridges, types) -> Dict[str, Any]:
    p = str(conn.get('protocol', 'tcp') if conn.get('protocol') is not None else 'tcp').strip().lower()
    if conn.get('bridge'):
        b = next((x for x in bridges or [] if isinstance(x, dict) and x.get('id') == conn.get('bridge')), None)
        listen = p == 'rtu_tap'
        if b is None:
            # the stored keys, as stored: nothing resolves them
            return _axes('serial', 'bridge', 'modbus', None, False, 'listen' if listen else 'poll', dangling=True)
        t = (types or {}).get(str(b.get('type') or ''), {})
        framing = _TYPE_FRAMING.get(t.get('framing', 'rtu'), 'rtu')
        # a tap stays a tap whatever the bridge frames (ADD-7): the spec says
        # listen; a framing other than rtu makes it no matrix cell
        return _axes('ethernet' if framing == 'mbap' else 'serial', 'bridge', 'modbus', framing,
                     False, 'listen' if listen else 'poll')
    if p in ('tcp', 'udp'):
        return _axes('ethernet', 'address', 'modbus', 'mbap', p == 'udp', 'poll')
    if p in ('rtu', 'ascii'):
        return _axes('serial', 'here', 'modbus', p, False, 'poll')
    if p in ('rtu-tcp', 'ascii-tcp'):
        return _axes('serial', 'address', 'modbus', p.split('-')[0], False, 'poll')
    if p == 'rtu_tap':
        # the runtime prefers the serial port when both are there (rtu_tap.py)
        here = bool(str(conn.get('serial_port', '') or '').strip())
        return _axes('serial', 'here' if here else 'address', 'modbus', 'rtu', False, 'listen')
    if p in ('seplos_v2', 'seplos_v2-tcp'):
        mode = str(conn.get('mode') or '').strip().lower() or 'poll'
        return _axes('serial', 'here' if p == 'seplos_v2' else 'address', 'seplos_v2', None, False, mode)
    if p == 'can':
        carrier = str(conn.get('carrier') or 'socketcan').strip().lower()
        return _axes('can', carrier, 'can_signals', None, False, 'listen')
    if p == 'http':
        return _axes('http', None, 'json_http', None, False, 'poll')
    if p == 'mqtt':
        return _axes('mqtt', None, 'json_mqtt', None, False, 'listen')
    if p == 'none':
        return _axes()                              # measured by its meters: not a connection
    # a string nothing reads: never taken for meter-only (review)
    return _axes('unknown')


def spec_of(conn: Optional[Dict[str, Any]], bridges: Optional[List[Dict]] = None,
            bridge_types: Optional[Dict[str, Dict]] = None) -> Dict[str, Any]:
    """A stored connection read out on its axes, with its bridge resolved
    the way ``bridges.resolve_connection`` resolves it. Read only."""
    if bridge_types is None:
        from .bridges import TYPES as bridge_types
    conn = dict(conn or {})
    axes = _read_axes(conn, bridges, bridge_types)
    stub = _stub(axes)
    if axes['protocol'] == 'can_signals' and axes['reach'] != 'socketcan':
        stub = dict(stub, carrier=axes['reach'])
    # exactness: every stored key the stub does not say as stored is a
    # parameter; a stub key the stored form left out is implicit
    params = {k: v for k, v in conn.items() if k not in stub or stub[k] != v}
    implicit = sorted(k for k in stub if k not in conn)
    return {**axes, 'params': params, 'implicit': implicit}


def conn_of(spec: Dict[str, Any]) -> Dict[str, Any]:
    """The legacy connection a spec is stored as (inverse of ``spec_of``)."""
    stub = _stub(spec)
    if spec.get('protocol') == 'can_signals' and spec.get('reach') not in (None, 'socketcan'):
        stub = dict(stub, carrier=spec['reach'])
    implicit = set(spec.get('implicit') or ())
    out = {k: v for k, v in stub.items() if k not in implicit}
    out.update(spec.get('params') or {})
    return out


def public(spec: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """What the API shows: the axes, never the private keys."""
    if spec is None:
        return None
    return {k: spec.get(k) for k in PUBLIC_KEYS}


def drop_spec_keys(payload):
    """A device payload without any spec in it (ADR-0026 §1): beside the
    connection (`connection_spec`, `source_specs`), inside it
    (`connection.spec`) and inside a source (`sources[].spec`). A copy; the
    payload given is untouched."""
    if not isinstance(payload, dict):
        return payload
    out = {k: v for k, v in payload.items() if k not in ('connection_spec', 'source_specs')}
    if isinstance(out.get('connection'), dict) and 'spec' in out['connection']:
        out['connection'] = {k: v for k, v in out['connection'].items() if k != 'spec'}
    if isinstance(out.get('sources'), list):
        out['sources'] = [{k: v for k, v in s.items() if k != 'spec'} if isinstance(s, dict) else s
                          for s in out['sources']]
    return out


def device_specs(raw: Dict[str, Any], bridges, bridge_types=None) -> Tuple[Dict, List[Dict]]:
    """A device's spec, and one per source when it has several. The device's
    spec is its ``connection``'s, else its first source's (as
    ``DeviceConfig.protocol`` is)."""
    srcs = [s for s in (raw.get('sources') or []) if isinstance(s, dict)]
    conn = raw.get('connection')
    if not conn and srcs:
        conn = srcs[0]
    dev = spec_of(conn or {}, bridges, bridge_types)
    per = ([{'id': str(s.get('id', '') or f'source{i + 1}'), **spec_of(s, bridges, bridge_types)}
            for i, s in enumerate(srcs)] if len(srcs) > 1 else [])
    return dev, per


# ── the matrix ───────────────────────────────────────────────────────────
def _cell(status, port, reach, protocol, framing=None, mode='poll', udp=False, note=''):
    axes = _axes(port, reach, protocol, framing, udp, mode)
    stub = _stub(axes)
    if protocol == 'can_signals' and reach != 'socketcan':
        stub = dict(stub, carrier=reach)
    cell = {**{k: axes[k] for k in PUBLIC_KEYS if k != 'dangling'}, 'status': status, 'connection': stub}
    if note:
        cell['note'] = note
    return cell


_DEVICE_CELLS = [
    _cell('today', 'serial', 'here', 'modbus', 'rtu'),
    _cell('today', 'serial', 'here', 'modbus', 'ascii'),
    _cell('today', 'serial', 'here', 'modbus', 'rtu', 'listen'),
    _cell('today', 'serial', 'address', 'modbus', 'rtu'),
    _cell('today', 'serial', 'address', 'modbus', 'ascii'),
    _cell('new', 'serial', 'address', 'modbus', 'rtu', 'listen',
          note='the runtime can listen at a host:port; a device save asks for a serial port or a bridge'),
    # a bridge's type sets the framing; the stub is the bridge reference
    _cell('today', 'serial', 'bridge', 'modbus', 'rtu'),
    _cell('today', 'serial', 'bridge', 'modbus', 'ascii'),
    _cell('today', 'serial', 'bridge', 'modbus', 'rtu', 'listen'),
    _cell('today', 'serial', 'here', 'seplos_v2', None, 'poll'),
    _cell('today', 'serial', 'here', 'seplos_v2', None, 'listen'),
    _cell('today', 'serial', 'address', 'seplos_v2', None, 'poll'),
    _cell('today', 'serial', 'address', 'seplos_v2', None, 'listen'),
    _cell('not_yet', 'serial', 'bridge', 'seplos_v2', None, 'poll',
          note='a Seplos V2 bus is reached by its serial port or a bridge\'s host and port'),
    _cell('not_yet', 'serial', 'bridge', 'seplos_v2', None, 'listen',
          note='a Seplos V2 bus is reached by its serial port or a bridge\'s host and port'),
    _cell('today', 'ethernet', 'address', 'modbus', 'mbap'),
    _cell('today', 'ethernet', 'address', 'modbus', 'mbap', udp=True),
    _cell('today', 'ethernet', 'bridge', 'modbus', 'mbap'),
    _cell('today', 'can', 'socketcan', 'can_signals', None, 'listen'),
    _cell('not_yet', 'can', 'bridge', 'can_signals', None, 'listen',
          note='CAN over the serial bridge is not available yet'),
    _cell('today', 'http', None, 'json_http', None, 'poll'),
    _cell('today', 'mqtt', None, 'json_mqtt', None, 'listen'),
]


def _subset(stored_ok, extra=()):
    """The device cells whose stored form a context accepts today (never a
    bridge record: the installation editors take an address), plus its own."""
    out = []
    for c in _DEVICE_CELLS:
        if c['status'] != 'today' or c['reach'] == 'bridge':
            continue
        if c['connection'].get('protocol') in stored_ok:
            out.append(dict(c, connection=dict(c['connection'])))
    return out + list(extra)


def _build_matrix() -> Dict[str, List[Dict]]:
    from .config import NETWORK_PROTOCOLS, SERIAL_PROTOCOLS
    polled = NETWORK_PROTOCOLS + SERIAL_PROTOCOLS
    return {
        'device': [dict(c, connection=dict(c['connection'])) for c in _DEVICE_CELLS],
        'source': _subset(polled + ('http', 'mqtt')),
        # a serial line here is read one device per slave; an installation
        # reaches its units through a network master, or listens
        'endpoint': _subset(NETWORK_PROTOCOLS + ('rtu_tap',)),
        'group': _subset(NETWORK_PROTOCOLS + ('rtu_tap', 'http'),
                         extra=[_cell('today', 'serial', 'address', 'modbus', 'rtu', 'listen')]),
    }


MATRIX = _build_matrix()


def matrix_cell(context: str, spec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The cell of ``context`` a spec sits in, or None (off the matrix)."""
    keys = ('port', 'reach', 'protocol', 'framing', 'udp', 'mode')
    for c in MATRIX.get(context, []):
        if all(c.get(k) == spec.get(k) for k in keys):
            return c
    return None


# ── templates ────────────────────────────────────────────────────────────
_FAMILY_SPEAKS = {'modbus': 'modbus', 'seplos_v2': 'seplos_v2', 'can': 'can_signals',
                  'http': 'json_http', 'mqtt': 'json_mqtt'}
_SPEAKS_FAMILY = {v: k for k, v in _FAMILY_SPEAKS.items()}


def template_speaks(tpl) -> Dict[str, Any]:
    """The protocol a template's map is read by, and whether it only ever
    listens — derived from ``protocol.transports`` (``transports`` stays
    the stored key: the editor and older gateways read only it)."""
    from .device_template import template_listens, template_transport
    fam = template_transport(tpl)
    return {'protocol': _FAMILY_SPEAKS.get(fam, fam), 'listen_only': bool(template_listens(tpl))}


def family_of(spec: Optional[Dict[str, Any]]) -> Optional[str]:
    """The template family a spec reads (``config.protocol_family``'s word)."""
    if spec is None:
        return None
    if spec.get('protocol') is None:
        return 'none' if spec.get('port') is None else None
    return _SPEAKS_FAMILY.get(spec['protocol'])


# ── edit locks (ADR-0026 §2) ─────────────────────────────────────────────
_CLASS_WORDS = {'modbus': 'Modbus', 'seplos_v2': 'Seplos V2', 'can_signals': 'CAN',
                'json_http': 'HTTP/JSON', 'json_mqtt': 'MQTT', None: 'nothing (measured by its meters)'}

PRIMARY_TEXT = "the primary device (UMG512) is Modbus TCP"
METER_ONLY_TEXT = ("connection.protocol: fixed after creation — a device measured only by its "
                   "meters, or one read through a connection, is created as such")


def _is_meter_only(spec) -> bool:
    """Only `protocol: none` (port and protocol null) — never an unknown string."""
    return spec is not None and spec.get('protocol') is None and spec.get('port') is None


def edit_errors(old: Optional[Dict[str, Any]], new: Dict[str, Any],
                device: Optional[Dict[str, Any]] = None) -> List[Tuple[str, str]]:
    """The edit locks, each a ``(field, error)`` — the error worded as the
    save has always worded it. ``old`` is the stored spec (None on create).
    ``device``: ``primary`` (bool), ``components`` (how many it holds),
    ``template_changed`` (bool), ``template`` (id) with ``template_family``
    (its map's family, None = nothing to check) and ``protocol`` (the
    resolved legacy string, for the words).

    The primary and the meter-only rules answer alone (a save refused for
    them is not judged further), as before; a device holding components is
    told that, not that its class is fixed."""
    d = device or {}
    if d.get('primary'):
        if not (new.get('port') == 'ethernet' and new.get('reach') == 'address'
                and new.get('protocol') == 'modbus' and not new.get('udp')):
            return [('connection.protocol', PRIMARY_TEXT)]
        return []
    if old is not None and _is_meter_only(old) != _is_meter_only(new):
        return [('connection.protocol', METER_ONLY_TEXT)]
    out: List[Tuple[str, str]] = []
    n = int(d.get('components') or 0)
    if n and (d.get('template_changed') or new.get('protocol') != 'json_mqtt'):
        did = d.get('id', '')
        out.append(('connection.protocol' if new.get('protocol') != 'json_mqtt' else 'template',
                    f"'{did}' has {n} component(s): its template and protocol stay "
                    "while they exist — remove them first"))
    if (not out and old is not None and not _is_meter_only(old) and old.get('protocol') is not None
            and new.get('protocol') is not None and old['protocol'] != new['protocol']):
        out.append(('connection.protocol',
                    f"connection.protocol: fixed after creation — this device speaks "
                    f"{_CLASS_WORDS[old['protocol']]}, and one that speaks "
                    f"{_CLASS_WORDS[new['protocol']]} is a new device (its template map is "
                    "read by one protocol only)"))
    fam = d.get('template_family')
    if fam:
        dev_class = family_of(new)
        if dev_class is not None and dev_class != fam:
            proto = str(d.get('protocol') or '')
            out.append(('template',
                        f"template: '{d.get('template')}' is a {fam.upper()} map but this "
                        f"device is {proto.upper()} — pick a {dev_class.upper()} template"))
    return out
