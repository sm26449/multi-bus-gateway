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
"""Device templates — the register map of an equipment type.

A device template is the portable artifact that makes ANY Modbus meter usable:
metadata (vendor/model), protocol hints (transports, unit id, batch size, byte
order), suggested poll groups, a categorized register catalog, and optional
per-register defaults (MQTT topic, InfluxDB measurement/tags, UI widget,
thresholds) applied when a register is selected on a device.

Two sources, one registry:
- BUILT-IN templates ship inside the package (``janitza/device_templates/``) —
  always present, read-only (production bind-mounts hide ``config/`` and
  ``docs/``, so built-ins must live in code).
- USER templates live in ``config/device_templates/`` (bind-mounted, writable)
  — uploads/creations survive restarts and image upgrades.

Design: docs/design/tier2-device-profiles.md §2.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Parser vocabulary (RegisterParser) — the only types a template may use.
VALID_DATA_TYPES = {
    'float', 'float32', 'double',
    'int16', 'uint16', 'short',
    'int32', 'uint32',
    'int64', 'uint64', 'long64',
    'sm16', 'sm32',      # signed-magnitude (3.17.0) — RegisterParser decodes
                         # them and yaml_import passes them through; missing
                         # here meant YAML preview OK but save rejected
    'string',
    # 'string' carries its length in the type: 'string:7' = 7 registers (ASCII,
    # 2 bytes/register). RegisterParser decodes it (length-aware); the validator
    # below accepts the 'string:N' form.
}

_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{1,63}$')
# derived-measurement names share the device's value namespace with real
# register names (expressions resolve by name), so they use the same shape
_CALC_NAME_RE = re.compile(r'^[A-Za-z0-9_\[\]]{1,64}$')


def _norm_rtype(v) -> str:
    """holding | input | coil | discrete (shared with the runtime normalizer)."""
    from .config import normalize_register_type
    return normalize_register_type(v)


@dataclass
class TemplateRegister:
    address: int
    name: str
    label: str = ""
    unit: str = ""
    data_type: str = "float"
    access: str = "RD"                       # RD | RD/WR (informative)
    category: str = "other"
    description: str = ""
    scale: float = 1.0
    offset: float = 0.0                      # engineering = raw / scale + offset
    # SunSpec-style dynamic scale factor: the NAME of a sibling register whose
    # current raw value is a signed base-10 exponent → engineering = raw × 10^SF.
    # When set, the static `scale` is ignored. The referent should live in the
    # same contiguous block (same batch) — the poller keeps a last-good SF as a
    # bridge across batches/cycles and treats the value as missing when no
    # valid SF (|SF| ≤ 10) has ever been seen.
    scale_from: str = ""
    poll_group: str = ""                     # suggested group when selected
    json_path: str = ""                      # HTTP/JSON + MQTT input: path into the JSON payload
    topic: str = ""                          # MQTT input: the topic this register reads from
    register_type: str = "holding"           # holding (FC3) | input (FC4) | coil (FC1/5) | discrete (FC2)
    defaults: Dict[str, Any] = field(default_factory=dict)
    # ── write envelope — GUARDS ARE OPT-IN (F3a trust model) ─────────────────
    # `writable: true` marks the register for the declared write path (UI
    # affordances, HA write entities). Guards are the USER'S declaration, not a
    # product limitation: each one declared buys enforcement + a better UI
    # (min/max → bounded input, allowed → dropdown, safe → auto-revert lease).
    # A register with NO guards still writes verbatim; even an UNdeclared
    # register can be written through the raw path (`unguarded: true`).
    writable: bool = False                   # declared for the write API / HA
    write_min: Optional[float] = None        # optional: reject values below this
    write_max: Optional[float] = None        # optional: reject values above this
    write_allowed: Optional[list] = None     # optional: only these exact values
    write_safe: Optional[float] = None       # value to revert to when a write-lease expires
    # not-available sentinel: True = the type's SunSpec not-implemented value
    # (0x8000/0xFFFF/…), or a raw value / list; a match reads as missing, not data
    nan: Any = None
    # cumulative counter (energy Wh/kWh/varh): reject a downward glitch so it
    # never looks like a counter reset to HA/Victron/InfluxDB difference()
    monotonic: bool = False
    # day counter recomputed by the source from whatever is awake (Solar API
    # Site.E_Day): hold the day's maximum, adopt only the midnight reset
    daily: bool = False
    # status-register decode (raw int → text): enum (one state) or bits (flags)
    enum: Optional[Dict[Any, str]] = None
    bits: Optional[Dict[Any, str]] = None
    mask: Optional[int] = None
    shift: Optional[int] = None
    # ── explicit Home Assistant entity typing (override the unit heuristic) ──
    device_class: str = ""
    state_class: str = ""
    entity_category: str = ""
    enabled_by_default: Optional[bool] = None
    icon: str = ""
    suggested_display_precision: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            'address': self.address, 'name': self.name, 'label': self.label,
            'unit': self.unit, 'data_type': self.data_type,
            'access': self.access, 'category': self.category,
            'description': self.description,
        }
        if self.scale != 1.0:
            d['scale'] = self.scale
        if self.offset:
            d['offset'] = self.offset
        if self.scale_from:
            d['scale_from'] = self.scale_from
        if self.poll_group:
            d['poll_group'] = self.poll_group
        if self.json_path:
            d['json_path'] = self.json_path
        if self.topic:
            d['topic'] = self.topic
        if self.register_type and self.register_type != 'holding':
            d['register_type'] = self.register_type
        if self.defaults:
            d['defaults'] = self.defaults
        if self.writable:
            d['writable'] = True
            if self.write_min is not None:
                d['write_min'] = self.write_min
            if self.write_max is not None:
                d['write_max'] = self.write_max
            if self.write_allowed is not None:
                d['write_allowed'] = self.write_allowed
            if self.write_safe is not None:
                d['write_safe'] = self.write_safe
        if self.nan is not None:
            d['nan'] = self.nan
        if self.monotonic:
            d['monotonic'] = True
        if self.daily:
            d['daily'] = True
        if self.enum:
            d['enum'] = self.enum
        if self.bits:
            d['bits'] = self.bits
        if self.mask is not None:
            d['mask'] = self.mask
        if self.shift is not None:
            d['shift'] = self.shift
        if self.device_class:
            d['device_class'] = self.device_class
        if self.state_class:
            d['state_class'] = self.state_class
        if self.entity_category:
            d['entity_category'] = self.entity_category
        if self.enabled_by_default is not None:
            d['enabled_by_default'] = self.enabled_by_default
        if self.icon:
            d['icon'] = self.icon
        if self.suggested_display_precision is not None:
            d['suggested_display_precision'] = self.suggested_display_precision
        return d


@dataclass
class TemplateCalculated:
    """A derived measurement a template ships with the device.

    A plain calculated register — evaluated by ``CalcEngine`` at a synthetic
    address like any user-defined one — except that the TEMPLATE owns it, so
    every device seeded from the template gets it without anyone re-typing a
    formula. This is how a vendor's decoded status ("Tracking power point"),
    an alarm flag or a derived total ships WITH the device map instead of
    being reinvented per unit.

    ``topic`` pins the MQTT leaf (a derived measurement usually belongs under
    an existing branch, e.g. ``status/text``); ``enum`` turns the computed code
    into text through the same decoder real registers use.
    """
    name: str
    expr: str
    label: str = ""
    unit: str = ""
    poll_group: str = ""
    decimals: Optional[int] = None
    topic: str = ""                 # explicit MQTT leaf, relative to the device prefix
    measurement: str = ""           # explicit InfluxDB measurement
    enum: Optional[Dict[Any, str]] = None    # computed code → text
    mqtt: bool = True
    influxdb: bool = True

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {'name': self.name, 'expr': self.expr}
        for k in ('label', 'unit', 'poll_group', 'topic', 'measurement'):
            if getattr(self, k):
                d[k] = getattr(self, k)
        if self.decimals is not None:
            d['decimals'] = self.decimals
        if self.enum:
            d['enum'] = self.enum
        if not self.mqtt:
            d['mqtt'] = False
        if not self.influxdb:
            d['influxdb'] = False
        return d


@dataclass
class DeviceTemplate:
    id: str
    name: str
    vendor: str = ""
    model: str = ""
    version: str = "1.0.0"
    author: str = ""
    description: str = ""
    source_document: str = ""
    schema_version: int = SCHEMA_VERSION
    protocol: Dict[str, Any] = field(default_factory=dict)
    poll_groups: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    categories: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # opt in to canonical field-name validation: when true, register names that
    # are not in multibus/canonical_fields are surfaced as load warnings.
    canonical: bool = False
    # on-device PQ event recorder family ("jasic" = Janitza UMG web firmware);
    # empty = the device has none. Gates the PQ recorder feature + UI tab.
    pq_recorder: str = ""
    # command presets: what a controller may ask of this kind of device and
    # how it is said (docs/commands-design.md) — offered, enabled per device
    commands: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    registers: List[TemplateRegister] = field(default_factory=list)
    # derived measurements the template ships with (seeded into each device's
    # `calculated` list) — see TemplateCalculated
    calculated: List['TemplateCalculated'] = field(default_factory=list)
    # provenance (not serialized into exports)
    builtin: bool = False
    path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Export form — round-trips through load_template()."""
        return {'device_template': {
            'schema_version': self.schema_version,
            'id': self.id, 'name': self.name,
            'vendor': self.vendor, 'model': self.model,
            'version': self.version, 'author': self.author,
            'description': self.description,
            'source_document': self.source_document,
            'protocol': self.protocol,
            'poll_groups': self.poll_groups,
            'categories': self.categories,
            'canonical': self.canonical,
            **({'pq_recorder': self.pq_recorder} if self.pq_recorder else {}),
            **({'commands': self.commands} if self.commands else {}),
            'registers': [r.to_dict() for r in self.registers],
            **({'calculated': [c.to_dict() for c in self.calculated]}
               if self.calculated else {}),
        }}

    def summary(self) -> Dict[str, Any]:
        """List-view form for the API/UI."""
        return {
            'id': self.id, 'name': self.name, 'vendor': self.vendor,
            'model': self.model, 'version': self.version,
            'builtin': self.builtin, 'registers': len(self.registers),
            'categories': len(self.categories),
            'transport': template_transport(self),   # 'modbus' | 'http'
            'commands': sorted(self.commands.keys()),
        }


