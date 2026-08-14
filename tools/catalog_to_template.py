#!/usr/bin/env python3
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
"""Generate the built-in Janitza UMG 512-PRO device template.

Converts the legacy register catalog (``docs/modbus_data.json``) plus the
curated selection defaults (``config/selected_registers.json``, when present)
into the Tier-2 device-template format at
``janitza/device_templates/janitza_umg512_pro.json``.

- Categories: catalog category (or ``category_subtype`` when the catalog nests
  ``subtypes``), labels taken from the catalog's ``name`` fields.
- Data types are NORMALIZED to the parser vocabulary: the catalog's ``int`` /
  ``uint`` would silently fall through to the float decoder at runtime (latent
  bug for e.g. ``_SYSTIME``), so they become ``int32`` / ``uint32`` here.
- Per-register ``defaults`` (mqtt topic, influx measurement/tags, ui widget,
  thresholds, poll_group) are lifted from the currently selected registers so
  the out-of-box Janitza experience stays curated.

Deterministic: same inputs -> byte-identical output (stable ordering), so the
file is reviewable in git diffs.

Usage: python tools/catalog_to_template.py [--catalog PATH] [--selected PATH] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

TYPE_MAP = {
    'int': 'int32',
    'uint': 'uint32',
    'long64': 'int64',
    'short': 'int16',
    'char': 'uint16',
    'byte': 'int16',
}

CATEGORY_LABELS_EN = {
    'voltage': 'Voltage', 'current': 'Current', 'power': 'Power',
    'energy': 'Energy', 'power_factor': 'Power factor',
    'frequency': 'Frequency', 'thd_harmonics': 'THD & harmonics',
    'quality': 'Power quality', 'statistics': 'Statistics',
    'time': 'Date & time', 'config': 'Configuration',
}


def norm_type(dt: str) -> str:
    dt = (dt or 'float').lower()
    return TYPE_MAP.get(dt, dt)


def convert(catalog_path: Path, selected_path: Path | None, out_path: Path) -> dict:
    cat = json.loads(catalog_path.read_text(encoding='utf-8'))

    # curated defaults keyed by (address, name)
    curated: dict = {}
    poll_groups = {
        'realtime': {'interval': 1, 'description': 'Real-time values (voltage, current, power)'},
        'normal': {'interval': 5, 'description': 'Standard measurements'},
        'slow': {'interval': 60, 'description': 'Energy counters, statistics'},
    }
    if selected_path and selected_path.exists():
        sel = json.loads(selected_path.read_text(encoding='utf-8'))
        for g, spec in (sel.get('poll_groups') or {}).items():
            poll_groups[g] = {'interval': spec.get('interval', 5),
                              'description': spec.get('description', '')}
        for r in sel.get('registers', []):
            defaults = {}
            for key in ('mqtt', 'influxdb', 'ui', 'thresholds'):
                v = r.get(key)
                if v:
                    defaults[key] = v
            curated[(r['address'], r['name'])] = {
                'poll_group': r.get('poll_group', ''),
                'defaults': defaults,
            }

    categories: dict = {}
    registers: list = []
    order = 0
    for cat_id, cdata in cat.get('measurements', {}).items():
        groups = []
        if cdata.get('entries'):
            groups.append((cat_id, cdata.get('name', ''), cdata['entries']))
        for sub_id, sdata in (cdata.get('subtypes') or {}).items():
            if sdata.get('entries'):
                groups.append((f'{cat_id}_{sub_id}',
                               sdata.get('name', '') or f'{cat_id} {sub_id}',
                               sdata['entries']))
        for gid, glabel, entries in groups:
            order += 1
            label = CATEGORY_LABELS_EN.get(gid) or glabel or gid.replace('_', ' ').title()
            categories[gid] = {'label': label, 'order': order}
            for e in entries:
                reg = {
                    'address': int(e['address']),
                    'name': str(e['name']),
                    'label': str(e.get('description') or e['name']),
                    'unit': str(e.get('unit', '') or ''),
                    'data_type': norm_type(e.get('data_type')),
                    'access': str(e.get('access', 'RD')),
                    'category': gid,
                    'description': str(e.get('description', '')),
                }
                cur = curated.get((reg['address'], reg['name']))
                if cur:
                    if cur['poll_group']:
                        reg['poll_group'] = cur['poll_group']
                    if cur['defaults']:
                        reg['defaults'] = cur['defaults']
                registers.append(reg)

    registers.sort(key=lambda r: (r['address'], r['name']))

    template = {'device_template': {
        'schema_version': 1,
        'id': 'janitza_umg512_pro',
        'name': 'Janitza UMG 512-PRO',
        'vendor': cat.get('device', {}).get('manufacturer', 'Janitza electronics GmbH'),
        'model': cat.get('device', {}).get('model', 'UMG 512-PRO'),
        'version': '1.0.0',
        'author': 'multi-bus-gateway built-in',
        'description': 'Full Modbus register map of the Janitza UMG 512-PRO power '
                       'quality analyzer, generated from the vendor Modbus address '
                       'list. Curated defaults included for the common electrical '
                       'measurements.',
        'source_document': cat.get('device', {}).get('document', 'Modbus Address List'),
        'protocol': {
            'transports': ['tcp', 'rtu'],
            'default_unit_id': 1,
            'functions': [f['code'] for f in
                          cat.get('modbus', {}).get('protocol', {}).get('slave_functions', [])
                          if f.get('code') in (3, 4)] or [3],
            'byte_order': 'big',
            'max_registers_per_read': 125,
        },
        'poll_groups': poll_groups,
        'categories': categories,
        'registers': registers,
    }}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(template, indent=1, ensure_ascii=False) + '\n',
                        encoding='utf-8')
    return template


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--catalog', default=str(REPO / 'docs/modbus_data.json'))
    ap.add_argument('--selected', default=str(REPO / 'config/selected_registers.json'))
    ap.add_argument('--out', default=str(REPO / 'janitza/device_templates/janitza_umg512_pro.json'))
    args = ap.parse_args()

    sel = Path(args.selected)
    t = convert(Path(args.catalog), sel if sel.exists() else None, Path(args.out))
    tt = t['device_template']
    curated_n = sum(1 for r in tt['registers'] if r.get('defaults'))
    print(f"wrote {args.out}")
    print(f"  registers: {len(tt['registers'])}  categories: {len(tt['categories'])}  "
          f"curated defaults: {curated_n}")

    # self-check: the emitted file must pass the runtime validator
    sys.path.insert(0, str(REPO))
    from multibus.device_template import load_template
    loaded = load_template(args.out, builtin=True)
    print(f"  validator: OK ({loaded.id}, {len(loaded.registers)} registers)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
