# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Lost updates (ADR-0026 §6): `GET /api/devices/{id}` sends an ETag — a
hash of the stored entry — and `PUT` / `DELETE` honour `If-Match`. A stale
one answers 412 with the current entry and changes nothing; no header
behaves as before."""
import pathlib

from multibus.config import Config
from tests.test_devices_api import make_app, needs_tc

SETUP = """
devices:
  - id: inv1
    name: Inverter
    template: eastron_sdm630
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.80, unit_id: 1}
"""


def _body(cfg, did='inv1', **change):
    raw = cfg.get_raw_device(did)
    body = {k: v for k, v in raw.items() if k in ('id', 'name', 'template', 'enabled', 'connection')}
    body.update(change)
    return body


def _etag(client, did='inv1'):
    r = client.get(f"/api/devices/{did}")
    assert r.status_code == 200, r.text
    tag = r.headers.get('etag')
    assert tag and tag.startswith('"') and tag.endswith('"'), tag
    return tag


@needs_tc
def test_the_etag_is_stable_while_nothing_changes(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml=SETUP)
    tag = _etag(client)
    assert _etag(client) == tag
    cfg.load()                                       # a reload of the same file
    assert _etag(client) == tag
    r = client.put("/api/devices/inv1", json=_body(cfg))
    assert r.json()['status'] == 'unchanged' and _etag(client) == tag
    again = Config(str(tmp_path / "config.yaml"))     # a restart reads the same
    assert again.get_raw_device('inv1') == cfg.get_raw_device('inv1')


@needs_tc
def test_a_stale_if_match_is_refused_with_the_current_entry(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml=SETUP)
    old = _etag(client)
    r = client.put("/api/devices/inv1", json=_body(cfg, name='First'), headers={'If-Match': old})
    assert r.status_code == 200, r.text
    new = r.headers.get('etag')
    assert new and new != old and new == _etag(client)
    path = pathlib.Path(cfg.config_path)
    before = path.read_bytes()
    r = client.put("/api/devices/inv1", json=_body(cfg, name='Second'), headers={'If-Match': old})
    assert r.status_code == 412, r.text
    d = r.json()['detail']
    assert d['current']['name'] == 'First' and d['etag'] == new and d['errors']
    assert path.read_bytes() == before
    r = client.put("/api/devices/inv1?dry_run=true", json=_body(cfg, name='Second'), headers={'If-Match': old})
    assert r.status_code == 412
    r = client.delete("/api/devices/inv1", headers={'If-Match': old})
    assert r.status_code == 412 and cfg.get_device('inv1') is not None
    r = client.delete("/api/devices/inv1?dry_run=true", headers={'If-Match': old})
    assert r.status_code == 412


@needs_tc
def test_a_matching_or_wildcard_if_match_and_no_header_go_through(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml=SETUP)
    r = client.put("/api/devices/inv1", json=_body(cfg, name='A'), headers={'If-Match': '*'})
    assert r.status_code == 200, r.text
    r = client.put("/api/devices/inv1", json=_body(cfg, name='B'))           # as before
    assert r.status_code == 200 and cfg.get_device('inv1').name == 'B'
    weak = 'W/' + _etag(client)
    r = client.put("/api/devices/inv1", json=_body(cfg, name='C'), headers={'If-Match': weak})
    assert r.status_code == 200, r.text
    r = client.delete("/api/devices/inv1", headers={'If-Match': _etag(client)})
    assert r.status_code == 200, r.text
    assert cfg.get_device('inv1') is None
    assert client.get("/api/devices/inv1").status_code == 404


@needs_tc
def test_the_primary_has_an_etag_too(tmp_path):
    _cfg, client = make_app(tmp_path)
    tag = _etag(client, 'umg512')
    body = {'connection': {'protocol': 'tcp', 'host': '10.9.9.9', 'port': 502, 'unit_id': 1}}
    assert client.put("/api/devices/umg512", json=body, headers={'If-Match': '"stale"'}).status_code == 412
    r = client.put("/api/devices/umg512", json=body, headers={'If-Match': tag})
    assert r.status_code == 200, r.text
    assert _etag(client, 'umg512') != tag
