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
"""
from __future__ import annotations

import json
import yaml
import math
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
    # MQTT: where a command publishes this register's new value, and the text
    # it publishes (${value} = the number or code, ${label} = an enum's text);
    # what arrives on `topic` is the read-back
    write_topic: str = ""
    write_payload: str = ""
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
    # how this field combines across the UNITS of an endpoint (a battery
    # bank, an inverter farm): {output_name: op}, op in sum|avg|min|max|spread.
    # The template teaches the endpoint aggregator the vendor's semantics —
    # soc can fan out to pack_average_soc/pack_min_soc/pack_max_soc at once.
    aggregates: Optional[Dict[str, str]] = None

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
        if self.write_topic:
            d['write_topic'] = self.write_topic
        if self.write_payload:
            d['write_payload'] = self.write_payload
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
        if self.aggregates:
            d['aggregates'] = self.aggregates
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
    category: str = ""              # the template section it belongs to (as a row's)
    enum: Optional[Dict[Any, str]] = None    # computed code → text
    mqtt: bool = True
    influxdb: bool = True
    aggregates: Optional[Dict[str, str]] = None   # endpoint fan-out, like registers
    ui: Optional[Dict[str, Any]] = None           # {'show_on_dashboard', 'widget'}

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {'name': self.name, 'expr': self.expr}
        if self.aggregates:
            d['aggregates'] = self.aggregates
        if self.ui:
            d['ui'] = self.ui
        for k in ('label', 'unit', 'poll_group', 'topic', 'measurement', 'category'):
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
    # a few files say `manufacturer` where the schema says `vendor`; the list
    # shows it as the vendor (the file itself stays as it is)
    manufacturer: str = ""
    version: str = "1.0.0"
    author: str = ""
    description: str = ""
    source_document: str = ""
    # what the equipment is, in the vocabulary installation groups use for
    # their role (attribute_catalog.KINDS): the attributes that fit it are
    # offered first. Empty: unknown (guessed from the canonical names)
    kind: str = ""
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
    # how a unit of this kind is PRESENTED — read live from the template (never
    # seeded), so improving it reaches every existing unit at once:
    #   unit_label / unit_label_plural / icon — what a unit / a group of them is
    #   glance   — the fields of a unit row (installation table, device list)
    #   hero     — the fields of a fleet row
    #   headline — the installation's top line, from its TOTALS:
    #              [{field, label?, hint?}]
    # Absent → the role/canonical defaults the UI has always used.
    display: Dict[str, Any] = field(default_factory=dict)
    # how a bus scan recognises this device: registers that must read given
    # values, and/or the FC43 identification it answers (regexes)
    identify: Dict[str, Any] = field(default_factory=dict)
    # what the device must be told before part of its map exists (written at
    # connect, again when it forgets) — see device_setup.py
    setup: List[Dict[str, Any]] = field(default_factory=list)
    # what the user should know before relying on the template — a limitation,
    # a model it does not fit, a step to take first. Shown where the template
    # is picked and on each device made from it:
    # [{level: info|warning, text, fields?: [row/command names], link?}]
    notes: List[Dict[str, Any]] = field(default_factory=list)
    # rules a device of this kind is commonly run with — examples the rule
    # editor offers, never armed by themselves. `{device}` stands for the
    # device's id: [{id, label, description?, kind, target: {device:
    # "{device}", command}, when/signal…}] (see rules_preset)
    rules: List[Dict[str, Any]] = field(default_factory=list)
    # what a channel (the registers some commands control together) must
    # respect whoever writes: {name: {combine: priority|min|max, min_on_s,
    # min_off_s, max_starts_per_h}} — rules phase 3, docs/rules.md
    channels: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    registers: List[TemplateRegister] = field(default_factory=list)
    # derived measurements the template ships with (seeded into each device's
    # `calculated` list) — see TemplateCalculated
    calculated: List['TemplateCalculated'] = field(default_factory=list)
    # kinds of things the device holds, each added by instance:
    # {type: {name, description, kind, instance, discover, registers, …}}
    components: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # set on a component's own template (`<parent>:<key>`), never saved
    component_of: str = ""
    component_key: str = ""
    component: Dict[str, Any] = field(default_factory=dict)
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
            **({'kind': self.kind} if self.kind else {}),
            'protocol': self.protocol,
            'poll_groups': self.poll_groups,
            'categories': self.categories,
            'canonical': self.canonical,
            **({'pq_recorder': self.pq_recorder} if self.pq_recorder else {}),
            **({'commands': self.commands} if self.commands else {}),
            **({'display': self.display} if self.display else {}),
            **({'identify': self.identify} if self.identify else {}),
            **({'setup': self.setup} if self.setup else {}),
            **({'notes': self.notes} if self.notes else {}),
            **({'rules': self.rules} if self.rules else {}),
            **({'channels': self.channels} if self.channels else {}),
            'registers': [r.to_dict() for r in self.registers],
            **({'calculated': [c.to_dict() for c in self.calculated]}
               if self.calculated else {}),
            **({'components': self.components} if self.components else {}),
        }}

    def summary(self) -> Dict[str, Any]:
        """List-view form for the API/UI."""
        return {
            'id': self.id, 'name': self.name, 'vendor': self.vendor or self.manufacturer,
            'model': self.model, 'version': self.version,
            'builtin': self.builtin, 'registers': len(self.registers),
            'kind': self.kind or kind_from_names(self),
            'categories': len(self.categories),
            'transport': template_transport(self),   # 'modbus' | 'http'
            'commands': sorted(self.commands.keys()),
            'notes': self.notes,
            'rules': [r.get('id') for r in self.rules],
            **({'components': [{'type': k, 'name': str(c.get('name') or k),
                                'kind': str(c.get('kind', '') or '')}
                               for k, c in self.components.items()]}
               if self.components else {}),
            **({'component_of': self.component_of, 'component_key': self.component_key}
               if self.component_of else {}),
        }


def kind_from_names(tpl) -> str:
    """A template without a declared ``kind``: what its canonical names say
    it is — DC inputs → an inverter; a state of charge → a battery; import
    and export energy without DC → a meter. '' when they say nothing."""
    names = {getattr(r, 'name', '') for r in (getattr(tpl, 'registers', None) or [])}
    names |= {getattr(c, 'name', '') for c in (getattr(tpl, 'calculated', None) or [])}
    if any(n.startswith('power_dc') or n.startswith('current_dc_mppt') for n in names):
        return 'inverter'
    if 'soc' in names:
        return 'battery'
    if 'energy_active_import' in names or 'energy_active_export' in names:
        return 'meter'
    return ''


def mppt_count_of(tpl) -> int:
    """How many MPPT inputs the template's canonical names show
    (``power_dc_mppt2`` → 2); 0 when none."""
    import re as _re
    n = 0
    for r in (getattr(tpl, 'registers', None) or []):
        m = _re.match(r'^(?:power|voltage|current)_dc_mppt(\d+)$', getattr(r, 'name', '') or '')
        if m:
            n = max(n, int(m.group(1)))
    return n


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
    from .config import MODBUS_PROTOCOLS
    if any(x in MODBUS_PROTOCOLS for x in transports):
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


def validate_template(data: Dict[str, Any], *, extras: bool = True) -> List[str]:
    """Validate a raw template dict. Returns a list of human-readable errors
    (empty = valid). Row-level issues carry the register address/name so the
    UI can mark the exact row red. ``extras=False`` leaves out the checks
    added in 3.91 (row extras, categories, commands in depth): a file already
    on disk keeps loading, they become load warnings (see ``extra_errors``)."""
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
        if isinstance(addr, bool) or not isinstance(addr, int) or not (0 <= addr <= 65535):
            errors.append(f"{where}: address must be an integer 0..65535")
        name = r.get('name')
        if not name or not isinstance(name, str):
            errors.append(f"{where}: name is required")
        # One row per address IN ITS TABLE: coil 0, holding 0 and input 0 are
        # three registers (the live store keys each by address + table). A
        # second row at the same address of the same table would overwrite
        # the first.
        from .config import normalize_register_type
        _tbl = normalize_register_type(r.get('register_type') or r.get('fc') or 'holding')
        if (_tbl, addr) in seen:
            errors.append(f"{where}: duplicate address {addr!r} in the {_tbl} table "
                          f"(each address is unique within its table — coil, "
                          f"holding, input and discrete are separate)")
        seen.add((_tbl, addr))
        dt = str(r.get('data_type', 'float')).lower()
        # a string carries its length: 'string:7'. Validate the base + the length.
        _dt_base = dt.split(':', 1)[0] if dt.startswith('string') else dt
        if _dt_base not in VALID_DATA_TYPES:
            errors.append(f"{where}: data_type {dt!r} not supported "
                          f"({', '.join(sorted(VALID_DATA_TYPES))})")
        elif dt.startswith('string'):
            _m = re.search(r'(\d+)', dt)
            if not _m or not (1 <= int(_m.group(1)) <= 125):
                errors.append(f"{where}: string needs a register length of 1..125, e.g. 'string:7'")
        cat = r.get('category', 'other')
        if categories and cat not in categories:
            errors.append(f"{where}: category {cat!r} not declared in categories")
        pg = r.get('poll_group', '')
        if pg and poll_groups and pg not in poll_groups:
            errors.append(f"{where}: poll_group {pg!r} not declared in poll_groups")
        scale = r.get('scale', 1)
        if (isinstance(scale, bool) or not isinstance(scale, (int, float)) or scale == 0
                or not math.isfinite(scale)):
            errors.append(f"{where}: scale must be a finite, non-zero number")
        offset = r.get('offset', 0)
        if offset is not None and (isinstance(offset, bool) or not isinstance(offset, (int, float))
                                   or not math.isfinite(offset)):
            errors.append(f"{where}: offset must be a finite number")
        # Write guards are OPT-IN (F3a): a writable register without bounds is
        # legal (the value is sent verbatim after an explicit confirmation).
        # What IS validated: declared guards must be coherent.
        if r.get('writable'):
            wmin, wmax, wsafe = r.get('write_min'), r.get('write_max'), r.get('write_safe')
            for _k, _v in (('write_min', wmin), ('write_max', wmax), ('write_safe', wsafe)):
                if _v is not None and (isinstance(_v, bool) or not isinstance(_v, (int, float))
                                       or not math.isfinite(_v)):
                    errors.append(f"{where}: {_k} must be a finite number")
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
            # the safe value is what a lease reverts to — it must itself be
            # inside the envelope, or the revert writes what the guard forbids
            # (F-21, 3.83.0)
            if isinstance(wsafe, (int, float)) and not isinstance(wsafe, bool) and math.isfinite(wsafe):
                if wmin is not None and isinstance(wmin, (int, float)) and wsafe < wmin:
                    errors.append(f"{where}: write_safe {wsafe} is below write_min {wmin}")
                if wmax is not None and isinstance(wmax, (int, float)) and wsafe > wmax:
                    errors.append(f"{where}: write_safe {wsafe} is above write_max {wmax}")
                if isinstance(wa, list) and wa and wsafe not in wa:
                    errors.append(f"{where}: write_safe {wsafe} is not in write_allowed")
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
        ok, err, _refs = _expr.validate_expression(str(c.get('expr', '') or ''), check_places=False)
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
    if extras:
        errors.extend(extra_errors(data))
    errors.extend(_display_errors(t))
    errors.extend(_identify_errors(t))
    errors.extend(_setup_errors(t))
    errors.extend(_notes_errors(t))
    errors.extend(_rule_preset_errors(t))
    gap = (t.get('protocol') or {}).get('min_write_gap_s') if isinstance(t.get('protocol'), dict) else None
    if gap is not None and (isinstance(gap, bool) or not isinstance(gap, (int, float)) or not 0 <= gap <= 60):
        errors.append("protocol.min_write_gap_s: seconds between two writes, 0..60")
    single = (t.get('protocol') or {}).get('single_register_writes') if isinstance(t.get('protocol'), dict) else None
    if single is not None and not isinstance(single, bool):
        errors.append("protocol.single_register_writes: true or false")
    errors.extend(_availability_errors(t.get('protocol')))
    if isinstance(t.get('protocol'), dict) and t['protocol'].get('keepalive') is not None:
        from .mqtt_input import keepalive_errors
        errors.extend(keepalive_errors(t['protocol']['keepalive'], 'protocol.keepalive'))
    if t.get('components') is not None:
        errors.extend(_component_errors(t, extras))
    return errors


# ── components: kinds of things a device holds, added by instance ──────────
# A component is described inside its
# parent's template and becomes a template of its own, `<parent>:<key>`, that
# reads through the parent's connection with `{instance}` filled in.
COMPONENT_SEP = ':'
_COMPONENT_KEYS = {'name', 'description', 'kind', 'instance', 'discover', 'categories',
                   'display', 'notes', 'poll_groups', 'registers', 'calculated'}
_INHERITED_KEYS = ('schema_version', 'vendor', 'model', 'version', 'author',
                   'source_document', 'protocol', 'canonical')


def component_document(t: Dict[str, Any], key: str, c: Dict[str, Any]) -> Dict[str, Any]:
    """One component as a template document of its own, so the same
    validator and parser read it as read any template. The id is a
    placeholder: the registry names it ``<parent>:<key>``."""
    doc = {k: t[k] for k in _INHERITED_KEYS if k in t}
    doc.update({k: c[k] for k in _COMPONENT_KEYS if k in c
                and k not in ('instance', 'discover')})
    doc['id'] = 'component'
    doc['name'] = str(c.get('name') or key)
    return {'device_template': doc}


def _component_errors(t: Dict[str, Any], extras: bool) -> List[str]:
    comps = t.get('components')
    if not isinstance(comps, dict):
        return ["components must be a mapping of type → definition"]
    transports = [str(x).lower() for x in ((t.get('protocol') or {}).get('transports') or [])] \
        if isinstance(t.get('protocol'), dict) else []
    errors: List[str] = []
    if comps and 'mqtt' not in transports:
        errors.append("components: only an MQTT template (protocol.transports: [mqtt]) may declare them for now")
    for key, c in comps.items():
        where = f"component {key!r}"
        if not isinstance(key, str) or not _ID_RE.match(key):
            errors.append(f"{where}: type invalid (a-z 0-9 - _, 2-64 chars, starts alphanumeric)")
            continue
        if not isinstance(c, dict):
            errors.append(f"{where}: definition must be a mapping")
            continue
        unknown = sorted(set(c) - _COMPONENT_KEYS)
        if unknown:
            errors.append(f"{where}: {', '.join(unknown)} not allowed here — a component is read-only "
                          f"and takes {', '.join(sorted(_COMPONENT_KEYS))}")
        errors.extend(f"{where}: {e}" for e in validate_template(component_document(t, key, c), extras=extras))
        for i, r in enumerate(c.get('registers') or []):
            if isinstance(r, dict) and '{instance}' not in str(r.get('topic', '') or ''):
                errors.append(f"{where}: register #{i} ({r.get('name')!r}): its topic must contain "
                              "{instance}, or every instance would read the same value")
        inst = c.get('instance')
        if inst is not None and (not isinstance(inst, dict) or not all(
                isinstance(inst.get(k, ''), str) for k in ('label', 'hint'))):
            errors.append(f"{where}: instance must be {{label?, hint?}} (text)")
        disc = c.get('discover')
        if disc is not None:
            if not isinstance(disc, dict) or not isinstance(disc.get('topic'), str):
                errors.append(f"{where}: discover must be {{topic, name_topic?}}")
            else:
                levels = disc['topic'].split('/')
                if levels.count('+') != 1 or '#' in disc['topic'] or '{instance}' in disc['topic']:
                    errors.append(f"{where}: discover.topic must have exactly one + level (the instance) and no #")
                nt = disc.get('name_topic')
                if nt is not None and (not isinstance(nt, str) or '{instance}' not in nt
                                       or any(x in nt for x in '+#')):
                    errors.append(f"{where}: discover.name_topic must contain {{instance}} and no wildcard")
    return errors


def component_templates(parent: 'DeviceTemplate') -> List['DeviceTemplate']:
    """The templates a parent's components become: ``<parent>:<key>``."""
    out: List[DeviceTemplate] = []
    raw = parent.to_dict()['device_template']
    for key, c in (parent.components or {}).items():
        try:
            ct = parse_template(component_document(raw, key, c), builtin=parent.builtin,
                                path=parent.path, strict=False)
        except ValueError as e:
            logger.warning("template %s: component %s skipped: %s", parent.id, key, e)
            continue
        ct.id = f"{parent.id}{COMPONENT_SEP}{key}"
        ct.component_of, ct.component_key = parent.id, key
        ct.component = {'name': str(c.get('name') or key),
                        'description': str(c.get('description', '') or ''),
                        'instance': dict(c.get('instance') or {}),
                        'discover': dict(c.get('discover') or {})}
        out.append(ct)
    return out


