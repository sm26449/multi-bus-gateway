# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The device-type catalog: one list for every screen (ADR-0027).

- **Built-in kinds** (``device_kind.KINDS``) carry behaviour: meter-only
  kinds, site calculations, group roles, dashboard order.
- **User types** live in ``config.yaml`` under ``device_types:``:

      device_types:
        - id: chiller
          labels: {en: Chiller, ro: Răcitor}
          icon: snow
          behaves_like: heat_pump

  A user type never reaches the wire: the published ``kind`` is its
  ``behaves_like`` (a built-in), so a consumer never sees an unknown kind.

The user types are held here for the derivation (``device_kind``), set by
the configuration each time it loads and by the API on each change.
"""
from __future__ import annotations

import re
import threading
from typing import Any, Dict, List, Optional, Tuple

from .device_kind import KINDS, OVERRIDABLE, normalize

ID_RE = re.compile(r'^[a-z][a-z0-9_]{1,31}$')
LANG_RE = re.compile(r'^[a-z]{2}$')
MAX_LABEL = 40
MAX_TYPES = 64

# what a built-in is called, in the languages the UI ships (the UI files keep
# their `kind.<id>` keys for the same words)
BUILTIN_LABELS: Dict[str, Dict[str, str]] = {
    'meter': {'en': 'Meter', 'ro': 'Contor'},
    'inverter': {'en': 'Inverter', 'ro': 'Invertor'},
    'battery': {'en': 'Battery / BMS', 'ro': 'Baterie / BMS'},
    'site': {'en': 'System / GX', 'ro': 'Sistem / GX'},
    'heat_pump': {'en': 'Heat pump', 'ro': 'Pompă de căldură'},
    'drive': {'en': 'Drive (VFD)', 'ro': 'Variator (VFD)'},
    'ups': {'en': 'UPS', 'ro': 'UPS'},
    'charger': {'en': 'EV charger', 'ro': 'Stație de încărcare'},
    'sensor': {'en': 'Sensor', 'ro': 'Senzor'},
    'converter': {'en': 'Converter (DC/DC, AC/DC)', 'ro': 'Convertor (DC/DC, AC/DC)'},
    'other': {'en': 'Other', 'ro': 'Altele'},
}

# the fixed icon set: a name → its Bootstrap Icons class. A built-in's icon is
# its own id; a user type picks one of these
ICONS: Dict[str, str] = {
    'meter': 'bi-speedometer2', 'inverter': 'bi-sun', 'battery': 'bi-battery-half',
    'site': 'bi-hdd-stack', 'heat_pump': 'bi-thermometer-sun', 'drive': 'bi-gear-wide-connected',
    'ups': 'bi-plug', 'charger': 'bi-ev-station', 'sensor': 'bi-thermometer-half',
    'converter': 'bi-arrow-left-right', 'other': 'bi-box',
    'sun': 'bi-sun', 'fan': 'bi-fan', 'pump': 'bi-droplet', 'snow': 'bi-snow', 'fire': 'bi-fire',
    'switch': 'bi-toggle-on', 'light': 'bi-lightbulb', 'lightning': 'bi-lightning-charge',
    'tank': 'bi-database', 'water': 'bi-water', 'wind': 'bi-wind', 'cpu': 'bi-cpu',
    'router': 'bi-router', 'tools': 'bi-tools', 'house': 'bi-house', 'car': 'bi-car-front',
}

_lock = threading.Lock()
_user: Dict[str, Dict[str, Any]] = {}


# ── the catalog held now ─────────────────────────────────────────────────────

def set_user_types(raw: Any) -> Dict[str, str]:
    """Hold the user types as the configuration has them. An entry that
    cannot be read is skipped (never the configuration) and named in the
    answer ``{where: why}``."""
    held: Dict[str, Dict[str, Any]] = {}
    ignored: Dict[str, str] = {}
    for i, entry in enumerate(raw if isinstance(raw, list) else []):
        clean, errs = validate(entry, taken=held)
        if errs:
            ignored[f"device_types[{i}]"] = '; '.join(f"{k}: {v}" for k, v in errs.items())
            continue
        held[clean['id']] = clean
    with _lock:
        _user.clear()
        _user.update(held)
    return ignored


def user_types() -> List[Dict[str, Any]]:
    with _lock:
        return [dict(v, labels=dict(v['labels'])) for v in _user.values()]


def is_user_type(value: Any) -> bool:
    return isinstance(value, str) and value in _user


def is_type(value: Any) -> bool:
    """A catalog id as stored: a built-in or a user type (no alias)."""
    return isinstance(value, str) and (value in KINDS or value in _user)


def builtin_of(value: Any) -> Optional[str]:
    """The built-in kind a type stands for: a built-in (any alias) itself, a
    user type its ``behaves_like``; None when it is neither."""
    k = normalize(value)
    if k:
        return k
    u = _user.get(value) if isinstance(value, str) else None
    return u['behaves_like'] if u else None


def label(type_id: str, lang: str = 'en') -> str:
    """A type's words in a language, English as the fallback."""
    labels = BUILTIN_LABELS.get(type_id) or (_user.get(type_id) or {}).get('labels') or {}
    return labels.get(lang) or labels.get('en') or next(iter(labels.values()), type_id)


