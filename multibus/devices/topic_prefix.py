# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Where a topic prefix may go (ADR-0026 §4, FINDINGS ADD-11).

Every device, component and installation publishes under its own prefix,
and the gateway under its own root. Two that are equal, or one inside the
other (`a/b` and `a/b/c`), share topics: one's values overwrite the other's,
and deleting one clears the other's retained topics. A `+` or `#` is a
subscription wildcard, never a topic. So a prefix is checked wherever it is
SET (a new device, a new component, an installation's aggregate root);
stored prefixes are only reported, once — refusing them at load would take a
running site dark over a name.
"""
from __future__ import annotations

import logging
from typing import Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# (prefix, owner, words): owner is what may overlap itself (a device, an
# installation with its units, the gateway); words name it in a refusal
Claim = Tuple[str, str, str]

_reported: Set[str] = set()


def shape_error(prefix) -> Optional[str]:
    """Why ``prefix`` is not a topic prefix, or None."""
    p = prefix if isinstance(prefix, str) else ''
    if not p.strip():
        return "required (a topic prefix such as mbg/devices/inverter)"
    if p.startswith('/') or p.endswith('/'):
        return f"'{p}' starts or ends with / — a topic level would be empty"
    if '//' in p:
        return f"'{p}' has an empty level (//)"
    shown = p.replace('\x00', '\\0')
    for ch, name in (('+', '+ (a wildcard'), ('#', '# (a wildcard'), ('\x00', 'a NUL character (')):
        if ch in p:
            return f"'{shown}' contains {name}, never part of a topic)"
    if p != p.strip():
        return f"'{p}' starts or ends with a space"
    return None


def overlap(a: str, b: str) -> Optional[str]:
    """How prefix ``a`` stands to ``b``: 'equal', 'under' (a inside b),
    'over' (b inside a) — by levels, never as strings — or None."""
    if a == b:
        return 'equal'
    if a.startswith(b + '/'):
        return 'under'
    if b.startswith(a + '/'):
        return 'over'
    return None


def claims(config) -> List[Claim]:
    """Every prefix this gateway publishes under, with its owner."""
    out: List[Claim] = []
    gw = str(getattr(config.mqtt, 'topic_prefix', '') or '')
    if gw:
        out.append((gw, 'gateway', f"the gateway's own topics ({gw})"))
    for d in config.devices:
        p = str(getattr(d, 'mqtt_topic_prefix', '') or '')
        if not p or d.primary:
            continue                  # the primary publishes under the gateway's root
        if d.endpoint_id:
            out.append((p, f"installation:{d.endpoint_id}", f"installation '{d.endpoint_id}' (unit {d.id})"))
        else:
            out.append((p, f"device:{d.id}", f"device '{d.id}'"))
    for raw in getattr(config, 'endpoints', None) or []:
        pid = str(raw.get('id', '') or '')
        for root in aggregate_roots(raw, config):
            out.append((root, f"installation:{pid}", f"installation '{pid}'"))
    return out


def aggregate_roots(raw, config=None) -> List[str]:
    """Where an installation publishes its totals: one root per group (the
    first group's is the installation's own)."""
    from .endpoint_aggregator import aggregate_topic
    pid = str(raw.get('id', '') or '')
    groups = [str(g.get('id', '') or '') for g in (raw.get('groups') or []) if isinstance(g, dict)]
    if not groups:
        return [aggregate_topic(raw, pid)]
    return [aggregate_topic(raw, pid, gid, first=(i == 0)) for i, gid in enumerate(groups)]


def conflicts(prefix: str, others: Iterable[Claim], own: Iterable[str]) -> List[str]:
    """What ``prefix`` would share topics with, in words."""
    own = set(own)
    out: List[str] = []
    for q, owner, words in others:
        if owner in own:
            continue
        how = overlap(prefix, q)
        if how == 'equal':
            out.append(f"'{prefix}' is already used by {words}")
        elif how == 'under':
            out.append(f"'{prefix}' is inside {words}'s '{q}'")
        elif how == 'over':
            out.append(f"'{prefix}' holds {words}'s '{q}'")
    return out


def prefix_errors(prefix, config, own: Iterable[str]) -> List[str]:
    """Why ``prefix`` cannot be set (empty: it can), in words."""
    e = shape_error(prefix)
    if e:
        return [e]
    return conflicts(prefix, claims(config), own)[:1]


def report_stored(config) -> List[str]:
    """Stored prefixes that break the rule: logged once per process, never
    refused. Returns what was found (logged or not)."""
    found: List[str] = []
    cl = claims(config)
    for i, (p, owner, words) in enumerate(cl):
        e = shape_error(p)
        if e:
            found.append(f"{words}: {e}")
        for q, o2, w2 in cl[i + 1:]:
            if o2 != owner and overlap(p, q):
                found.append(f"{words} ('{p}') and {w2} ('{q}') share topics")
    for msg in found:
        if msg not in _reported:
            _reported.add(msg)
            logger.warning("topic prefix: %s — kept as stored; a new one like it is refused", msg)
    return found
