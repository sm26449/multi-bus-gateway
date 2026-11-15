# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Assemble / split the one logical config document to and from a light base + per-entity
``config.d/`` files (ADR-0047 P3, design §9). The tree is an ON-DISK detail: backup/restore, the
HA mirror and the sanitized export keep operating on the single assembled document, and only the
loader/writer see the tree — so the `.d` layout never has to be taught to every consumer.

``split(data)`` takes the dict ``save_yaml_config`` builds (singletons + the entity LISTS
``devices``/``endpoints``/``bridges``/``device_types``) and returns ``{path: entry}``:
``base.yaml`` (the singletons, plus a ``_layout_order`` index) and one
``config.d/<kind>.<key>.yaml`` per entity. ``assemble(files)`` is the exact inverse. Invariant:
**assemble(split(x)) == x** — the entity list ORDER is preserved (it is the UI order; review M2),
and empty lists stay omitted exactly as the writer omits them.

The file map is **injective** (design §4; FINDINGS P3-PATH-1):

* the kind and the key are joined by a dot, and no kind holds one — ``device`` + ``type-foo`` and
  ``device-type`` + ``foo`` cannot meet;
* the key is the entity's id with every character outside ``a-z 0-9 _ -`` percent-encoded (upper
  case too: a case-insensitive disk must not fold ``Pump`` onto ``pump``; dots and slashes too:
  no path leaves ``config.d/``), so two different ids never share a name;
* what has no usable id is keyed by its position, ``~<index>``, and the n-th repeat of an id is
  ``<key>~<n>`` — ``~`` is always encoded in a real id, so neither can meet one. A document with
  duplicate or id-less entries still loads today (the first wins); it must survive a split.

