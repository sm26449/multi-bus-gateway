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
    # NOTE: 'string' is intentionally NOT here for input maps — the register
    # reader has no string decoder (it would silently decode as float) and
    # strings need a per-register length the schema does not carry. Re-add only
    # alongside real length-aware string parsing in RegisterParser.
}

_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{1,63}$')


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
    poll_group: str = ""                     # suggested group when selected
    json_path: str = ""                      # HTTP/JSON + MQTT input: path into the JSON payload
    topic: str = ""                          # MQTT input: the topic this register reads from
    register_type: str = "holding"           # holding (FC3) | input (FC4) | coil (FC1/5) | discrete (FC2)
    defaults: Dict[str, Any] = field(default_factory=dict)
    # ── write safety envelope (opt-in; a register is unwritable unless declared) ──
    writable: bool = False                   # may be written via the write API
    write_min: Optional[float] = None        # reject engineering values below this
    write_max: Optional[float] = None        # reject engineering values above this
    write_safe: Optional[float] = None       # value to revert to when a write-lease expires

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            'address': self.address, 'name': self.name, 'label': self.label,
            'unit': self.unit, 'data_type': self.data_type,
            'access': self.access, 'category': self.category,
            'description': self.description,
        }
        if self.scale != 1.0:
            d['scale'] = self.scale
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
            if self.write_safe is not None:
                d['write_safe'] = self.write_safe
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
    registers: List[TemplateRegister] = field(default_factory=list)
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
            'registers': [r.to_dict() for r in self.registers],
        }}

    def summary(self) -> Dict[str, Any]:
        """List-view form for the API/UI."""
        return {
            'id': self.id, 'name': self.name, 'vendor': self.vendor,
            'model': self.model, 'version': self.version,
            'builtin': self.builtin, 'registers': len(self.registers),
            'categories': len(self.categories),
            'transport': template_transport(self),   # 'modbus' | 'http'
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
        if dt not in VALID_DATA_TYPES:
            errors.append(f"{where}: data_type {dt!r} not supported "
                          f"({', '.join(sorted(VALID_DATA_TYPES))})")
        cat = r.get('category', 'other')
        if categories and cat not in categories:
            errors.append(f"{where}: category {cat!r} not declared in categories")
        pg = r.get('poll_group', '')
        if pg and poll_groups and pg not in poll_groups:
            errors.append(f"{where}: poll_group {pg!r} not declared in poll_groups")
        scale = r.get('scale', 1)
        if not isinstance(scale, (int, float)) or scale == 0:
            errors.append(f"{where}: scale must be a non-zero number")
        # A writable holding register must declare BOTH bounds — otherwise an
        # out-of-range write silently clamps to the data-type limit at the encoder
        # instead of being refused. Coils are single bits, so they need no bounds.
        if r.get('writable'):
            rt = str(r.get('register_type', 'holding')).lower()
            if rt in ('holding', 'input', 'fc3', 'fc4', 'fc6', 'fc16') and (
                    r.get('write_min') is None or r.get('write_max') is None):
                errors.append(f"{where}: writable register must declare write_min "
                              f"and write_max (bounds are required for writes)")
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
        poll_group=str(r.get('poll_group', '')),
        json_path=str(r.get('json_path', '')),
        topic=str(r.get('topic', '')),
        register_type=_norm_rtype(r.get('register_type') or r.get('fc')),
        defaults=r.get('defaults', {}) or {},
        writable=bool(r.get('writable', False)),
        write_min=(float(r['write_min']) if r.get('write_min') is not None else None),
        write_max=(float(r['write_max']) if r.get('write_max') is not None else None),
        write_safe=(float(r['write_safe']) if r.get('write_safe') is not None else None),
    ) for r in t['registers']]
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
        registers=regs, builtin=builtin, path=path,
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
        self.reload()

    def reload(self) -> None:
        self._templates.clear()
        self.load_errors.clear()
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
                except Exception as e:  # noqa: BLE001
                    self.load_errors[f.name] = str(e)
                    logger.warning("device template %s skipped: %s", f.name, e)
        logger.info("device templates loaded: %d (%d errors)",
                    len(self._templates), len(self.load_errors))

    def get(self, template_id: str) -> Optional[DeviceTemplate]:
        return self._templates.get(template_id)

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
        path.write_text(json.dumps(t.to_dict(), indent=1, ensure_ascii=False) + "\n",
                        encoding='utf-8')
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
