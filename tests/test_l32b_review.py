# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""L3.2b independent review (M1–M2, m4–m7, NIT8–9, the EMS round trip):

- a bridge's own values (max_connections 12) are not judged as the device's;
- a PATCH whose poll intervals cannot be written stores nothing half-way;
- PATCH on a stalled device with its connection reconnects it, as PUT does;
- a dry run never schedules a snapshot;
- only=connection says the bus claim even when another field fails;
- finding components before saving: admin only, two at a time, one answer
  shape whatever the broker did;
- the diff masks secrets with the one mask and redacts URL credentials;
- a unit number in several groups must be named by its id;
- an EMS GET → PUT of victron-gx (dry run) is accepted unchanged."""
import pathlib
import time

import pytest

from tests.test_device_components import PORTAL
from tests.test_devices_api import make_app, needs_tc

SETUP = f"""
primary_device: false
bridges:
  - {{id: gw, name: Gateway, type: modbus_gateway, host: 192.0.2.70, max_connections: 12, ports: [{{port: 502}}]}}
devices:
  - id: tap
    name: Listens on ttyS9
    template: seplos_bms_v3_rtu_tap
    enabled: true
    connection: {{protocol: rtu_tap, serial_port: /dev/ttyS9, baudrate: 19200, unit_id: 2}}
  - id: inv1
    name: Inverter
    template: eastron_sdm630
    enabled: false
    connection: {{protocol: tcp, host: 192.0.2.80, unit_id: 1}}
  - id: live
    name: Enabled, no client
    template: eastron_sdm630
    enabled: true
    connection: {{protocol: tcp, host: 192.0.2.82, unit_id: 1}}
  - id: victron-gx
    name: Victron GX
    template: victron_gx_mqtt
    enabled: false
    connection: {{protocol: mqtt, broker: 192.0.2.10, port: 8883, tls: true, tls_insecure: true, username: u,
                  password: secret, topic: 'N/{PORTAL}/#', vars: {{portal: {PORTAL}, vebus: '274'}}}}
    mqtt: {{topic_prefix: mbg/devices/victron-gx}}
    components:
      - {{type: battery, instance: 288, name: SmartShunt}}
endpoints:
  - id: plant
    name: Plant
    template: eastron_sdm120
    enabled: false
    connection: {{protocol: tcp, host: 192.0.2.20, port: 502}}
    groups:
      - {{id: meters, units: [1, {{unit_id: 3, id: plant-m3}}]}}
      - {{id: more, units: [1]}}
