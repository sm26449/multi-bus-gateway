"""Asset attributes (docs/attributes.md): the catalog's checks, the config
store, the service's lifecycle and privacy, the API's roles, and the bundle
wiring (snapshots, mirror, export, import)."""
import io
import json
import os
import zipfile

import pytest
import yaml

from multibus import attribute_catalog as cat
from multibus.attributes import AttributeService, Invalid, NotFound
from multibus.config_store import Conflict, FileStore
from tests.test_devices_api import needs_tc

ROW = {'mppt': 1, 'modules': 24, 'module_wp': 450, 'azimuth_deg': 180, 'tilt_deg': 30}


# ── the catalog ──────────────────────────────────────────────────────────────

def test_a_pv_input_is_checked_and_its_kwp_derived():
    rows, errors, _w = cat.validate('pv_input', [ROW], level='device', mppt_count=2)
    assert not errors and rows[0]['kwp'] == 10.8
    _r, errors, _w = cat.validate('pv_input', [{**ROW, 'kwp': 12}], level='device')
    assert any('24 × 450 Wp is 10.8' in e for e in errors)
    _r, errors, _w = cat.validate('pv_input', [{'mppt': 1, 'azimuth_deg': 180, 'tilt_deg': 30}], level='device')
    assert any('kwp: required' in e for e in errors)
    _r, _e, warnings = cat.validate('pv_input', [{**ROW, 'mppt': 3}], level='device', mppt_count=2)
    assert warnings and 'shows 2 inputs' in warnings[0]


def test_azimuth_wraps_and_types_are_strict():
    rows, errors, _w = cat.validate('pv_input', [{**ROW, 'azimuth_deg': 360}], level='device')
    assert not errors and rows[0]['azimuth_deg'] == 0
    _r, errors, _w = cat.validate('pv_input', [{**ROW, 'tilt_deg': '30'}], level='device')
    assert any('tilt_deg: a number' in e for e in errors)
    _r, errors, _w = cat.validate('pv_input', [{**ROW, 'mppt': 1.5}], level='device')
    assert any('whole number' in e for e in errors)
    _r, errors, _w = cat.validate('pv_input', [{**ROW, 'colour': 'blue'}], level='device')
    assert any('not an attribute' in e for e in errors)


def test_only_the_connection_point_carries_grid_limits():
    ok, errors, _w = cat.validate('grid_connection', {'role': 'connection_point', 'max_import_kw': 10,
                                                      'max_export_kw': 8, 'phases': 3}, level='device')
    assert not errors and ok['max_export_kw'] == 8
    _r, errors, _w = cat.validate('grid_connection', {'role': 'sub_meter', 'max_import_kw': 10}, level='device')
    assert any('only a connection point' in e for e in errors)
    _r, errors, _w = cat.validate('grid_connection', {'role': 'grid'}, level='device')
    assert any('one of' in e for e in errors)


def test_a_category_sits_only_where_it_belongs():
    _r, errors, _w = cat.validate('pv_input', [ROW], level='site')
    assert errors and 'not on a site' in errors[0]
    _r, errors, _w = cat.validate('nope', {}, level='device')
    assert errors and 'not in the attribute catalog' in errors[0]


def test_coordinates_coarsen_or_vanish():
    loc = {'latitude_deg': 48.85661, 'longitude_deg': 2.35221, 'altitude_m': 80}
    assert cat.mask_coordinates({'site': {'location': loc}}) == {'site': {'location': {'latitude_deg': 48.86, 'longitude_deg': 2.35}}}
    assert cat.mask_coordinates({'location': loc}, 'off') == {'location': {}}
    assert cat.mask_coordinates({'location': loc}, 'exact') == {'location': loc}


# ── the config store ─────────────────────────────────────────────────────────

