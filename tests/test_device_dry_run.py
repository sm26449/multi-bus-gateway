# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""One validator (ADR-0026 §5): `POST /api/devices?dry_run=true` and
`PUT /api/devices/{id}?dry_run=true` run every check a save runs — the
fields, the edit locks, the bus claims (ADR-0025), the topic prefix, what
the store itself refuses — answer what a real save answers, and store
nothing."""
import pathlib

import pytest

from tests.test_devices_api import make_app, needs_tc

SETUP = """
devices:
  - id: tap
    name: Listens on ttyS9
    template: seplos_bms_v3_rtu_tap
    enabled: true
    connection: {protocol: rtu_tap, serial_port: /dev/ttyS9, baudrate: 19200, unit_id: 2}
  - id: inv1
    name: Inverter
    template: eastron_sdm630
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.80, unit_id: 1}
    mqtt: {topic_prefix: site/inv1}
endpoints:
  - id: plant
    name: Plant
    template: eastron_sdm120
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.20, port: 502}
    units: [1]
"""

TCP = {'protocol': 'tcp', 'host': '192.0.2.81', 'unit_id': 1}

POSTS = [
    ("a field", {'id': 'n1', 'connection': {'protocol': 'tcp', 'host': '', 'unit_id': 1}}, 422),
    ("a bus another device listens to", {'id': 'n2', 'template': 'eastron_sdm630', 'connection': {
        'protocol': 'rtu', 'serial_port': '/dev/ttyS9', 'baudrate': 19200, 'unit_id': 7}}, 422),
    ("a prefix", {'id': 'n3', 'connection': TCP, 'mqtt': {'topic_prefix': 'site/inv1/x'}}, 422),
    ("a template's family", {'id': 'n4', 'template': 'eastron_sdm630',
                              'connection': {'protocol': 'http', 'url': 'http://192.0.2.5/x'}}, 422),
    ("an id taken", {'id': 'inv1', 'connection': TCP}, 422),
    ("the reserved id, with no primary", {'id': 'umg512', 'connection': TCP}, 422),
    ("accepted", {'id': 'n5', 'connection': TCP}, 200),
]


def _no_primary(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml="primary_device: false\n" + SETUP)
    assert cfg.primary_device is None
    return cfg, client


@needs_tc
@pytest.mark.parametrize("label,body,status", POSTS, ids=[p[0] for p in POSTS])
def test_a_dry_run_create_answers_what_the_save_answers_and_stores_nothing(tmp_path, label, body, status):
    cfg, client = _no_primary(tmp_path)
    body = {'name': 'x', 'enabled': False, **body}
    path = pathlib.Path(cfg.config_path)
    before, ids = path.read_bytes(), [d.id for d in cfg.devices]
    dry = client.post("/api/devices?dry_run=true", json=body)
    assert path.read_bytes() == before and [d.id for d in cfg.devices] == ids
    assert dry.status_code == status, (label, dry.text)
    real = client.post("/api/devices", json=body)
    assert real.status_code == status, (label, real.text)
    if status == 200:
        # ADR-0031 §5: the dry run also answers the would-be entry
        assert {k: v for k, v in dry.json().items() if k != 'entry'} == {"status": "valid", "would": "create"}
        assert dry.json()['entry']['id'] == body['id'] and dry.json()['entry']['write_locked'] is True
        assert real.json()['status'] == 'created'
    else:
        assert dry.json() == real.json(), label
        assert path.read_bytes() == before


PUTS = [
    ("a class lock", 'inv1', {'connection': {'protocol': 'http', 'url': 'http://192.0.2.80/x'}}, 422),
    ("a bus another device listens to", 'inv1', {'connection': {
        'protocol': 'rtu', 'serial_port': '/dev/ttyS9', 'baudrate': 19200, 'unit_id': 7}}, 422),
    ("an installation's unit", 'plant-u1', {'connection': {'protocol': 'tcp', 'host': '192.0.2.30'}}, 422),
    ("unchanged", 'inv1', {}, 200),
    ("a change", 'inv1', {'name': 'Renamed'}, 200),
]


@needs_tc
@pytest.mark.parametrize("label,did,change,status", PUTS, ids=[p[0] for p in PUTS])
def test_a_dry_run_edit_answers_what_the_save_answers_and_stores_nothing(tmp_path, label, did, change, status):
    cfg, client = _no_primary(tmp_path)
    stored = cfg.get_raw_device(did) or {'id': did, 'name': did, 'template': 'eastron_sdm120', 'enabled': False}
    body = {k: v for k, v in stored.items() if k in ('id', 'name', 'template', 'enabled', 'connection')}
    body.update(change)
    path = pathlib.Path(cfg.config_path)
    before = path.read_bytes()
    dry = client.put(f"/api/devices/{did}?dry_run=true", json=body)
    assert path.read_bytes() == before
    assert dry.status_code == status, (label, dry.text)
    real = client.put(f"/api/devices/{did}", json=body)
    assert real.status_code == status, (label, real.text)
    if status == 200:
        assert dry.json()['status'] == 'valid'
        assert dry.json()['would'] == ('unchanged' if real.json()['status'] == 'unchanged' else dry.json()['would'])
    else:
        assert dry.json() == real.json(), label
        assert path.read_bytes() == before