# ── checks ───────────────────────────────────────────────────────────────────

def validate(entry: Any, *, taken: Optional[Dict] = None,
             existing_id: Optional[str] = None) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """One user type as it would be stored: ``(clean, {field: why})``."""
    errs: Dict[str, str] = {}
    if not isinstance(entry, dict):
        return {}, {'type': 'an object {id, labels, icon, behaves_like}'}
    tid = entry.get('id', existing_id)
    tid = tid.strip() if isinstance(tid, str) else tid
    if existing_id is not None and tid != existing_id:
        errs['id'] = 'cannot be changed — add a new type and move the devices to it'
    elif not isinstance(tid, str) or not ID_RE.match(tid):
        errs['id'] = 'a-z 0-9 _ (2–32 characters, starts with a letter)'
    elif normalize(tid) is not None:
        errs['id'] = f"{tid!r} is a built-in type ({normalize(tid)}) — pick another id"
    elif taken is not None and tid in taken:
        errs['id'] = f"{tid!r} exists already"
    labels = entry.get('labels')
    clean_labels: Dict[str, str] = {}
    if not isinstance(labels, dict) or not labels:
        errs['labels'] = 'a map {language: words}, at least {en: …}'
    else:
        from .text_check import topic_issue
        for lang, text in labels.items():
            if not isinstance(lang, str) or not LANG_RE.match(lang):
                errs['labels'] = f"{str(lang)[:8]!r}: a two-letter language code (en, ro)"
                break
            if not isinstance(text, str) or not text.strip():
                errs[f'labels.{lang}'] = 'words'
                continue
            text = text.strip()
            if len(text) > MAX_LABEL:
                errs[f'labels.{lang}'] = f"at most {MAX_LABEL} characters"
                continue
            issue = topic_issue(text, f"labels.{lang}")
            if issue:
                errs[f'labels.{lang}'] = issue.split(': ', 1)[-1]
                continue
            clean_labels[lang] = text
        if 'labels' not in errs and 'en' not in labels:
            errs['labels.en'] = 'required — the English words are the fallback for every other language'
    icon = entry.get('icon', 'other') or 'other'
    if icon not in ICONS:
        errs['icon'] = f"one of {', '.join(ICONS)}"
    behaves = entry.get('behaves_like', 'other') or 'other'
    if behaves not in KINDS:
        errs['behaves_like'] = f"one of {', '.join(KINDS)}"
    extra = sorted(k for k in entry if k not in ('id', 'labels', 'icon', 'behaves_like'))
    if extra:
        errs[extra[0]] = 'not a field of a device type (id, labels, icon, behaves_like)'
    if errs:
        return {}, errs
    return {'id': tid, 'labels': clean_labels, 'icon': icon, 'behaves_like': behaves}, {}


def override_error(value: Any, *, meter_only: bool = False) -> Optional[str]:
    """Why a device's own type cannot be ``value`` (None when it can)."""
    from .device_kind import METER_ONLY
    k = normalize(value)
    if k is None and not is_user_type(value):
        return f"{str(value)[:40]!r}: one of {', '.join(type_ids(include_other=False))}"
    if k == 'other':
        return "other: send null — a device with no type of its own shows other"
    if meter_only:
        b = builtin_of(value)
        if (k is not None and k not in METER_ONLY) or (k is None and b == 'meter'):
            return (f"a device measured only by its meters is one of {', '.join(METER_ONLY)} or a user type "
                    "that does not behave like a meter (it has no template to derive a kind from, "
                    "and it is not a meter)")
    return None