def test_the_store_saves_atomically_with_a_revision_and_reads_a_changed_file(tmp_path):
    s = FileStore(tmp_path / 'a.yaml', {'schema': 1})
    doc, rev = s.read()
    assert doc == {'schema': 1} and rev == ''
    _d, rev1, _ = s.mutate(lambda d: d.update(x=1))
    assert oct(os.stat(tmp_path / 'a.yaml').st_mode)[-3:] == '600'
    with pytest.raises(Conflict):
        s.mutate(lambda d: d.update(x=2), expected_rev='stale')
    (tmp_path / 'a.yaml').write_text('schema: 1\nx: 9\n')          # a restore, an import, the mirror
    doc, rev2 = s.read()
    assert doc['x'] == 9 and rev2 != rev1


# ── the service ──────────────────────────────────────────────────────────────

def _svc(tmp_path, devices=None, installations=('pv',)):
    devs = devices if devices is not None else [('umg512', '', 'meter', None),
                                                ('pv-u1', 'pv', 'inverter', 2), ('pv-u2', 'pv', 'inverter', 2)]
    holder = {'devs': list(devs)}
    audit = []
    svc = AttributeService(tmp_path / 'attributes.yaml', devices=lambda: holder['devs'],
                           installations=lambda: list(installations),
                           audit=type('A', (), {'append': lambda self, **kw: audit.append(kw)})())
    return svc, holder, audit


def test_location_is_inherited_device_from_installation_from_site(tmp_path):
    svc, _h, _a = _svc(tmp_path)
    svc.put('site', '', 'location', {'latitude_deg': 48.86, 'longitude_deg': 2.35})
    loc, src = svc.effective_location('device', 'pv-u1')
    assert src == 'site' and loc['latitude_deg'] == 48.86
    svc.put('installation', 'pv', 'location', {'latitude_deg': 45.0, 'longitude_deg': 25.0})
    loc, src = svc.effective_location('device', 'pv-u1')
    assert src == 'installation pv' and loc['latitude_deg'] == 45.0
    assert svc.effective_location('device', 'umg512')[1] == 'site'


def test_a_viewer_sees_coarse_coordinates_an_admin_exact(tmp_path):
    svc, _h, audit = _svc(tmp_path)
    svc.put('site', '', 'location', {'latitude_deg': 48.85661, 'longitude_deg': 2.35221})
    assert svc.read_all(exact=True)['site']['location']['latitude_deg'] == 48.85661
    assert svc.read_all()['site']['location'] == {'latitude_deg': 48.86, 'longitude_deg': 2.35}
    svc.set_publish_mode('off')
    assert svc.read_all()['site']['location'] == {}
    assert audit[0]['detail']['after'] == {'latitude_deg': 48.86, 'longitude_deg': 2.35}   # never exact in the log


def test_orphans_are_kept_and_dropped_only_on_purpose(tmp_path):
    svc, holder, _a = _svc(tmp_path)
    svc.put('device', 'pv-u2', 'rating', {'rated_ac_kw': 20})
    holder['devs'] = [d for d in holder['devs'] if d[0] != 'pv-u2']      # deleted (a tombstone)
    assert svc.read_all()['orphans']['device'] == ['pv-u2']
    assert svc.get('device', 'pv-u2')['orphan'] is True                  # still readable
    with pytest.raises(NotFound):
        svc.put('device', 'pv-u2', 'rating', {'rated_ac_kw': 20})        # no writes to what is gone
    assert svc.drop('device', 'pv-u2', reason='forgotten') is True
    assert svc.read_all()['orphans']['device'] == []


def test_copy_to_other_units_checks_each_target(tmp_path):
    svc, holder, _a = _svc(tmp_path)
    svc.put('device', 'pv-u1', 'pv_input', [ROW, {**ROW, 'mppt': 2, 'azimuth_deg': 90}])
    _rev, warnings = svc.copy_to('pv-u1', 'pv_input', ['pv-u2'])
    assert not warnings and svc.get('device', 'pv-u2')['attributes']['pv_input'][1]['azimuth_deg'] == 90
    holder['devs'].append(('pv-u3', 'pv', 'inverter', 1))
    _rev, warnings = svc.copy_to('pv-u1', 'pv_input', ['pv-u3'])
    assert warnings and 'pv-u3' in warnings[0]                           # only one input there
    with pytest.raises(Invalid):
        svc.copy_to('pv-u1', 'pv_input', ['nope'])