def template_transport(tpl) -> str:
    """Which device transport this template's register map is for: ``'http'`` or
    ``'modbus'``. A map is transport-specific — Modbus reads by register address,
    HTTP/JSON by ``json_path`` — so a template can't be shared across the two.

    Decided by the declared ``protocol.transports`` when present, else inferred
    from the register shape (all rows carry a json_path → HTTP; otherwise Modbus).
    """
    transports = [str(x).lower() for x in ((getattr(tpl, 'protocol', {}) or {}).get('transports') or [])]
    if 'mqtt' in transports:
        return 'mqtt'
    if any(x in ('tcp', 'rtu') for x in transports):
        return 'modbus'
    if 'http' in transports:
        return 'http'
    regs = getattr(tpl, 'registers', None) or []
    # MQTT maps carry a per-register topic; HTTP maps carry json_path only.
    if regs and any(getattr(r, 'topic', '') for r in regs):
        return 'mqtt'
    if regs and all(getattr(r, 'json_path', '') for r in regs):
        return 'http'
    return 'modbus'


def validate_template(data: Dict[str, Any]) -> List[str]:
    """Validate a raw template dict. Returns a list of human-readable errors
    (empty = valid). Row-level issues carry the register address/name so the
    UI can mark the exact row red."""
    errors: List[str] = []
    t = data.get('device_template')
    if not isinstance(t, dict):
        return ["top-level key 'device_template' missing"]

    tid = t.get('id', '')
    if not isinstance(tid, str) or not _ID_RE.match(tid):
        errors.append(f"id {tid!r} invalid (a-z 0-9 - _, 2-64 chars, starts alphanumeric)")
    if not t.get('name'):
        errors.append("name is required")
    sv = t.get('schema_version', SCHEMA_VERSION)
    if not isinstance(sv, int) or sv > SCHEMA_VERSION:
        errors.append(f"schema_version {sv} not supported (max {SCHEMA_VERSION})")

    poll_groups = t.get('poll_groups', {}) or {}
    for gname, g in poll_groups.items():
        if not isinstance(g, dict) or not isinstance(g.get('interval', 0), (int, float)) \
                or g.get('interval', 0) <= 0:
            errors.append(f"poll_group {gname!r}: interval must be a positive number")

    categories = t.get('categories', {}) or {}
    regs = t.get('registers', [])
    if not isinstance(regs, list) or not regs:
        errors.append("registers: at least one register is required")
        regs = []

    seen: set = set()
    for i, r in enumerate(regs):
        where = f"register #{i} (addr {r.get('address')!r}, name {r.get('name')!r})"
        addr = r.get('address')
        if not isinstance(addr, int) or not (0 <= addr <= 65535):
            errors.append(f"{where}: address must be an integer 0..65535")
        name = r.get('name')
        if not name or not isinstance(name, str):
            errors.append(f"{where}: name is required")
        # Reject duplicate ADDRESSES, not just (address, name): runtime parse/poll
        # state, MQTT and Influx are all keyed by address alone, so a second row
        # at the same address would silently overwrite the first.
        if addr in seen:
            errors.append(f"{where}: duplicate address {addr!r} "
                          f"(each register address must be unique)")
        seen.add(addr)
        dt = str(r.get('data_type', 'float')).lower()
        # a string carries its length: 'string:7'. Validate the base + the length.
        _dt_base = dt.split(':', 1)[0] if dt.startswith('string') else dt
        if _dt_base not in VALID_DATA_TYPES:
            errors.append(f"{where}: data_type {dt!r} not supported "
                          f"({', '.join(sorted(VALID_DATA_TYPES))})")
        elif dt.startswith('string'):
            _m = re.search(r'(\d+)', dt)
            if not _m or int(_m.group(1)) < 1:
                errors.append(f"{where}: string needs a register length, e.g. 'string:7'")
        cat = r.get('category', 'other')
        if categories and cat not in categories:
            errors.append(f"{where}: category {cat!r} not declared in categories")
        pg = r.get('poll_group', '')
        if pg and poll_groups and pg not in poll_groups:
            errors.append(f"{where}: poll_group {pg!r} not declared in poll_groups")
        scale = r.get('scale', 1)
        if not isinstance(scale, (int, float)) or scale == 0:
            errors.append(f"{where}: scale must be a non-zero number")
        # Write guards are OPT-IN (F3a): a writable register without bounds is
        # legal (the value is sent verbatim after an explicit confirmation).
        # What IS validated: declared guards must be coherent.
        if r.get('writable'):
            wmin, wmax = r.get('write_min'), r.get('write_max')
            if (wmin is not None and wmax is not None
                    and float(wmin) > float(wmax)):
                errors.append(f"{where}: write_min {wmin} > write_max {wmax}")
            wa = r.get('write_allowed')
            if wa is not None:
                if (not isinstance(wa, list) or not wa
                        or not all(isinstance(v, (int, float))
                                   and not isinstance(v, bool) for v in wa)):
                    errors.append(f"{where}: write_allowed must be a non-empty "
                                  f"list of numbers")
    # scale_from must reference an existing register NAME in this template —
    # a dangling referent would silently render every dependent value missing.
    _names = {r.get('name') for r in regs if isinstance(r, dict)}
    for i, r in enumerate(regs):
        if not isinstance(r, dict):
            continue
        sf_ref = r.get('scale_from')
        if sf_ref and sf_ref not in _names:
            errors.append(f"register #{i} (addr {r.get('address')!r}, "
                          f"name {r.get('name')!r}): scale_from "
                          f"{sf_ref!r} does not match any register name")
        # scale ACCOMPANIES scale_from (it divides after the exponent, as a
        # unit conversion — SunSpec power factor is a percentage), so the two
        # are no longer exclusive. A zero scale is still nonsense.
        if r.get('scale', 1) == 0:
            errors.append(f"register #{i} (name {r.get('name')!r}): "
                          f"scale must not be 0")

    # ── derived measurements the template ships with ─────────────────────────
    calcs = t.get('calculated', [])
    if calcs and not isinstance(calcs, list):
        errors.append("calculated: must be a list")
        calcs = []
    _reg_names = {r.get('name') for r in regs if isinstance(r, dict)}
    _seen_calc: set = set()
    for i, c in enumerate(calcs):
        where = f"calculated #{i} (name {(c or {}).get('name')!r})"
        if not isinstance(c, dict):
            errors.append(f"{where}: must be an object")
            continue
        cname = c.get('name')
        if not isinstance(cname, str) or not _CALC_NAME_RE.match(cname):
            errors.append(f"{where}: name must be letters, digits, _ or []")
        elif cname in _reg_names:
            # expressions resolve by NAME out of one per-device value store, so
            # a derived name that shadows a register would make the formula
            # reference itself and the topic collide
            errors.append(f"{where}: name collides with a register name")
        elif cname in _seen_calc:
            errors.append(f"{where}: duplicate name")
        if isinstance(cname, str):
            _seen_calc.add(cname)
        from . import expressions as _expr
        ok, err, _refs = _expr.validate_expression(str(c.get('expr', '') or ''))
        if not ok:
            errors.append(f"{where}: {err}")
        pg = c.get('poll_group')
        if pg and poll_groups and pg not in poll_groups:
            errors.append(f"{where}: poll_group {pg!r} is not declared by this template")
        topic = c.get('topic')
        if topic is not None and not isinstance(topic, str):
            errors.append(f"{where}: topic must be a string")
        elif isinstance(topic, str) and topic and (
                topic.startswith('/') or topic.endswith('/')
                or '+' in topic or '#' in topic):
            errors.append(f"{where}: topic must be a relative leaf "
                          f"without wildcards (e.g. 'status/text')")
        enum_map = c.get('enum')
        if enum_map is not None:
            if not isinstance(enum_map, dict) or not enum_map:
                errors.append(f"{where}: enum must be a non-empty object")
            else:
                for k, v in enum_map.items():
                    try:
                        int(k)
                    except (TypeError, ValueError):
                        errors.append(f"{where}: enum key {k!r} is not an integer")
                    if not isinstance(v, str):
                        errors.append(f"{where}: enum label for {k!r} must be a string")
        dec = c.get('decimals')
        if dec is not None and (not isinstance(dec, int) or isinstance(dec, bool)):
            errors.append(f"{where}: decimals must be an integer")

    # Commands: names travel into MQTT topics, HA discovery ids and the UI's
    # element ids, so they are identifiers, not free text. Params likewise.
    cmds = t.get('commands')
    if cmds is not None and not isinstance(cmds, dict):
        errors.append("commands must be a mapping of name → definition")
    for cname, c in (cmds or {}).items() if isinstance(cmds, dict) else []:
        if not isinstance(cname, str) or not _ID_RE.match(cname):
            errors.append(f"command {cname!r}: name invalid (a-z 0-9 - _, 2-64 chars, starts alphanumeric)")
            continue
        if not isinstance(c, dict):
            errors.append(f"command {cname!r}: definition must be a mapping")
            continue
        params = c.get('params')
        if params is not None and not isinstance(params, dict):
            errors.append(f"command {cname!r}: params must be a mapping")
            continue
        for pname in (params or {}):
            if not isinstance(pname, str) or not _ID_RE.match(pname):
                errors.append(f"command {cname!r}: param {pname!r} invalid (a-z 0-9 - _, 2-64 chars)")
    return errors


