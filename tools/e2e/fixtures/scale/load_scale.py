#!/usr/bin/env python3
# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Fill a throwaway gateway with many devices and templates, to measure how
the Devices and Templates pages behave at scale (findability_e2e.mjs).

    python3 tools/e2e/fixtures/scale/load_scale.py http://127.0.0.1:18096 [DEVICES] [TEMPLATES]

Every device is created OFF (nothing is ever polled) at a loopback address
where nothing listens. Templates are copies of a bundled one under new ids,
vendors and kinds. Ids start with `sc-` so a run can be told from the
fixture; running it twice answers 409 for what already exists and goes on.
Standard library only.
"""
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip('/') if len(sys.argv) > 1 else 'http://127.0.0.1:18096'
N_DEV = int(sys.argv[2]) if len(sys.argv) > 2 else 200
N_TPL = int(sys.argv[3]) if len(sys.argv) > 3 else 100

VENDORS = ['Acme', 'Borealis', 'Cobalt', 'Delta Grid', 'Ember', 'Fjord', 'Gauss', 'Helix',
           'Ion', 'Joule', 'Kelvin', 'Lumen', 'Mho', 'Nimbus', 'Ohm', 'Pulse']
KINDS = ['meter', 'inverter', 'battery', 'sensor', 'ups', 'charger', 'drive', 'heat_pump']
# the bundled templates the devices are read with (one per kind the fixture shows)
DEVICE_TEMPLATES = ['eastron_sdm630', 'eastron_sdm120', 'abb_b23', 'fronius_sunspec_inverter',
                    'janitza_umg512_pro']


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'{}')


def main():
    status, base = call('GET', '/api/device-templates/eastron_sdm630/export')
    if status != 200:
        sys.exit(f'cannot read a bundled template to copy: {status} {base}')
    made_t = 0
    for i in range(N_TPL):
        doc = json.loads(json.dumps(base))
        tpl = doc.get('device_template', doc)
        tpl['id'] = f'sc-tpl-{i:03d}'
        tpl['name'] = f'{VENDORS[i % len(VENDORS)]} model {100 + i}'
        tpl['vendor'] = VENDORS[i % len(VENDORS)]
        tpl['model'] = f'M{100 + i}'
        tpl['kind'] = KINDS[i % len(KINDS)] if KINDS[i % len(KINDS)] in ('meter', 'inverter', 'battery', 'sensor') else 'meter'
        status, out = call('POST', '/api/device-templates/upload', {'template': doc, 'overwrite': True})
        made_t += status == 200
        if status not in (200, 409):
            print('template', tpl['id'], status, out, file=sys.stderr)
    made_d = 0
    for i in range(N_DEV):
        body = {
            'id': f'sc-dev-{i:03d}', 'name': f'{VENDORS[i % len(VENDORS)]} {KINDS[i % len(KINDS)]} {i}',
            'template': DEVICE_TEMPLATES[i % len(DEVICE_TEMPLATES)], 'enabled': False,
            'connection': {'protocol': 'tcp', 'host': '127.0.0.1', 'port': 20000 + i, 'unit_id': 1 + i % 247},
        }
        status, out = call('POST', '/api/devices', body)
        made_d += status == 200
        if status == 200 and i % 3 == 0:
            call('PATCH', f'/api/devices/sc-dev-{i:03d}/organise', {'kind': KINDS[i % len(KINDS)]})
        if status not in (200, 409, 422):
            print('device', body['id'], status, out, file=sys.stderr)
    print(f'templates {made_t}/{N_TPL}, devices {made_d}/{N_DEV}')


if __name__ == '__main__':
    main()