def test_two_connection_points_are_a_warning(tmp_path):
    svc, holder, _a = _svc(tmp_path)
    holder['devs'].append(('em24', '', 'meter', None))
    svc.put('device', 'umg512', 'grid_connection', {'role': 'connection_point'})
    _rev, warnings = svc.put('device', 'em24', 'grid_connection', {'role': 'connection_point'})
    assert warnings and 'a plant has one' in warnings[0]


def test_what_is_offered_first_follows_the_kind(tmp_path):
    svc, _h, _a = _svc(tmp_path)
    offered = [o['category'] for o in svc.get('device', 'pv-u1')['offered']]
    assert offered[:2] == ['pv_input', 'rating'] and 'grid_connection' in offered     # offered, never hidden
    offered = [o['category'] for o in svc.get('device', 'umg512')['offered']]
    assert offered[0] == 'grid_connection'


# ── the API ──────────────────────────────────────────────────────────────────

def _api(tmp_path, *, auth=True):
    from fastapi.testclient import TestClient
    from multibus import auth as _authmod
    from multibus.api import create_api
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml="""
devices:
  - id: em24
    template: carlo_gavazzi_em24
    connection: { protocol: tcp, host: 192.0.2.24, port: 502, unit_id: 1 }
""")
    if auth:
        cfg.ui.auth_enabled = True
        cfg.ui.auth_username = "admin"
        cfg.ui.auth_password = _authmod.hash_password("pw")
        cfg.ui.viewer_username = "viewer"
        cfg.ui.viewer_password = _authmod.hash_password("vw")
        cfg.ui.operator_username, cfg.ui.operator_password = "ops", _authmod.hash_password("op")
    app, _ = create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    return cfg, app, TestClient(app, raise_server_exceptions=False)


def _login(client, user, pw):
    assert client.post("/api/auth/login", json={"username": user, "password": pw}).status_code == 200


@needs_tc
def test_the_api_reads_for_all_and_writes_for_the_admin(tmp_path):
    cfg, app, client = _api(tmp_path)
    _login(client, 'admin', 'pw')
    assert 'pv_input' in client.get("/api/attributes/catalog").json()['categories']
    r = client.put("/api/attributes/site/location", json={'latitude_deg': 48.85661, 'longitude_deg': 2.35221})
    assert r.status_code == 200, r.text
    r = client.put("/api/attributes/devices/em24/grid_connection",
                   json={'role': 'connection_point', 'max_import_kw': 10, 'max_export_kw': 8})
    assert r.status_code == 200, r.text
    rev = r.json()['rev']
    assert client.get("/api/attributes").json()['site']['location']['latitude_deg'] == 48.85661
    # a stale If-Match is a conflict, nothing written
    r = client.put("/api/attributes/devices/em24/grid_connection", json={'role': 'sub_meter'},
                   headers={'If-Match': 'stale'})
    assert r.status_code == 412
    assert client.put("/api/attributes/devices/em24/grid_connection", json={'role': 'connection_point'},
                      headers={'If-Match': rev}).status_code == 200
    assert client.put("/api/attributes/devices/nope/rating", json={'rated_ac_kw': 5}).status_code == 404
    assert client.put("/api/attributes/devices/em24/rating", json={'rated_ac_kw': 'x'}).status_code == 422
    # a viewer reads, coarse, and cannot write
    client.post("/api/auth/logout")
    _login(client, 'viewer', 'vw')
    assert client.get("/api/attributes").json()['site']['location'] == {'latitude_deg': 48.86, 'longitude_deg': 2.35}
    assert client.put("/api/attributes/site/location", json={'latitude_deg': 1, 'longitude_deg': 1}).status_code in (401, 403)


@needs_tc
def test_a_standby_answers_reads_and_refuses_writes(tmp_path):
    cfg, app, client = _api(tmp_path)
    _login(client, 'admin', 'pw')
    app.state.role.set('standby', 1, by='test')
    assert client.get("/api/attributes").status_code == 200
    r = client.put("/api/attributes/site/location", json={'latitude_deg': 48.8, 'longitude_deg': 2.3})
    assert r.status_code == 503 and 'standby' in r.text


