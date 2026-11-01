# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""What a template says about connecting to a device made from it (ADR-0031 §5).

:func:`connection_facts` is what the template summary adds for the Add and
Edit forms: the protocol its map is read by, whether it only listens, the
ports it fits, its default unit, its setup writes, whether a bus scan can
recognise it, and three optional ``protocol`` keys a template may declare:

- ``protocol.line``: ``{baudrate?, parity?, bytesize?, stopbits?}``, the
  vendor's default serial line (a key left out is the gateway's default);
- ``protocol.http.path``: the path (and query) of the device's Web API call,
  ``${unit_id}`` standing for the device id behind the host;
- ``protocol.master_hint``: a sentence on who usually polls this device's bus;
- ``protocol.sunspec``: true for a SunSpec map (whose rows sit after the
  marker, so the map alone does not show it).

:func:`protocol_hint_errors` validates the four on a template save.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)
_WARNED: set = set()

# a transport string → the port it is plugged into
_PORT_OF = {'rtu': 'serial', 'ascii': 'serial', 'rtu-tcp': 'serial', 'ascii-tcp': 'serial',
            'rtu_tap': 'serial', 'seplos_v2': 'serial', 'seplos_v2-tcp': 'serial',
            'tcp': 'ethernet', 'udp': 'ethernet', 'http': 'http', 'mqtt': 'mqtt'}
# a family with no declared transports → the ports it can be reached by
_FAMILY_PORTS = {'modbus': ['serial', 'ethernet'], 'http': ['http'], 'mqtt': ['mqtt'], 'can': ['can'],
                 'seplos_v2': ['serial']}
_PORT_ORDER = ('serial', 'ethernet', 'can', 'http', 'mqtt')

LINE_KEYS = ('baudrate', 'parity', 'bytesize', 'stopbits')
LINE_DEFAULTS = {'baudrate': 9600, 'parity': 'N', 'bytesize': 8, 'stopbits': 1}
MASTER_HINT_MAX = 300
HTTP_PATH_MAX = 512


def _proto(tpl) -> Dict[str, Any]:
    p = tpl.get('protocol') if isinstance(tpl, dict) else getattr(tpl, 'protocol', None)
    return p if isinstance(p, dict) else {}


def ports_of(tpl) -> List[str]:
    """The ports a device made from this template can be plugged into."""
    from .can_signal import CAN_TRANSPORTS
    from .device_template import template_transport
    transports = [str(x).lower() for x in (_proto(tpl).get('transports') or [])]
    got = set()
    for x in transports:
        got.add('can' if x in CAN_TRANSPORTS else _PORT_OF.get(x, ''))
    got.discard('')
    if not got:
        got = set(_FAMILY_PORTS.get(template_transport(tpl), ['serial', 'ethernet']))
    return [p for p in _PORT_ORDER if p in got]


def line_of(tpl) -> Optional[Dict[str, Any]]:
    """The template's serial line with the gateway's defaults filled in, or
    None when it declares none."""
    line = _proto(tpl).get('line')
    if not isinstance(line, dict) or not line:
        return None
    # a file on disk is read as a save would have it: parity upper-cased; one
    # still out of shape is left out (said once in the log), never guessed
    norm = dict(line)
    if isinstance(norm.get('parity'), str):
        norm['parity'] = norm['parity'].upper()
    errs = protocol_hint_errors({'line': norm})
    if errs:
        tid = getattr(tpl, 'id', '?')
        if tid not in _WARNED:
            _WARNED.add(tid)
            logger.warning("device template %s: %s — left out", tid, '; '.join(errs))
        return None
    return {k: norm.get(k, LINE_DEFAULTS[k]) for k in LINE_KEYS}


def setup_writes(tpl) -> Dict[str, Any]:
    """Its setup steps as the function code and address each is written with
    (FC16, or FC6 when the template writes single registers only)."""
    steps = [s for s in (getattr(tpl, 'setup', None) or []) if isinstance(s, dict)]
    fc = 6 if _proto(tpl).get('single_register_writes') is True else 16
    return {'count': len(steps), 'writes': [{'fc': fc, 'address': s.get('address')} for s in steps]}


# where the SunSpec marker ("SunS") sits: 40000 (0-based) or 40001
SUNSPEC_MARKER = (40000, 40001)


def _sunspec(tpl) -> bool:
    """A SunSpec map, judged from the map: the template declares it
    (``protocol.sunspec: true``), reads the SunS marker, or checks it in its
    ``identify`` block. A bus scan recognises such a device by that marker."""
    from .device_template import template_transport
    if template_transport(tpl) != 'modbus':
        return False
    if _proto(tpl).get('sunspec') is True:
        return True
    if any(getattr(r, 'address', None) in SUNSPEC_MARKER for r in (getattr(tpl, 'registers', None) or [])):
        return True
    idf = getattr(tpl, 'identify', None) or {}
    regs = idf.get('registers') if isinstance(idf, dict) else None
    return any(isinstance(r, dict) and r.get('address') in SUNSPEC_MARKER for r in (regs or []))


