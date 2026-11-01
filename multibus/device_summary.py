# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""A device's connection in one line, and what an edit may not change
(ADR-0031 §3, §5).

:func:`device_view` gives both, for ``GET /api/devices`` and
``GET /api/devices/{id}``; the device page's header, the device list and the
bridge cards read the same words:

- ``summary``: the axes (port, reach, protocol, framing, udp), ``role``
  (``master`` | ``listen-only`` | None where the word means nothing: HTTP,
  MQTT, a device measured by its meters), ``where`` (the serial port,
  host:port, the CAN interface, the HTTP host or the MQTT broker),
  ``line`` (``{baud, parity, bytesize, stopbits}`` for a serial port here,
  else None) or ``bridge`` (``{id, port, type, framing, host}``), ``unit``
  with ``address_kind`` (``unit`` | ``pack``), ``poll_s`` ([fastest,
  slowest] of the poll groups it reads, or None), ``bitrate`` (CAN),
  ``endpoint_path`` (the HTTP path without its query, or the MQTT topic) and
  ``text``, e.g. ``Modbus RTU · /dev/ttySC1 · 9600 8E1 · unit 1 · Master``.
  Never a credential: an HTTP URL gives its host and path only.
- ``locks``: ``[{field, code, text}]``, each a field an edit may not change
  and why: the connection's class (``connection_spec.edit_errors``, in its
  words), the line of a serial bus other devices share (``set by the bus:
  …``) or one set on a bridge or converter, and the role a template, CAN or
  another device on the bus decides (bus claims, ADR-0025). The primary meter
  also says what it keeps whatever is sent (code ``primary``: its name, type,
  template, polling, MQTT, InfluxDB and the gateway's HA switch —
  ``device_patch.PRIMARY_FIXED``).

Read only: nothing is opened, nothing is stored.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

MASTER, LISTEN = 'master', 'listen-only'
GATEWAY_BROKER = '@gateway'          # an MQTT source on the gateway's own broker
ROLE_WORDS = {MASTER: 'Master', LISTEN: 'Listen-only'}

# locks: the fields and codes a form maps them by
F_PROTOCOL, F_TEMPLATE, F_LINE, F_ROLE = 'connection.protocol', 'template', 'connection.line', 'connection.role'


# ── the words ────────────────────────────────────────────────────────────
def line_text(line: Optional[Dict[str, Any]]) -> str:
    """``9600 8N1``."""
    if not line:
        return ''
    return f"{line['baud']} {line['bytesize']}{line['parity']}{line['stopbits']}"


def _num(v: float) -> str:
    return f"{v:g}"


def _poll_text(poll_s) -> str:
    if not poll_s:
        return ''
    lo, hi = poll_s
    return f"{_num(lo)} s" if lo == hi else f"{_num(lo)} s/{_num(hi)} s"


def _protocol_word(s: Dict[str, Any]) -> str:
    proto, framing, reach = s.get('protocol'), s.get('framing'), s.get('reach')
    if proto == 'modbus':
        if framing == 'mbap':
            return 'Modbus UDP' if s.get('udp') else 'Modbus TCP'
        word = f"Modbus {(framing or 'rtu').upper()}"
        return f"{word} over TCP" if reach == 'address' else word
    if proto == 'seplos_v2':
        return 'Seplos V2 over TCP' if reach == 'address' else 'Seplos V2'
    return {'can_signals': 'CAN', 'json_http': 'HTTP/JSON', 'json_mqtt': 'MQTT/JSON'}.get(proto, '')


def summary_text(s: Dict[str, Any]) -> str:
    """The one-liner of a summary (the device header, the list, a bridge card)."""
    if s.get('port') is None and s.get('protocol') is None:
        return 'Measured by its meters'
    if s.get('port') == 'unknown' or not _protocol_word(s):
        return f"Unknown connection ({s.get('stored_protocol') or '?'})"
    parts = [_protocol_word(s)]
    proto = s.get('protocol')
    if proto == 'json_http':
        parts.append(f"{s.get('where') or ''}{s.get('endpoint_path') or ''}")
    elif proto == 'json_mqtt':
        parts += [x for x in (s.get('where'), s.get('endpoint_path')) if x]
    else:
        b = s.get('bridge')
        parts.append(f"bridge {b['id']} port {b['port']}" if b else (s.get('where') or ''))
        if s.get('line'):
            parts.append(line_text(s['line']))
        if s.get('bitrate'):
            parts.append(f"{_num(s['bitrate'] / 1000)} kbit/s")
        if s.get('unit') is not None:
            parts.append(f"{'pack' if s.get('address_kind') == 'pack' else 'unit'} {s['unit']}")
        if s.get('role'):
            parts.append(ROLE_WORDS[s['role']])
    if s.get('role') != LISTEN and s.get('poll_s'):
        parts.append(_poll_text(s['poll_s']))
    return ' · '.join(p for p in parts if p)