"""


def _app(tmp_path):
    return make_app(tmp_path, extra_yaml=SETUP)


def _tag(client, did):
    return client.get(f"/api/devices/{did}").headers['etag']


# ── M1: a bridge's values are the bridge's ──────────────────────────────────

@needs_tc
def test_a_device_on_a_bridge_of_12_connections_is_saved_every_way(tmp_path):
    cfg, client = _app(tmp_path)
    body = {'id': 'br1', 'name': 'b', 'template': 'eastron_sdm630', 'enabled': False,
            'connection': {'bridge': 'gw', 'bridge_port': 502, 'unit_id': 4}}
    res = client.post("/api/devices", json=body)
    assert res.status_code == 200, res.text
    assert cfg.get_device('br1').connection.max_connections == 12
    res = client.put("/api/devices/br1", json={**body, 'name': 'b2'})
    assert res.status_code == 200, res.text
    res = client.patch("/api/devices/br1", json={'sections': {'connection': {'unit_id': 5}}},
                       headers={'If-Match': _tag(client, 'br1')})
    assert res.status_code == 200, res.text
    # the device's own value is still judged
    bad = client.post("/api/devices", json={**body, 'id': 'br2',
                                            'connection': {**body['connection'], 'max_connections': 12}})
    assert bad.status_code == 422 and 'connection.max_connections' in bad.json()['detail']['fields']


# ── M2: nothing half-way ────────────────────────────────────────────────────

@needs_tc
@pytest.mark.parametrize("how", ["corrupt", "oserror"])
def test_poll_intervals_that_cannot_be_written_store_nothing(tmp_path, monkeypatch, how):
    cfg, client = _app(tmp_path)
    if how == "corrupt":
        p = pathlib.Path(cfg.device_registers_path('inv1'))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ not json", encoding="utf-8")
    else:
        def boom(*a, **k):
            raise OSError(28, "No space left on device")
        monkeypatch.setattr(cfg, "save_device_poll_groups", boom)
    path = pathlib.Path(cfg.config_path)
    before = path.read_bytes()
    res = client.patch("/api/devices/inv1", json={'sections': {'identity': {'name': 'Renamed'},
                                                               'polling': {'poll_groups': {'normal': 7}}}},
                       headers={'If-Match': _tag(client, 'inv1')})
    assert res.status_code in (422, 500, 503), res.text
    assert 'polling.poll_groups' in res.text
    assert path.read_bytes() == before                     # the entry was not written either
    assert cfg.get_device('inv1').name == 'Inverter'


# ── m4: a stalled device reconnects ─────────────────────────────────────────

@needs_tc
def test_patch_with_the_connection_reconnects_a_stalled_device(tmp_path):
    cfg, client = _app(tmp_path)
    path = pathlib.Path(cfg.config_path)
    before = path.read_bytes()
    put = client.put("/api/devices/live?dry_run=true", json={'id': 'live', 'connection': {'host': '192.0.2.82'}})
    assert put.json()['would'] == 'update_restart', put.text
    dry = client.patch("/api/devices/live?dry_run=true", json={'sections': {'connection': {'host': '192.0.2.82'}}})
    assert dry.json()['would'] == 'update_restart' and dry.json()['restart_required'] is True, dry.text
    res = client.patch("/api/devices/live", json={'sections': {'connection': {'host': '192.0.2.82'}}},
                       headers={'If-Match': _tag(client, 'live')})
    assert res.status_code == 200 and res.json()['status'] == 'updated' and res.json()['restarted'] is True, res.text
    assert path.read_bytes() == before                     # nothing to write: only a reconnect


# ── m5: no snapshot for a dry run ───────────────────────────────────────────

@needs_tc
def test_a_dry_run_never_schedules_a_snapshot(tmp_path, monkeypatch):
    _cfg, client = _app(tmp_path)
    got = []
    store = client.app.state.snapshot_store
    monkeypatch.setattr(store, "schedule", lambda *a, **k: got.append(a))
    conn = {'protocol': 'tcp', 'host': '192.0.2.90', 'unit_id': 1}
    assert client.post("/api/devices?dry_run=true", json={'id': 'n1', 'connection': conn}).status_code == 200
    assert client.post("/api/devices?dry_run=true&only=connection", json={'connection': conn}).status_code == 200
    assert client.put("/api/devices/inv1?dry_run=true", json={'id': 'inv1', 'name': 'x'}).status_code == 200
    assert client.patch("/api/devices/inv1?dry_run=true",
                        json={'sections': {'identity': {'name': 'x'}}}).status_code == 200
    assert got == []
    assert client.put("/api/devices/inv1", json={'id': 'inv1', 'name': 'x'}).status_code == 200
    assert len(got) == 1


# ── m6: the bus claim, whatever else fails ──────────────────────────────────

@needs_tc
def test_only_connection_says_the_claim_beside_another_refusal(tmp_path):
    _cfg, client = _app(tmp_path)
    res = client.post("/api/devices?dry_run=true&only=connection", json={'connection': {
        'protocol': 'rtu', 'serial_port': '/dev/ttyS9', 'baudrate': 19200, 'unit_id': 7, 'max_gap': 500}})
    assert res.status_code == 422, res.text
    errs = res.json()['detail']['errors']
    assert any('max_gap' in e for e in errs) and any('tap' in e for e in errs), errs
    assert res.json()['detail']['bus']['listeners'] == ['tap']


@needs_tc
def test_only_connection_says_unknown_when_the_bus_cannot_be_told(tmp_path):
    _cfg, client = _app(tmp_path)
    res = client.post("/api/devices?dry_run=true&only=connection", json={'connection': {
        'protocol': 'rtu', 'serial_port': '', 'unit_id': 7}})
    assert res.status_code == 422, res.text
    assert res.json()['detail']['bus'] == 'unknown'


# ── m7: finding components before saving ────────────────────────────────────

GX = {'template': 'victron_gx_mqtt', 'connection': {'protocol': 'mqtt', 'broker': '192.0.2.10', 'port': 1883,
                                                     'topic': f'N/{PORTAL}/#', 'vars': {'portal': PORTAL}}}


@needs_tc
def test_finding_components_before_saving_is_for_the_admin(tmp_path):
    from tests.test_operator_role import _build_clients
    c = _build_clients(tmp_path)
    for who in (c.op, c.viewer):
        assert who.post("/api/devices/components/discover", json=GX).status_code == 403
    assert c.admin.post("/api/devices/components/discover", json={'template': 'nope'}).status_code == 422


@needs_tc
def test_two_finds_at_once_at_most_the_third_is_told_to_wait(tmp_path, monkeypatch):
    from multibus import component_discover as cd
    _cfg, client = _app(tmp_path)
    opened = []
    monkeypatch.setattr(cd, "open_link", lambda conn: opened.append(conn))
    taken = [cd.SLOTS.acquire(blocking=False) for _ in range(cd.MAX_AT_ONCE)]
    try:
        assert all(taken)
        res = client.post("/api/devices/components/discover", json=GX)
        assert res.status_code == 429 and res.headers.get('retry-after'), res.text
        assert opened == []
    finally:
        for _ in taken:
            cd.SLOTS.release()
    res = client.post("/api/devices/components/discover", json=GX)
    assert res.status_code == 409                      # open_link said None: not reached


@needs_tc
def test_one_answer_whatever_the_broker_did(tmp_path, monkeypatch):
    from multibus import component_discover as cd
    from multibus import mqtt_link
    _cfg, client = _app(tmp_path)
    monkeypatch.setattr(cd, "CONNECT_S", 0.4)
    answers = []
    for start in (lambda self: False,                       # refused at once (TLS, a bad host)
                  lambda self: True):                       # never connected (closed port, timeout)
        monkeypatch.setattr(mqtt_link.MqttLink, "start", start)
        monkeypatch.setattr(mqtt_link.MqttLink, "_close", lambda self: None)
        t0 = time.monotonic()
        res = client.post("/api/devices/components/discover", json=GX)
        answers.append((res.status_code, res.json(), time.monotonic() - t0 >= 0.4))
    assert answers[0] == answers[1] and answers[0][0] == 409 and answers[0][2], answers


# ── NIT8: one mask, URLs redacted ───────────────────────────────────────────

def test_the_diff_masks_with_the_one_mask_and_redacts_url_credentials():
    from multibus.device_patch import changes
    from multibus.save_merge import MASKED
    before = {'connection': {'url': 'http://192.0.2.5/x', 'password': 'a'},
              'rest_push': {'url': 'https://u:pw@example.org/in?token=abc', 'headers': {'Authorization': 'x'}}}
    after = {'connection': {'url': 'http://admin:s3cret@192.0.2.5/x', 'password': 'b'},
             'rest_push': {'url': 'https://u:pw2@example.org/in?token=def', 'headers': {'Authorization': 'y'}}}
    out = changes(before, after)
    text = repr(out)
    for secret in ('s3cret', 'pw2', 'def', "'a'", "'b'"):
        assert secret not in text, (secret, out)
    pw = next(c for c in out if c['field'] == 'password')
    assert pw['from'] == pw['to'] == MASKED
    assert next(c for c in out if c['field'] == 'rest_push.headers.Authorization')['to'] == MASKED


# ── NIT9: a unit number in several groups ───────────────────────────────────

@needs_tc
def test_a_unit_number_in_several_groups_must_be_named_by_its_id(tmp_path):
    cfg, client = _app(tmp_path)
    path = pathlib.Path(cfg.config_path)
    before = path.read_bytes()
    res = client.patch("/api/endpoints/plant/units/1", json={'name': 'X'})
    assert res.status_code == 422, res.text
    msg = res.text
    assert 'meters' in msg and 'more' in msg and 'by its id' in msg
    assert path.read_bytes() == before


# ── the EMS round trip ──────────────────────────────────────────────────────

@needs_tc
def test_an_ems_get_put_of_victron_gx_is_accepted_unchanged(tmp_path):
    """An EMS reads the device and PUTs back what it read, as docs/API.md
    shows: the connection block of GET (password masked, every advanced key
    spelled out, nulls included) — a dry run must accept it as unchanged."""
    _cfg, client = _app(tmp_path)
    got = client.get("/api/devices/victron-gx").json()['device']
    body = {k: got[k] for k in ('id', 'name', 'template', 'enabled', 'connection')}
    body['ha_discovery_enabled'] = got['ha_discovery_enabled']
    res = client.put("/api/devices/victron-gx?dry_run=true", json=body,
                     headers={'If-Match': _tag(client, 'victron-gx')})
    assert res.status_code == 200, res.text
    assert res.json() == {'status': 'valid', 'would': 'unchanged'}, res.text
