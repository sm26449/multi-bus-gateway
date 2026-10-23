# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""A save keeps what it was not told.

An editor shows part of what a device or an installation stores. Saving it
unchanged must change nothing — yet a form that rebuilds the config from the
fields it shows drops everything else: a serial line's baud and parity, a
device's bridge, a source's tuning (the 2026-10-20 bank outage was one such
loss). The PUT routes therefore merge what is stored under what is sent:

- **same protocol → every stored key the payload does not carry is kept;**
- **an explicit ``None`` clears a key;**
- **a different protocol starts clean** (a serial line means nothing to TCP);
- **an absent or empty block keeps the stored one whole.**

A device on a bridge is never taken off it implicitly: a payload that names
another address without saying ``bridge: None`` is refused, because it is as
likely a stale page echoing an old address as a deliberate move.

Pure functions; the routes call them inside their serialized mutation, before
validation (connections) and after it (the top level).
"""
from __future__ import annotations

import copy
from typing import Any, Dict, Iterable, List, Optional

# what says WHERE a connection goes; a payload carrying one of these speaks of
# the route, and a different route is a different protocol unless it says so
ADDRESS_KEYS = ('host', 'port', 'serial_port', 'url', 'broker')
# a value echoed masked by the API is not something the operator said
MASKED = '******'


class BridgeConflict(ValueError):
    """A payload names another address for a device that is on a bridge."""


def _clean(d: Dict[str, Any]) -> Dict[str, Any]:
    """Explicit ``None`` clears: drop it."""
    return {k: v for k, v in d.items() if v is not None}


def _said(v: Any) -> bool:
    return v is not None and v != MASKED


def _effective_protocol(conn: Dict[str, Any], bridges: Optional[List[Dict]]) -> str:
    if conn.get('bridge') and bridges is not None:
        from .bridges import resolve_connection
        conn = resolve_connection(conn, bridges)
    return str(conn.get('protocol') or 'tcp').lower()


def _speaks_of_address(conn: Dict[str, Any]) -> bool:
    return any(k in conn and conn[k] not in (None, '') for k in ADDRESS_KEYS)


def merge_connection(new: Any, old: Any, bridges: Optional[List[Dict]] = None) -> Any:
    """The connection to store when ``new`` is saved over ``old``.

    ``bridges`` is the gateway's bridge list for a device (``None`` where
    bridges do not apply — an installation). A ``new`` that is ``None`` or
    ``{}`` means "the form does not speak of the connection": the stored one
    is kept whole (to take a group's own connection away, a group says
    ``connection: None`` — see merge_group_route). Raises :class:`BridgeConflict`.
    """
    if not isinstance(old, dict) or not old:
        return new
    if new is None or new == {}:
        return copy.deepcopy(old)
    if not isinstance(new, dict):
        return new                                   # validation says what is wrong
    new = {k: v for k, v in new.items() if v != MASKED}
    old_proto = _effective_protocol(old, bridges)

    leaving = 'bridge' in new and not new.get('bridge')
    if bridges is not None and old.get('bridge') and 'bridge' not in new:
        if _speaks_of_address(new):
            from .bridges import resolve_connection
            here = resolve_connection(old, bridges)
            echo = (set(k for k in ADDRESS_KEYS if new.get(k) not in (None, '')) <= {'host', 'port'}
                    and str(new.get('host', '')).strip() == str(here.get('host', ''))
                    and str(new.get('port', here.get('port'))) == str(here.get('port')))
            if not echo:
                raise BridgeConflict(
                    f"connection: this device reaches its bus through bridge '{old['bridge']}', now at "
                    f"{here.get('host', '')}:{here.get('port', '')} — if the page was opened before the "
                    "bridge changed, reload it; to take the device off the bridge, move it on the "
                    "Bridges card (or send bridge: null)")
            # an old page echoing what the bridge resolves to: the reference stays
            new.pop('host', None)
            new.pop('port', None)
        if 'protocol' in new and 'protocol' not in old and str(new['protocol']).lower() == old_proto:
            new.pop('protocol')                      # the bridge's, not the device's

    if 'protocol' in new and new['protocol']:
        new_proto = str(new['protocol']).lower()
    elif not _speaks_of_address(new) and not leaving:
        new_proto = old_proto
    else:
        new_proto = 'tcp'                            # what validation defaults to
    if new.get('bridge'):
        new_proto = _effective_protocol({**old, **new}, bridges)

    if new_proto != old_proto:
        return _clean(new)
    out = {**old, **new}
    if leaving:
        out.pop('bridge', None)
        out.pop('bridge_port', None)
    return _clean(out)


def merge_sources(new: Any, old: Any) -> Any:
    """A source list saved over the stored one: each source merged with the
    stored source of the same id (a source is a flat connection plus how
    often it reads). New ids and reordering are the payload's."""
    if not isinstance(new, list) or not isinstance(old, list):
        return new
    by_id = {str(s.get('id')): s for s in old if isinstance(s, dict)}
    out = []
    for s in new:
        prev = by_id.get(str(s.get('id'))) if isinstance(s, dict) else None
        out.append(merge_connection(s, prev) if prev else _clean(s) if isinstance(s, dict) else s)
    return out