def stored_form(value: Any) -> str:
    """What a device stores for a type it was given: a built-in by its id
    (an alias read as its kind), a user type as it is."""
    return normalize(value) or str(value)


def from_payload(payload: Any) -> Tuple[Any, Optional[str]]:
    """The type a request names: ``type``, or ``kind`` (its older word).
    ``(value or None, why it cannot be read)``."""
    if not isinstance(payload, dict):
        return None, None
    t, k = payload.get('type'), payload.get('kind')
    if t not in (None, '') and k not in (None, '') and stored_form(t) != stored_form(k):
        return None, "type and kind name different types — send one of them"
    v = t if t not in (None, '') else k
    return (None if v in (None, '') else v), None


def role_error(value: Any, stored: Any = None) -> Optional[str]:
    """A group's role, judged only when it changes (ADR-0006): a type id
    exactly as the catalog has it, never ``other``; empty is allowed."""
    if value in (None, '') or value == stored:
        return None
    if not isinstance(value, str) or not (value in OVERRIDABLE or is_user_type(value)):
        return f"{str(value)[:40]!r}: one of {', '.join(type_ids(include_other=False))}"
    return None


def roles_in_use(endpoints) -> Dict[str, List[str]]:
    """Every role an installation holds, case-folded → where (``pid`` for a
    flat one, ``pid/gid`` for a group): a new type must not take one of
    those ids, or the units' published kind would change under them."""
    out: Dict[str, List[str]] = {}
    for p in endpoints or []:
        if not isinstance(p, dict):
            continue
        pid = str(p.get('id') or '')
        if isinstance(p.get('role'), str) and p['role'].strip():
            out.setdefault(p['role'].strip().lower(), []).append(pid)
        for g in p.get('groups') or []:
            if isinstance(g, dict) and isinstance(g.get('role'), str) and g['role'].strip():
                out.setdefault(g['role'].strip().lower(), []).append(f"{pid}/{g.get('id')}")
    return out


def role_is(dev, kind: str) -> bool:
    """A unit's role stands for this built-in kind (a user type by its
    behaves_like, a built-in by any of its words)."""
    return builtin_of(getattr(dev, 'role', '') or '') == kind


def meter_only_model(dev) -> str:
    """How Home Assistant names a device that is its meters: its type's
    words, never a user type's bare id."""
    k = getattr(dev, 'kind', '') or ''
    return f"Meter-only ({label(normalize(k) or k) if k else 'device'})"


def type_ids(include_other: bool = True) -> List[str]:
    ids = [k for k in KINDS if include_other or k != 'other']
    return ids + sorted(_user)


# ── what a device is ─────────────────────────────────────────────────────────

def device_type(dev, templates) -> str:
    """A device's type: its own (a built-in or a user type), else its
    group's role when that is a type, else the kind it is derived to be."""
    from .device_kind import device_kind
    for raw in (getattr(dev, 'kind', ''), getattr(dev, 'role', '')):
        if is_user_type(raw):
            return raw
        k = normalize(raw)
        if k and k != 'other':
            return k
    return device_kind(dev, templates)[0]


# ── the catalog as the API serves it ─────────────────────────────────────────

def template_matches(type_id: str, tpl_kind: str, tpl_types) -> bool:
    """A template suits a type: its kind is the type's built-in, or it lists
    the type."""
    return tpl_kind == builtin_of(type_id) or type_id in (tpl_types or ())


def catalog_view(templates_rows=None) -> Dict[str, Any]:
    """``templates_rows`` = [(device kind, listed types)] for the counts."""
    from .attribute_catalog import recommended_for
    rows = list(templates_rows or [])

    def count(tid):
        return sum(1 for k, ts in rows if template_matches(tid, k, ts))
    out = []
    for k in KINDS:
        out.append({'id': k, 'builtin': True, 'labels': dict(BUILTIN_LABELS[k]), 'icon': k,
                    'icon_class': ICONS[k], 'behaves_like': k, 'templates': count(k),
                    'recommended_attributes': recommended_for(k)})
    for u in user_types():
        out.append({**u, 'builtin': False, 'icon_class': ICONS.get(u['icon'], ICONS['other']),
                    'templates': count(u['id']),
                    'recommended_attributes': recommended_for(u['behaves_like'])})
    return {'types': out, 'icons': dict(ICONS), 'behaves_like': list(KINDS)}