def is_component_id(template_id: Any) -> bool:
    return isinstance(template_id, str) and COMPONENT_SEP in template_id


def _availability_errors(proto: Any) -> List[str]:
    """protocol.availability: how an MQTT source says it is alive (its birth
    and last will), e.g. {topic: "~/status", online: "online", offline: "offline"}."""
    av = proto.get('availability') if isinstance(proto, dict) else None
    if av is None:
        return []
    if not isinstance(av, dict) or not isinstance(av.get('topic'), str) or not av['topic'].strip():
        return ["protocol.availability: {topic, online, offline}, the topic required "
                "(relative to the device's topic as ~/…)"]
    errs = []
    if any(c in av['topic'] for c in '+#'):
        errs.append("protocol.availability.topic: one topic, no wildcards")
    from .text_check import topic_issue
    _ti = topic_issue(av['topic'], "protocol.availability.topic")
    if _ti:
        errs.append(_ti)
    for k in ('online', 'offline'):
        if k in av and not (isinstance(av[k], str) and av[k]):
            errs.append(f"protocol.availability.{k}: the payload text, e.g. {k!r}")
    if av.get('online', 'online') == av.get('offline', 'offline'):
        errs.append("protocol.availability: online and offline must differ")
    return errs


def rule_preset_for(preset: Dict[str, Any], device_id: str) -> Dict[str, Any]:
    """A template's rule preset made concrete for one device: `{device}`
    replaced everywhere, the id prefixed with the device's, always shadow."""
    import json as _json
    txt = _json.dumps({k: v for k, v in preset.items() if k != 'description'})
    rule = _json.loads(txt.replace('{device}', device_id))
    # a rule id starts with a letter and is at most 48 characters; a device id
    # may start with a digit and be longer — keep the id valid either way
    rid = f"{device_id}-{preset.get('id')}"
    if not rid[:1].isalpha():
        rid = 'r-' + rid
    rule['id'] = rid[:48].rstrip('-_') or 'rule'
    rule['mode'] = 'shadow'
    return rule