# ── the summary ──────────────────────────────────────────────────────────
def _template(templates, dev):
    tid = getattr(dev, 'template', '') or ''
    try:
        return templates.get(tid) if (templates is not None and tid) else None
    except Exception:  # noqa: BLE001 — a template lookup never hides a device
        return None


def _role(spec: Dict[str, Any], tpl) -> Optional[str]:
    proto = spec.get('protocol')
    if proto == 'can_signals':
        return LISTEN
    if proto not in ('modbus', 'seplos_v2'):
        return None
    if spec.get('mode') == 'listen':
        return LISTEN
    if tpl is not None:
        from .device_template import template_listens
        if template_listens(tpl):
            return LISTEN
    return MASTER


def poll_intervals(config, dev) -> Optional[List[float]]:
    """[fastest, slowest] interval of the poll groups its rows are in."""
    try:
        srcs = list(getattr(dev, 'sources', None) or [])
        loads = ([config.load_device_registers(dev, source=s, shared=True) for s in srcs]
                 if len(srcs) > 1 else [config.load_device_registers(dev, shared=True)])
        ivals = []
        for regs, groups in loads:
            used = {getattr(r, 'poll_group', '') for r in regs}
            ivals += [float(g.interval) for n, g in (groups or {}).items()
                      if n in used and getattr(g, 'interval', 0)]
        return [min(ivals), max(ivals)] if ivals else None
    except Exception:  # noqa: BLE001 — a summary never fails on the selection file
        logger.debug("device %s: poll intervals unavailable", getattr(dev, 'id', '?'), exc_info=True)
        return None


def _bridge(config, raw_conn: Dict[str, Any], dev) -> Optional[Dict[str, Any]]:
    bid = raw_conn.get('bridge') if isinstance(raw_conn, dict) else None
    if not bid:
        return None
    from .bridges import TYPES
    b = (config.get_raw_bridge(str(bid)) if hasattr(config, 'get_raw_bridge') else None) or {}
    t = TYPES.get(str(b.get('type') or ''), {})
    framing = {'rtu': 'rtu', 'ascii': 'ascii', 'modbus_tcp': 'mbap'}.get(t.get('framing', '')) if b else None
    port = raw_conn.get('bridge_port') or getattr(dev.connection, 'port', None)
    try:
        port = int(port) if port is not None else None
    except (TypeError, ValueError):
        pass
    return {'id': str(bid), 'port': port, 'type': b.get('type'), 'framing': framing,
            'host': b.get('host')}


def _raw_conn(raw: Dict[str, Any]) -> Dict[str, Any]:
    conn = raw.get('connection')
    if not conn:
        srcs = [s for s in (raw.get('sources') or []) if isinstance(s, dict)]
        conn = srcs[0] if srcs else {}
    return conn if isinstance(conn, dict) else {}


