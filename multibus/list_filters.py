# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Search, filter and sort for the device and template lists.

Every parameter is optional; with none, a list comes back exactly as before.
A value list is comma-separated and matches any of its values; parameters
combine with AND. ``q`` matches every word, case-insensitively, anywhere in
the searchable text. ``sort`` takes a key, ``-key`` for descending; rows the
key cannot tell apart stay in name order. A wrong value is a ``FilterError``
with ``{param: message}``.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .config import MODBUS_PROTOCOLS
from .device_kind import KINDS, normalize

DEVICE_STATES = ('online', 'offline', 'disabled')
DEVICE_SORTS = ('name', 'id', 'kind', 'state', 'protocol')
TEMPLATE_ORIGINS = ('builtin', 'custom')
TEMPLATE_SORTS = ('name', 'id', 'vendor', 'kind', 'used_by')
# one vocabulary for both lists: a device's own protocols, plus `modbus` for
# any of them — a template is filed by transport (modbus / http / mqtt)
PROTOCOLS = ('modbus',) + MODBUS_PROTOCOLS + ('http', 'mqtt')
MAX_Q = 200            # characters
MAX_VALUES = 20        # per comma list


class FilterError(ValueError):
    def __init__(self, fields: Dict[str, str]):
        super().__init__('; '.join(f"{k}: {v}" for k, v in fields.items()))
        self.fields = fields


def values(raw: Optional[str]) -> List[str]:
    """``"a, b,,c"`` → ``['a', 'b', 'c']`` (lower-cased); None/'' → []."""
    if raw is None:
        return []
    return [v.strip().lower() for v in str(raw).split(',') if v.strip()]


def _list(param: str, raw: Optional[str], errors: Dict[str, str]) -> List[str]:
    got = values(raw)
    if len(got) > MAX_VALUES:
        errors[param] = f"at most {MAX_VALUES} values"
        return []
    return got


def _check(param: str, got: Sequence[str], allowed: Sequence[str], errors: Dict[str, str]) -> None:
    bad = [v for v in got if v not in allowed]
    if bad:
        shown = ', '.join(repr(b[:40]) for b in bad[:3]) + (' …' if len(bad) > 3 else '')
        errors[param] = f"{shown}: not one of {', '.join(allowed)}"


def _kinds(raw: Optional[str], errors: Dict[str, str]) -> List[str]:
    """A kind list, aliases read as their kind (``system`` → ``site``)."""
    got = _list('kind', raw, errors)
    out = [normalize(v) or v for v in got]
    _check('kind', out, KINDS, errors)
    return out


def _protocols(raw: Optional[str], errors: Dict[str, str]) -> List[str]:
    got = _list('protocol', raw, errors)
    _check('protocol', got, PROTOCOLS, errors)
    return got


def _q(raw: Optional[str], errors: Dict[str, str]) -> List[str]:
    q = raw or ''
    if len(q) > MAX_Q:
        errors['q'] = f"at most {MAX_Q} characters"
        return []
    return q.lower().split()


def _sort_key(raw: Optional[str], allowed: Sequence[str], errors: Dict[str, str]) -> Optional[Tuple[str, bool]]:
    if raw in (None, ''):
        return None
    s = str(raw).strip().lower()
    desc = s.startswith('-')
    key = s[1:] if desc else s
    if key not in allowed:
        errors['sort'] = f"{str(raw)[:40]!r}: one of {', '.join(allowed)} (one leading - for descending)"
        return None
    return key, desc


def _sorted(rows: List[Dict], key: Callable[[Dict], Any], desc: bool,
            name: Callable[[Dict], str]) -> List[Dict]:
    """Sorted by ``key``; equal keys in ascending name order either way."""
    rows = sorted(rows, key=name)                       # the tiebreak, always A→Z
    return sorted(rows, key=key, reverse=desc)          # stable: ties keep it


def _text(parts: Iterable[Any]) -> str:
    return ' '.join(str(p) for p in parts if p not in (None, '')).lower()


def device_state(entry: Dict) -> str:
    if not entry.get('enabled', True):
        return 'disabled'
    return 'online' if entry.get('connected') else 'offline'


def device_protocols(dev) -> Set[str]:
    """How a device is actually read: its sources' protocols when it has
    sources (a unit read over HTTP only is not a TCP device), else its own."""
    srcs = [s for s in (getattr(dev, 'sources', None) or []) if getattr(s, 'protocol', '')]
    if srcs:
        return {str(s.protocol).lower() for s in srcs}
    return {str(getattr(dev, 'protocol', '') or '').lower()} - {''}