def _rule_preset_errors(t: Dict[str, Any]) -> List[str]:
    """``rules``: examples — each a rule (rules.md) over `{device}`, checked
    as the rule editor would check it, on a placeholder device, and its
    command and fields must be this template's."""
    rp = t.get('rules')
    if rp is None:
        return []
    if not isinstance(rp, list) or len(rp) > 20:
        return ["rules must be a list of at most 20 rule examples"]
    from .rules import validate_rule_def
    from .expressions import validate_expression

    def _expr_err(e):
        ok, err, _refs = validate_expression(e, check_places=False)
        return None if ok else err
    names = {r.get('name') for k in ('registers', 'calculated')
             for r in (t.get(k) or []) if isinstance(r, dict)}
    cmds = set(t.get('commands') or {}) if isinstance(t.get('commands'), dict) else set()
    out, ids = [], set()
    for i, p in enumerate(rp):
        w = f"rules[{i}]"
        if not isinstance(p, dict):
            out.append(f"{w}: must be an object")
            continue
        pid = p.get('id')
        if not isinstance(pid, str) or not _ID_RE.match(pid):
            out.append(f"{w}: id invalid (a-z 0-9 - _, 2-64 chars)")
        elif pid in ids:
            out.append(f"{w}: duplicate id {pid!r}")
        else:
            ids.add(pid)
        tg = p.get('target')
        if not isinstance(tg, dict):
            out.append(f"{w}: target must be an object {{device: \"{{device}}\", command}}")
            continue
        if tg.get('device') != '{device}':
            out.append(f"{w}: target.device must be \"{{device}}\" (the device the example is offered for)")
        cmd = tg.get('command')
        if not isinstance(cmd, str) or cmd not in cmds:
            out.append(f"{w}: target.command {cmd!r} is not a command of this template")
        try:
            rule = rule_preset_for(p, 'dev')
            out += [f"{w}: {e}" for e in validate_rule_def(rule, validate_expr=_expr_err)]
        except Exception as e:  # noqa: BLE001 — a malformed example is an answer, not a 500
            out.append(f"{w}: cannot be read as a rule: {e}")
        for k in ('signal', 'when', 'release'):
            for ref in re.findall(r"\{device\}\.([A-Za-z_][A-Za-z0-9_\[\]]*)", str(p.get(k) or '')):
                if ref not in names:
                    out.append(f"{w}: {k} reads {ref!r}, which is not a field of this template")
    return out


