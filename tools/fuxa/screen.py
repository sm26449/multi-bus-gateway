# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Make a FUXA screen from the tags provision.py made: one row per value
(device, label, live value, unit), and FUXA's home page opens on it.

By default every device of the connection shows its _Online, _Health and
power_active_total (when it has one); --tag adds more rows.

    python tools/fuxa/screen.py --fuxa http://127.0.0.1:1881 \\
        --tag umg512/frequency --tag samsung-dvm/water_out_temp

Run again to change it: the screen keeps its id; other screens and menu
entries in the project are left alone.
"""
import argparse
import os
import re
import sys
from xml.sax.saxutils import escape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from provision import fuxa, guid  # noqa: E402

DEFAULT_FIELDS = ('_Online', '_Health', 'power_active_total')
ROW, TOP, WIDTH = 30, 90, 1100
COLS = {'device': 40, 'label': 260, 'value': 760, 'unit': 790}


def unit_of(description):
    m = re.search(r'\[([^\]]+)\]\s*$', description or '')
    return m.group(1) if m else ''


def rows_for(tags, extra):
    """The tags to show, in device order: the defaults, then --tag ones."""
    by_name = {t['name']: t for t in tags.values()}
    devices = sorted({n.split('/', 1)[0] for n in by_name})
    out = []
    for d in devices:
        out += [by_name[f"{d}/{f}"] for f in DEFAULT_FIELDS if f"{d}/{f}" in by_name]
        out += [by_name[n] for n in extra if n.startswith(d + '/') and n in by_name
                and n.split('/', 1)[1] not in DEFAULT_FIELDS]
    missing = [n for n in extra if n not in by_name]
    if missing:
        sys.exit(f"no such tag(s): {', '.join(missing)} (names are <device>/<field>)")
    return out


def text(x, y, s, size=14, weight='normal', fill='#000000', anchor='start'):
    return (f'<text x="{x}" y="{y}" font-family="sans-serif" font-size="{size}" '
            f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}" '
            f'xml:space="preserve" id="{guid("svg")}">{escape(s)}</text>')


def build(view_id, title, rows):
    height = TOP + ROW * (len(rows) + 1)
    svg, items, last = [], {}, None
    svg.append(text(COLS['device'], 45, title, size=22, weight='bold'))
    svg.append(text(COLS['device'], 70, 'values served by the gateway over OPC UA, as FUXA reads them',
                    size=12, fill='#666666'))
    for i, t in enumerate(rows):
        y = TOP + ROW * (i + 1)
        device, field = t['name'].split('/', 1)
        if i % 2:
            svg.append(f'<rect x="30" y="{y - 20}" width="{WIDTH - 60}" height="{ROW}" '
                       f'fill="#f3f4f6ff" id="{guid("svg")}"/>')
        if device != last:
            svg.append(text(COLS['device'], y, device, weight='bold'))
            last = device
        svg.append(text(COLS['label'], y, t['label'].split(' · ', 1)[-1]))
        vid = f"VAL_{guid('v')[2:]}"
        svg.append(f'<g id="{vid}" type="svg-ext-value" text-anchor="end" font-family="sans-serif" '
                   f'font-size="14" fill="none" stroke-width="0">'
                   f'<text id="{guid("VAL")}" x="{COLS["value"]}" y="{y}" text-anchor="end" '
                   f'font-family="monospace" font-size="14" fill="#0b5394" '
                   f'xml:space="preserve">##.##</text></g>')
        items[vid] = {'id': vid, 'type': 'svg-ext-value', 'name': t['name'], 'label': 'Value',
                      'property': {'variableId': t['id'], 'events': [], 'actions': [], 'ranges': []}}
        svg.append(text(COLS['unit'], y, unit_of(t.get('description')), fill='#666666'))
    content = (f'<svg width="{WIDTH}" height="{height}" xmlns="http://www.w3.org/2000/svg" '
               f'xmlns:svg="http://www.w3.org/2000/svg"><g><title>Layer 1</title>'
               + ''.join(svg) + '</g></svg>')
    return {'id': view_id, 'name': title, 'type': 'svg',
            'profile': {'width': WIDTH, 'height': height, 'bkcolor': '#ffffffff', 'margin': 10},
            'items': items, 'variables': {}, 'svgcontent': content}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--fuxa', default='http://127.0.0.1:1881')
    ap.add_argument('--name', default='MBG', help='the connection provision.py made')
    ap.add_argument('--title', default='MBG', help="the screen's name")
    ap.add_argument('--tag', action='append', default=[], help='<device>/<field> to add (repeatable)')
    a = ap.parse_args()

    project = fuxa(a.fuxa, '/api/project') or {}
    dev = next((d for d in (project.get('devices') or {}).values() if d.get('name') == a.name), None)
    if not dev:
        sys.exit(f"FUXA has no connection named '{a.name}'; run provision.py first")
    rows = rows_for(dev.get('tags', {}), a.tag)
    hmi = project.get('hmi') or {}
    old = next((v for v in hmi.get('views') or [] if v.get('name') == a.title), None)
    view = build(old['id'] if old else guid('v'), a.title, rows)
    fuxa(a.fuxa, '/api/projectData', {'cmd': 'set-view', 'data': view})

    layout = hmi.get('layout') or {}
    layout.setdefault('navigation', {'mode': 'over', 'type': 'block', 'items': []})
    nav = layout['navigation'].setdefault('items', [])
    if not any(i.get('view') == view['id'] for i in nav):
        nav.append({'icon': 'dashboard', 'view': view['id'], 'link': '', 'text': a.title})
    if not layout.get('start') or not any(v.get('id') == layout['start'] for v in hmi.get('views') or []):
        layout['start'] = view['id']
    fuxa(a.fuxa, '/api/projectData', {'cmd': 'layout', 'data': layout})
    print(f"screen '{a.title}' ({view['id']}): {len(rows)} value(s); "
          f"{'home page' if layout['start'] == view['id'] else 'in the menu'}")


if __name__ == '__main__':
    main()