@needs_tc
def test_export_leaves_coordinates_out_unless_identity_and_import_keeps_them(tmp_path):
    cfg, app, client = _api(tmp_path)
    _login(client, 'admin', 'pw')
    client.put("/api/attributes/site/location", json={'latitude_deg': 48.86, 'longitude_deg': 2.35})
    client.put("/api/attributes/devices/em24/grid_connection", json={'role': 'connection_point', 'max_export_kw': 8})
    z = zipfile.ZipFile(io.BytesIO(client.get("/api/config/export").content))
    doc = yaml.safe_load(z.read("attributes.yaml"))
    assert 'location' not in doc['site'] and doc['device']['em24']['grid_connection']['max_export_kw'] == 8
    z2 = zipfile.ZipFile(io.BytesIO(client.get("/api/config/export?include_identity=true").content))
    assert yaml.safe_load(z2.read("attributes.yaml"))['site']['location']['latitude_deg'] == 48.86
    # importing the sanitised backup keeps the live coordinates
    r = client.post("/api/config/import", content=client.get("/api/config/export").content,
                    headers={'Content-Type': 'application/zip'})
    assert r.status_code == 200, r.text
    assert client.get("/api/attributes").json()['site']['location']['latitude_deg'] == 48.86


def test_snapshots_carry_attributes_and_refuse_a_bad_one(tmp_path):
    from multibus import snapshots
    assert "attributes.yaml" in snapshots.BUNDLE_EXTRAS
    bad = yaml.safe_dump({'schema': 1, 'device': {'x': {'pv_input': [{'mppt': 1}]}}}).encode()
    assert any('pv_input' in e for e in snapshots.validate_attributes_doc(bad))
    good = yaml.safe_dump({'schema': 1, 'site': {'location': {'latitude_deg': 1, 'longitude_deg': 2}}}).encode()
    assert snapshots.validate_attributes_doc(good) == []
    assert snapshots._mask('site.location.latitude_deg', 48.85661) == 48.86
    assert snapshots._mask('site.location.altitude_m', 80) == '***'


@needs_tc
def test_a_new_device_at_an_old_id_starts_without_attributes(tmp_path):
    cfg, app, client = _api(tmp_path)
    _login(client, 'admin', 'pw')
    client.put("/api/attributes/devices/em24/rating", json={'rated_ac_kw': 5})
    assert client.delete("/api/devices/em24").status_code == 200
    assert client.get("/api/attributes").json()['orphans']['device'] == ['em24']     # kept: it can be restored
    body = {'id': 'em24', 'template': 'carlo_gavazzi_em24', 'enabled': False,
            'connection': {'protocol': 'tcp', 'host': '192.0.2.25', 'port': 502, 'unit_id': 1}}
    # a new device created at the id (the tombstone still there): it starts clean
    r = client.post("/api/devices", json=body)
    assert r.status_code == 200, r.text
    assert client.get("/api/attributes").json()['device'] == {}
    # forgetting a deleted device drops what it held
    client.put("/api/attributes/devices/em24/rating", json={'rated_ac_kw': 5})
    assert client.delete("/api/devices/em24").status_code == 200
    assert client.delete("/api/devices/restorable/em24").status_code == 200
    assert client.get("/api/attributes").json()['device'] == {}


def test_saving_back_rounded_coordinates_keeps_the_exact_ones(tmp_path):
    svc, _h, _a = _svc(tmp_path)
    svc.put('site', '', 'location', {'latitude_deg': 48.85661, 'longitude_deg': 2.35221, 'altitude_m': 80})
    shown = svc.get('site')['attributes']['location']
    assert shown == {'latitude_deg': 48.86, 'longitude_deg': 2.35} and svc.get('site')['exact'] is False
    svc.put('site', '', 'location', shown)                      # saved back as seen
    assert svc.read_all(exact=True)['site']['location']['latitude_deg'] == 48.85661
    svc.put('site', '', 'location', {'latitude_deg': 45.1, 'longitude_deg': 25.9})   # a real change
    assert svc.read_all(exact=True)['site']['location'] == {'latitude_deg': 45.1, 'longitude_deg': 25.9}


# ── what the second review asked for ─────────────────────────────────────────