NOTE_LEVELS = ('info', 'warning')


def _notes_errors(t: Dict[str, Any]) -> List[str]:
    """``notes``: ``[{level: info|warning, text, fields?: [names], link?: url}]``
    — ``fields`` names the rows or commands a note is about, so the UI can
    mark them where they are listed."""
    nt = t.get('notes')
    if nt is None:
        return []
    if not isinstance(nt, list) or len(nt) > 20:
        return ["notes must be a list of at most 20 notes"]
    known = {r.get('name') for k in ('registers', 'calculated')
             for r in (t.get(k) or []) if isinstance(r, dict)}
    known |= set(t.get('commands') or {}) if isinstance(t.get('commands'), dict) else set()
    out = []
    for i, n in enumerate(nt):
        w = f"notes[{i}]"
        if not isinstance(n, dict):
            out.append(f"{w}: must be an object")
            continue
        if n.get('level', 'info') not in NOTE_LEVELS:
            out.append(f"{w}: level is info or warning")
        txt = n.get('text')
        if not isinstance(txt, str) or not txt.strip() or len(txt) > 1000:
            out.append(f"{w}: text is required (at most 1000 characters)")
        fl = n.get('fields')
        if fl is not None and (not isinstance(fl, list)
                               or not all(isinstance(x, str) and x for x in fl)):
            out.append(f"{w}: fields is a list of row or command names")
        elif fl:
            missing = [x for x in fl if x not in known]
            if missing:
                out.append(f"{w}: fields not in this template: {', '.join(missing)}")
        link = n.get('link')
        if link not in (None, '') and (not isinstance(link, str)
                                       or not link.startswith(('https://', 'http://'))):
            out.append(f"{w}: link must be an http(s) address")
    return out


def _setup_errors(t: Dict[str, Any]) -> List[str]:
    """``setup``: ``[{name, label?, address, values: [int | "0x…"], provides?: {from, to}}]``
    — written as one write-multiple frame each, at connect and when the
    registers a step provides are refused."""
    st = t.get('setup')
    if st is None:
        return []
    if not isinstance(st, list):
        return ["setup must be a list of steps"]
    out, names = [], set()
    for i, s in enumerate(st):
        w = f"setup[{i}]"
        if not isinstance(s, dict):
            out.append(f"{w}: must be an object")
            continue
        n = s.get('name')
        if not isinstance(n, str) or not _ID_RE.match(n):
            out.append(f"{w}: name invalid (a-z 0-9 - _, 2-64 chars)")
        elif n in names:
            out.append(f"{w}: duplicate name {n!r}")
        names.add(n)
        a = s.get('address')
        if isinstance(a, bool) or not isinstance(a, int) or not 0 <= a <= 65535:
            out.append(f"{w}: address must be 0..65535")
        vals = s.get('values')
        if not isinstance(vals, list) or not 1 <= len(vals) <= 123:
            out.append(f"{w}: values must be a list of 1..123 words")
        else:
            for j, v in enumerate(vals):
                try:
                    if isinstance(v, float) and not v.is_integer():
                        raise ValueError            # 1.7 would be written as 1, silently
                    iv = int(v, 0) if isinstance(v, str) else (None if isinstance(v, bool) else int(v))
                    if iv is None or not 0 <= iv <= 0xFFFF:
                        raise ValueError
                except (TypeError, ValueError):
                    out.append(f"{w}: values[{j}] {v!r} is not a 16-bit word (0..65535 or \"0x…\")")
        p = s.get('provides')
        if p is not None:
            if (not isinstance(p, dict) or not isinstance(p.get('from'), int)
                    or not isinstance(p.get('to', p.get('from')), int) or p.get('to', p['from']) < p['from']):
                out.append(f"{w}: provides must be {{from, to}} with from ≤ to")
    return out


def extra_errors(data: Dict[str, Any]) -> List[str]:
    """The checks a SAVE enforces beyond the original schema: what a row may
    carry, categories, commands checked against the map."""
    t = (data or {}).get('device_template') or {}
    regs = t.get('registers') if isinstance(t.get('registers'), list) else []
    calcs = t.get('calculated') if isinstance(t.get('calculated'), list) else []
    return (_register_extra_errors(regs, calcs) + _category_errors(t) + _command_errors(t) + _channel_errors(t)
            + _kind_errors(t))


def _kind_errors(t: Dict[str, Any]) -> List[str]:
    """``kind``: the attribute catalog's vocabulary, or nothing."""
    k = t.get('kind')
    if k in (None, ''):
        return []
    from .attribute_catalog import KINDS
    return [] if k in KINDS else [f"kind: one of {', '.join(KINDS)} (or empty)"]


AGGREGATE_OPS = ('sum', 'avg', 'min', 'max', 'spread', 'mode')
_HA_STATE_CLASSES = ('', 'measurement', 'total', 'total_increasing', 'none')
_HA_ENTITY_CATEGORIES = ('', 'diagnostic', 'config', 'none')


