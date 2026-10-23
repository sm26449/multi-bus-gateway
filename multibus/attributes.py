# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Asset attributes — facts about the equipment, from the gateway's catalog
(docs/attributes.md).

The document lives in ``config/attributes.yaml``, through a config store
(multibus/config_store.py), apart from the device configuration:
- the device, installation and group saves rebuild their entries from
  listed keys, and an installation's units carry only {unit_id, id, name},
  so anything nested there would be lost on the next edit;
- saving an attribute never restarts a poller.

    schema: 1
    settings: {location_publish: coarse}
    site:         {location: {...}}
    installation: {<endpoint id>: {<category>: ...}}
    device:       {<device id>:   {<category>: ...}}

Lifecycle:
- An entry outlives its equipment: a deleted device is a tombstone that can
  be restored, and a deleted installation keeps its units' register files
  for a re-add.
- An entry whose id is unknown is an **orphan**: listed, kept, and dropped
  only when the device is forgotten, when it is deleted by hand, or when a
  new device is created at that id (nothing an earlier device kept survives).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import attribute_catalog as cat
from .config_store import Broken, Conflict, FileStore  # noqa: F401 — Broken re-exported for the routes

logger = logging.getLogger(__name__)

SCHEMA = 1
DEFAULT_DOC = {'schema': SCHEMA, 'settings': {'location_publish': 'coarse'},
               'site': {}, 'installation': {}, 'device': {}}


def normalize_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """A document as the service expects it, whatever a hand edit, a restore
    or a mirror left: every level a mapping, every entry a mapping."""
    out = dict(doc) if isinstance(doc, dict) else {}
    out.setdefault('schema', SCHEMA)
    if not isinstance(out.get('settings'), dict):
        out['settings'] = {}
    if out['settings'].get('location_publish') not in cat.PUBLISH_MODES:
        out['settings']['location_publish'] = 'coarse'
    if not isinstance(out.get('site'), dict):
        out['site'] = {}
    for lv in ('installation', 'device'):
        held = out.get(lv)
        out[lv] = {str(k): v for k, v in held.items() if isinstance(v, dict)} if isinstance(held, dict) else {}
    return out


class NotFound(Exception):
    pass


class Invalid(Exception):
    def __init__(self, errors: List[str]):
        super().__init__('; '.join(errors))
        self.errors = errors