def _tok(app, role):
    st = app.state.ctx.service_tokens
    return {'Authorization': f"Bearer {st.create(f'ems-{role}', role)}"}


@needs_tc
def test_roles_operator_and_viewer_tokens_cannot_write_an_admin_token_can(tmp_path):
    cfg, app, client = _api(tmp_path)
    _login(client, 'ops', 'op')
    assert client.put("/api/attributes/site/location", json={'latitude_deg': 1, 'longitude_deg': 1}).status_code == 403
    client.post("/api/auth/logout")
    from fastapi.testclient import TestClient
    anon = TestClient(app, raise_server_exceptions=False)
    for role in ('viewer', 'operator'):
        r = anon.put("/api/attributes/site/location", json={'latitude_deg': 1, 'longitude_deg': 1}, headers=_tok(app, role))
        assert r.status_code in (401, 403), (role, r.status_code)
    r = anon.put("/api/attributes/site/location", json={'latitude_deg': 48.8, 'longitude_deg': 2.3}, headers=_tok(app, 'admin'))
    assert r.status_code == 200, r.text                                   # how an EMS writes


@needs_tc
def test_the_audit_never_holds_exact_coordinates(tmp_path):
    cfg, app, client = _api(tmp_path)
    _login(client, 'admin', 'pw')
    assert client.put("/api/attributes/site/location",
                      json={'latitude_deg': 48.856612, 'longitude_deg': 2.352214, 'altitude_m': 81}).status_code == 200
    text = (tmp_path / "audit.jsonl").read_text()
    assert '48.856612' not in text and '2.352214' not in text and '81' not in text.split('altitude_m')[-1][:12]


@needs_tc
def test_a_stale_revision_is_a_precondition_failure_and_star_means_any(tmp_path):
    cfg, app, client = _api(tmp_path)
    _login(client, 'admin', 'pw')
    r = client.put("/api/attributes/devices/em24/rating", json={'rated_ac_kw': 5}, headers={'If-Match': 'stale'})
    assert r.status_code == 412 and 'rev' in r.json()['detail']
    assert client.put("/api/attributes/devices/em24/rating", json={'rated_ac_kw': 5}, headers={'If-Match': '*'}).status_code == 200
    assert client.get("/api/attributes/site/anything").status_code == 404       # the plant has its own routes
    assert client.post("/api/attributes/devices/em24/rating/copy", json={'to': 'abc'}).status_code == 422


def test_types_are_strict_and_huge_numbers_are_refused():
    for bad in (True, 1.0, '3'):
        _c, errors, _w = cat.validate('grid_connection', {'role': 'connection_point', 'phases': bad}, level='device')
        assert any('phases' in e for e in errors), bad
    for bad in (float('nan'), 10 ** 400, True):
        _c, errors, _w = cat.validate('rating', {'rated_ac_kw': bad}, level='device')
        assert any('a number' in e for e in errors), bad
    clean, errors, _w = cat.validate('pv_input', [{**ROW, 'module_model': 'JA Solar JAM54S30-450', 'technology': 'monocrystalline'}], level='device')
    assert not errors and clean[0]['module_model'] == 'JA Solar JAM54S30-450'
    _c, errors, _w = cat.validate('pv_input', [{**ROW, 'module_model': 'JA' + chr(0x200B)}], level='device')
    assert any('invisible' in e for e in errors)


def test_a_merge_import_keeps_what_the_backup_does_not_hold(tmp_path):
    from multibus.snapshots import merge_attributes_doc
    live = yaml.safe_dump({'schema': 1, 'site': {'location': {'latitude_deg': 48.8, 'longitude_deg': 2.3}},
                           'device': {'a': {'rating': {'rated_ac_kw': 5}}, 'b': {'rating': {'rated_ac_kw': 7}}}}).encode()
    backup = yaml.safe_dump({'schema': 1, 'installation': None,
                             'device': {'a': {'rating': {'rated_ac_kw': 6}}}}).encode()
    merged = yaml.safe_load(merge_attributes_doc(live, backup))
    assert merged['device']['a']['rating']['rated_ac_kw'] == 6                  # the backup's wins
    assert merged['device']['b']['rating']['rated_ac_kw'] == 7                  # live only: kept
    assert merged['site']['location']['latitude_deg'] == 48.8                   # stripped: kept


