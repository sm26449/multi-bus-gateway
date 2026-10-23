# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The connection form (ui/js/connform-model.js), as the server receives it.

The chain, with no browser:
1. this file serves every way a device is reached (fixtures/connform_shapes.yaml
   plus the primary meter) and keeps the GET /api/devices rows the form opens
   from in fixtures/connform_rows.json — a changed row fails here first;
2. tools/e2e/connform_model_test.mjs runs the form's own model over those rows
   and keeps what it would send in fixtures/connform_payloads.json — the
   whole PUT body (the page's own fields beside the connection, built by the
   model's ``pagePayload``) for an untouched form, and one edit per way of
   reaching a device;
3. this file sends those bodies as they are: an untouched form changes nothing on the
   FIRST save (nothing written, the running client kept), and an edit
   changes that field and nothing else.

Regenerate after a deliberate change: ``CONNFORM_WRITE=1 pytest tests/test_connform_payloads.py``
then ``node tools/e2e/connform_model_test.mjs --write``.
"""
import copy
import json
import os
import pathlib

import yaml

from tests.test_devices_api import make_app, needs_tc

FIX = pathlib.Path(__file__).parent / "fixtures"
ROWS = FIX / "connform_rows.json"
PAYLOADS = FIX / "connform_payloads.json"
SHAPES = yaml.safe_load((FIX / "connform_shapes.yaml").read_text())
IDS = ['umg512'] + [d['id'] for d in SHAPES['devices']]
# what the page reads from a row (connform-model.js `model` and `pageFields`)
ROW_KEYS = ('id', 'primary', 'protocol', 'bridge', 'bridge_port', 'connection', 'serial', 'http',
            'host', 'port', 'unit_id', 'mqtt_in_topic',
            'name', 'template', 'enabled', 'mqtt_topic_prefix', 'influxdb_bucket', 'influxdb_device_tag',
            'ha_discovery_enabled', 'mqtt_enabled', 'influxdb_enabled')


def _app(tmp_path):
    return make_app(tmp_path, extra_yaml=(FIX / "connform_shapes.yaml").read_text())


def _rows(client):
    r = client.get("/api/devices")
    assert r.status_code == 200, r.text
    by = {d['id']: d for d in r.json()['devices']}
    return by, {i: {k: by[i][k] for k in ROW_KEYS if k in by[i]} for i in IDS}


@needs_tc
def test_the_rows_the_form_opens_from_are_the_fixture(tmp_path):
    _cfg, client = _app(tmp_path)
    _by, rows = _rows(client)
    if os.environ.get('CONNFORM_WRITE'):
        ROWS.write_text(json.dumps(rows, indent=1, sort_keys=True) + "\n")
    assert json.loads(ROWS.read_text()) == rows, "GET rows changed: regenerate (see the module doc)"


@needs_tc
def test_an_untouched_form_changes_nothing_on_the_first_save(tmp_path):
    cfg, client = _app(tmp_path)
    path = pathlib.Path(cfg.config_path)
    fixtures = json.loads(PAYLOADS.read_text())
    assert set(fixtures['untouched']) == set(IDS) == set(fixtures['bodies'])
    for did, conn in fixtures['untouched'].items():
        assert conn == {}, (did, conn)                    # the form itself sends nothing
        before = path.read_bytes()
        body = fixtures['bodies'][did]                    # the model's whole PUT, as sent
        assert body['connection'] == conn and body['id'] == did
        dry = client.put(f"/api/devices/{did}?dry_run=true", json=body)
        assert dry.status_code == 200 and dry.json()['would'] == 'unchanged', (did, dry.text)
        r = client.put(f"/api/devices/{did}", json=body)
        assert r.status_code == 200 and r.json()['status'] == 'unchanged', (did, r.text)
        assert path.read_bytes() == before, did


@needs_tc
def test_one_edited_field_changes_that_field_and_nothing_else(tmp_path):
    cfg, client = _app(tmp_path)
    fixtures = json.loads(PAYLOADS.read_text())
    seen = set()
    for case in fixtures['edits']:
        did, conn = case['id'], case['payload']
        assert case['body']['connection'] == conn
        before = copy.deepcopy(cfg.get_raw_device(did) or {}).get('connection', {}) if did != 'umg512' else None
        r = client.put(f"/api/devices/{did}", json=case['body'])
        assert r.status_code == 200, (case['label'], r.text)
        assert r.json()['status'] != 'unchanged', (case['label'], r.text)
        seen.add(did)
        if did == 'umg512':
            for k, v in conn.items():
                if k != 'protocol':
                    assert getattr(cfg.modbus, k) == v, (case['label'], k)
            continue
        after = cfg.get_raw_device(did)['connection']
        if 'stored_has' in case:                          # a change of how it is reached
            for k, v in case['stored_has'].items():
                assert after.get(k) == v, (case['label'], k, after)
            for k in case['stored_lacks']:
                assert k not in after, (case['label'], k, after)
            continue
        want = dict(before)
        for k, v in conn.items():
            if k in ('protocol', 'password'):
                continue
            if v is None:
                want.pop(k, None)                         # null clears
            else:
                want[k] = v
        assert after == want, (case['label'], before, after)
    assert {'umg512', 'gx', 'on-bridge', 'tap-local', 'http', 'via-gw'} <= seen


@needs_tc
def test_a_serial_mode_never_carries_a_host_nor_a_bridge_a_line(tmp_path):
    # the 2026-10-20 outage (FINDINGS BANK-1020) was a mode → payload slip:
    # every connection the form can build for a serial or bridged mode is
    # checked here, as sent
    fixtures = json.loads(PAYLOADS.read_text())
    for did, conn in fixtures['wire'].items():
        proto = conn.get('protocol')
        if proto in ('rtu', 'ascii') or (proto == 'rtu_tap' and not conn.get('bridge')):
            assert 'host' not in conn and conn.get('serial_port', '').startswith('/dev/'), (did, conn)
        if conn.get('bridge'):
            assert not {'host', 'port', 'baudrate', 'parity', 'stopbits', 'bytesize', 'serial_port'} & set(conn), (did, conn)


class _Running:
    def get_stats(self):
        return {"connected": True}

    def data_health(self):
        return {"status": "ok"}

    def disconnect(self):
        raise AssertionError("an untouched save disconnected a running client")


@needs_tc
def test_an_untouched_form_keeps_a_running_client(tmp_path):
    from fastapi.testclient import TestClient

    from multibus.api import create_api
    from tests.test_devices import write_config
    data = copy.deepcopy(SHAPES)
    for d in data['devices']:
        if d['id'] in ('gx', 'on-bridge'):
            d['enabled'] = True
    cfg = write_config(tmp_path, extra_yaml=yaml.safe_dump(data, sort_keys=False))
    clients = {'gx': _Running(), 'on-bridge': _Running()}
    app, _ = create_api(cfg, None, None, None, devices=[(d, clients.get(d.id)) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    fixtures = json.loads(PAYLOADS.read_text())
    for did in ('gx', 'on-bridge'):
        # the model's body, with the one thing this test changed in the
        # fixture: these two are enabled (a running client)
        body = dict(fixtures['bodies'][did], enabled=True)
        r = client.put(f"/api/devices/{did}", json=body)
        assert r.status_code == 200 and r.json()['status'] == 'unchanged', (did, r.text)
        _i, _d, c = app.state.registry.find(did)
        assert c is clients[did], did


@needs_tc
def test_a_timeout_set_is_1_to_30_s_and_a_stored_one_stands(tmp_path):
    # the server says what the form says (review 8b MINOR-6); a sub-second
    # timeout written by hand before is not judged again on another edit
    from fastapi.testclient import TestClient

    from multibus.api import create_api
    from tests.test_devices import write_config
    data = copy.deepcopy(SHAPES)
    next(d for d in data['devices'] if d['id'] == 'tcp')['connection']['timeout'] = 0.5
    cfg = write_config(tmp_path, extra_yaml=yaml.safe_dump(data, sort_keys=False))
    app, _ = create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    fixtures = json.loads(PAYLOADS.read_text())
    body = copy.deepcopy(fixtures['bodies']['tcp'])
    body['name'] = 'renamed'
    r = client.put("/api/devices/tcp", json=body)
    assert r.status_code == 200 and r.json()['status'] != 'unchanged', r.text
    assert cfg.get_raw_device('tcp')['connection']['timeout'] == 0.5
    for bad in (0.4, 0, 31, 'x'):      # 0.5 is the stored one: it stands
        body['connection'] = {'timeout': bad, 'protocol': 'tcp'}
        r = client.put("/api/devices/tcp", json=body)
        assert r.status_code == 422 and 'connection.timeout' in r.text, (bad, r.text)
    body['connection'] = {'timeout': 30, 'protocol': 'tcp'}
    assert client.put("/api/devices/tcp", json=body).status_code == 200
    prim = copy.deepcopy(fixtures['bodies']['umg512'])
    prim['connection'] = {'protocol': 'tcp', 'host': '192.168.1.207', 'port': 502, 'unit_id': 1, 'timeout': 45}
    r = client.put("/api/devices/umg512", json=prim)
    assert r.status_code == 422 and 'connection.timeout' in r.text, r.text