def identifies(tpl) -> Dict[str, bool]:
    """How a bus scan can recognise a device of this template:
    ``fingerprint`` — its own ``identify`` block (registers and/or FC43);
    ``sunspec`` — the SunSpec marker, for a SunSpec map."""
    return {'fingerprint': bool(getattr(tpl, 'identify', None)), 'sunspec': _sunspec(tpl)}


def connection_facts(tpl) -> Dict[str, Any]:
    """The keys the template summary adds (ADR-0031 §5)."""
    from .connection_spec import template_speaks
    proto = _proto(tpl)
    sp = template_speaks(tpl)
    unit = proto.get('default_unit_id')
    http = proto.get('http') if isinstance(proto.get('http'), dict) else {}
    hint = proto.get('master_hint')
    return {
        'speaks': sp['protocol'],
        # a CAN map is only ever heard: the driver has no transmit path
        'listen_only': bool(sp['listen_only'] or sp['protocol'] == 'can_signals'),
        'transports': [str(x).lower() for x in (proto.get('transports') or [])],
        'default_unit_id': unit if isinstance(unit, int) and not isinstance(unit, bool) else None,
        'setup': setup_writes(tpl),
        'identify': identifies(tpl),
        'ports': ports_of(tpl),
        'line': line_of(tpl),
        'http_path': http.get('path') if isinstance(http.get('path'), str) else None,
        'master_hint': hint if isinstance(hint, str) and hint else None,
    }


HTTP_PATH_PLACEHOLDERS = ('${unit_id}', '${device_id}')


def http_path_errors(proto: Any) -> List[str]:
    """``protocol.http`` / ``protocol.http.path`` (optional), the one check:
    the path and query of the call on the device's host, starting with /, no
    spaces, at most HTTP_PATH_MAX characters; the only placeholders are
    ``${unit_id}`` and ``${device_id}`` (filled in per device). The Add flow
    and the installation wizard build a URL from it (L3-F6)."""
    if not isinstance(proto, dict) or 'http' not in proto:
        return []
    http = proto['http']
    if not isinstance(http, dict) or set(http) - {'path'}:
        return ["protocol.http: {path}"]
    if 'path' not in http:
        return []
    p = http['path']
    if (not isinstance(p, str) or not p.startswith('/') or p.startswith('//')
            or len(p) > HTTP_PATH_MAX or any(c.isspace() for c in p)):
        return ["protocol.http.path: the path of the call on the device's host, starting with /, "
                f"no spaces, at most {HTTP_PATH_MAX} characters (${{unit_id}} stands for the device id)"]
    unknown = [m for m in re.findall(r'\$\{[^}]*\}', p) if m not in HTTP_PATH_PLACEHOLDERS]
    if unknown:
        return [f"protocol.http.path: {unknown[0]} is not a placeholder ({', '.join(HTTP_PATH_PLACEHOLDERS)})"]
    return []


def protocol_hint_errors(proto: Any) -> List[str]:
    """``protocol.line``, ``protocol.http``, ``protocol.master_hint`` and
    ``protocol.sunspec`` as a save checks them (each optional)."""
    if not isinstance(proto, dict):
        return []
    out: List[str] = []
    if 'line' in proto:
        line = proto['line']
        if not isinstance(line, dict) or not line or set(line) - set(LINE_KEYS):
            out.append("protocol.line: {baudrate, parity, bytesize, stopbits}, at least one, nothing else")
        else:
            for k, lo, hi in (('baudrate', 300, 4_000_000), ('bytesize', 5, 8), ('stopbits', 1, 2)):
                v = line.get(k)
                if k in line and (isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi):
                    out.append(f"protocol.line.{k}: an integer {lo}..{hi}")
            if 'parity' in line and line['parity'] not in ('N', 'E', 'O'):
                out.append("protocol.line.parity: N, E or O")
    out += http_path_errors(proto)
    if 'sunspec' in proto and not isinstance(proto['sunspec'], bool):
        out.append("protocol.sunspec: true or false (a SunSpec map: a scan recognises it by the SunS marker)")
    if 'master_hint' in proto:
        h = proto['master_hint']
        if not isinstance(h, str) or not h.strip() or len(h) > MASTER_HINT_MAX:
            out.append(f"protocol.master_hint: a sentence, 1..{MASTER_HINT_MAX} characters")
    return out