def _aggregate_errors(where: str, agg: Any) -> List[str]:
    if agg is None:
        return []
    if not isinstance(agg, dict) or not agg:
        return [f"{where}: aggregates must be an object {{output_name: op}}"]
    out = []
    for name, op in agg.items():
        if not isinstance(name, str) or not _CALC_NAME_RE.match(name):
            out.append(f"{where}: aggregates output {name!r} must be letters, digits and _")
        if op not in AGGREGATE_OPS:
            out.append(f"{where}: aggregates op {op!r} for {name!r} is not one of {', '.join(AGGREGATE_OPS)}")
    return out


def _register_extra_errors(regs: List[Any], calcs: List[Any]) -> List[str]:
    """What a register row may carry beyond its address and type — checked
    on save, so a typo is named instead of silently doing nothing."""
    errors: List[str] = []
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)  # noqa: E731
    for i, r in enumerate(regs):
        if not isinstance(r, dict):
            continue
        where = f"register #{i} (name {r.get('name')!r})"
        errors.extend(_aggregate_errors(where, r.get('aggregates')))
        wt = r.get('write_topic')
        if wt is not None and (not isinstance(wt, str) or any(c in wt for c in '+#')):
            errors.append(f"{where}: write_topic is one topic, no + or #")
        from .text_check import formula_issue, topic_issue
        if isinstance(r.get('name'), str):
            # a name is what a formula, a rule and a topic leaf call the row
            _ni = formula_issue(r['name'], f"{where} name")
            if _ni:
                errors.append(_ni)
            if r['name'].lower() == 'meta' or str(r.get('topic') or '').strip('/').lower() == 'meta' \
                    or str((r.get('defaults') or {}).get('mqtt', {}).get('topic') or '').strip('/').lower() == 'meta':
                errors.append(f"{where}: 'meta' is reserved — <device topic>/meta carries the device's attributes")
        for _tk in ('topic', 'write_topic'):
            if isinstance(r.get(_tk), str) and r.get(_tk):
                _ti = topic_issue(r[_tk], f"{where} {_tk}")
                if _ti:
                    errors.append(_ti)
        if r.get('write_payload') is not None and not isinstance(r.get('write_payload'), str):
            errors.append(f"{where}: write_payload is text, with ${{value}} or ${{label}} where the value goes")
        nan = r.get('nan')
        if nan is not None and not (nan is True or num(nan)
                                    or (isinstance(nan, list) and nan and all(num(x) for x in nan))):
            errors.append(f"{where}: nan must be true (the type's standard 'not available'), a number or a list of numbers")
        for flag in ('monotonic', 'daily', 'writable'):
            if flag in r and not isinstance(r[flag], bool):
                errors.append(f"{where}: {flag} must be true or false")
        for k in ('enum', 'bits'):
            m = r.get(k)
            if m is None:
                continue
            if not isinstance(m, dict) or not m:
                errors.append(f"{where}: {k} must be a non-empty object {{code: text}}")
                continue
            for code, label in m.items():
                try:
                    int(code)
                except (TypeError, ValueError):
                    errors.append(f"{where}: {k} key {code!r} is not an integer")
                if not isinstance(label, str):
                    errors.append(f"{where}: {k} text for {code!r} must be a string")
        for k in ('mask', 'shift'):
            v = r.get(k)
            if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
                errors.append(f"{where}: {k} must be a whole number ≥ 0")
        if r.get('state_class', '') not in _HA_STATE_CLASSES:
            errors.append(f"{where}: state_class must be one of {', '.join(x or '(empty)' for x in _HA_STATE_CLASSES)}")
        if r.get('entity_category', '') not in _HA_ENTITY_CATEGORIES:
            errors.append(f"{where}: entity_category must be one of {', '.join(x or '(empty)' for x in _HA_ENTITY_CATEGORIES)}")
        ebd = r.get('enabled_by_default')
        if ebd is not None and not isinstance(ebd, bool):
            errors.append(f"{where}: enabled_by_default must be true or false")
        sdp = r.get('suggested_display_precision')
        if sdp is not None and (not isinstance(sdp, int) or isinstance(sdp, bool) or not 0 <= sdp <= 10):
            errors.append(f"{where}: suggested_display_precision must be 0..10")
        icon = r.get('icon', '')
        if icon and (not isinstance(icon, str) or not icon.startswith('mdi:')):
            errors.append(f"{where}: icon must look like 'mdi:flash'")
        for k in ('label', 'unit', 'description', 'device_class', 'access'):
            if k in r and r[k] is not None and not isinstance(r[k], str):
                errors.append(f"{where}: {k} must be text")
    for i, c in enumerate(calcs):
        if not isinstance(c, dict):
            continue
        where = f"calculated #{i} (name {c.get('name')!r})"
        errors.extend(_aggregate_errors(where, c.get('aggregates')))
        ui = c.get('ui')
        if ui is not None and not isinstance(ui, dict):
            errors.append(f"{where}: ui must be an object {{show_on_dashboard, widget}}")
        for flag in ('mqtt', 'influxdb'):
            if flag in c and not isinstance(c[flag], bool):
                errors.append(f"{where}: {flag} must be true or false")
    return errors


def _category_errors(t: Dict[str, Any]) -> List[str]:
    cats = t.get('categories')
    if cats is None:
        return []
    if not isinstance(cats, dict):
        return ["categories must be an object {id: {label, order}}"]
    out = []
    # a calculated field names its section like a row does
    for i, c in enumerate(t.get('calculated') or []):
        cc = c.get('category') if isinstance(c, dict) else None
        if cc and cats and cc not in cats:
            out.append(f"calculated[{i}] ({c.get('name')}): category {cc!r} not declared in categories")
    for cid, c in cats.items():
        if not isinstance(c, dict):
            out.append(f"category {cid!r}: must be an object {{label, order}}")
            continue
        if 'label' in c and not isinstance(c['label'], str):
            out.append(f"category {cid!r}: label must be text")
        o = c.get('order')
        if o is not None and (not isinstance(o, (int, float)) or isinstance(o, bool)):
            out.append(f"category {cid!r}: order must be a number")
    return out


CHANNEL_COMBINE = ('priority', 'min', 'max')


