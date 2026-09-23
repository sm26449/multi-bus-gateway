# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""One classification for what a measurement IS.

The picker filed every Solar API register under "other" while the Monitor
grouped the same fields under "Power_active / Dc" (UI audit 3.4). The
category now comes from the canonical name, once, on the server.
"""

from multibus.routes.registers_routes import canonical_category
from tests.test_endpoint_edit_guard import PLANT
from tests.test_endpoints import make_app, needs_tc


def test_category_comes_from_the_canonical_name_then_the_unit():
    assert canonical_category('power_active_total') == 'power'
    assert canonical_category('power_factor_l1') == 'power'
    assert canonical_category('voltage_dc') == 'dc'
    assert canonical_category('energy_active_generated') == 'energy'
    assert canonical_category('power_pv') == 'site'
    assert canonical_category('event_flags_1') == 'status'
    assert canonical_category('temperature_cabinet') == 'temperature'
    # not canonical: the unit still says what it is
    assert canonical_category('vendor_x_17', 'kWh') == 'energy'
    assert canonical_category('vendor_x_17', 'Hz') == 'frequency'
    assert canonical_category('vendor_x_17', '') == 'other'


@needs_tc
def test_the_catalog_files_canonical_fields_under_their_category(tmp_path):
    cfg, client = make_app(tmp_path)
    assert client.post("/api/endpoints", json=PLANT).status_code == 200
    cat = client.get("/api/registers/all?device=pv-u1&source=solar_api").json()["measurements"]
    assert 'other' not in cat or not cat['other']['entries']
    names = {c: [e['name'] for e in v['entries']] for c, v in cat.items()}
    assert 'power_active_total' in names['power'] and cat['power']['name'] == 'Power'
    assert 'voltage_dc' in names['dc'] and 'current_dc' in names['dc']
    assert 'energy_active_generated' in names['energy']
    assert 'frequency' in names['frequency']


@needs_tc
def test_the_selection_carries_its_category_and_the_sources_their_census(tmp_path):
    cfg, client = make_app(tmp_path)
    assert client.post("/api/endpoints", json=PLANT).status_code == 200
    d = client.get("/api/registers/selected?device=pv-u1&source=solar_api").json()
    assert all('category' in r for r in d['registers'])
    by = {r['name']: r['category'] for r in d['registers']}
    assert by['power_active_total'] == 'power' and by['voltage_dc'] == 'dc'
    src = {s['id']: s for s in d['sources']}
    assert src['solar_api']['selected'] == len(d['registers'])
    assert src['solar_api']['catalog'] >= src['solar_api']['selected']
    assert src['solar_api']['interval_s'] == 2 and src['solar_api']['protocol'] == 'http'
    assert src['sunspec']['interval_s'] == 20 and src['sunspec']['selected'] > 0