def test_a_file_that_does_not_parse_is_read_but_never_overwritten(tmp_path):
    from multibus.config_store import Broken
    svc, _h, _a = _svc(tmp_path)
    svc.put('site', '', 'location', {'latitude_deg': 48.8, 'longitude_deg': 2.3})
    (tmp_path / 'attributes.yaml').write_text("site: [unclosed\n")
    out = svc.read_all(exact=True)
    assert out['broken'] and out['site']['location']['latitude_deg'] == 48.8     # the last good one
    with pytest.raises(Broken):
        svc.put('site', '', 'location', {'latitude_deg': 1, 'longitude_deg': 1})
    assert (tmp_path / 'attributes.yaml').read_text() == "site: [unclosed\n"
    (tmp_path / 'attributes.yaml').write_text("schema: 1\ndevice: null\nsettings: []\n")
    assert svc.read_all()['device'] == {} and svc.publish_mode() == 'coarse'      # normalised


def test_the_exact_publish_mode_is_honoured(tmp_path):
    svc, _h, _a = _svc(tmp_path)
    svc.put('site', '', 'location', {'latitude_deg': 48.85661, 'longitude_deg': 2.35221})
    svc.set_publish_mode('exact')
    g = svc.get('site')
    assert g['exact'] is True and g['attributes']['location']['latitude_deg'] == 48.85661


# ── phase 2: what is published ───────────────────────────────────────────────

def test_meta_and_flat_follow_the_publishing_setting(tmp_path):
    svc, _h, _a = _svc(tmp_path)
    svc.put('site', '', 'location', {'latitude_deg': 48.85661, 'longitude_deg': 2.35221})
    svc.put('device', 'pv-u1', 'pv_input', [ROW])
    m = svc.meta('device', 'pv-u1')
    assert m['schema'] == 1 and m['rev'] and m['kind'] == 'inverter'
    assert m['location'] == {'value': {'latitude_deg': 48.86, 'longitude_deg': 2.35}, 'from': 'site'}
    flat = svc.flat('device', 'pv-u1')
    assert flat['pv_input.1.kwp'] == 10.8 and flat['location.latitude_deg'] == 48.86
    svc.set_publish_mode('off')
    assert 'location' not in svc.meta('device', 'pv-u1') and 'location.latitude_deg' not in svc.flat('device', 'pv-u1')
    assert svc.meta('site')['attributes']['location'] == {}


@needs_tc
def test_meta_is_published_retained_by_the_active_gateway_only(tmp_path):
    from fastapi.testclient import TestClient
    from multibus import auth as _authmod
    from multibus.api import create_api
    from tests.test_commands_api import _Mqtt
    from tests.test_devices import write_config
    m = _Mqtt()
    m.unregister_command = lambda t: m.topics.pop(t, None)
    m._publish = lambda topic, payload, retain=None: (m.sent.append((topic, payload, retain)), True)[1]
    m.discovery_hooks = []
    cfg = write_config(tmp_path)
    cfg.ui.auth_enabled, cfg.ui.auth_username, cfg.ui.auth_password = True, "admin", _authmod.hash_password("pw")
    app, _ = create_api(cfg, None, m, None, devices=[(d, None) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    _login(client, 'admin', 'pw')
    m.sent.clear()
    assert client.put("/api/attributes/site/location", json={'latitude_deg': 48.85661, 'longitude_deg': 2.35221}).status_code == 200
    site = [json.loads(p) for t, p, r in m.sent if t == f"{cfg.mqtt.topic_prefix}/site/meta" and r]
    assert site and site[-1]['attributes']['location'] == {'latitude_deg': 48.86, 'longitude_deg': 2.35}
    assert not any('48.85661' in p for _t, p, _r in m.sent)                  # never exact on the broker
    m.sent.clear()
    app.state.role.set('standby', 1, by='test')
    app.state.publish_attribute_meta(force=True)
    assert m.sent == []                                                     # a standby publishes nothing
