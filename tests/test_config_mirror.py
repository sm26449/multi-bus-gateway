# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""A standby takes its partner's configuration but keeps what is its own
machine's — its serial adapters, its address — and restarts to run it; the
same configuration twice does nothing."""
import base64
import io
import json
import zipfile

import yaml
from fastapi.testclient import TestClient

from multibus.config_mirror import DEFAULT_KEEP_LOCAL, bundle_hash, check_paths, keep_local

from tests.test_devices_api import needs_tc

THEIRS = {
    'ui': {'port': 8080, 'host': '0.0.0.0', 'timezone': 'Europe/Bucharest'},
    'devices': [{'id': 'fronius_rtu', 'connection': {'protocol': 'rtu', 'serial_port': '/dev/ttyA', 'baudrate': 9600}},
                {'id': 'meter', 'connection': {'protocol': 'tcp', 'host': '10.0.0.5'}}],
    'endpoints': [{'id': 'bms', 'groups': [{'id': 'packs', 'connection': {'serial_port': '/dev/ttyA2'}}]}],
}
MINE = {
    'ui': {'port': 9090, 'host': '0.0.0.0', 'timezone': 'UTC'},
    'devices': [{'id': 'fronius_rtu', 'connection': {'protocol': 'rtu', 'serial_port': '/dev/ttyB', 'baudrate': 4800}},
                {'id': 'gone', 'connection': {'serial_port': '/dev/ttyX'}}],
    'endpoints': [{'id': 'bms', 'groups': [{'id': 'packs', 'connection': {'serial_port': '/dev/ttyB2'}}]}],
}


def _copy(d):
    return json.loads(json.dumps(d))


def test_the_machines_own_values_are_kept_the_rest_is_the_partners():
    merged, kept = keep_local(_copy(THEIRS), _copy(MINE), list(DEFAULT_KEEP_LOCAL))
    assert merged['devices'][0]['connection'] == {'protocol': 'rtu', 'serial_port': '/dev/ttyB', 'baudrate': 9600}
    assert merged['ui'] == {'port': 9090, 'host': '0.0.0.0', 'timezone': 'Europe/Bucharest'}
    assert merged['endpoints'][0]['groups'][0]['connection']['serial_port'] == '/dev/ttyB2'
    assert [d['id'] for d in merged['devices']] == ['fronius_rtu', 'meter']          # theirs: no 'gone'
    assert 'devices.fronius_rtu.connection.serial_port' in kept and not any('gone' in k for k in kept)
    whole, _ = keep_local(_copy(THEIRS), _copy(MINE), ['devices.fronius_rtu.connection'])
    assert whole['devices'][0]['connection']['baudrate'] == 4800                     # a whole block, by id


def test_paths_are_checked():
    assert check_paths(['devices.*.connection.serial_port']) == []
    assert len(check_paths(['', 'a..b', 7])) == 3


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for n, t in files.items():
            z.writestr(n, t)
    return buf.getvalue()


def test_the_hash_is_the_configuration_not_the_zip():
    a = _zip({'config.yaml': 'x: 1\n', 'manifest.json': '{"t": 1}'})
    b = _zip({'manifest.json': '{"t": 2}', 'config.yaml': 'x: 1\n'})
    assert bundle_hash(a) == bundle_hash(b) != bundle_hash(_zip({'config.yaml': 'x: 2\n'}))


@needs_tc
def test_a_standby_takes_the_partners_configuration_keeps_its_own_and_restarts(tmp_path, monkeypatch):
    from multibus.routes import role_routes
    from tests.test_operator_role import _build_clients
    a_dir, b_dir = tmp_path / 'a', tmp_path / 'b'
    a_dir.mkdir()
    b_dir.mkdir()
    a, b = _build_clients(a_dir), _build_clients(b_dir)
    restarts = []
    monkeypatch.setattr(role_routes.threading, 'Timer', lambda s, fn: type('T', (), {'start': lambda self: restarts.append(s)})())
    tok = {}
    for name, c in (('a', a), ('b', b)):
        tok[name] = {"Authorization": f"Bearer {c.admin.post('/api/auth/service-tokens', json={'name': 'mbg-ha', 'role': 'ha'}).json()['token']}"}
    sa, sb = TestClient(a.app, raise_server_exceptions=False), TestClient(b.app, raise_server_exceptions=False)
    # the bundle carries secrets: not for a viewer
    assert a.viewer.get('/api/state/config').status_code == 403
    got = sa.get('/api/state/config', headers=tok['a']).json()
    assert len(got['hash']) == 64 and got['bundle']
    assert sa.get('/api/state/config?hash_only=true', headers=tok['a']).json().get('bundle') is None
    body = {'bundle': got['bundle'], 'hash': got['hash'], 'keep_local': ['ui.port'], 'from': 'A'}
    assert sb.post('/api/state/config', json=body, headers=tok['b']).status_code == 409    # B is active yet
    sb.put('/api/role', json={'role': 'standby', 'epoch': 1}, headers=tok['b'])
    own = yaml.safe_load((b_dir / 'config.yaml').read_text())
    own.setdefault('ui', {})['port'] = 9999                                              # B's own port
    (b_dir / 'config.yaml').write_text(yaml.safe_dump(own))
    assert yaml.safe_load(base64.b64decode(got['bundle']) and zipfile.ZipFile(io.BytesIO(base64.b64decode(got['bundle']))).read('config.yaml')).get('ui', {}).get('port') != 9999
    b_port = 9999
    r = sb.post('/api/state/config', json=body, headers=tok['b'])
    assert r.status_code == 200 and r.json()['status'] == 'applied' and restarts == [1.5]
    applied = yaml.safe_load((b_dir / 'config.yaml').read_text())
    assert applied.get('ui', {}).get('port') == b_port                                    # its own, kept
    assert json.loads((b_dir / 'config_mirror.json').read_text())['hash'] == got['hash']
    assert any(s['trigger'] == 'pre-partner-config' for s in b.app.state.snapshot_store.list())
    r = sb.post('/api/state/config', json=body, headers=tok['b'])
    assert r.json()['status'] == 'same' and restarts == [1.5]                             # nothing twice
    bad = dict(body, bundle=base64.b64encode(b'not a zip').decode(), hash=None)
    assert sb.post('/api/state/config', json=bad, headers=tok['b']).status_code == 422


@needs_tc
def test_behind_a_load_balancer_the_standby_takes_the_partners_opc_ua_certificate(tmp_path, monkeypatch):
    from multibus.routes import role_routes
    from tests.test_operator_role import _build_clients
    a_dir, b_dir = tmp_path / 'a', tmp_path / 'b'
    a_dir.mkdir()
    b_dir.mkdir()
    a, b = _build_clients(a_dir), _build_clients(b_dir)
    monkeypatch.setattr(role_routes.threading, 'Timer', lambda s, fn: type('T', (), {'start': lambda self: None})())
    tok = {n: {"Authorization": f"Bearer {c.admin.post('/api/auth/service-tokens', json={'name': 'mbg-ha', 'role': 'ha'}).json()['token']}"}
           for n, c in (('a', a), ('b', b))}
    (a_dir / 'opcua').mkdir()
    (a_dir / 'opcua' / 'server_cert.der').write_bytes(b'\x30A-cert')
    (a_dir / 'opcua' / 'server_key.pem').write_bytes(b'A-key')
    sa, sb = TestClient(a.app, raise_server_exceptions=False), TestClient(b.app, raise_server_exceptions=False)

    def names():
        got = sa.get('/api/state/config', headers=tok['a']).json()
        return got, zipfile.ZipFile(io.BytesIO(base64.b64decode(got['bundle']))).namelist()
    assert 'opcua/server_key.pem' not in names()[1]                                      # one gateway: its own
    a.cfg.opcua.public_url = 'opc.tcp://lb.lan:4840/mbg'
    got, listed = names()
    assert {'opcua/server_cert.der', 'opcua/server_key.pem'} <= set(listed)
    sb.put('/api/role', json={'role': 'standby', 'epoch': 1}, headers=tok['b'])
    r = sb.post('/api/state/config', json={'bundle': got['bundle'], 'hash': got['hash'], 'from': 'A'}, headers=tok['b'])
    assert r.status_code == 200 and r.json()['status'] == 'applied'
    assert (b_dir / 'opcua' / 'server_cert.der').read_bytes() == b'\x30A-cert'
    assert oct((b_dir / 'opcua' / 'server_key.pem').stat().st_mode)[-3:] == '600'