def parse_template(data: Dict[str, Any], *, builtin: bool = False,
                   path: str = "") -> DeviceTemplate:
    """Dict → DeviceTemplate. Raises ValueError with all problems at once."""
    errors = validate_template(data)
    if errors:
        raise ValueError("invalid device template:\n- " + "\n- ".join(errors))
    t = data['device_template']
    regs = [TemplateRegister(
        address=int(r['address']), name=str(r['name']),
        label=str(r.get('label', '') or r.get('description', '') or r['name']),
        unit=str(r.get('unit', '')),
        data_type=str(r.get('data_type', 'float')).lower(),
        access=str(r.get('access', 'RD')),
        category=str(r.get('category', 'other')),
        description=str(r.get('description', '')),
        scale=float(r.get('scale', 1)),
        offset=float(r.get('offset', 0) or 0),
        scale_from=str(r.get('scale_from', '') or ''),
        poll_group=str(r.get('poll_group', '')),
        json_path=str(r.get('json_path', '')),
        topic=str(r.get('topic', '')),
        register_type=_norm_rtype(r.get('register_type') or r.get('fc')),
        defaults=r.get('defaults', {}) or {},
        writable=bool(r.get('writable', False)),
        write_min=(float(r['write_min']) if r.get('write_min') is not None else None),
        write_max=(float(r['write_max']) if r.get('write_max') is not None else None),
        write_allowed=(list(r['write_allowed'])
                       if isinstance(r.get('write_allowed'), list) else None),
        write_safe=(float(r['write_safe']) if r.get('write_safe') is not None else None),
        nan=r.get('nan'),
        monotonic=bool(r.get('monotonic', False)),
        daily=bool(r.get('daily', False)),
        enum=r.get('enum'),
        bits=r.get('bits'),
        mask=r.get('mask'),
        shift=r.get('shift'),
        device_class=str(r.get('device_class', '') or ''),
        state_class=str(r.get('state_class', '') or ''),
        entity_category=str(r.get('entity_category', '') or ''),
        enabled_by_default=r.get('enabled_by_default'),
        icon=str(r.get('icon', '') or ''),
        suggested_display_precision=r.get('suggested_display_precision'),
    ) for r in t['registers']]
    calcs = [TemplateCalculated(
        name=str(c['name']), expr=str(c.get('expr', '') or ''),
        label=str(c.get('label', '') or c['name']),
        unit=str(c.get('unit', '') or ''),
        poll_group=str(c.get('poll_group', '') or ''),
        decimals=c.get('decimals'),
        topic=str(c.get('topic', '') or ''),
        measurement=str(c.get('measurement', '') or ''),
        enum=c.get('enum'),
        mqtt=bool(c.get('mqtt', True)),
        influxdb=bool(c.get('influxdb', True)),
    ) for c in (t.get('calculated') or [])]
    return DeviceTemplate(
        id=t['id'], name=t['name'],
        vendor=t.get('vendor', ''), model=t.get('model', ''),
        version=str(t.get('version', '1.0.0')), author=t.get('author', ''),
        description=t.get('description', ''),
        source_document=t.get('source_document', ''),
        schema_version=int(t.get('schema_version', SCHEMA_VERSION)),
        protocol=t.get('protocol', {}) or {},
        poll_groups=t.get('poll_groups', {}) or {},
        categories=t.get('categories', {}) or {},
        canonical=bool(t.get('canonical', False)),
        pq_recorder=str(t.get('pq_recorder', '') or ''),
        commands=dict(t.get('commands') or {}),
        registers=regs, calculated=calcs, builtin=builtin, path=path,
    )