def _channel_errors(t: Dict[str, Any]) -> List[str]:
    """`channels:` and each command's `channel` / `on`."""
    out: List[str] = []
    ch = t.get('channels')
    if ch is not None and not isinstance(ch, dict):
        return ["channels: an object {name: {combine, min_on_s, min_off_s, max_starts_per_h}}"]
    for name, c in (ch or {}).items():
        w = f"channel {name!r}"
        if not isinstance(c, dict):
            out.append(f"{w}: an object")
            continue
        unknown = set(c) - {'combine', 'min_on_s', 'min_off_s', 'max_starts_per_h', 'min_interval_s', 'label'}
        if unknown:
            out.append(f"{w}: unknown {', '.join(sorted(unknown))}")
        if c.get('combine', 'priority') not in CHANNEL_COMBINE:
            out.append(f"{w}: combine is one of {', '.join(CHANNEL_COMBINE)}")
        for k in ('min_on_s', 'min_off_s', 'max_starts_per_h', 'min_interval_s'):
            v = c.get(k)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 7 * 86400):
                out.append(f"{w}: {k} 0..604800")
    from .commands import _COND
    for cname, c in (t.get('commands') or {}).items() if isinstance(t.get('commands'), dict) else ():
        if not isinstance(c, dict):
            continue
        if c.get('channel') is not None and not (isinstance(c['channel'], str) and _ID_RE.match(c['channel'])):
            out.append(f"command {cname!r}: channel is a name (a-z 0-9 - _)")
        if c.get('on') is not None and not (isinstance(c['on'], str) and _COND.match(c['on'])):
            out.append(f"command {cname!r}: on is a condition on its parameters, e.g. \"${{value}} > 0\"")
    return out


def _command_errors(t: Dict[str, Any]) -> List[str]:
    """A command is checked against the map it lives in: the registers it
    reads and writes exist, its expressions evaluate, its parameters have
    coherent bounds — so a mistake shows on save, not at the first write."""
    cmds = t.get('commands')
    if not isinstance(cmds, dict):
        return []
    from .commands import ExprError, evaluate
    names = {r.get('name') for r in t.get('registers') or [] if isinstance(r, dict)}
    # an MQTT template writes by publishing: each written register needs the
    # topic it is published on
    _transports = [str(x).lower() for x in ((t.get('protocol') or {}).get('transports') or [])] \
        if isinstance(t.get('protocol'), dict) else []
    publishes = {r.get('name') for r in t.get('registers') or []
                 if isinstance(r, dict) and r.get('write_topic')}
    is_mqtt = 'mqtt' in _transports or bool(publishes)
    groups = set((t.get('poll_groups') or {}).keys())
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)  # noqa: E731
    out: List[str] = []
    for cname, c in cmds.items():
        if not isinstance(c, dict) or not isinstance(cname, str):
            continue
        w = f"command {cname!r}"
        params = c.get('params') or {}
        if not isinstance(params, dict):
            continue
        sample: Dict[str, Any] = {}
        for pn, pd in params.items():
            if not isinstance(pd, dict):
                out.append(f"{w}: param {pn!r} must be an object")
                continue
            lo, hi, dflt = pd.get('min'), pd.get('max'), pd.get('default')
            for k, v in (('min', lo), ('max', hi), ('default', dflt)):
                if v is not None and not num(v):
                    out.append(f"{w}: param {pn!r} {k} must be a number")
            if num(lo) and num(hi) and lo > hi:
                out.append(f"{w}: param {pn!r} min {lo} > max {hi}")
            if num(dflt) and ((num(lo) and dflt < lo) or (num(hi) and dflt > hi)):
                out.append(f"{w}: param {pn!r} default {dflt} is outside min/max")
            al = pd.get('allowed')
            if al is not None and (not isinstance(al, list) or not al or not all(num(x) for x in al)):
                out.append(f"{w}: param {pn!r} allowed must be a non-empty list of numbers")
            sample[pn] = dflt if num(dflt) else (lo if num(lo) else (hi if num(hi) else 0))
        alias = c.get('alias')
        if alias is not None:
            if not isinstance(alias, dict) or alias.get('command') not in cmds:
                out.append(f"{w}: alias.command must name another command of this template")
            continue
        if not c.get('writes'):
            out.append(f"{w}: writes is empty — a command writes at least one register (or is an alias)")

        def _check(kind: str, items: Any, key: str) -> None:
            if items is None:
                return
            if not isinstance(items, list):
                out.append(f"{w}: {kind} must be a list")
                return
            for j, it in enumerate(items):
                if not isinstance(it, dict):
                    out.append(f"{w}: {kind}[{j}] must be an object")
                    continue
                reg = it.get(key)
                if reg not in names:
                    out.append(f"{w}: {kind}[{j}] {key} {reg!r} is not a register of this template")
                for vk in ('value', 'expect'):
                    if vk in it:
                        try:
                            evaluate(it[vk], sample)
                        except (ExprError, TypeError, ValueError, ZeroDivisionError) as e:
                            out.append(f"{w}: {kind}[{j}] {vk}: {e}")
        _check('writes', c.get('writes'), 'register')
        if is_mqtt and isinstance(c.get('writes'), list):
            for j, it in enumerate(c['writes']):
                if isinstance(it, dict) and it.get('register') in names and it.get('register') not in publishes:
                    out.append(f"{w}: writes[{j}] register {it.get('register')!r} has no write_topic "
                               "— an MQTT command publishes there")
        _check('guard', c.get('guard'), 'read')
        _check('verify', c.get('verify'), 'read')
        st = c.get('settle_s')
        if st is not None and (not num(st) or st < 0):
            out.append(f"{w}: settle_s must be a number ≥ 0")
        safe = c.get('safe')
        if safe is not None:
            if not isinstance(safe, dict):
                out.append(f"{w}: safe must be an object {{param: value}}")
            else:
                for k in safe:
                    if k not in params:
                        out.append(f"{w}: safe names {k!r}, which is not a parameter")
        rg = c.get('readback_group')
        if rg and groups and rg not in groups:
            out.append(f"{w}: readback_group {rg!r} is not a poll group of this template")
        if 'confirm' in c and not isinstance(c['confirm'], bool):
            out.append(f"{w}: confirm must be true or false")
        rf = c.get('refresh_s')
        if rf is not None and (not num(rf) or (rf != 0 and not 10 <= rf <= 86400)):
            out.append(f"{w}: refresh_s must be 0 (off) or 10..86400 seconds")
        if rf and not c.get('safe'):
            out.append(f"{w}: refresh_s needs safe parameters — they are what stops the refresh")
    return out