def device_search_text(dev, entry: Dict, templates=None, *, redact: bool = False) -> str:
    """What ``q`` looks through for a device: its names and ids, every
    template it reads with (and their vendor, model and name), every address
    it is read from, its installation and its kind."""
    from .redact import redact_url
    parts: List[Any] = [entry.get('id'), entry.get('name'), entry.get('endpoint_id'),
                        entry.get('component_of'), entry.get('kind'), entry.get('host'),
                        entry.get('http_url'), entry.get('mqtt_in_topic'), entry.get('mqtt_in_broker')]
    tids = [getattr(dev, 'template', '') or '']
    for s in getattr(dev, 'sources', None) or []:
        tids.append(getattr(s, 'template', '') or '')
        parts += [getattr(s, 'id', ''), getattr(s, 'protocol', '')]
        conn = getattr(s, 'connection', None)
        if conn is not None and getattr(s, 'protocol', '') not in ('http', 'mqtt'):
            parts.append(getattr(conn, 'host', ''))
        url = (getattr(s, 'http', None) or {}).get('url', '')
        if url:
            parts.append(redact_url(url) if redact else url)
        mi = getattr(s, 'mqtt_in', None) or {}
        parts += [mi.get('broker', ''), mi.get('topic', '')]
    parts += sorted(device_protocols(dev))
    for tid in dict.fromkeys(t for t in tids if t):
        tpl = templates.get(tid) if templates is not None else None
        parts += [tid, getattr(tpl, 'vendor', ''), getattr(tpl, 'model', ''), getattr(tpl, 'name', '')]
    return _text(parts)


def _proto_hit(protocols: Set[str], wanted: List[str]) -> bool:
    for w in wanted:
        if w == 'modbus' and protocols & set(MODBUS_PROTOCOLS):
            return True
        if w in protocols:
            return True
    return False


def filter_devices(rows: List[Tuple[Dict, Set[str], str]], *, q: Optional[str] = None,
                   kind: Optional[str] = None, state: Optional[str] = None,
                   protocol: Optional[str] = None, installation: Optional[str] = None,
                   sort: Optional[str] = None) -> List[Dict]:
    """``rows`` = [(entry, protocols, search text)] in registry order; the
    entries that match, sorted if asked."""
    errors: Dict[str, str] = {}
    kinds = _kinds(kind, errors)
    states = _list('state', state, errors)
    _check('state', states, DEVICE_STATES, errors)
    protos = _protocols(protocol, errors)
    insts = _list('installation', installation, errors)
    words = _q(q, errors)
    order = _sort_key(sort, DEVICE_SORTS, errors)
    if errors:
        raise FilterError(errors)
    out = []
    for entry, protocols, text in rows:
        if kinds and entry.get('kind') not in kinds:
            continue
        if states and device_state(entry) not in states:
            continue
        if protos and not _proto_hit({p.lower() for p in protocols}, protos):
            continue
        if insts and str(entry.get('endpoint_id') or '').lower() not in insts:
            continue
        if words and not all(w in text for w in words):
            continue
        out.append(entry)
    if order:
        key, desc = order
        name = lambda e: str(e.get('name') or e.get('id') or '').lower()  # noqa: E731
        get: Callable[[Dict], Any] = {
            'name': name,
            'id': lambda e: str(e.get('id') or ''),
            'kind': lambda e: str(e.get('kind') or ''),
            'state': lambda e: DEVICE_STATES.index(device_state(e)),
            'protocol': lambda e: str(e.get('protocol') or ''),
        }[key]
        out = _sorted(out, get, desc, name)
    return out


def template_transport_of(protocol: str) -> str:
    """A protocol of the shared vocabulary as a template's transport."""
    return 'modbus' if protocol in ('modbus',) + MODBUS_PROTOCOLS else protocol


def filter_templates(rows: List[Tuple[Dict, str]], *, q: Optional[str] = None,
                     kind: Optional[str] = None, protocol: Optional[str] = None,
                     origin: Optional[str] = None, sort: Optional[str] = None) -> List[Dict]:
    """``rows`` = [(summary, device kind)] in library order."""
    errors: Dict[str, str] = {}
    kinds = _kinds(kind, errors)
    protos = {template_transport_of(p) for p in _protocols(protocol, errors)}
    origins = _list('origin', origin, errors)
    _check('origin', origins, TEMPLATE_ORIGINS, errors)
    words = _q(q, errors)
    order = _sort_key(sort, TEMPLATE_SORTS, errors)
    if errors:
        raise FilterError(errors)
    out = []
    for s, dkind in rows:
        if kinds and dkind not in kinds:
            continue
        if protos and str(s.get('transport') or '').lower() not in protos:
            continue
        if origins and ('builtin' if s.get('builtin') else 'custom') not in origins:
            continue
        if words and not all(w in _text([s.get('id'), s.get('name'), s.get('vendor'), s.get('model'),
                                         dkind, s.get('transport')]) for w in words):
            continue
        out.append(s)
    if order:
        key, desc = order
        name = lambda s: str(s.get('name') or s.get('id') or '').lower()  # noqa: E731
        get: Callable[[Dict], Any] = {
            'name': name,
            'id': lambda s: str(s.get('id') or ''),
            'vendor': lambda s: str(s.get('vendor') or '').lower(),
            'kind': lambda s: str(s.get('device_kind') or ''),
            'used_by': lambda s: len(s.get('used_by') or []),
        }[key]
        out = _sorted(out, get, desc, name)
    return out
