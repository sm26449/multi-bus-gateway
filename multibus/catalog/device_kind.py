# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""What kind of equipment a device is — one answer for every list, filter
and attribute.

The kind is told, first hit wins, by:
1. the device's own override (``kind:`` on its entry, set by an admin);
2. the role of its installation group;
3. its component type (a component's template carries the kind its parent
   declares for it);
4. its template: the declared ``kind``, else a known template id, else what
   its canonical names say;
5. nothing → ``other``.

``site`` is the API value for a system / GX (the vocabulary templates and
attributes already use). ``other`` is never stored.

These are the built-in kinds, the ones that carry behaviour. A user type
(``device_types.py``, ADR-0027) is stored in the same place as an override
or a group's role and is told here as its ``behaves_like``: what goes out is
always one of these.
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

KINDS = ('meter', 'inverter', 'battery', 'site', 'heat_pump', 'drive',
         'ups', 'charger', 'sensor', 'converter', 'other')
# the kinds the attribute service has always published in `<dev>/meta` (a
# heat pump or a drive was nothing to it, and stays nothing: the wire `kind`
# of every existing device is unchanged)
META_KINDS = ('inverter', 'meter', 'battery', 'sensor', 'site', 'ups', 'charger')
SOURCES = ('override', 'role', 'component', 'template', 'names', 'none')
# an override may name any kind but `other` (no override is what says that)
OVERRIDABLE = tuple(k for k in KINDS if k != 'other')
# what a device that IS its meters may be (a UPS, a charger, a boiler…):
# never a meter (it is measured BY meters) and never `other`; the usual
# ones first, the rest offered under "more"
METER_ONLY_FIRST = ('ups', 'charger', 'heat_pump', 'drive')
METER_ONLY = METER_ONLY_FIRST + tuple(k for k in OVERRIDABLE if k not in METER_ONLY_FIRST and k != 'meter')
# words accepted on input for the same kind
_ALIASES = {'system': 'site', 'gx': 'site', 'heatpump': 'heat_pump', 'vfd': 'drive'}
# bundled templates that declared no kind before 3.97 (they declare it now;
# a user copy saved from an older file may still lack it)
_BY_TEMPLATE = {'delta_vfd_el': 'drive', 'samsung_mim_b19n': 'heat_pump'}


def normalize(value: Any) -> Optional[str]:
    """A kind as written (any case, an alias) → its API value, or None when it
    is not one."""
    if not isinstance(value, str):
        return None
    k = value.strip().lower().replace('-', '_').replace(' ', '_')
    k = _ALIASES.get(k, k)
    return k if k in KINDS else None


def _resolve(value: Any) -> Tuple[Optional[str], bool]:
    """A stored type → (its built-in kind or None, whether it is a user
    type). A user type that behaves like ``other`` still counts as said."""
    k = normalize(value)
    if k is not None:
        return k, False
    from .device_types import builtin_of, is_user_type
    if is_user_type(value):
        return builtin_of(value), True
    return None, False


def _template_of(dev) -> str:
    """The template a device reads with: its own, else its first source's."""
    tid = getattr(dev, 'template', '') or ''
    if tid:
        return tid
    return next((s.template for s in (getattr(dev, 'sources', None) or []) if getattr(s, 'template', '')), '')


def template_kind(tpl, template_id: str = '') -> Tuple[str, str]:
    """A template's kind and how it is known: ``template`` (declared or a
    known id), ``names`` (its canonical names) or ``none``."""
    from .device_template import kind_from_names
    declared = normalize(getattr(tpl, 'kind', '') if tpl is not None else '')
    if declared and declared != 'other':
        return declared, 'template'
    base = (template_id or getattr(tpl, 'id', '') or '').split(':', 1)[0]
    if base in _BY_TEMPLATE:
        return _BY_TEMPLATE[base], 'template'
    named = normalize(kind_from_names(tpl)) if tpl is not None else None
    if named:
        return named, 'names'
    return 'other', 'none'


def device_kind(dev, templates) -> Tuple[str, str]:
    """``(kind, source)`` of a device; ``templates`` answers ``get(id)``.
    Never raises: anything it cannot read is skipped."""
    try:
        own, user = _resolve(getattr(dev, 'kind', ''))
        if own and (own != 'other' or user):
            return own, 'override'
        role, user = _resolve(getattr(dev, 'role', ''))
        if role and (role != 'other' or user):
            return role, 'role'
        tid = _template_of(dev)
        tpl = templates.get(tid) if (templates is not None and tid) else None
        if getattr(dev, 'component_of', '') and tpl is not None:
            ck = normalize(getattr(tpl, 'kind', ''))
            if ck and ck != 'other':
                return ck, 'component'
        if tpl is None and not tid:
            return 'other', 'none'
        return template_kind(tpl, tid)
    except Exception:  # noqa: BLE001 — a kind is a label: never fail a list over it
        return 'other', 'none'


def attribute_kind(dev, templates) -> Optional[str]:
    """The kind the attribute catalog speaks of, as the attribute service has
    always said it: a group's role exactly as written, else the template's
    kind in the catalog's words (its KINDS stay as they are), else None. Its
    published ``meta`` therefore stays the same for every device nobody gave
    an override. (A hand-written role such as ``grid`` is said as written
    here; the device list, which speaks only in kinds, files that unit by
    its template.)"""
    role = getattr(dev, 'role', '') or ''
    if role:
        b, user = _resolve(role)
        if user:
            # a user type never goes out: its behaves_like does, and only a
            # kind meta has always published
            return b if b in META_KINDS else None
        return role                     # as it always was: a group's role, said as written
    k, _src = device_kind(dev, templates)
    return k if k in META_KINDS else None