def _identify_errors(t: Dict[str, Any]) -> List[str]:
    """``identify``: ``{registers: [{address, register_type?, data_type?,
    equals | in | min/max}], fc43: {vendor?, product?}}`` — every register must
    match; FC43 fields are regexes on the device's own identification."""
    idf = t.get('identify')
    if idf is None:
        return []
    errs: List[str] = []
    if not isinstance(idf, dict):
        return ["identify: a mapping with registers and/or fc43"]
    regs = idf.get('registers') or []
    if not isinstance(regs, list) or len(regs) > 8:
        errs.append("identify.registers: a list of at most 8 checks")
        regs = []
    for i, r in enumerate(regs):
        if not isinstance(r, dict) or not isinstance(r.get('address'), int) or not 0 <= r['address'] <= 65535:
            errs.append(f"identify.registers[{i}]: needs an address 0-65535")
            continue
        if r.get('register_type', 'holding') not in ('holding', 'input'):
            errs.append(f"identify.registers[{i}].register_type: holding or input")
        if not any(k in r for k in ('equals', 'in', 'min', 'max')):
            errs.append(f"identify.registers[{i}]: say what it must read — equals, in, or min/max")
        if 'in' in r and not isinstance(r['in'], list):
            errs.append(f"identify.registers[{i}].in: a list of values")
    fc = idf.get('fc43')
    if fc is not None:
        if not isinstance(fc, dict) or not (fc.get('vendor') or fc.get('product')):
            errs.append("identify.fc43: {vendor and/or product} regexes")
        else:
            for k in ('vendor', 'product'):
                if fc.get(k):
                    try:
                        re.compile(str(fc[k]))
                    except re.error:
                        errs.append(f"identify.fc43.{k}: not a valid regular expression")
    if not regs and fc is None:
        errs.append("identify: give registers to check, an fc43 pattern, or both")
    return errs


def _display_errors(t: Dict[str, Any]) -> List[str]:
    """The template's `display` block: glance/hero name fields the template
    itself declares (a typo would silently show nothing); headline names
    totals, which the template's aggregates or the operator's totals declare,
    so only its shape is checked here."""
    d = t.get('display')
    if d is None:
        return []
    if not isinstance(d, dict):
        return ["display must be a mapping"]
    errs: List[str] = []
    known = {r.get('name') for r in (t.get('registers') or []) if isinstance(r, dict)} \
        | {c.get('name') for c in (t.get('calculated') or []) if isinstance(c, dict)}
    for key in ('glance', 'hero'):
        v = d.get(key)
        if v is None:
            continue
        if not isinstance(v, list) or len(v) > 12 or not all(isinstance(x, str) for x in v):
            errs.append(f"display.{key}: a list of at most 12 field names")
            continue
        for x in v:
            if x not in known:
                errs.append(f"display.{key}: {x!r} is not a register or calculated field of this template")
    h = d.get('headline')
    if h is not None:
        if not isinstance(h, list) or len(h) > 6:
            errs.append("display.headline: a list of at most 6 {field, label?, hint?}")
        else:
            for i, item in enumerate(h):
                if not isinstance(item, dict) or not isinstance(item.get('field'), str) \
                        or not item.get('field'):
                    errs.append(f"display.headline[{i}]: needs a 'field' (a total's name)")
    for key in ('unit_label', 'unit_label_plural', 'icon'):
        if key in d and (not isinstance(d[key], str) or len(d[key]) > 64):
            errs.append(f"display.{key}: a short string")
    al = d.get('alarms')
    if al is not None:
        if not isinstance(al, list) or len(al) > 50:
            errs.append("display.alarms: a list of {field, severity} (at most 50)")
        else:
            for i, a in enumerate(al):
                if not isinstance(a, dict) or a.get('field') not in known:
                    errs.append(f"display.alarms[{i}]: 'field' must be a field of this template")
                elif a.get('severity', 'warning') not in ('warning', 'danger'):
                    errs.append(f"display.alarms[{i}]: severity is warning or danger")
                elif a.get('for_s') is not None and (isinstance(a.get('for_s'), bool)
                                                     or not isinstance(a.get('for_s'), (int, float))
                                                     or not 0 <= a['for_s'] <= 86400):
                    errs.append(f"display.alarms[{i}]: for_s is how long it must stay active before an alert, 0..86400 s")
    secs = d.get('sections')
    if secs is not None:
        cats = set((t.get('categories') or {}).keys())
        if not isinstance(secs, dict):
            errs.append("display.sections: a mapping of category: {widget}")
        else:
            for c, v in secs.items():
                if cats and c not in cats:
                    errs.append(f"display.sections.{c}: not a category of this template")
                if not isinstance(v, dict) or v.get('widget') not in ('grid', 'active_only'):
                    errs.append(f"display.sections.{c}: widget is grid or active_only")
                    continue
                # a grid may say which fields are tiles (a regex on the name;
                # default: those sharing the section's main unit) and how
                # finely they read (a pack's cells differ in millivolts)
                tiles = v.get('tiles')
                if tiles is not None:
                    # the browser runs it: Python-only syntax (named groups
                    # `(?P<…>`, inline flags `(?i)`) would fail there silently
                    try:
                        re.compile(str(tiles))
                        if re.search(r"\(\?P|\(\?[aiLmsux-]+[:)]", str(tiles)):
                            raise re.error("Python-only syntax")
                    except re.error:
                        errs.append(f"display.sections.{c}.tiles: not a valid regular expression "
                                    "(plain syntax both Python and JavaScript read)")
                dec = v.get('decimals')
                if dec is not None and (isinstance(dec, bool) or not isinstance(dec, int)
                                        or not 0 <= dec <= 6):
                    errs.append(f"display.sections.{c}.decimals: an integer 0–6")
    bm = d.get('bitmasks')
    if bm is not None:
        if not isinstance(bm, dict):
            errs.append("display.bitmasks: a mapping of field: label pattern")
        else:
            for f, pat in bm.items():
                if f not in known:
                    errs.append(f"display.bitmasks.{f}: not a field of this template")
                elif not isinstance(pat, str) or '{n}' not in pat:
                    errs.append(f"display.bitmasks.{f}: a label with {{n}} for the bit number, e.g. 'Cell {{n}}'")
    return errs