class AttributeService:
    """Reads and changes the attribute document, checked against the catalog.

    The gateway's knowledge of its equipment comes in as callables, so this
    module never imports the configuration:
    - ``devices()`` → [(device id, installation id or '', kind or None,
      mppt count or None)] of the devices that exist now;
    - ``installations()`` → [installation id];
    - ``on_change(trigger, who)`` → a rollback point, the publishers.
    """

    def __init__(self, path, *, devices: Callable[[], Iterable[Tuple[str, str, Optional[str], Optional[int]]]],
                 installations: Callable[[], Iterable[str]],
                 audit=None, on_change: Optional[Callable[[str, str], None]] = None):
        self.store = FileStore(path, DEFAULT_DOC, normalize=normalize_doc)
        self._devices = devices
        self._installations = installations
        self._audit = audit
        self._on_change = on_change
        # the readers on a hot path (a formula each poll, the aggregator each
        # cycle, the OPC UA sync) share a snapshot for a second: a change made
        # here is seen at once (the snapshot is dropped), one made under the
        # file (a restore, the mirror) within a second
        self._snap: Optional[Tuple[float, Dict[str, Any], str]] = None
        self._devmap: Optional[Tuple[float, Dict]] = None

    SNAPSHOT_S = 1.0

    def _read(self) -> Tuple[Dict[str, Any], str]:
        import time as _t
        now = _t.monotonic()
        snap = self._snap
        if snap is None or now - snap[0] > self.SNAPSHOT_S:
            doc, rev = self.store.read()
            snap = self._snap = (now, doc, rev)
        return snap[1], snap[2]

    def _invalidate(self) -> None:
        self._snap = None

    # ── what exists ──────────────────────────────────────────────────────────
    def _device_map(self, cached: bool = False) -> Dict[str, Tuple[str, Optional[str], Optional[int]]]:
        """The devices that exist now. ``cached``: the hot readers (a formula
        each poll, the aggregator, the OPC UA sync) take a map at most two
        seconds old; the API always takes the current one."""
        import time as _t
        now = _t.monotonic()
        dm = self._devmap
        if not cached or dm is None or now - dm[0] > 2.0:
            dm = self._devmap = (now, {d: (inst, kind, mppt) for d, inst, kind, mppt in self._devices()})
        return dm[1]

    def _exists(self, level: str, target: str) -> bool:
        if level == 'site':
            return True
        if level == 'installation':
            return target in set(self._installations())
        return target in self._device_map()

    # ── reading ──────────────────────────────────────────────────────────────
    def publish_mode(self, doc: Optional[Dict] = None) -> str:
        doc = doc if doc is not None else self.store.read()[0]
        mode = ((doc.get('settings') or {}).get('location_publish') or 'coarse')
        return mode if mode in cat.PUBLISH_MODES else 'coarse'

    def _view(self, obj: Any, exact: bool, doc: Dict) -> Any:
        """What a reader may see: exact for an admin; else per the setting
        (coarse by default, nothing when off)."""
        if exact:
            return obj
        return cat.mask_coordinates(obj, self.publish_mode(doc))

    def read_all(self, *, exact: bool = False) -> Dict[str, Any]:
        doc, rev = self.store.read()
        devs = self._device_map()
        insts = set(self._installations())
        orphans = {'device': sorted(d for d in (doc.get('device') or {}) if d not in devs),
                   'installation': sorted(i for i in (doc.get('installation') or {}) if i not in insts)}
        return {'rev': rev, 'schema': doc.get('schema', SCHEMA), 'broken': self.store.broken,
                'settings': dict(doc.get('settings') or {}),
                'site': self._view(doc.get('site') or {}, exact, doc),
                'installation': self._view(doc.get('installation') or {}, exact, doc),
                'device': self._view(doc.get('device') or {}, exact, doc),
                'orphans': orphans, 'warnings': self.site_warnings(doc)}

    def site_warnings(self, doc: Optional[Dict] = None) -> List[str]:
        doc = doc if doc is not None else self.store.read()[0]
        points = sorted(d for d, attrs in (doc.get('device') or {}).items()
                        if ((attrs or {}).get('grid_connection') or {}).get('role') == 'connection_point')
        if len(points) > 1:
            return [f"{len(points)} devices are marked as the grid connection point "
                    f"({', '.join(points)}) — a plant has one"]
        return []

    def effective_location(self, level: str, target: str = '',
                           doc: Optional[Dict] = None) -> Tuple[Optional[Dict], str]:
        """(the location, where it comes from: 'installation <id>' | 'site' | '')."""
        doc = doc if doc is not None else self._read()[0]
        inst = target if level == 'installation' else (
            self._device_map(cached=True).get(target, ('', None, None))[0] if level == 'device' else '')
        if inst:
            loc = ((doc.get('installation') or {}).get(inst) or {}).get('location')
            if loc:
                return dict(loc), f"installation {inst}"
        loc = (doc.get('site') or {}).get('location')
        return (dict(loc), 'site') if loc else (None, '')

    def get(self, level: str, target: str = '', *, exact: bool = False) -> Dict[str, Any]:
        if not self._exists(level, target) and not self._held(level, target):
            raise NotFound(f"no {level} {target!r}")
        doc, rev = self.store.read()
        attrs = (doc.get('site') or {}) if level == 'site' else \
            ((doc.get(level) or {}).get(target) or {})
        kind = mppt = None
        if level == 'device':
            _inst, kind, mppt = self._device_map().get(target, ('', None, None))
        loc, src = self.effective_location(level, target, doc)
        offered = [c for c, spec in cat.CATALOG.items() if level in spec['levels']]
        offered.sort(key=lambda c: (not cat.fits(c, kind), list(cat.CATALOG).index(c)))
        return {'rev': rev, 'level': level, 'id': target, 'kind': kind, 'mppt_count': mppt,
                'exact': exact or self.publish_mode(doc) == 'exact',
                'attributes': self._view(attrs, exact, doc),
                'effective_location': ({'value': self._view(loc, exact, doc), 'from': src} if loc else None),
                'offered': [{'category': c, 'fits': cat.fits(c, kind)} for c in offered],
                'orphan': not self._exists(level, target)}

    def _held(self, level: str, target: str) -> bool:
        doc, _rev = self.store.read()
        return level != 'site' and target in (doc.get(level) or {})

    # ── what formulas read (expressions: sun_elevation(), attr()) ───────────
    SUMMED = ('kwp', 'modules', 'strings')

    def place_location(self, place: str) -> Optional[Tuple[float, float]]:
        """(lat, lon) of a place: 'site', an installation, a device (its
        installation's, else the plant's). None when nothing is set."""
        if place in ('', 'site'):
            loc, _src = self.effective_location('site')
        elif place in set(self._installations()):
            loc, _src = self.effective_location('installation', place)
        else:
            loc, _src = self.effective_location('device', place)
        if not loc or 'latitude_deg' not in loc or 'longitude_deg' not in loc:
            return None
        return float(loc['latitude_deg']), float(loc['longitude_deg'])

    def place_ids(self) -> set:
        return {'site'} | set(self._installations()) | set(self._device_map())

    def attr_value(self, target: str, category: str, field: str) -> Any:
        """One attribute's value for a formula. A repeating category (PV
        inputs) answers the total of a field that adds up (kWp, modules,
        strings), or the value every row shares; otherwise nothing."""
        doc, _rev = self._read()
        if target == 'site':
            node = doc.get('site') or {}
        else:
            node = (doc.get('installation') or {}).get(target) or (doc.get('device') or {}).get(target) or {}
        value = node.get(category)
        if value is None:
            return None
        num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)  # noqa: E731
        if isinstance(value, list):
            vals = [r.get(field) for r in value if isinstance(r, dict) and r.get(field) is not None]
            if not vals:
                return None
            if field in self.SUMMED:
                return round(sum(vals), 6) if all(num(v) for v in vals) else None
            return vals[0] if all(v == vals[0] for v in vals) else None
        return value.get(field) if isinstance(value, dict) else None

    # ── published (MQTT meta, OPC UA): what anyone may read ─────────────────
    def meta(self, level: str, target: str = '') -> Dict[str, Any]:
        """A place's attributes as published: versioned, coordinates as the
        publishing setting says (never exact unless it says exact)."""
        doc, rev = self._read()
        attrs = (doc.get('site') or {}) if level == 'site' else ((doc.get(level) or {}).get(target) or {})
        out: Dict[str, Any] = {'schema': SCHEMA, 'rev': rev, 'level': level, 'id': target or 'site',
                               'attributes': self._view(attrs, False, doc)}
        if level == 'device':
            out['kind'] = self._device_map(cached=True).get(target, ('', None, None))[1]
        loc, src = self.effective_location(level, target, doc)
        if loc and self.publish_mode(doc) != 'off':
            out['location'] = {'value': self._view(loc, False, doc), 'from': src}
        return out

    def flat(self, level: str, target: str = '') -> Dict[str, Any]:
        """The published attributes as flat names (``pv_input.1.kwp``) for an
        OPC UA address space; the effective location as ``location.*``."""
        m = self.meta(level, target)
        out: Dict[str, Any] = {}
        for c, v in (m.get('attributes') or {}).items():
            if isinstance(v, list):
                for i, row in enumerate(v, 1):
                    for f, x in (row or {}).items():
                        out[f"{c}.{i}.{f}"] = x
            elif isinstance(v, dict):
                for f, x in v.items():
                    out[f"{c}.{f}"] = x
        for f, x in ((m.get('location') or {}).get('value') or {}).items():
            out.setdefault(f"location.{f}", x)
        return out

    # ── changing ─────────────────────────────────────────────────────────────
    def _record(self, action: str, level: str, target: str, category: str, before, after,
                who: str, ip: str, doc: Dict) -> None:
        if self._audit is not None:
            try:
                self._audit.append(user=who or '-', ip=ip or '-', action=f"attributes {action}",
                                   target=f"{level} {target or '-'} {category}".strip(), status='ok',
                                   detail={'before': cat.mask_coordinates(before),
                                           'after': cat.mask_coordinates(after)})
            except Exception:  # noqa: BLE001 — an audit failure never loses the change
                logger.exception("attributes audit failed")
        if self._on_change is not None:
            try:
                self._on_change(f"attributes {action}", who)
            except Exception:  # noqa: BLE001
                logger.exception("attributes on_change failed")

    def put(self, level: str, target: str, category: str, value: Any, *, who: str = '', ip: str = '',
            expected_rev: Optional[str] = None) -> Tuple[str, List[str]]:
        """Replace one whole category. Returns (new revision, warnings)."""
        if not self._exists(level, target):
            raise NotFound(f"no {level} {target!r}")
        mppt = self._device_map().get(target, ('', None, None))[2] if level == 'device' else None
        clean, errors, warnings = cat.validate(category, value, level=level, mppt_count=mppt)
        if errors:
            raise Invalid(errors)
        box: Dict[str, Any] = {}

        def change(doc):
            node = doc.setdefault('site', {}) if level == 'site' else \
                doc.setdefault(level, {}).setdefault(target, {})
            box['before'] = node.get(category)
            # a reader who sees coordinates coarse and saves them back unchanged
            # must not overwrite the exact ones with the rounded ones
            if category == 'location' and isinstance(box['before'], dict) \
                    and clean == cat.coarse_location(box['before']):
                node[category] = box['before']
                return
            node[category] = clean
        doc, rev, _ = self.store.mutate(change, expected_rev)
        self._invalidate()
        self._record('set', level, target, category, box.get('before'), clean, who, ip, doc)
        return rev, warnings + self.site_warnings(doc)

    def delete(self, level: str, target: str, category: Optional[str] = None, *, who: str = '',
               ip: str = '', expected_rev: Optional[str] = None) -> str:
        """Delete one category, or (no category) everything held for a
        device or an installation — the way to drop an orphan by hand."""
        box: Dict[str, Any] = {}

        def change(doc):
            if level == 'site':
                node = doc.setdefault('site', {})
            else:
                node = (doc.get(level) or {}).get(target)
                if node is None:
                    raise NotFound(f"nothing held for {level} {target!r}")
            if category is None:
                if level == 'site':
                    raise Invalid(["the plant's attributes are deleted one category at a time"])
                box['before'] = doc[level].pop(target)
            else:
                if category not in node:
                    raise NotFound(f"{level} {target or ''} has no {category}".replace('  ', ' '))
                box['before'] = node.pop(category)
                if level != 'site' and not node:
                    doc[level].pop(target, None)
        doc, rev, _ = self.store.mutate(change, expected_rev)
        self._invalidate()
        self._record('delete', level, target, category or '*', box.get('before'), None, who, ip, doc)
        return rev

    def set_publish_mode(self, mode: str, *, who: str = '', ip: str = '',
                         expected_rev: Optional[str] = None) -> str:
        if mode not in cat.PUBLISH_MODES:
            raise Invalid([f"location_publish: one of {', '.join(cat.PUBLISH_MODES)}"])
        box: Dict[str, Any] = {}

        def change(doc):
            s = doc.setdefault('settings', {})
            box['before'] = s.get('location_publish')
            s['location_publish'] = mode
        doc, rev, _ = self.store.mutate(change, expected_rev)
        self._invalidate()
        self._record('settings', 'site', '', 'location_publish', box.get('before'), mode, who, ip, doc)
        return rev

    def copy_to(self, source: str, category: str, targets: List[str], *, who: str = '',
                ip: str = '') -> Tuple[str, List[str]]:
        """Copy one device's category to other devices (four identical
        inverters). The values are copied, not linked; each target is
        checked against its own inputs."""
        doc, _rev = self.store.read()
        value = ((doc.get('device') or {}).get(source) or {}).get(category)
        if value is None:
            raise NotFound(f"device {source!r} has no {category}")
        devs = self._device_map()
        errors: List[str] = []
        warnings: List[str] = []
        cleaned: Dict[str, Any] = {}
        for t in targets:
            if t == source:
                continue
            if t not in devs:
                errors.append(f"{t}: no such device")
                continue
            clean, e, w = cat.validate(category, value, level='device', mppt_count=devs[t][2])
            errors += [f"{t}: {x}" for x in e]
            warnings += [f"{t}: {x}" for x in w]
            cleaned[t] = clean
        if errors:
            raise Invalid(errors)
        befores: Dict[str, Any] = {}

        def change(d):
            for t, v in cleaned.items():
                node = d.setdefault('device', {}).setdefault(t, {})
                befores[t] = node.get(category)
                node[category] = v
        doc, rev, _ = self.store.mutate(change)
        self._invalidate()
        for t, v in cleaned.items():
            self._record('copy', 'device', t, category, befores.get(t), v, who, ip, doc)
        return rev, warnings

    # ── lifecycle (called by the configuration's own paths) ─────────────────
    def drop(self, level: str, target: str, *, reason: str, who: str = '', ip: str = '') -> bool:
        """Remove what is held for an id: a forgotten device, a new device at
        an old id. Never on a soft delete (it can be restored)."""
        doc, _rev = self.store.read()
        if target not in (doc.get(level) or {}):
            return False
        try:
            self.delete(level, target, who=who or '-', ip=ip)
        except NotFound:
            return False
        logger.info("attributes: %s %s dropped (%s)", level, target, reason)
        return True