def load_template(path: str, *, builtin: bool = False) -> DeviceTemplate:
    """Load one template file. JSON is canonical; YAML accepted for
    hand-written community files."""
    p = Path(path)
    text = p.read_text(encoding='utf-8')
    if p.suffix.lower() in ('.yaml', '.yml'):
        import yaml
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    return parse_template(data, builtin=builtin, path=str(p))


BUILTIN_DIR = Path(__file__).parent / 'device_templates'
USER_DIR = Path('config/device_templates')


class TemplateRegistry:
    """All known device templates: built-ins (package, read-only) + user files
    (config dir, writable). User templates may NOT shadow a built-in id."""

    def __init__(self, builtin_dir: Path = None, user_dir: Path = None):
        # resolved at INIT time (not def time) so tests can monkeypatch the
        # module-level dirs before constructing a registry
        self.builtin_dir = Path(builtin_dir or BUILTIN_DIR)
        self.user_dir = Path(user_dir or USER_DIR)
        self._templates: Dict[str, DeviceTemplate] = {}
        self.load_errors: Dict[str, str] = {}      # filename -> error (surfaced in UI)
        self.load_warnings: Dict[str, str] = {}    # filename -> advisory (non-blocking)
        self.reload()

    def reload(self) -> None:
        self._templates.clear()
        self.load_errors.clear()
        self.load_warnings.clear()
        for d, builtin in ((self.builtin_dir, True), (self.user_dir, False)):
            if not d.is_dir():
                continue
            for f in sorted(d.iterdir()):
                if f.suffix.lower() not in ('.json', '.yaml', '.yml'):
                    continue
                try:
                    t = load_template(str(f), builtin=builtin)
                    if t.id in self._templates:
                        raise ValueError(
                            f"id {t.id!r} already provided by "
                            f"{self._templates[t.id].path}")
                    self._templates[t.id] = t
                    # opt-in canonical checks (warnings, not load failures):
                    # flag non-canonical names, and duplicate names — which
                    # collide on one MQTT topic / InfluxDB field (raw vendor maps
                    # like Janitza legitimately repeat cryptic names, so this is
                    # scoped to canonical templates only).
                    if t.canonical:
                        from collections import Counter
                        from .canonical_fields import non_canonical, suggest
                        warns = []

                        def _routed(r):
                            # plumbing registers (e.g. SunSpec scale factors)
                            # whose defaults disable BOTH sinks never become a
                            # topic or a field — naming them canonically buys
                            # nothing, so the lint skips them
                            d = r.defaults or {}
                            return ((d.get('mqtt') or {}).get('enabled', True)
                                    or (d.get('influxdb') or {}).get('enabled', True))
                        nc = non_canonical([r.name for r in t.registers if _routed(r)])
                        if nc:
                            hints = ", ".join(
                                f"{n}→{suggest(n)}" if suggest(n) else n for n in nc[:8])
                            more = f" (+{len(nc) - 8} more)" if len(nc) > 8 else ""
                            warns.append(f"{len(nc)} non-canonical field name(s): {hints}{more}")
                        dups = [n for n, c in Counter(
                            r.name.lower() for r in t.registers if r.name).items() if c > 1]
                        if dups:
                            warns.append(f"{len(dups)} duplicate name(s) (MQTT/InfluxDB "
                                         f"collision): {', '.join(dups[:8])}")
                        if warns:
                            self.load_warnings[f.name] = " · ".join(warns)
                except Exception as e:  # noqa: BLE001
                    self.load_errors[f.name] = str(e)
                    logger.warning("device template %s skipped: %s", f.name, e)
        logger.info("device templates loaded: %d (%d errors)",
                    len(self._templates), len(self.load_errors))

    def get(self, template_id: str) -> Optional[DeviceTemplate]:
        return self._templates.get(template_id)

    def byte_order_for(self, template_id: Optional[str]) -> str:
        """The word/byte decode order a device's template declares (default
        'big'). The SINGLE source of truth for both the boot path (main.py) and
        the runtime create/apply path (api.py) — they must agree, or a non-big
        device decodes garbage and encodes wrong-order writes after a restart."""
        tpl = self.get(template_id) if template_id else None
        return (tpl.protocol.get('byte_order', 'big') if tpl else 'big')

    def list(self) -> List[DeviceTemplate]:
        return sorted(self._templates.values(),
                      key=lambda t: (not t.builtin, t.vendor.lower(), t.name.lower()))

    def save_user(self, data: Dict[str, Any]) -> DeviceTemplate:
        """Validate + persist a user template (create or update). Refuses to
        touch built-ins."""
        t = parse_template(data)
        existing = self.get(t.id)
        if existing and existing.builtin:
            raise ValueError(f"template {t.id!r} is built-in (read-only) — duplicate it under a new id")
        self.user_dir.mkdir(parents=True, exist_ok=True)
        path = self.user_dir / f"{t.id}.json"
        # atomic temp+rename — a crash mid-write must not truncate the template
        # (the API.md-0-bytes failure class)
        _body = json.dumps(t.to_dict(), indent=1, ensure_ascii=False) + "\n"
        _tmp = path.with_suffix(".json.tmp")
        with open(_tmp, "w", encoding="utf-8") as _f:
            _f.write(_body)
            _f.flush()
            os.fsync(_f.fileno())
        os.replace(_tmp, path)
        t.path = str(path)
        self._templates[t.id] = t
        logger.info("device template %s saved (%d registers)", t.id, len(t.registers))
        return t

    def delete_user(self, template_id: str) -> None:
        t = self.get(template_id)
        if not t:
            raise KeyError(template_id)
        if t.builtin:
            raise ValueError(f"template {template_id!r} is built-in (read-only)")
        Path(t.path).unlink(missing_ok=True)
        del self._templates[template_id]
        logger.info("device template %s deleted", template_id)
