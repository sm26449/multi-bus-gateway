# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The edit locks, held by the server in one place (ADR-0026 §2, FINDINGS
ADD-5): the class lock the connection form applied alone, the primary that
stays Modbus TCP, a device holding components that stays MQTT, meter-only
fixed after creation, a template's family. Every refusal names its field."""
import json

from tests.test_devices_api import make_app, needs_tc

DEVICES = """
devices:
  - id: bare
    name: Modbus TCP, no template
    template: ''
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.80, port: 502, unit_id: 1}
  - id: meter
    name: Modbus TCP with a Modbus map
    template: eastron_sdm630
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.81, port: 502, unit_id: 1}
  - id: pack
    name: Seplos V2 here
    template: seplos_bms_v2
    enabled: false
    connection: {protocol: seplos_v2, serial_port: /dev/ttyBMS0, unit_id: 1, mode: poll}
"""


def _spec(conn):
    from multibus.connection_spec import spec_of
    return spec_of(conn, [])


# ── the function ─────────────────────────────────────────────────────────
def test_edit_errors_holds_each_rule():
    from multibus.connection_spec import METER_ONLY_TEXT, PRIMARY_TEXT, edit_errors
    tcp, rtu = _spec({'protocol': 'tcp'}), _spec({'protocol': 'rtu'})
    http, mqtt = _spec({'protocol': 'http'}), _spec({'protocol': 'mqtt'})
    none = _spec({'protocol': 'none'})
    assert edit_errors(tcp, rtu) == []                                    # within the class
    assert edit_errors(None, http) == []                                  # a create has no class to keep
    assert [f for f, _e in edit_errors(tcp, http)] == ['connection.protocol']
    assert edit_errors(none, tcp) == [('connection.protocol', METER_ONLY_TEXT)]
    assert edit_errors(tcp, none) == [('connection.protocol', METER_ONLY_TEXT)]
    assert edit_errors(None, rtu, {'primary': True}) == [('connection.protocol', PRIMARY_TEXT)]
    assert edit_errors(None, tcp, {'primary': True}) == []
    assert edit_errors(None, _spec({'protocol': 'udp'}), {'primary': True})
    held = edit_errors(mqtt, tcp, {'id': 'gx', 'components': 2})
    assert len(held) == 1 and held[0][0] == 'connection.protocol' and 'component' in held[0][1]
    held = edit_errors(mqtt, mqtt, {'id': 'gx', 'components': 2, 'template_changed': True})
    assert held and held[0][0] == 'template'
    assert edit_errors(mqtt, mqtt, {'id': 'gx', 'components': 2}) == []
    fam = edit_errors(None, http, {'template': 'eastron_sdm630', 'template_family': 'modbus',
                                   'protocol': 'http'})
    assert fam == [('template', "template: 'eastron_sdm630' is a MODBUS map but this device is HTTP — "
                                "pick a HTTP template")]


# ── the API ──────────────────────────────────────────────────────────────
@needs_tc
def test_a_modbus_device_without_a_template_stays_modbus(tmp_path):
    # before ADR-0026 only the form held this: the API moved a template-less
    # Modbus device to HTTP
    cfg, client = make_app(tmp_path, extra_yaml=DEVICES)
    body = {'id': 'bare', 'name': 'x', 'template': '', 'enabled': False,
            'connection': {'protocol': 'http', 'url': 'http://192.0.2.80/x'}}
    for q in ('?dry_run=true', ''):
        r = client.put(f"/api/devices/bare{q}", json=body)
        assert r.status_code == 422, r.text
        assert 'connection.protocol' in r.json()['detail']['fields']
    assert cfg.get_raw_device('bare')['connection']['protocol'] == 'tcp'


@needs_tc
def test_a_modbus_map_on_http_is_refused_for_its_class_and_its_template(tmp_path):
    # already refused before (the template's family); now also by its class
    _cfg, client = make_app(tmp_path, extra_yaml=DEVICES)
    r = client.put("/api/devices/meter", json={'id': 'meter', 'template': 'eastron_sdm630', 'enabled': False,
                                                 'connection': {'protocol': 'http', 'url': 'http://192.0.2.81/x'}})
    assert r.status_code == 422, r.text
    fields = r.json()['detail']['fields']
    assert {'connection.protocol', 'template'} <= set(fields)
    assert any('is a MODBUS map but this device is HTTP' in e for e in r.json()['detail']['errors'])


@needs_tc
def test_moves_within_the_class_stay_allowed(tmp_path):
    _cfg, client = make_app(tmp_path, extra_yaml=DEVICES)
    r = client.put("/api/devices/meter?dry_run=true", json={
        'id': 'meter', 'template': 'eastron_sdm630', 'enabled': False,
        'connection': {'protocol': 'rtu', 'serial_port': '/dev/ttyUSB7', 'unit_id': 1}})
    assert r.status_code == 200, r.text
    r = client.put("/api/devices/pack?dry_run=true", json={
        'id': 'pack', 'template': 'seplos_bms_v2', 'enabled': False,
        'connection': {'protocol': 'seplos_v2-tcp', 'host': '192.0.2.95', 'port': 4198, 'unit_id': 1,
                       'mode': 'poll'}})
    assert r.status_code == 200, r.text
    r = client.put("/api/devices/pack?dry_run=true", json={
        'id': 'pack', 'template': 'seplos_bms_v2', 'enabled': False,
        'connection': {'protocol': 'tcp', 'host': '192.0.2.95', 'port': 502, 'unit_id': 1}})
    assert r.status_code == 422 and 'connection.protocol' in r.json()['detail']['fields'], r.text


@needs_tc
def test_the_primary_stays_modbus_tcp_with_a_field_error(tmp_path):
    _cfg, client = make_app(tmp_path)
    r = client.put("/api/devices/umg512", json={'connection': {'protocol': 'rtu', 'serial_port': '/dev/x'}})
    assert r.status_code == 422
    d = r.json()['detail']
    assert d['errors'] == ["the primary device (UMG512) is Modbus TCP"]        # the words as before
    assert d['fields'] == {'connection.protocol': "the primary device (UMG512) is Modbus TCP"}


@needs_tc
def test_meter_only_stays_meter_only_with_a_field_error(tmp_path):
    _cfg, client = make_app(tmp_path, extra_yaml=DEVICES)
    r = client.put("/api/devices/bare", json={'id': 'bare', 'connection': {'protocol': 'none'}})
    assert r.status_code == 422
    assert r.json()['detail']['fields'] == {'connection.protocol': 'fixed after creation'}


@needs_tc
def test_a_device_holding_components_stays_mqtt_with_a_field_error(tmp_path):
    from tests.test_device_components import GX_YAML, PORTAL
    _cfg, client = make_app(tmp_path, extra_yaml=GX_YAML)
    body = {"id": "gx", "name": "GX", "template": "", "enabled": False,
            "connection": {"protocol": "tcp", "host": "192.0.2.10"}}
    for q in ('?dry_run=true', ''):
        r = client.put(f"/api/devices/gx{q}", json=body)
        assert r.status_code == 422 and 'component' in r.text, r.text
        assert 'connection.protocol' in r.json()['detail']['fields']
    body = {"id": "gx", "name": "GX", "template": "mqtt_json_generic", "enabled": False,
            "connection": {"protocol": "mqtt", "broker": "192.0.2.10", "port": 8883, "topic": f"N/{PORTAL}/#",
                           "vars": {"portal": PORTAL, "vebus": "274"}}}
    r = client.put("/api/devices/gx", json=body)
    assert r.status_code == 422 and 'template' in r.json()['detail']['fields'], r.text
    assert 'component' in json.dumps(r.json())