``_layout_order`` holds the file KEYS, per list, in order: the index — not a directory listing —
says what the document contains. A listed file that is absent is an error that names it; a file
that is present but not listed is reported by :func:`check` and left out (what to do with a
hand-dropped file is P4's decision — never a silent merge).
"""
from __future__ import annotations

from typing import Any, Dict, List

# top-level key -> filename kind; each value in data[key] is an entity (normally an id-keyed dict)
ENTITY_LISTS = {
    "devices": "device",
    "endpoints": "endpoint",
    "bridges": "bridge",
    "device_types": "device-type",
}
BASE = "base.yaml"
DROPIN_DIR = "config.d/"
_ORDER_KEY = "_layout_order"
SOURCE_KEY = "_layout_source"      # the config.yaml revision a written tree came from (tree_store)
_SAFE = "abcdefghijklmnopqrstuvwxyz0123456789_-"
_MAX_KEY = 200            # the name stays under the 255-byte limit of every filesystem we meet


class AssembleError(ValueError):
    """The document cannot be split, or the tree cannot be assembled — said with the path."""


def _encode(eid: str) -> str:
    """An id as a file-name part: injective, lower-case, no dot, no separator."""
    return "".join(c if c in _SAFE else "".join(f"%{b:02x}" for b in c.encode("utf-8"))
                   for c in eid)


def path_for(kind: str, key: str, *, encoded: bool = False) -> str:
    """The drop-in path of entity ``key`` of ``kind``. ``key`` is an id unless ``encoded``."""
    return f"{DROPIN_DIR}{kind}.{key if encoded else _encode(str(key))}.yaml"


def _entity_id(item: Any) -> str:
    """An entity's id (a device type's older ``type`` key too), '' when it has none."""
    if not isinstance(item, dict):
        return ""
    eid = item.get("id") or item.get("type")
    return eid if isinstance(eid, str) else ("" if eid is None else str(eid))


def _keys(items: List[Any], kind: str) -> List[str]:
    """One distinct file key per entry, in order."""
    keys: List[str] = []
    seen: Dict[str, int] = {}
    for index, item in enumerate(items):
        eid = _entity_id(item)
        try:
            key = _encode(eid) if eid else f"~{index}"
        except UnicodeEncodeError as e:
            raise AssembleError(f"{kind} id {eid[:40]!r} cannot be a file name ({e.reason})") from e
        if len(key) > _MAX_KEY:
            raise AssembleError(f"{kind} id {eid[:40]!r}… is too long for a file name "
                                f"({len(key)} > {_MAX_KEY} characters once encoded)")
        seen[key] = seen.get(key, 0) + 1
        keys.append(key if seen[key] == 1 else f"{key}~{seen[key]}")
    return keys


def split(data: Dict[str, Any]) -> Dict[str, Any]:
    """One logical config dict -> ``{BASE: base_dict, 'config.d/<kind>.<key>.yaml': entry, …}``.
    The base keeps every non-entity key plus ``_layout_order`` (so assemble restores the exact
    lists). A key absent or empty in ``data`` produces no files and no order entry. Raises
    :class:`AssembleError` before returning anything when the document cannot be mapped."""
    for own in (_ORDER_KEY, SOURCE_KEY):
        if own in data:
            raise AssembleError(f"the document already has a `{own}` key — it is the tree's own")
    base: Dict[str, Any] = {k: v for k, v in data.items() if k not in ENTITY_LISTS}
    order: Dict[str, List[str]] = {}
    files: Dict[str, Any] = {}
    for key, kind in ENTITY_LISTS.items():
        items = data.get(key) or []
        if not items:
            continue
        if not isinstance(items, list):
            raise AssembleError(f"`{key}` is not a list")
        keys = _keys(items, kind)
        for file_key, item in zip(keys, items):
            files[path_for(kind, file_key, encoded=True)] = item
        order[key] = keys
    if order:
        base[_ORDER_KEY] = order
    return {BASE: base, **files}


def _order(files: Dict[str, Any]) -> Dict[str, List[str]]:
    """The base's index, shape-checked: a base or an index that is not a mapping, or a list of
    keys that is not one, is a broken tree — said as such, not an AttributeError."""
    base = files.get(BASE) or {}
    if not isinstance(base, dict):
        raise AssembleError(f"{BASE} is not a mapping")
    order = base.get(_ORDER_KEY) or {}
    if not isinstance(order, dict) or not all(
            isinstance(v, list) and all(isinstance(k, str) for k in v) for v in order.values()):
        raise AssembleError(f"{BASE}: `{_ORDER_KEY}` is not a mapping of key lists")
    return order


def _listed(files: Dict[str, Any]) -> List[str]:
    order = _order(files)
    return [path_for(kind, k, encoded=True)
            for key, kind in ENTITY_LISTS.items() for k in (order.get(key) or [])]


def check(files: Dict[str, Any]) -> Dict[str, List[str]]:
    """What the tree and its index disagree on: ``missing`` (listed, no file) and ``unlisted``
    (a drop-in the index does not know). Both empty: the tree is whole."""
    listed = _listed(files)
    known = set(listed)
    return {"missing": [p for p in listed if p not in files],
            "unlisted": sorted(p for p in files if p.startswith(DROPIN_DIR) and p not in known)}


def assemble(files: Dict[str, Any]) -> Dict[str, Any]:
    """The inverse of :func:`split`. Reconstructs the one logical config dict from the base + the
    per-entity files, restoring each list in its recorded order and dropping ``_layout_order``.
    A listed file that is absent raises :class:`AssembleError` naming it; unlisted drop-ins are
    not merged (see :func:`check`)."""
    order = _order(files)
    base = dict(files.get(BASE) or {})
    base.pop(_ORDER_KEY, None)
    base.pop(SOURCE_KEY, None)
    missing = check(files)["missing"]
    if missing:
        raise AssembleError("listed in base.yaml but not there: " + ", ".join(missing))
    data: Dict[str, Any] = dict(base)
    for key, kind in ENTITY_LISTS.items():
        keys = order.get(key)
        if not keys:
            continue
        data[key] = [files[path_for(kind, k, encoded=True)] for k in keys]
    return data