def summarize(config, dev, templates=None, *, raw: Optional[Dict[str, Any]] = None,
              spec: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A device's structured connection summary with its ``text``."""
    from .connection_spec import device_specs, public
    raw = raw if raw is not None else ((config.get_raw_device(dev.id) if hasattr(config, 'get_raw_device')
                                        else None) or {})
    if spec is None:
        spec = public(device_specs(raw, getattr(config, 'bridges', None) or [])[0])
    tpl = _template(templates, dev)
    c = dev.connection
    s: Dict[str, Any] = {k: spec.get(k) for k in ('port', 'reach', 'protocol', 'framing', 'udp')}
    s.update(role=_role(spec, tpl), where=None, line=None, bridge=None, unit=None, address_kind=None,
             poll_s=None, bitrate=None, endpoint_path=None, stored_protocol=str(dev.protocol or ''))
    proto = spec.get('protocol')
    if proto in ('modbus', 'seplos_v2'):
        s['unit'] = getattr(c, 'unit_id', None)
        s['address_kind'] = 'pack' if proto == 'seplos_v2' else 'unit'
        if spec.get('reach') == 'bridge':
            s['bridge'] = _bridge(config, _raw_conn(raw), dev)
            s['where'] = f"{c.host}:{c.port}" if getattr(c, 'host', '') else None
        elif spec.get('reach') == 'here':
            s['where'] = str(getattr(c, 'serial_port', '') or '')
            s['line'] = {'baud': int(c.baudrate or 9600), 'parity': str(c.parity or 'N').upper(),
                         'bytesize': int(c.bytesize or 8), 'stopbits': int(c.stopbits or 1)}
        else:
            s['where'] = f"{c.host}:{c.port}"
    elif proto == 'can_signals':
        can = dict(getattr((getattr(dev, 'sources', None) or [None])[0], 'can', None) or {})
        s['where'] = str(can.get('channel') or 'can0')
        s['bitrate'] = can.get('bitrate')
    elif proto == 'json_http':
        u = urlparse(str((getattr(dev, 'http', None) or {}).get('url', '') or ''))
        host = u.hostname or ''
        try:
            port = u.port
        except ValueError:           # a port out of range or not a number: the host alone
            port = None
        s['where'] = f"{host}:{port}" if (host and port) else host
        s['endpoint_path'] = u.path or None
    elif proto == 'json_mqtt':
        m = getattr(dev, 'mqtt_in', None) or {}
        broker = str(m.get('broker', '') or '')
        s['where'] = ('gateway broker' if broker == GATEWAY_BROKER else broker) or None
        s['endpoint_path'] = str(m.get('topic', '') or '') or None
    if s['role'] != LISTEN and proto in ('modbus', 'seplos_v2', 'json_http'):
        s['poll_s'] = poll_intervals(config, dev)
    s['text'] = summary_text(s)
    return s


# ── the locks ────────────────────────────────────────────────────────────
def _probe(conn: Dict[str, Any]) -> Dict[str, Any]:
    from .connection_spec import public, spec_of
    return public(spec_of(conn, [], {}))


def _lock(field: str, code: str, text: str, **params: Any) -> Dict[str, Any]:
    """One lock: the field, its code, the server's words and ``params`` —
    the values those words name, so a page says them in its own language
    (``ui/js/lock-text.js``, L3.10-F16); the text stays the fallback."""
    return {'field': field, 'code': code, 'text': text, 'params': params}


def _class_locks(dev, spec: Dict[str, Any], components: int) -> List[Dict[str, str]]:
    """The connection's class, in ``edit_errors``'s own words: asked what it
    would refuse were the class to change."""
    from .connection_spec import edit_errors
    other = (_probe({'protocol': 'tcp', 'host': 'x'}) if spec.get('protocol') in ('json_http', None)
             else _probe({'protocol': 'http', 'url': 'http://x'}))
    meter_only = spec.get('protocol') is None and spec.get('port') is None
    facts = {'id': dev.id, 'primary': bool(getattr(dev, 'primary', False)), 'components': components}
    errs = edit_errors(spec, other, facts)
    from .connection_spec import _CLASS_WORDS
    params: Dict[str, Any] = {}
    if facts['primary']:
        code = 'primary'
    elif meter_only:
        code = 'meter_only'
    elif components:
        code = 'components'
        params = {'device': dev.id, 'n': components}
    else:
        code = 'protocol_fixed'
        params = {'speaks': _CLASS_WORDS.get(spec.get('protocol'), str(spec.get('protocol') or ''))}
    out = [_lock(F_PROTOCOL, code, text, **params) for f, text in errs if f == F_PROTOCOL]
    if components and not facts['primary']:
        same = edit_errors(spec, spec, dict(facts, template_changed=True))
        out += [_lock(F_TEMPLATE, 'components', text, device=dev.id, n=components) for _f, text in same[:1]]
    return out


class Claims:
    """bus → [(device id, enabled, listens, line)] over every device, enabled
    or not; built once, on first use. A list builds one for all its rows."""

    def __init__(self, config, templates=None):
        self._config, self._templates, self._by = config, templates, None

    def on(self, bus: str) -> List[tuple]:
        if self._by is None:
            self._by = _claims_by_bus(self._config, self._templates)
        return self._by.get(bus, [])


def _source_line(dev, i: int):
    srcs = list(getattr(dev, 'sources', None) or [])
    conn = srcs[i].connection if i < len(srcs) else dev.connection
    sp = str(getattr(conn, 'serial_port', '') or '').strip()
    if not sp:
        return None
    return {'baud': int(conn.baudrate or 9600), 'parity': str(conn.parity or 'N').upper(),
            'bytesize': int(conn.bytesize or 8), 'stopbits': int(conn.stopbits or 1)}


def _claims_by_bus(config, templates) -> Dict[str, List[tuple]]:
    from . import bus_claims
    out: Dict[str, List[tuple]] = {}
    for d in getattr(config, 'devices', None) or []:
        try:
            claims = bus_claims.source_claims(d, templates, standby=False)
        except Exception:  # noqa: BLE001
            continue
        for i, bus, listens, _w in claims:
            out.setdefault(bus, []).append((d.id, bool(getattr(d, 'enabled', False)), listens,
                                            _source_line(d, i)))
    return out


def _line_locks(dev, s: Dict[str, Any], others: List[tuple]) -> List[Dict[str, str]]:
    """``others``: (device id, line) of the other devices on its serial line."""
    if s.get('port') != 'serial':
        return []
    if s.get('reach') == 'bridge' and s.get('bridge'):
        # settable: the bridge takes its line from here (our serial bridge's
        # API) — Edit bus line… sets it; else it is the bridge's own
        from .bridges import TYPES as _BT
        settable = (_BT.get(str(s['bridge'].get('type') or ''), {}) or {}).get('serial_config') == 'api'
        return [dict(_lock(F_LINE, 'line_bridge', f"set on the bridge '{s['bridge']['id']}': the line is the "
                                                  "bridge's, not this device's", bridge=s['bridge']['id']),
                     settable=settable)]
    if s.get('reach') == 'address':
        return [_lock(F_LINE, 'line_remote', f"set on the converter at {s.get('where')}, not here",
                      where=s.get('where') or '')]
    if s.get('reach') == 'here' and others:
        groups: List[tuple] = []                 # (line, [ids]) in config order
        for did, ln in others:
            hit = next((g for g in groups if g[0] == ln), None)
            if hit is None:
                groups.append((ln, [did]))
            else:
                hit[1].append(did)
        said = '; '.join(f"{', '.join(ids)} {'reads' if len(ids) == 1 else 'read'} it at "
                         f"{line_text(ln) or 'an unknown line'}" for ln, ids in groups)
        lines = {line_text(ln) for ln, _ids in groups} | {line_text(s.get('line'))}
        disagree = (f" — the devices on this bus disagree (this one: {line_text(s.get('line'))}); "
                    "a bus has one line" if len(lines) > 1 else "")
        return [_lock(F_LINE, 'line_shared',
                      f"set by the bus: {said}{disagree}. Edit bus line… changes it for every device on "
                      f"{s.get('where')}",
                      devices=', '.join(did for _ln, ids in groups for did in ids),
                      line=line_text(groups[0][0]) if len(groups) == 1 else '',
                      own=line_text(s.get('line')), where=s.get('where') or '', disagree=len(lines) > 1)]
    return []


def _role_locks(dev, spec, tpl, mine: List[tuple]) -> List[Dict[str, str]]:
    proto = spec.get('protocol')
    if proto == 'can_signals':
        from .connection_matrix import INVALID
        text = next(c['text'] for c in INVALID['device'] if c['code'] == 'can_poll')
        return [_lock(F_ROLE, 'can_listen', text)]
    if proto not in ('modbus', 'seplos_v2'):
        return []
    out = []
    if tpl is not None:
        from .device_template import template_listens
        if template_listens(tpl):
            out.append(_lock(F_ROLE, 'template_listens',
                             f"template '{dev.template}' is only heard on a bus another master polls: "
                             "the role is Listen-only", template=dev.template or ''))
    listened = [x[0] for x in mine if x[1] and x[2]]
    polled = [x[0] for x in mine if x[1] and not x[2]]
    if listened:
        out.append(_lock(F_ROLE, 'role_bus_listened',
                         f"listened to by {', '.join(listened)}: another master owns this bus, so the role "
                         "is Listen-only", devices=', '.join(listened)))
    elif polled:
        out.append(_lock(F_ROLE, 'role_bus_polled',
                         f"polled by {', '.join(polled)}: a bus is either polled or listened to, never both, "
                         "so the role is Master", devices=', '.join(polled)))
    return out


def _whole_device_lock(config, dev, summary: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A component or an installation's unit is not edited as a device: its
    parent or its installation is (the words a save refuses it with)."""
    parent = getattr(dev, 'component_of', '') or ''
    if parent:
        return [dict(_lock('device', 'component', f"'{dev.id}' is a component of '{parent}' — manage it "
                                                  "from that device", device=dev.id, parent=parent), parent=parent)]
    ep = getattr(dev, 'endpoint_id', '') or ''
    if not ep:
        return []
    why = None
    try:
        why = config.raw_device_refusal(dev.id)
    except Exception:  # noqa: BLE001
        pass
    out = [dict(_lock('device', 'installation_unit',
                      why or f"device {dev.id!r} is materialized from endpoint {ep!r} — edit the endpoint "
                             "instead", device=dev.id, installation=ep), installation=ep)]
    if (summary or {}).get('port') == 'serial':
        out.append(dict(_lock(F_LINE, 'line_installation',
                              f"set by the installation '{ep}': its connection says the line for every "
                              "unit", installation=ep), installation=ep))
    return out


def _bus_owner_locks(config, s: Dict[str, Any], others) -> List[Dict[str, Any]]:
    """A serial line an installation's unit or a component is on is set by
    that installation (or that component's device): Edit bus line… refuses
    it there (busLineOpen), so the device's own locks say it first (L3.10
    review m4) — with whom, for the menu's words. Codes of their own
    (``line_bus_*``): ``line_installation`` is a unit's own lock, which the
    connection step answers early for (review 2, N2)."""
    if s.get('port') != 'serial':
        return []
    for d in getattr(config, 'devices', None) or []:
        if d.id not in others:
            continue
        ep = getattr(d, 'endpoint_id', '') or ''
        if ep:
            return [dict(_lock(F_LINE, 'line_bus_installation',
                               f"set by the installation '{ep}' ({d.id} is one of its units on this bus): "
                               "change its connection there", installation=ep, unit=d.id), installation=ep)]
        parent = getattr(d, 'component_of', '') or ''
        if parent:
            return [dict(_lock(F_LINE, 'line_bus_component',
                               f"set by '{parent}' ({d.id} is one of its components on this bus): "
                               "change its connection there", parent=parent, component=d.id), parent=parent)]
    return []


def locks(config, dev, templates=None, *, spec: Dict[str, Any], summary: Optional[Dict[str, Any]],
          claims: Optional[Claims] = None) -> List[Dict[str, Any]]:
    """The fields an edit may not change, each with its reason. A component
    or an installation's unit says that first, and only that (with, for a
    unit on a serial bus, that its installation sets the line)."""
    whole = _whole_device_lock(config, dev, summary)
    if whole:
        return whole
    comps = 0
    try:
        comps = len(config.component_devices(dev.id))
    except Exception:  # noqa: BLE001
        pass
    out = _class_locks(dev, spec, comps)
    if getattr(dev, 'primary', False):
        # what the gateway's own meter keeps whatever is sent (ADR-0032 §6):
        # the words PATCH refuses a change of them with
        from .device_patch import PRIMARY_FIXED
        out += [_lock(f, 'primary', text) for f, text in PRIMARY_FIXED.items()]
    from . import bus_claims
    try:
        own = bus_claims.source_claims(dev, templates, standby=False)
    except Exception:  # noqa: BLE001
        own = []
    if not own:                     # no bus: nothing shares it (CAN still says its role)
        return out + _role_locks(dev, spec, _template(templates, dev), [])
    claims = claims if claims is not None else Claims(config, templates)
    mine = [x for x in claims.on(own[0][1]) if x[0] != dev.id]
    others, seen = [], set()
    for did, _en, _lis, ln in mine:
        if did not in seen:
            seen.add(did)
            others.append((did, ln))
    # without a summary the spec still says the port and the reach
    s_ = summary if summary is not None else dict(spec, line=None, where=None)
    out += _bus_owner_locks(config, s_, seen)
    out += _line_locks(dev, s_, others)
    out += _role_locks(dev, spec, _template(templates, dev), mine)
    return out


_LOGGED: set = set()


def _log_once(what: str, dev_id: str) -> None:
    key = (what, dev_id)
    if key in _LOGGED:
        logger.debug("device %s: %s unavailable", dev_id, what, exc_info=True)
        return
    _LOGGED.add(key)
    logger.warning("device %s: %s unavailable", dev_id, what, exc_info=True)


def device_view(config, dev, templates=None, *, raw: Optional[Dict[str, Any]] = None,
                spec: Optional[Dict[str, Any]] = None, claims: Optional[Claims] = None) -> Dict[str, Any]:
    """``{summary, locks}`` of one device (see the module's words). ``spec``:
    its public connection spec when the caller has it; ``claims``: one
    :class:`Claims` shared by every row of a list. Each half is computed on
    its own: a failure leaves that half None / [] (logged once), never the other."""
    from .connection_spec import device_specs, public
    raw = raw if raw is not None else ((config.get_raw_device(dev.id) if hasattr(config, 'get_raw_device')
                                        else None) or {})
    if spec is None:
        spec = public(device_specs(raw, getattr(config, 'bridges', None) or [])[0])
    try:
        s = summarize(config, dev, templates, raw=raw, spec=spec)
    except Exception:  # noqa: BLE001 — a summary must not hide the locks
        _log_once('connection summary', dev.id)
        s = None
    try:
        lk = locks(config, dev, templates, spec=spec, summary=s, claims=claims)
    except Exception:  # noqa: BLE001 — the locks must not hide the summary
        _log_once('edit locks', dev.id)
        lk = []
    return {'summary': s, 'locks': lk}
