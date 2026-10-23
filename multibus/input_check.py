# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""What a formula reads, judged against what the gateway has.

A rule whose signal names a device that is not here, or a field its device
does not read, can only ever be stale. This says so — once, the same way for
the rules list, the editor's preview and an import — without touching how a
formula is evaluated.

``fields_of(id)`` answers for one id:
- ``False``: no device and no installation by that name;
- ``None``: cannot judge (an installation before its first totals, a device
  whose names cannot be listed) — never a problem;
- a set of names: what it reads (an empty set: it reads nothing).
"""
from __future__ import annotations

import difflib
from typing import Callable, Dict, Iterable, List, Optional, Set, Union

from . import expressions

FieldsOf = Callable[[str], Union[None, bool, Set[str]]]


def refs_of(expr: str) -> List[str]:
    """The `device.field` references of a formula, ids as written. Empty when
    the formula does not parse (its own error says why)."""
    try:
        ok, _err, refs = expressions.validate_expression(expr, check_places=False)
    except Exception:  # noqa: BLE001 — a judgement, never a failure
        return []
    if not ok:
        return []
    return sorted({expressions.unprepare_ref(r) for r in refs if '.' in r})


def problems(refs: Iterable[str], fields_of: FieldsOf) -> List[Dict[str, str]]:
    """``[{ref, problem, hint?}]`` with problem ``no_device`` / ``nothing_read``
    / ``no_field``. An array element ``x[0]`` is judged on ``x``."""
    out: List[Dict[str, str]] = []
    cache: Dict[str, Union[None, bool, Set[str]]] = {}
    for ref in refs:
        dev, field = ref.split('.', 1)
        if dev not in cache:
            try:
                cache[dev] = fields_of(dev)
            except Exception:  # noqa: BLE001
                cache[dev] = None
        names = cache[dev]
        if names is None or names is True:
            continue
        if names is False:
            out.append({'ref': ref, 'problem': 'no_device'})
            continue
        base = field.split('[', 1)[0]
        if field in names or base in names:
            continue
        if not names:
            out.append({'ref': ref, 'problem': 'nothing_read'})
            continue
        p = {'ref': ref, 'problem': 'no_field'}
        near = difflib.get_close_matches(base, sorted(names), n=1, cutoff=0.75)
        if near:
            p['hint'] = near[0]
        out.append(p)
    return out


def text(p: Dict[str, str]) -> str:
    """One problem in words."""
    dev, field = p['ref'].split('.', 1)
    if p['problem'] == 'no_device':
        return f"reads '{p['ref']}' — there is no device or installation '{dev}'"
    if p['problem'] == 'nothing_read':
        return f"reads '{p['ref']}' — {dev} reads no measurement"
    hint: Optional[str] = p.get('hint')
    return f"reads '{p['ref']}' — {dev} has no field '{field}'" + (f"; did you mean '{hint}'?" if hint else '')