def parse_template(data: Dict[str, Any], *, builtin: bool = False,
                   path: str = "", strict: bool = True) -> DeviceTemplate:
    """Dict → DeviceTemplate. Raises ValueError with all problems at once.
    ``strict=False`` (a file loaded from disk) logs the newer checks as
    warnings instead of refusing the file — an upgrade never drops a device."""
    errors = validate_template(data, extras=strict)
    if not strict:
        for w in extra_errors(data):
            logger.warning("device template %s: %s — fix it in the editor (saving checks it)",
                           path or (data or {}).get('device_template', {}).get('id'), w)
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
        write_topic=str(r.get('write_topic', '') or ''),
        write_payload=str(r.get('write_payload', '') or ''),
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
        aggregates=(dict(r['aggregates'])
                    if isinstance(r.get('aggregates'), dict) else None),
    ) for r in t['registers']]
    calcs = [TemplateCalculated(
        name=str(c['name']), expr=str(c.get('expr', '') or ''),
        label=str(c.get('label', '') or c['name']),
        unit=str(c.get('unit', '') or ''),
        poll_group=str(c.get('poll_group', '') or ''),
        decimals=c.get('decimals'),
        topic=str(c.get('topic', '') or ''),
        measurement=str(c.get('measurement', '') or ''),
        category=str(c.get('category', '') or ''),
        enum=c.get('enum'),
        mqtt=bool(c.get('mqtt', True)),
        influxdb=bool(c.get('influxdb', True)),
        aggregates=(dict(c['aggregates'])
                    if isinstance(c.get('aggregates'), dict) else None),
        ui=(dict(c['ui']) if isinstance(c.get('ui'), dict) else None),
    ) for c in (t.get('calculated') or [])]
    return DeviceTemplate(
        id=t['id'], name=t['name'],
        vendor=t.get('vendor', ''), model=t.get('model', ''),
        manufacturer=str(t.get('manufacturer', '') or ''),
        version=str(t.get('version', '1.0.0')), author=t.get('author', ''),
        description=t.get('description', ''),
        source_document=t.get('source_document', ''),
        kind=str(t.get('kind', '') or ''),
        schema_version=int(t.get('schema_version', SCHEMA_VERSION)),
        protocol=t.get('protocol', {}) or {},
        poll_groups=t.get('poll_groups', {}) or {},
        categories=t.get('categories', {}) or {},
        canonical=bool(t.get('canonical', False)),
        pq_recorder=str(t.get('pq_recorder', '') or ''),
        commands=dict(t.get('commands') or {}),
        display=dict(t.get('display') or {}) if isinstance(t.get('display'), dict) else {},
        identify=dict(t.get('identify') or {}) if isinstance(t.get('identify'), dict) else {},
        setup=[dict(x) for x in (t.get('setup') or []) if isinstance(x, dict)],
        notes=[dict(x) for x in (t.get('notes') or []) if isinstance(x, dict)],
        rules=[dict(x) for x in (t.get('rules') or []) if isinstance(x, dict)],
        channels={str(k): dict(v) for k, v in (t.get('channels') or {}).items() if isinstance(v, dict)}
        if isinstance(t.get('channels'), dict) else {},
        registers=regs, calculated=calcs, builtin=builtin, path=path,
        components={str(k): dict(v) for k, v in (t.get('components') or {}).items()
                    if isinstance(v, dict)} if isinstance(t.get('components'), dict) else {},
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
    return parse_template(data, builtin=builtin, path=str(p), strict=False)


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
                    self._put(t)
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
                            # nothing, so the lint skips them. A register with
                            # no defaults at all is not curated (nobody polls
                            # it unless an operator opts in), so it is not
                            # routed either.
                            d = r.defaults
                            if not d:
                                return False
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

    def _put(self, t: DeviceTemplate) -> None:
        """Hold a template and the templates its components become; a
        component the template no longer declares goes."""
        self._drop_components(t.id)
        self._templates[t.id] = t
        for ct in component_templates(t):
            self._templates[ct.id] = ct

    def _drop_components(self, template_id: str) -> None:
        for k in [k for k, v in self._templates.items() if v.component_of == template_id]:
            del self._templates[k]

    def get(self, template_id: str) -> Optional[DeviceTemplate]:
        return self._templates.get(template_id)

    def byte_order_for(self, template_id: Optional[str]) -> str:
        """The word/byte decode order a device's template declares (default
        'big'). The SINGLE source of truth for both the boot path (main.py) and
        the runtime create/apply path (api.py) — they must agree, or a non-big
        device decodes garbage and encodes wrong-order writes after a restart."""
        tpl = self.get(template_id) if template_id else None
        return (tpl.protocol.get('byte_order', 'big') if tpl else 'big')

    def all(self) -> List[DeviceTemplate]:
        """Every template, components included (what their formulas name)."""
        return list(self._templates.values())

    def list(self) -> List[DeviceTemplate]:
        # a component is added through its parent, never picked by itself
        return sorted((t for t in self._templates.values() if not t.component_of),
                      key=lambda t: (not t.builtin, t.vendor.lower(), t.name.lower()))

    def save_user(self, data: Dict[str, Any]) -> DeviceTemplate:
        """Validate + persist a user template (create or update). Refuses to
        touch built-ins."""
        t = parse_template(data)
        if is_component_id(t.id):
            raise ValueError(f"{t.id!r} is a component — edit its parent template")
        existing = self.get(t.id)
        if existing and existing.builtin:
            raise ValueError(f"template {t.id!r} is built-in (read-only) — duplicate it under a new id")
        self.user_dir.mkdir(parents=True, exist_ok=True)
        path = self.user_dir / f"{t.id}.json"
        # a user file named differently but carrying this id (a hand copy, an
        # old upload) would shadow the fresh save at the next reload and
        # resurrect a deleted template (F-20, 3.83.0): remove such twins
        if existing and existing.path and Path(existing.path) != path \
                and Path(existing.path).parent == self.user_dir:
            Path(existing.path).unlink(missing_ok=True)
        for twin in self._user_files_with_id(t.id):
            if twin != path:
                twin.unlink(missing_ok=True)
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
        self._put(t)
        logger.info("device template %s saved (%d registers)", t.id, len(t.registers))
        return t

    def _user_files_with_id(self, template_id: str) -> List[Path]:
        """Every file in the user dir whose content declares ``template_id``,
        whatever it is named."""
        out: List[Path] = []
        if not self.user_dir.is_dir():
            return out
        for f in self.user_dir.iterdir():
            if f.suffix.lower() not in ('.json', '.yaml', '.yml'):
                continue
            try:
                with open(f, encoding='utf-8') as fh:
                    raw = json.load(fh) if f.suffix.lower() == '.json' else yaml.safe_load(fh)
                if ((raw or {}).get('device_template') or {}).get('id') == template_id:
                    out.append(f)
            except Exception:  # noqa: BLE001 — an unreadable file is not this id
                continue
        return out

    def delete_user(self, template_id: str) -> None:
        t = self.get(template_id)
        if not t:
            raise KeyError(template_id)
        if t.component_of:
            raise ValueError(f"{template_id!r} is a component of {t.component_of!r} — edit that template")
        if t.builtin:
            raise ValueError(f"template {template_id!r} is built-in (read-only)")
        Path(t.path).unlink(missing_ok=True)
        for twin in self._user_files_with_id(template_id):      # F-20: no resurrection
            twin.unlink(missing_ok=True)
        del self._templates[template_id]
        self._drop_components(template_id)
        logger.info("device template %s deleted", template_id)
