# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""What uses a device, an installation or a template.

Nothing is deleted while something depends on it: a rule that commands or
reads it, a sequence step, another device's calculated field, a device it
measures, a virtual meter it feeds, a template formula that names it. The
answer has three parts:

- ``blocking``: dependents that would break. A delete is refused (409) until
  each is changed or removed — or the object is disabled instead, which
  keeps every reference intact.
- ``follows``: what goes with the object (its components, an installation's
  units, its attribute entries, a saved formula preset that names it).
- ``warnings``: what keeps working but should be known (a rule that commands
  the unit's whole group: it goes on with the units that remain).

Each entry is ``{type, id, label, where, ref: {type, id, tab?}, of}``:
``type`` says what kind of dependent it is, ``where`` the field in it that
names the object, ``ref`` how to open it, ``of`` which watched id it names.

One pass builds an index of every reference the configuration makes (each
formula scanned once, by one compiled expression); a single check and the
counts for a list both read that index, so a list's "used by N" is what a
delete would refuse. Nothing is cached between calls: a stale answer is what
this exists to prevent. A source that cannot be read (rules.yaml, a
calculated-fields file, a virtual meter template) makes the check fail
closed: :class:`Unreadable`, never "nothing uses it".
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from .meter_links import links_of

logger = logging.getLogger(__name__)

# the expressions a rule reads its inputs through (rules_runtime._INPUT_KEYS)
RULE_INPUT_KEYS = ('signal', 'when', 'release')
# the expressions a sequence step reads
SEQUENCE_EXPR_KEYS = ('wait_until', 'check')
# a template formula's own device
_SELF = '{device}'

# `<id>.<field>`: an id may hold hyphens (`pv-u1`), so `a-b.x` is either the
# device `a-b` or `a` minus the device `b` — decided against the known ids
_REF = re.compile(r'(?<![\w.\-])([A-Za-z0-9_][\w\-]*)\.(?=[A-Za-z_])')
# the place arguments: every string of sun_*() and is_day(), the first of attr()
_PLACE_CALL = re.compile(r'\b(sun_elevation|sun_azimuth|is_day|attr)\s*\(([^()]*)\)')
_QUOTED = re.compile(r'"([^"\\]*)"|\'([^\'\\]*)\'')


class Unreadable(Exception):
    """A source of references could not be read: what uses the object is not
    known, so nothing is deleted."""


@dataclass
class Sources:
    """What the index reads. Callables, so a caller gives only what it has
    (a test builds one from plain lists)."""
    config: Any
    rules: Callable[[], Dict[str, Dict]] = field(default=lambda: {})
    sequences: Callable[[], List[Dict]] = field(default=lambda: [])
    # template id -> the device ids its rows and instance read
    vmeter_sources: Callable[[], Dict[str, Iterable[str]]] = field(default=lambda: {})
    templates: Callable[[], Iterable[Any]] = field(default=lambda: ())
    attributes: Callable[[], Dict[str, Any]] = field(default=lambda: {})
    # device id -> its calculated rows; raises when its file cannot be read
    calculated: Optional[Callable[[str], List[Dict]]] = None
    # sources that report they could not be read ("rules.yaml: …")
    unreadable: Callable[[], List[str]] = field(default=lambda: [])


def _dep(type_: str, oid: str, label: str, where: str, ref_type: str, ref_id: str,
         of: str, tab: str = '') -> Dict[str, Any]:
    ref = {'type': ref_type, 'id': ref_id}
    if tab:
        ref['tab'] = tab
    return {'type': type_, 'id': oid, 'label': label or oid, 'where': where, 'ref': ref, 'of': of}


# ── which known ids a formula names ──────────────────────────────────────────

def _resolve(tok: str, known: Set[str]) -> Optional[str]:
    """`pv-u1` if it is an id; else the longest id after a hyphen (`a-b`
    with no device `a-b` is `a` minus `b`)."""
    if tok in known:
        return tok
    i = tok.find('-')
    while i != -1:
        tail = tok[i + 1:]
        if tail in known:
            return tail
        i = tok.find('-', i + 1)
    return None


def names_in(expr: Any, known: Set[str]) -> Set[str]:
    """The ids of ``known`` a formula names: as the device of an
    ``id.field`` reference, or as the place of ``sun_*()`` / ``is_day()`` /
    ``attr()``. A text scan, so a formula that does not parse (a rule
    already in error) is read the same way — a broken dependent is still a
    dependent. A quoted string elsewhere (a rule id in run_today_h("…"))
    is not a reference."""
    text = str(expr or '')
    if not text or not known:
        return set()
    found: Set[str] = set()
    for m in _REF.finditer(text):
        hit = _resolve(m.group(1), known)
        if hit is not None:
            found.add(hit)
    for m in _PLACE_CALL.finditer(text):
        args = [a or b for a, b in _QUOTED.findall(m.group(2))]
        for a in (args[:1] if m.group(1) == 'attr' else args):
            if a in known:
                found.add(a)
    return found


# kept for callers that named it: the same scan
def named_in(expr: Any, ids: Set[str]) -> Set[str]:
    return names_in(expr, set(ids))


# ── the index ────────────────────────────────────────────────────────────────

@dataclass
class _Ref:
    dep: Dict[str, Any]           # the entry, `of` filled per answer
    names: Set[str]               # the ids it names
    owner: str = ''               # the device it lives on (its own formula is not a dependent)
    group: Tuple[str, str] = ('', '')   # a rule target {endpoint, group}


class Index:
    """Every reference the configuration makes, built in one pass."""

    def __init__(self, src: Sources):
        self.src = src
        config = src.config
        self.devices = {d.id: d for d in list(getattr(config, 'devices', None) or [])}
        self.endpoints = [p for p in list(getattr(config, 'endpoints', None) or []) if p.get('id')]
        self.installations = {str(p['id']) for p in self.endpoints}
        self.known = set(self.devices) | self.installations
        self.refs: List[_Ref] = []
        self.by_name: Dict[str, List[int]] = {}
        self._rules(src.rules() or {})
        self._sequences(src.sequences() or [])
        problems = list(src.unreadable() or [])     # asked after the reads: they set it
        problems += self._calculated()
        self._meter_links()
        problems += self._vmeters()
        self._template_formulas()
        if problems:
            raise Unreadable('; '.join(problems))
        for i, r in enumerate(self.refs):
            for n in r.names:
                self.by_name.setdefault(n, []).append(i)

    def _add(self, dep, names, owner='', group=('', '')):
        names = set(names)
        if names or group[0]:
            self.refs.append(_Ref(dep, names, owner, group))

    def _rules(self, rules):
        for rid, raw in rules.items():
            if not isinstance(raw, dict):
                continue
            label = str(raw.get('label') or rid)
            t = raw.get('target') if isinstance(raw.get('target'), dict) else {}
            dev, ep, grp = str(t.get('device') or ''), str(t.get('endpoint') or ''), str(t.get('group') or '')
            if dev:
                self._add(_dep('rule_target', rid, label, 'target', 'rule', rid, ''), {dev})
            elif ep:
                # the installation is named; its units are reached as a group
                self._add(_dep('rule_target', rid, label, 'target', 'rule', rid, ''), {ep},
                          group=(ep, grp))
            for key in RULE_INPUT_KEYS:
                ns = names_in(raw.get(key), self.known)
                if ns:
                    self._add(_dep('rule_input', rid, label, key, 'rule', rid, ''), ns)

    def _sequences(self, seqs):
        for raw in seqs:
            if not isinstance(raw, dict):
                continue
            sid = str(raw.get('id') or '')
            label = str(raw.get('label') or sid)
            for part in ('steps', 'on_abort'):
                for i, step in enumerate(raw.get(part) or []):
                    if not isinstance(step, dict):
                        continue
                    cmd = step.get('command') if isinstance(step.get('command'), dict) else {}
                    if cmd.get('device'):
                        self._add(_dep('sequence_step', sid, label, f"{part}[{i}].command", 'sequence', sid, ''),
                                  {str(cmd['device'])})
                    for key in SEQUENCE_EXPR_KEYS:
                        ns = names_in(step.get(key), self.known)
                        if ns:
                            self._add(_dep('sequence_step', sid, label, f"{part}[{i}].{key}", 'sequence', sid, ''),
                                      ns)

    def _calculated(self) -> List[str]:
        """Every device's calculated fields — from its file, so a disabled
        device counts too. A file that cannot be read fails the check."""
        read = self.src.calculated or getattr(self.src.config, 'load_calculated', None)
        if read is None:
            return []
        bad = []
        for d in self.devices.values():
            try:
                rows = read(d.id) or []
            except Exception as e:  # noqa: BLE001 — said, and the check fails closed
                bad.append(f"the calculated fields of {d.id!r} could not be read ({e})")
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                ns = names_in(row.get('expr'), self.known)
                if ns:
                    name = str(row.get('name') or '')
                    self._add(_dep('calculated', d.id, d.name or d.id, f"calculated.{name}", 'device', d.id, '',
                                   tab='calculated'), ns, owner=d.id)
        return bad

    def _meter_links(self):
        for d in self.devices.values():
            for ln in links_of(d):
                if ln['device'] in self.devices:
                    self._add(_dep('meter_link', d.id, d.name or d.id, f"meters.{ln['role']}", 'device', d.id, '',
                                   tab='meters'), {ln['device']}, owner=d.id)

    def _vmeters(self) -> List[str]:
        try:
            srcs = self.src.vmeter_sources() or {}
        except Unreadable as e:
            return [str(e)]
        except Exception as e:  # noqa: BLE001
            return [f"the virtual meters could not be read ({e})"]
        for tid, devs in sorted(srcs.items()):
            ns = {x for x in devs if x in self.known}
            if ns:
                self._add(_dep('virtual_meter', tid, tid, 'source', 'virtual_meter', tid, ''), ns)
        return []

    def _template_formulas(self):
        for t in self.src.templates() or ():
            for c in getattr(t, 'calculated', None) or []:
                expr = str(getattr(c, 'expr', '') or (c.get('expr') if isinstance(c, dict) else '') or '')
                ns = names_in(expr.replace(_SELF, '__self__'), self.known)
                if ns:
                    name = getattr(c, 'name', '') or (c.get('name') if isinstance(c, dict) else '')
                    self._add(_dep('template_formula', t.id, getattr(t, 'name', '') or t.id,
                                   f"calculated.{name}", 'template', t.id, ''), ns)

    # ── answers ──
    def blocking(self, ids: Set[str], *, installations: Set[str] = frozenset()) -> List[Dict]:
        """What outside ``ids`` names one of them. A rule targeting an
        installation's group blocks only through the installation itself."""
        out = []
        seen = set()
        for n in sorted(ids):
            for i in self.by_name.get(n, ()):
                r = self.refs[i]
                if r.owner in ids:
                    continue                 # its own formula, or one that goes with it
                if r.group[0] and r.group[0] not in installations:
                    continue                 # a group target is not a reference to a unit
                key = (i, n)
                if key not in seen:
                    seen.add(key)
                    out.append({**r.dep, 'of': n})
        return out

    def group_targets(self, ep: str, groups: Set[str]) -> List[Dict]:
        return [{**r.dep, 'of': f"{ep}/{r.group[1]}"} for r in self.refs
                if r.group[0] == ep and (not r.group[1] or r.group[1] in groups)]


def _attribute_entries(doc: Dict[str, Any], level: str, oid: str) -> List[str]:
    held = ((doc or {}).get(level) or {}).get(oid)
    return sorted(held) if isinstance(held, dict) else []


def _safe(fn: Callable[[], Any], default: Any) -> Any:
    try:
        v = fn()
    except Exception:  # noqa: BLE001
        return default
    return default if v is None else v


def _finish(out: Dict[str, List[Dict]]) -> Dict[str, List[Dict]]:
    for part in out:
        seen, uniq = set(), []
        for d in out[part]:
            key = (d['type'], d['id'], d['where'], d['of'])
            if key not in seen:
                seen.add(key)
                uniq.append(d)
        out[part] = sorted(uniq, key=lambda d: (d['type'], d['id'], d['where'], d['of']))
    return out


def _presets(config: Any, ids: Set[str]) -> List[Dict]:
    out = []
    for p in _safe(getattr(config, 'load_calculated_templates', lambda: []), []) or []:
        if not isinstance(p, dict):
            continue
        for of in sorted(names_in(p.get('expr'), ids)):
            pid = str(p.get('name') or p.get('id') or '')
            out.append(_dep('calculated_preset', pid, str(p.get('label') or pid), 'expr',
                            'calculated_preset', pid, of))
    return out


def _device_ids(ix: Index, oid: str) -> Set[str]:
    return {oid} | {d.id for d in ix.devices.values() if getattr(d, 'component_of', '') == oid}


def _unit_ids(ix: Index, ep: str) -> Set[str]:
    return {d.id for d in ix.devices.values() if getattr(d, 'endpoint_id', '') == ep}


def _device_answer(ix: Index, oid: str) -> Dict[str, List[Dict]]:
    ids = _device_ids(ix, oid)
    out = {'blocking': ix.blocking(ids), 'follows': [], 'warnings': []}
    for c in sorted(ids - {oid}):
        d = ix.devices[c]
        out['follows'].append(_dep('component', c, d.name or c, 'components', 'device', c, oid))
    dev = ix.devices.get(oid)
    if dev is not None and getattr(dev, 'endpoint_id', ''):
        g = getattr(dev, 'group_id', '') or 'units'
        for w in ix.group_targets(dev.endpoint_id, {g}):
            out['warnings'].append({**w, 'of': oid})
    doc = _safe(ix.src.attributes, {})
    for i in sorted(ids):
        for cat in _attribute_entries(doc, 'device', i):
            out['follows'].append(_dep('attribute', cat, cat, f"device.{i}", 'device', i, i, tab='attributes'))
    out['follows'] += _presets(ix.src.config, ids)
    return _finish(out)


def _installation_answer(ix: Index, ep: str) -> Dict[str, List[Dict]]:
    units = _unit_ids(ix, ep)
    ids = {ep} | units
    out = {'blocking': ix.blocking(ids, installations={ep}), 'follows': [], 'warnings': []}
    for u in sorted(units):
        d = ix.devices[u]
        out['follows'].append(_dep('device', u, d.name or u, 'units', 'device', u, ep))
    doc = _safe(ix.src.attributes, {})
    for cat in _attribute_entries(doc, 'installation', ep):
        out['follows'].append(_dep('attribute', cat, cat, f"installation.{ep}", 'installation', ep, ep,
                                   tab='attributes'))
    for u in sorted(units):
        for cat in _attribute_entries(doc, 'device', u):
            out['follows'].append(_dep('attribute', cat, cat, f"device.{u}", 'device', u, u, tab='attributes'))
    out['follows'] += _presets(ix.src.config, ids)
    return _finish(out)


def _template_answer(src: Sources, tid: str) -> Dict[str, List[Dict]]:
    config = src.config
    out = {'blocking': [], 'follows': [], 'warnings': []}
    for p in list(getattr(config, 'endpoints', None) or []):
        ep = str(p.get('id') or '')
        name = str(p.get('name') or ep)
        if str(p.get('template') or '') == tid:
            out['blocking'].append(_dep('installation', ep, name, 'template', 'installation', ep, tid))
        for j, s in enumerate(p.get('sources') or []):
            if isinstance(s, dict) and str(s.get('template') or '') == tid:
                out['blocking'].append(_dep('installation_source', ep, name, f"sources[{j}].template",
                                            'installation', ep, tid))
        for i, g in enumerate(p.get('groups') or []):
            if not isinstance(g, dict):
                continue
            if str(g.get('template') or '') == tid:
                out['blocking'].append(_dep('installation_group', ep, name, f"groups[{i}].template",
                                            'installation', ep, tid))
            for j, s in enumerate(g.get('sources') or []):
                if isinstance(s, dict) and str(s.get('template') or '') == tid:
                    out['blocking'].append(_dep('installation_source', ep, name,
                                                f"groups[{i}].sources[{j}].template", 'installation', ep, tid))
    for d in getattr(config, 'devices', None) or []:
        if getattr(d, 'endpoint_id', ''):
            continue                      # said once, as its installation
        names = {str(getattr(d, 'template', '') or '')} | {
            str(getattr(s, 'template', '') or '') for s in getattr(d, 'sources', None) or []}
        if tid in names or any(n.split(':', 1)[0] == tid for n in names if ':' in n):
            where = 'template' if (getattr(d, 'template', '') or '').split(':', 1)[0] == tid else 'sources'
            out['blocking'].append(_dep('device', d.id, d.name or d.id, where, 'device', d.id, tid))
    return _finish(out)


def dependents_of(kind: str, oid: str, src: Sources, index: Optional[Index] = None) -> Dict[str, List[Dict]]:
    """``{blocking, follows, warnings}`` for a ``device`` (a component and an
    installation's unit are devices), an ``installation`` or a ``template``.
    Raises :class:`Unreadable` when a source could not be read."""
    if kind == 'template':
        return _template_answer(src, oid)
    ix = index or Index(src)
    if kind == 'device':
        return _device_answer(ix, oid)
    if kind == 'installation':
        return _installation_answer(ix, oid)
    raise ValueError(f"unknown kind {kind!r}")


def units_dependents(src: Sources, ep: str, unit_ids: Set[str], groups: Set[str] = frozenset(),
                     moved: Optional[Dict[str, Tuple[str, str]]] = None) -> Dict[str, List[Dict]]:
    """What an installation edit runs into.

    - ``blocking``: what names a unit that goes (a rule that commands or
      reads it, a sequence, a formula, a meter link, a virtual meter), and a
      rule that targets a group that goes;
    - ``warnings``: a rule that targets the group a unit leaves (removed, or
      moved to another group by ``moved: {unit: (old, new)}``) — it keeps
      working with the units that remain."""
    ix = Index(src)
    ids = set(unit_ids)
    blocking = ix.blocking(ids)
    if groups:
        blocking += ix.group_targets(ep, set(groups))      # a rule whose whole group goes
    warnings: List[Dict] = []
    left: Dict[str, Set[str]] = {}
    for u in ids:
        d = ix.devices.get(u)
        if d is not None:
            left.setdefault(getattr(d, 'group_id', '') or 'units', set()).add(u)
    for u, (old, _new) in (moved or {}).items():
        left.setdefault(old or 'units', set()).add(u)
    for g, us in left.items():
        if g in groups:
            continue                         # the whole group goes: blocking above
        for w in ix.group_targets(ep, {g}):
            for u in sorted(us):
                warnings.append({**w, 'of': u})
    return _finish({'blocking': blocking, 'warnings': warnings})


def summary(deps: Dict[str, List[Dict]], what: str) -> str:
    """One sentence for the 409: what uses it, and the way out."""
    words = {'rule_target': 'rule', 'rule_input': 'rule', 'sequence_step': 'sequence',
             'calculated': 'calculated field on another device', 'meter_link': 'device it measures',
             'virtual_meter': 'virtual meter', 'template_formula': 'template formula',
             'installation': 'installation', 'installation_group': 'installation',
             'installation_source': 'installation', 'device': 'device'}
    counted: Dict[str, Set[str]] = {}
    for d in deps.get('blocking') or []:
        counted.setdefault(words.get(d['type'], d['type']), set()).add(d['id'])
    parts = []
    for word, ids in sorted(counted.items()):
        n = len(ids)
        parts.append(f"{n} {word}{'' if n == 1 else 's'} ({', '.join(sorted(ids)[:5])}"
                     f"{', …' if n > 5 else ''})")
    return f"{what} is used by " + '; '.join(parts) + " — change or remove them first, or disable it instead"


def dependents_index(src: Sources) -> Dict[str, int]:
    """id → how many things use it, for every device and installation — the
    same answer a delete would refuse with (a parent counts its components'
    users, an installation its units'), from one index."""
    ix = Index(src)
    out: Dict[str, int] = {}
    for oid in ix.devices:
        ids = _device_ids(ix, oid)
        n = len({(x['ref']['type'], x['id']) for x in ix.blocking(ids)})
        if n:
            out[oid] = n
    for ep in ix.installations:
        n = len({(x['ref']['type'], x['id']) for x in ix.blocking({ep} | _unit_ids(ix, ep), installations={ep})})
        if n:
            out[ep] = n
    return out


def dangling(src: Sources, known: Optional[Set[str]] = None) -> List[Dict]:
    """References a configuration makes to devices or installations it does
    not have (after an import or a restore, which are not refused: they
    replace the whole set at once). ``[{type, id, where, missing, ref}]``."""
    from . import expressions
    from .input_check import refs_of
    config = src.config
    if known is None:
        known = {d.id for d in getattr(config, 'devices', None) or []} | {
            str(p.get('id')) for p in getattr(config, 'endpoints', None) or [] if p.get('id')}
    out: List[Dict] = []
    for rid, raw in (src.rules() or {}).items():
        if not isinstance(raw, dict):
            continue
        t = raw.get('target') if isinstance(raw.get('target'), dict) else {}
        for key in ('device', 'endpoint'):
            v = str(t.get(key) or '')
            if v and v not in known:
                out.append({'type': 'rule_target', 'id': rid, 'where': 'target', 'missing': v,
                            'ref': {'type': 'rule', 'id': rid}})
        for key in RULE_INPUT_KEYS:
            text = str(raw.get(key) or '')
            if not text:
                continue
            dashed = expressions.dashed_unknown(text) or []
            for ref in refs_of(text):
                dev = ref.split('.', 1)[0]
                # `pv-u9.x` with no pv-u9 parses as pv − u9.x: said once, whole
                if dev not in known and not any(t2.endswith('-' + dev) for t2 in dashed):
                    out.append({'type': 'rule_input', 'id': rid, 'where': key, 'missing': dev,
                                'ref': {'type': 'rule', 'id': rid}})
            for dev in dashed:
                if dev not in known:
                    out.append({'type': 'rule_input', 'id': rid, 'where': key, 'missing': dev,
                                'ref': {'type': 'rule', 'id': rid}})
    for raw in src.sequences() or []:
        if not isinstance(raw, dict):
            continue
        sid = str(raw.get('id') or '')
        for part in ('steps', 'on_abort'):
            for i, step in enumerate(raw.get(part) or []):
                cmd = step.get('command') if isinstance(step, dict) and isinstance(step.get('command'), dict) else {}
                v = str(cmd.get('device') or '')
                if v and v not in known:
                    out.append({'type': 'sequence_step', 'id': sid, 'where': f"{part}[{i}].command",
                                'missing': v, 'ref': {'type': 'sequence', 'id': sid}})
    uniq, seen = [], set()
    for d in out:
        k = (d['type'], d['id'], d['where'], d['missing'])
        if k not in seen:
            seen.add(k)
            uniq.append(d)
    return uniq
