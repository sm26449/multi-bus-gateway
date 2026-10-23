# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The FIRST save of an untouched device changes nothing (FINDINGS PROD-1..3).

The device page spells out defaults the stored entry may not have (timeout 3,
username '', tls false…). Echoing a value the device already runs on is not a
change: the save answers `unchanged` and writes nothing — before, an MQTT
input reconnected (`mqtt_in` copies only the keys present) and the primary
meter always reconnected. A serial device path is never a network host."""
import json
import pathlib

from tests.test_devices_api import make_app, needs_tc

FIX = pathlib.Path(__file__).parent / "fixtures"


def _prod(tmp_path):
    return make_app(tmp_path, extra_yaml=(FIX / "kind_prod_shaped.yaml").read_text())


def _ui_payload(dev):
    """What the device page (app-devices.js `_ddvPayload` over the model it
    builds from GET /api/devices/{id}) sends for an untouched form."""
    c = dev.get('connection') or {}
    serial = dev.get('serial') or {}
    proto = dev.get('protocol') or 'tcp'
    unit = c.get('unit_id', dev.get('unit_id', 1))
    timeout = c.get('timeout', 3)
    if proto == 'http':
        conn = {'protocol': 'http', 'url': c.get('url') or (dev.get('http') or {}).get('url') or ''}
    elif proto == 'mqtt':
        conn = {'protocol': 'mqtt', 'broker': c.get('broker') or '', 'port': c.get('port') or 1883,
                'topic': c.get('topic') or dev.get('mqtt_in_topic') or '',
                'username': c.get('username') or '', 'password': '', 'tls': bool(c.get('tls')),
                'tls_insecure': bool(c.get('tls_insecure')), 'vars': c.get('vars') or {},
                'keepalive': c.get('keepalive')}
    elif dev.get('bridge'):
        conn = {'bridge': dev['bridge'], 'unit_id': unit, 'timeout': timeout}
        if dev.get('bridge_port') is not None:
            conn['bridge_port'] = dev['bridge_port']
        if proto == 'rtu_tap':
            conn['protocol'] = 'rtu_tap'
    elif proto in ('tcp', 'udp', 'rtu-tcp', 'ascii-tcp'):
        conn = {'protocol': proto, 'host': c.get('host') or dev.get('host') or '',
                'port': c.get('port') or dev.get('port') or 502, 'unit_id': unit, 'timeout': timeout}
    else:
        conn = {'protocol': proto, 'serial_port': c.get('serial_port') or serial.get('serial_port') or '',
                'baudrate': c.get('baudrate') or serial.get('baudrate') or 9600,
                'parity': c.get('parity') or serial.get('parity') or 'N',
                'stopbits': c.get('stopbits') or serial.get('stopbits') or 1,
                'bytesize': c.get('bytesize') or serial.get('bytesize') or 8,
                'unit_id': unit, 'timeout': timeout}
    mqtt = {'enabled': dev.get('mqtt_enabled') is not False}
    if dev.get('mqtt_topic_prefix'):
        mqtt['topic_prefix'] = dev['mqtt_topic_prefix']
    influx = {'enabled': dev.get('influxdb_enabled') is not False}
    if dev.get('influxdb_bucket'):
        influx['bucket'] = dev['influxdb_bucket']
    influx['device_tag'] = dev.get('influxdb_device_tag') or dev['id']
    return {'id': dev['id'], 'name': dev.get('name') or dev['id'], 'template': dev.get('template') or '',
            'enabled': dev.get('enabled') is not False, 'connection': conn, 'mqtt': mqtt,
            'influxdb': influx, 'ha_discovery_enabled': dev.get('ha_discovery_enabled') is not False}


def _row(client, did):
    r = client.get("/api/devices")
    assert r.status_code == 200, r.text
    return next(d for d in r.json()['devices'] if d['id'] == did)


@needs_tc
def test_the_first_save_of_every_untouched_prod_device_changes_nothing(tmp_path):
    cfg, client = _prod(tmp_path)
    path = pathlib.Path(cfg.config_path)
    for did in ('victron-gx', 'samsung-dvm', 'vfd', 'generic', 'plug'):
        before = path.read_bytes()
        body = _ui_payload(_row(client, did))
        dry = client.put(f"/api/devices/{did}?dry_run=true", json=body)
        assert dry.status_code == 200 and dry.json()['would'] == 'unchanged', (did, dry.text)
        r = client.put(f"/api/devices/{did}", json=body)
        assert r.status_code == 200 and r.json()['status'] == 'unchanged', (did, r.text)
        assert path.read_bytes() == before, did                # nothing written


@needs_tc
def test_an_mqtt_input_is_not_reconnected_for_echoed_empty_credentials(tmp_path):
    # victron-gx stores no username / tls: '' and false are what it runs on
    cfg, client = _prod(tmp_path)
    body = _ui_payload(_row(client, 'victron-gx'))
    assert body['connection']['username'] == '' and body['connection']['tls'] is False
    r = client.put("/api/devices/victron-gx?dry_run=true", json=body)
    assert r.json()['would'] == 'unchanged', r.text
    # a real change still counts, and restarts the input
    body['connection']['tls'] = True
    r = client.put("/api/devices/victron-gx?dry_run=true", json=body)
    assert r.json()['would'] == 'update_restart', r.text


@needs_tc
def test_a_changed_key_is_kept_while_an_echoed_default_is_not_written(tmp_path):
    cfg, client = _prod(tmp_path)
    body = _ui_payload(_row(client, 'samsung-dvm'))
    body['connection']['timeout'] = 7                     # a real change on a bridged device
    r = client.put("/api/devices/samsung-dvm", json=body)
    assert r.status_code == 200 and r.json()['status'] == 'updated', r.text
    stored = cfg.get_raw_device('samsung-dvm')['connection']
    assert stored.get('timeout') == 7
    assert 'protocol' not in stored and 'host' not in stored   # the bridge still names them


@needs_tc
def test_the_primary_saved_unchanged_is_not_reconnected(tmp_path, monkeypatch):
    cfg, client = make_app(tmp_path)
    m = cfg.modbus
    called = []
    monkeypatch.setattr(cfg, "update_modbus", lambda **kw: called.append(kw))
    body = {'connection': {'protocol': 'tcp', 'host': m.host, 'port': m.port,
                           'unit_id': m.unit_id, 'timeout': m.timeout}}
    dry = client.put("/api/devices/umg512?dry_run=true", json=body)
    assert dry.json() == {"status": "valid", "would": "unchanged"}, dry.text
    r = client.put("/api/devices/umg512", json=body)
    assert r.status_code == 200 and r.json()['status'] == 'unchanged', r.text
    assert called == []
    body['connection']['port'] = int(m.port) + 1
    assert client.put("/api/devices/umg512?dry_run=true", json=body).json()['would'] == 'update_restart'


@needs_tc
def test_a_serial_path_is_never_a_network_host(tmp_path):
    cfg, client = make_app(tmp_path)
    for host in ('/dev/ttySEPLOS', 'COM3', 'ttyUSB0'):
        r = client.post("/api/devices?dry_run=true", json={
            'id': 'bad-host', 'name': 'x', 'template': 'eastron_sdm630',
            'connection': {'protocol': 'tcp', 'host': host, 'port': 502, 'unit_id': 1}})
        assert r.status_code == 422, (host, r.text)
        assert 'serial port' in r.json()['detail']['fields']['connection.host'], r.text
    r = client.put("/api/devices/umg512", json={'connection': {'protocol': 'tcp', 'host': '/dev/ttyUSB0',
                                                              'port': 502, 'unit_id': 1}})
    assert r.status_code == 422 and 'serial port' in json.dumps(r.json()), r.text
    # a host name that merely starts like one is a host
    r = client.post("/api/devices?dry_run=true", json={
        'id': 'ok-host', 'name': 'x', 'template': 'eastron_sdm630',
        'connection': {'protocol': 'tcp', 'host': 'comrade.lan', 'port': 502, 'unit_id': 1}})
    assert r.status_code == 200, r.text


@needs_tc
def test_real_hostnames_are_not_mistaken_for_serial_ports(tmp_path):
    from multibus.api import _is_serial_path
    for serial in ('/dev/ttySEPLOS', '/dev/ttyUSB0', 'COM3', 'com12', r'\\.\COM10',
                   'ttyUSB0', 'ttyS1', 'ttyAMA0', 'ttyACM0'):
        assert _is_serial_path(serial), serial
    for host in ('tty-gateway.local', 'ttyrouter', 'com1.example.com', 'comrade.lan', '192.0.2.5',
                 '::1', 'fe80::1%eth0', 'gw.local', 'tty', 'COM', ''):
        assert not _is_serial_path(host), host
    cfg, client = make_app(tmp_path)
    for host in ('tty-gateway.local', 'ttyrouter'):
        r = client.post("/api/devices?dry_run=true", json={
            'id': 'ok-host', 'name': 'x', 'template': 'eastron_sdm630',
            'connection': {'protocol': 'tcp', 'host': host, 'port': 502, 'unit_id': 1}})
        assert r.status_code == 200, (host, r.text)


@needs_tc
def test_the_primary_keeps_what_a_save_leaves_out(tmp_path, monkeypatch):
    cfg, client = make_app(tmp_path)
    monkeypatch.setattr(cfg.modbus, "timeout", 5)
    m = cfg.modbus
    # no timeout, no port, no unit: they keep what the meter runs on
    r = client.put("/api/devices/umg512?dry_run=true", json={'connection': {'protocol': 'tcp', 'host': m.host}})
    assert r.json() == {"status": "valid", "would": "unchanged"}, r.text
    r = client.put("/api/devices/umg512", json={'connection': {'protocol': 'tcp'}})
    assert r.status_code == 200 and r.json()['status'] == 'unchanged', r.text
    assert cfg.modbus.timeout == 5


@needs_tc
def test_a_stored_value_sent_as_the_default_is_written(tmp_path):
    # the drop logic only skips keys the device does not have: a stored
    # timeout 5 sent as 3 is a real change
    cfg, client = _prod(tmp_path)
    body = _ui_payload(_row(client, 'vfd'))
    body['connection']['timeout'] = 5
    assert client.put("/api/devices/vfd", json=body).json()['status'] == 'updated'
    body['connection']['timeout'] = 3
    r = client.put("/api/devices/vfd", json=body)
    assert r.status_code == 200 and r.json()['status'] == 'updated', r.text
    assert cfg.get_raw_device('vfd')['connection']['timeout'] == 3


class _Running:
    """A running client, as far as the device list and a save ask it."""
    def get_stats(self):
        return {"connected": True}

    def data_health(self):
        return {"status": "ok"}

    def disconnect(self):
        raise AssertionError("an untouched save disconnected a running client")


def _app_with(tmp_path, enable: str, running: bool):
    from fastapi.testclient import TestClient

    from multibus.api import create_api
    from tests.test_devices import write_config
    import yaml
    data = yaml.safe_load((FIX / "kind_prod_shaped.yaml").read_text())
    next(d for d in data['devices'] if d['id'] == enable)['enabled'] = True
    cfg = write_config(tmp_path, extra_yaml=yaml.safe_dump(data, sort_keys=False))
    clients = {enable: _Running()} if running else {}
    app, _ = create_api(cfg, None, None, None, devices=[(d, clients.get(d.id)) for d in cfg.devices])
    return cfg, app, TestClient(app, raise_server_exceptions=False), clients


@needs_tc
def test_the_first_untouched_save_keeps_a_running_client(tmp_path):
    cfg, app, client, clients = _app_with(tmp_path, 'victron-gx', running=True)
    row = _row(client, 'victron-gx')
    assert row['enabled'] is True and row['running'] is True
    r = client.put("/api/devices/victron-gx", json=_ui_payload(row))
    assert r.status_code == 200 and r.json()['status'] == 'unchanged', r.text
    _i, _d, c = app.state.registry.find('victron-gx')
    assert c is clients['victron-gx']                        # the same client: not reconnected


@needs_tc
def test_an_enabled_device_that_is_not_running_is_revived_by_saving_it(tmp_path):
    cfg, app, client, _ = _app_with(tmp_path, 'vfd', running=False)
    row = _row(client, 'vfd')
    assert row['enabled'] is True and row['running'] is False   # what the page's Reconnect reads
    r = client.put("/api/devices/vfd?dry_run=true", json=_ui_payload(row))
    assert r.json()['would'] != 'unchanged', r.text          # saving it again starts it