def merge_group_route(new_group: Dict[str, Any], old_group: Dict[str, Any]) -> Dict[str, Any]:
    """How a group reaches its units, saved over the stored one.

    - neither ``connection`` nor ``sources`` → the stored route, whole;
    - ``sources`` → the group is read through sources (the stored
      ``connection`` is not kept); each source merged by id, the stored
      ``connection`` counting as source ``default``;
    - ``connection: None`` → the group inherits the installation's;
    - ``connection`` → merged with the stored one.
    """
    g = dict(new_group)
    has_c, has_s = 'connection' in g, 'sources' in g
    if not has_c and not has_s:
        for k in ('connection', 'sources'):
            if k in old_group:
                g[k] = copy.deepcopy(old_group[k])
        return g
    if has_s:
        prev = list(old_group.get('sources') or [])
        if isinstance(old_group.get('connection'), dict) and not any(
                isinstance(s, dict) and s.get('id') == 'default' for s in prev):
            prev.append({'id': 'default', **old_group['connection']})
        g['sources'] = merge_sources(g['sources'], prev)
        if not has_c:
            g.pop('connection', None)
    if has_c:
        if g['connection'] is None:
            g.pop('connection')
        else:
            prev_c = old_group.get('connection')
            if not isinstance(prev_c, dict):
                prev_c = next((s for s in old_group.get('sources') or []
                               if isinstance(s, dict) and s.get('id') == 'default'), None)
                prev_c = {k: v for k, v in (prev_c or {}).items()
                          if k not in ('id', 'enabled', 'template', 'poll_groups')} or None
            g['connection'] = merge_connection(g['connection'], prev_c)
            if g['connection'] in (None, {}):
                g.pop('connection')
    return g


def merge_endpoint_routes(payload: Dict[str, Any], prev: Dict[str, Any]) -> Dict[str, Any]:
    """An installation payload with every route merged with what is stored."""
    out = dict(payload)
    if 'connection' in out:
        out['connection'] = merge_connection(out['connection'], prev.get('connection'))
    if isinstance(out.get('sources'), list):
        out['sources'] = merge_sources(out['sources'], prev.get('sources') or [])
    if isinstance(out.get('groups'), list):
        old = {str(g.get('id')): g for g in prev.get('groups') or [] if isinstance(g, dict)}
        out['groups'] = [merge_group_route(g, old[str(g.get('id'))])
                         if isinstance(g, dict) and str(g.get('id')) in old else g
                         for g in out['groups']]
    return out


def keep_unsaid(new: Dict[str, Any], old: Dict[str, Any], payload: Dict[str, Any], *,
                own: Iterable[str] = (), blocks: Iterable[str] = ('mqtt', 'influxdb')) -> Dict[str, Any]:
    """The validated ``new`` with what ``old`` holds and the payload did not
    speak of: every stored top-level key missing from ``new``, and per key
    inside ``blocks``. A key the payload carries — ``None`` or empty included —
    is the payload's to say (``totals: {}`` clears them). Keys in ``own`` are
    managed by their own routes: they always come from ``old``.
    """
    own = set(own)
    blocks = set(blocks)
    out = copy.deepcopy(new)
    for k, v in (old or {}).items():
        if k in own:
            out[k] = copy.deepcopy(v)
        elif k in payload and payload[k] is None:
            out.pop(k, None)
        elif k not in out:
            if k not in payload:
                out[k] = copy.deepcopy(v)
        elif k in blocks and isinstance(v, dict) and isinstance(out[k], dict):
            sent = payload.get(k) if isinstance(payload.get(k), dict) else {}
            for kk, vv in v.items():
                if kk in sent and sent[kk] is None:
                    out[k].pop(kk, None)
                elif kk not in out[k] and kk not in sent:
                    out[k][kk] = copy.deepcopy(vv)
    for k in own:
        if k not in (old or {}):
            out.pop(k, None)
    return out
