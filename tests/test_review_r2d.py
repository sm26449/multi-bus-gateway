# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""What the two-day review (ids R2D-*) found in the 2026-11-12 fixes — each test here is the inverse of a probe that
reproduced the defect. Fake clients and transmitters only; no bus, broker or network."""
import copy
import io
import threading
import zipfile
from types import SimpleNamespace

import pytest
import yaml

from multibus.core.config import Config
from multibus.core.config.models import (DeviceConfig, ModbusConfig, SourceConfig,
                                         device_wire_identity)
from multibus.devices.device_registry import DeviceRegistry
from tests.test_command_binding import _yaml_app
from tests.test_commands import _Inverter
from tests.test_commands_api import PLANT_YAML, _app, _Facade
from tests.test_devices import write_config


def _bundle(doc):
    b = io.BytesIO()
    with zipfile.ZipFile(b, 'w') as z:
        z.writestr('config.yaml', yaml.safe_dump(doc))
    return b.getvalue()


def _doc(cfg):
    return yaml.safe_load(cfg.config_path.read_text())


def _units(doc, units):
    doc['endpoints'][0]['groups'][0]['units'] = units
    return doc


def _dev(did, host="1.1.1.1", **src):
    return DeviceConfig(id=did, name=did, sources=[SourceConfig(
        id="s", protocol=src.pop("protocol", "tcp"),
        connection=ModbusConfig(host=host, port=502), **src)])


# ── R2D-AW-1: handing back the SAME client keeps it — the retarget stays pending ─────────

def test_replace_with_the_same_client_does_not_record_a_new_wire():
    r = DeviceRegistry("p", {})
    r.register(_dev("a"), "CLIENT")
    r.replace("a", _dev("a", host="9.9.9.9"), client="CLIENT")
    assert r.applied_wire["a"] == device_wire_identity(_dev("a"))
    r.replace("a", _dev("a", host="9.9.9.9"), client="OTHER")           # a real rebuild
    assert r.applied_wire["a"] == device_wire_identity(_dev("a", host="9.9.9.9"))


INV_YAML = '''
security: {allow_writes: true}
devices:
 - id: inv
   name: Inverter
   template: fronius_sunspec_inverter
   sources:
    - id: sunspec
      protocol: tcp
      host: 192.0.2.9
      port: 502
      template: fronius_sunspec_inverter
   commands:
    - {name: power_limit, enabled: true}
'''


def test_a_binding_edit_after_a_retarget_does_not_reopen_the_old_wire(tmp_path):
    """Import moves the device's wire (its client is kept and refuses writes); an ordinary
    command-binding edit must not make the gateway believe the client was rebuilt."""
    inv = _Inverter(sf=-2)
    cfg, app, c = _yaml_app(tmp_path, INV_YAML, {'inv': inv})
    cfg.save_yaml_config()
    doc = _doc(cfg)
    doc['devices'][0]['sources'][0]['host'] = '192.0.2.99'
    assert c.post('/api/config/import?lock_writes=false', content=_bundle(doc)).status_code == 200
    kept = app.state.registry.find('inv')[2]
    r = c.put('/api/devices/inv/commands/power_limit', json={'enabled': True})
    assert r.status_code == 200, r.text
    assert app.state.registry.find('inv')[2] is kept
    w = c.post('/api/devices/inv/commands/power_limit', json={'value': 60})
    assert w.status_code == 403 and 'retarget' in w.text.lower(), w.text
    assert not inv.writes


# ── R2D-AW-2: the wire identity covers MQTT and CAN targets ──────────────────────────────

@pytest.mark.parametrize("a,b", [
    (dict(protocol="mqtt", mqtt_in={"broker": "a.lan", "topic": "N/1/#"}),
     dict(protocol="mqtt", mqtt_in={"broker": "b.lan", "topic": "N/1/#"})),
    (dict(protocol="mqtt", mqtt_in={"broker": "a.lan", "topic": "N/1/#"}),
     dict(protocol="mqtt", mqtt_in={"broker": "a.lan", "topic": "N/2/#"})),
    (dict(protocol="mqtt", mqtt_in={"broker": "a.lan", "vars": {"portal": "1"}}),
     dict(protocol="mqtt", mqtt_in={"broker": "a.lan", "vars": {"portal": "2"}})),
    (dict(protocol="can", can={"channel": "can0"}), dict(protocol="can", can={"channel": "can1"})),
])
def test_a_moved_mqtt_or_can_target_is_a_different_wire(a, b):
    assert device_wire_identity(_dev("x", host="", **a)) != device_wire_identity(_dev("x", host="", **b))


def test_tuning_that_is_not_a_target_does_not_change_the_wire():
    a = _dev("x", host="", protocol="mqtt", mqtt_in={"broker": "a.lan", "topic": "t", "stale_after_s": 30})
    b = _dev("x", host="", protocol="mqtt", mqtt_in={"broker": "a.lan", "topic": "t", "stale_after_s": 90,
                                                     "accept_retained": True})
    assert device_wire_identity(a) == device_wire_identity(b)


# ── R2D-AW-3: a queued command never runs on a client that was replaced meanwhile ────────

def test_a_command_queued_before_a_rebuild_is_not_written_through_the_old_client(tmp_path):
    from multibus.command_dispatch import Job
    old_wire, new_wire = _Inverter(sf=-2), _Inverter(sf=-2)
    cfg, app, c = _app(tmp_path, {'pv-u1': old_wire})
    oldcfg, oldclient = app.state.registry.find('pv-u1')[1:]
    lane = app.state.lane_key(oldcfg, oldclient)
    started, release, admitted = threading.Event(), threading.Event(), threading.Event()

    def hold(job):
        started.set()
        assert release.wait(8)
        return 200, {'status': 'success'}
    dispatcher = app.state.command_dispatch
    dispatcher.submit(Job(lane=lane, device='blocker', channel='x', command='hold', params={}, run=hold))
    assert started.wait(3)
    submit = dispatcher.submit

    def spy(job):
        out = submit(job)
        if job.device == 'pv-u1':
            admitted.set()
        return out
    dispatcher.submit = spy
    responses = []
    t = threading.Thread(target=lambda: responses.append(
        c.post('/api/devices/pv-u1/commands/power_limit', json={'value': 60, 'revert_s': 0})))
    t.start()
    try:
        assert admitted.wait(3)
        app.state.registry.replace('pv-u1', cfg.get_device('pv-u1'), client=_Facade(new_wire))
    finally:
        release.set()
        t.join(5)
    assert responses and responses[0].status_code == 409, responses and responses[0].text
    assert 'rebuilt' in responses[0].text
    assert not old_wire.writes and not new_wire.writes


# ── R2D-F20-1: the forced lock is applied BEFORE the import is published ─────────────────

def test_a_lock_that_cannot_be_applied_refuses_the_import_and_locks_at_the_next_boot(tmp_path, monkeypatch):
    """The lock step fails (disk full). The import must NOT become the live config: the
    running one stays, the answer is 500, and the marker is still there — a process that
    starts on the imported file locks it before it runs anything."""
    inv = _Inverter(sf=-2)
    cfg, app, c = _app(tmp_path, {'pv-u1': inv})
    cfg.save_yaml_config()
    orig = Config.save_yaml_config

    def staged_save_fails(self):
        if getattr(self, '_staging', False):
            raise OSError(28, "No space left on device")
        return orig(self)
    monkeypatch.setattr(Config, 'save_yaml_config', staged_save_fails)
    r = c.post('/api/config/import', content=_bundle(_units(_doc(cfg), [1, 2, 3])))
    assert r.status_code == 500 and r.json()['detail']['written'] is True, r.text
    assert cfg.get_device('pv-u3') is None                       # the bundle is NOT live
    assert app.state.registry.find('pv-u3')[1] is None
    assert cfg.lock_pending_path().exists()                      # the intent survives
    monkeypatch.setattr(Config, 'save_yaml_config', orig)
    booted = Config(str(cfg.config_path))                        # "the gateway restarts"
    assert booted.get_device('pv-u3') is not None
    assert all(d.write_locked for d in booted.devices)
    assert not booted.lock_pending_path().exists()
    assert all(d.write_locked for d in Config(str(cfg.config_path)).devices)   # and it is on disk


def test_a_locked_import_is_never_published_unlocked(tmp_path, monkeypatch):
    """Whatever reads the live config during the import — at any point — sees every device
    locked or the old config; the lock is not a step after the publication any more."""
    cfg, app, c = _app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    cfg.save_yaml_config()
    seen = []
    orig_refresh = Config._refresh_good

    def after_publication(self):                 # the first thing load() does once published
        seen.append({d.id: d.write_locked for d in cfg.devices})
        return orig_refresh(self)
    monkeypatch.setattr(Config, '_refresh_good', after_publication)
    r = c.post('/api/config/import', content=_bundle(_units(_doc(cfg), [1, 2, 3])))
    assert r.status_code == 200, r.text
    assert seen and 'pv-u3' in seen[-1] and all(seen[-1].values()), seen
    assert not cfg.lock_pending_path().exists()


def test_the_forced_lock_is_one_save_not_one_per_device(tmp_path, monkeypatch):
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    saves = []
    orig = Config.save_yaml_config
    monkeypatch.setattr(Config, 'save_yaml_config', lambda self: (saves.append(1), orig(self))[1])
    assert cfg.force_lock_all_writes() >= 1
    assert len(saves) == 1 and all(d.write_locked for d in cfg.devices)
    assert cfg.force_lock_all_writes() == 0 and len(saves) == 1          # idempotent, no save


def test_an_import_that_opts_out_clears_a_marker_left_by_an_earlier_attempt(tmp_path):
    cfg, app, c = _app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    cfg.save_yaml_config()
    cfg.lock_pending_path().write_text("stale\n")
    r = c.post('/api/config/import?lock_writes=false', content=_bundle(_doc(cfg)))
    assert r.status_code == 200, r.text
    assert not cfg.lock_pending_path().exists()
    assert cfg.get_device('pv-u1').write_locked is False


def test_a_boot_that_cannot_apply_a_pending_lock_comes_up_with_writes_off(tmp_path, monkeypatch):
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    assert cfg.security.allow_writes is True
    cfg.lock_pending_path().write_text("pre-import\n")
    monkeypatch.setattr(Config, 'force_lock_all_writes',
                        lambda self: (_ for _ in ()).throw(OSError("read-only disk")))
    booted = Config(str(cfg.config_path))
    assert booted.security.allow_writes is False
    assert booted.lock_pending_path().exists()


# ── R2D-HEAL-1: a healed boot takes nothing from the file it rejected ────────────────────

def test_a_cold_heal_does_not_keep_write_permission_from_the_rejected_file(tmp_path):
    from multibus.lifecycle.snapshots import boot_seatbelt
    p = tmp_path / 'config.yaml'
    p.write_text('mqtt: {enabled: false}\nprimary_device: false\n')
    assert Config(str(p)).security.allow_writes is False
    assert 'security:' not in p.with_suffix('.yaml.good').read_text()
    p.write_text('mqtt: {enabled: false}\nprimary_device: false\n'
                 'security: {allow_writes: true, primary_write_locked: false}\n'
                 'opcua: {port: not-a-number}\n')
    booted = boot_seatbelt(str(p), lambda: Config(str(p)))
    assert booted._load_failed and booted._healed_from_snapshot
    assert booted.security.allow_writes is False
    assert booted.security.primary_write_locked is True


# ── R2D-REMOVE-1 / R2D-APPLY-1: what runs follows the config, whatever fails in between ──

def test_a_removed_device_is_stopped_even_when_the_primary_reconnect_then_fails(tmp_path):
    from fastapi.testclient import TestClient

    from multibus.api import create_api
    cfg, oldapp, _c = _app(tmp_path, {'pv-u1': _Inverter(sf=-2), 'pv-u2': _Inverter(sf=-2)})
    pairs = oldapp.state.registry.pairs()
    removed = oldapp.state.registry.find('pv-u2')[2]

    def fail():
        raise OSError('injected reconnect failure')
    primary = SimpleNamespace(publish_callback=None, update_config=lambda *a: None,
                              update_registers=lambda *a: None, reconnect=fail)
    app, _ = create_api(cfg, primary, None, None, devices=pairs)
    c = TestClient(app, raise_server_exceptions=False)
    assert c.post('/api/auth/login', json={'username': 'admin', 'password': 'pw'}).status_code == 200
    cfg.save_yaml_config()
    r = c.post('/api/config/import?lock_writes=false', content=_bundle(_units(_doc(cfg), [1])))
    assert r.status_code == 500, r.text                          # the reconnect did fail
    assert app.state.registry.find('pv-u2')[1] is None
    assert removed.connected is False                            # …but pv-u2 was stopped first


def test_a_device_saved_without_apply_is_started_by_the_next_apply(tmp_path):
    cfg, app, c = _app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    cfg.save_yaml_config()
    doc = _units(_doc(cfg), [1, 2, 3])
    assert c.post('/api/config/import?lock_writes=false&apply=false', content=_bundle(doc)).status_code == 200
    assert app.state.registry.find('pv-u3')[2] is None           # saved, not started
    assert c.post('/api/config/import?lock_writes=false', content=_bundle(doc)).status_code == 200
    assert app.state.registry.find('pv-u3')[2] is not None       # the same config, now applied
    assert 'pv-u3' in app.state.applied_wire


# ── R2D-NAMESPACE-1: a device cannot take a Virtual BMS's id ─────────────────────────────

def test_a_device_cannot_be_created_under_a_virtual_bms_id(tmp_path):
    cfg, app, c = _app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    cfg.virtual_bms = [{"id": "vb", "profile": "pylontech_can_inverter", "packs": ["pv-u1"]}]
    r = c.post('/api/devices', json={"id": "vb", "name": "clash", "template": "",
                                     "connection": {"host": "192.0.2.7", "port": 502}})
    assert r.status_code == 422 and 'Virtual BMS' in r.text, r.text


_VB = {"id": "vb", "profile": "pylontech_can_inverter", "packs": ["pv-u1"]}


def _clash(r):
    return r.status_code == 422 and 'Virtual BMS' in r.text


def test_an_installation_unit_cannot_take_a_virtual_bms_id(tmp_path):
    """Flat units and grouped units alike: an installation's unit is a device on the topics."""
    from tests.test_endpoint_edit_guard import PLANT
    from tests.test_endpoints import ENDPOINT_BODY, make_app
    cfg, c = make_app(tmp_path)
    cfg.virtual_bms = [dict(_VB, id="sdm-endpoint-u2")]
    assert _clash(c.post('/api/endpoints', json=ENDPOINT_BODY))
    cfg.virtual_bms = [dict(_VB, id="pv-site")]
    assert _clash(c.post('/api/endpoints', json=PLANT))
    cfg.virtual_bms = [dict(_VB, id="other")]
    assert c.post('/api/endpoints', json=PLANT).status_code == 200


def test_an_installation_edit_cannot_add_a_unit_under_a_virtual_bms_id(tmp_path):
    from tests.test_endpoint_edit_guard import PLANT
    from tests.test_endpoints import make_app
    cfg, c = make_app(tmp_path)
    assert c.post('/api/endpoints', json=PLANT).status_code == 200
    cfg.virtual_bms = [dict(_VB, id="pv-u3")]
    body = copy.deepcopy(PLANT)
    body['groups'][0]['units'] = [1, 2, 3]
    assert _clash(c.put('/api/endpoints/pv', json=body))


def test_a_component_cannot_take_a_virtual_bms_id(tmp_path):
    from tests.test_device_components import _app as _gx_app
    cfg, c = _gx_app(tmp_path)
    cfg.virtual_bms = [dict(_VB, id="gx-temperature-23"), dict(_VB, id="named")]
    assert _clash(c.post('/api/devices/gx/components', json={"type": "temperature", "instance": "23"}))
    assert _clash(c.post('/api/devices/gx/components',
                         json={"type": "temperature", "instance": "24", "id": "named"}))
    assert c.post('/api/devices/gx/components', json={"type": "temperature", "instance": "25"}).status_code == 200


def test_a_restored_device_cannot_bring_back_a_component_under_a_virtual_bms_id(tmp_path):
    from tests.test_device_components import _app as _gx_app
    cfg, c = _gx_app(tmp_path)
    assert c.delete('/api/devices/gx').status_code == 200
    cfg.virtual_bms = [dict(_VB, id="ruuvi-in")]                  # taken while it was deleted
    assert _clash(c.post('/api/devices/gx/restore'))
    assert cfg.get_device('gx') is None


def test_a_virtual_bms_cannot_take_a_unit_or_a_component_id(tmp_path):
    """The other direction, through the real API: units and components are devices."""
    from tests.test_device_components import _app as _gx_app
    cfg, app, c = _app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    r = c.post('/api/vbms', json=dict(_VB, id="pv-u2", channel="can9"))
    assert r.status_code == 422 and 'already a device' in r.text, r.text
    (tmp_path / "gx").mkdir()
    cfg2, c2 = _gx_app(tmp_path / "gx")
    r = c2.post('/api/vbms', json=dict(_VB, id="ruuvi-in", channel="can9"))
    assert r.status_code == 422 and 'already a device' in r.text, r.text


def _bundle_errors(doc, live=None):
    from multibus.lifecycle.snapshots import validate_bundle
    return [e for e in validate_bundle(zipfile.ZipFile(io.BytesIO(_bundle(doc))), live=live)
            if 'Virtual BMS' in e or 'virtual_bms' in e]


def test_a_bundle_cannot_put_a_virtual_bms_on_a_unit_or_a_component_id(tmp_path):
    from tests.test_device_components import GX_YAML
    plant = yaml.safe_load(PLANT_YAML)
    assert _bundle_errors({**plant, 'virtual_bms': [dict(_VB, id='other')]}) == []
    assert _bundle_errors({**plant, 'virtual_bms': [dict(_VB, id='pv-u2')]})
    gx = yaml.safe_load(GX_YAML)
    assert _bundle_errors({**gx, 'virtual_bms': [dict(_VB, id='gx-battery-288')]})
    assert _bundle_errors({**gx, 'virtual_bms': [dict(_VB, id='ruuvi-in')]})
    from multibus.outputs.vbms_registry import namespace_errors
    assert namespace_errors({'devices': 5, 'endpoints': [{'id': 'x', 'units': 7, 'groups': 'g'}],
                             'virtual_bms': [dict(_VB, id='x-u1')]}) == []    # malformed: no crash


def test_a_merge_import_cannot_bring_a_unit_under_the_live_virtual_bms_id(tmp_path):
    """An import merges: a bundle without `virtual_bms` keeps the live instances — its units
    are judged against them, before anything is written."""
    cfg, app, c = _app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    cfg.save_yaml_config()
    doc = _doc(cfg)
    live = copy.deepcopy(doc)
    live['virtual_bms'] = [dict(_VB, id='pv-u3')]
    assert _bundle_errors(_units(copy.deepcopy(doc), [1, 2, 3]), live=live)
    assert _bundle_errors(_units(copy.deepcopy(doc), [1, 2]), live=live) == []
    # through the route: the live file holds the instance, the bundle adds unit 3
    cfg.config_path.write_text(yaml.safe_dump(live))
    before = cfg.config_path.read_text()
    r = c.post('/api/config/import?lock_writes=false', content=_bundle(_units(doc, [1, 2, 3])))
    assert r.status_code == 422 and 'Virtual BMS' in r.text, r.text
    assert cfg.config_path.read_text() == before


def test_the_primary_id_is_a_device_id_for_a_virtual_bms():
    """review L1: `umg512` is the legacy primary's id, reserved whether or not there is one."""
    assert _bundle_errors({'virtual_bms': [dict(_VB, id='umg512')]})


def test_a_padded_virtual_bms_id_is_judged_as_stripped():
    """review L2: " inv" is judged as "inv", the id it would be stored under."""
    from multibus.outputs.vbms_registry import validate_instance
    errs = validate_instance({"id": " inv", "profile": "p", "channel": "can1", "packs": ["p1"]},
                             existing_ids=[], registry=[], sources=[{"id": "p1"}],
                             profile_is_emulator=lambda n: True, device_ids=["inv"])
    assert any('already a device' in e for e in errs), errs


def _apply(tmp_path, doc, *, replace_config):
    from multibus.lifecycle.snapshots import apply_bundle
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"virtual_bms": []}))
    return apply_bundle(zipfile.ZipFile(io.BytesIO(_bundle(doc))), cfg_dir=tmp_path, user_tpl_dir=tmp_path,
                        registers_path_for=lambda d: tmp_path / f"{d}.json", replace_config=replace_config,
                        allow_nonlan=False, snapshot_store=SimpleNamespace(create=lambda *a, **k: None),
                        pre_label="pre", user="u", reload_from_disk=lambda apply, restore_identity: False,
                        apply=True, restore_identity=False)


def test_restoring_an_own_snapshot_taken_with_a_collision_is_not_blocked(tmp_path, caplog):
    """review L3: a snapshot the gateway took while a collision already existed is the way
    back — restored, the collision logged; an import of the same bundle stays refused."""
    import logging

    from multibus.lifecycle.snapshots import BundleInvalid
    doc = {'devices': [{'id': 'bank'}], 'virtual_bms': [dict(_VB, id='bank')]}
    with pytest.raises(BundleInvalid):
        _apply(tmp_path, doc, replace_config=False)
    with caplog.at_level(logging.WARNING):
        _apply(tmp_path, doc, replace_config=True)
    assert yaml.safe_load((tmp_path / "config.yaml").read_text())['virtual_bms'][0]['id'] == 'bank'
    assert any('Virtual BMS' in r.getMessage() or 'virtual_bms' in r.getMessage() for r in caplog.records)


def test_a_collision_already_in_the_file_is_said_at_start(tmp_path, caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        cfg, app, c = _app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
        cfg.virtual_bms = [dict(_VB, id='pv-u2')]
        from multibus.api import create_api
        create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    assert any("pv-u2" in r.getMessage() and "Virtual BMS" in r.getMessage() for r in caplog.records)


# ── from the review of the corrections themselves (2026-11-13) ───────────────────────────

@pytest.mark.parametrize("spelled", [{"tls": False}, {"port": 1883}, {"port": "1883"},
                                     {"tls": False, "port": 1883}])
def test_a_default_written_out_is_not_a_retarget(spelled):
    """`tls: false` and `port: 1883` are what an absent key means: an import that spells them
    out must not refuse every write to that device as "retargeted"."""
    base = {"broker": "a.lan", "topic": "N/1/#"}
    a = _dev("x", host="", protocol="mqtt", mqtt_in=dict(base))
    b = _dev("x", host="", protocol="mqtt", mqtt_in={**base, **spelled})
    assert device_wire_identity(a) == device_wire_identity(b)
    c = _dev("x", host="", protocol="mqtt", mqtt_in={**base, "port": 8883, "tls": True})
    assert device_wire_identity(a) != device_wire_identity(c)


MIXED_YAML = PLANT_YAML + '''
devices:
  - id: lone
    name: A device of its own
    connection: {host: 192.0.2.50, port: 502, unit_id: 3}
'''


def test_the_forced_lock_covers_the_primary_a_plain_device_and_every_endpoint_unit(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=MIXED_YAML)
    kinds = {('primary' if d.primary else 'unit' if d.endpoint_id else 'own') for d in cfg.devices}
    assert kinds == {'primary', 'unit', 'own'}
    cfg.force_lock_all_writes()
    assert all(d.write_locked for d in cfg.devices), [(d.id, d.write_locked) for d in cfg.devices]
    again = Config(str(cfg.config_path))                          # and it is what the file says
    assert all(d.write_locked for d in again.devices)
    assert again.security.primary_write_locked is True


def test_a_later_import_that_writes_nothing_does_not_take_back_an_earlier_pending_lock(tmp_path, monkeypatch):
    """Import #1's lock failed: its bundle is on disk with the marker. Import #2 is rejected
    before writing anything — the marker of #1 must survive, or the next boot is unlocked."""
    from multibus.lifecycle import snapshots
    cfg, app, c = _app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    cfg.save_yaml_config()
    cfg.lock_pending_path().write_text("pre-import\n")           # what import #1 left behind

    def refuse(*a, **k):
        raise ValueError("unsafe path in bundle")
    monkeypatch.setattr(snapshots, 'write_bundle_files', refuse)
    r = c.post('/api/config/import', content=_bundle(_doc(cfg)))
    assert r.status_code == 422, r.text
    assert cfg.lock_pending_path().exists()


def test_an_import_that_writes_nothing_leaves_no_marker_of_its_own(tmp_path, monkeypatch):
    from multibus.lifecycle import snapshots
    cfg, app, c = _app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    cfg.save_yaml_config()
    monkeypatch.setattr(snapshots, 'write_bundle_files',
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("unsafe path in bundle")))
    assert c.post('/api/config/import', content=_bundle(_doc(cfg))).status_code == 422
    assert not cfg.lock_pending_path().exists()


def test_the_master_switch_cannot_be_turned_on_over_a_pending_forced_lock(tmp_path):
    cfg, app, c = _app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    cfg.security.allow_writes = False
    cfg.lock_pending_path().write_text("pre-import\n")
    r = c.post('/api/config/security', json={"allow_writes": True})
    assert r.status_code == 409 and 'pending' in r.text, r.text
    assert cfg.security.allow_writes is False
    cfg.lock_pending_path().unlink()
    assert c.post('/api/config/security', json={"allow_writes": True}).status_code == 200


def test_a_meter_link_edit_after_a_retarget_keeps_the_guard(tmp_path):
    """The second config-only caller (routes/meters.py): at registry level, what it does —
    replace WITHOUT a client — keeps the record of the wire the client was built on."""
    r = DeviceRegistry("p", {})
    r.register(_dev("a"), "CLIENT")
    r.replace("a", _dev("a", host="9.9.9.9"), add_if_missing=False)
    assert r.applied_wire["a"] == device_wire_identity(_dev("a"))
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "multibus"
    for rel in ("routes/meters.py", "api/command_service.py"):
        src = (root / rel).read_text()
        assert "registry.replace(device_id, new_cfg, client=client" not in src, rel


def test_an_unstartable_device_is_not_started_again_on_every_apply(tmp_path):
    """A configured device with nothing to run (its builder returns no client) is tried once,
    not on every import — each try cancels its queued commands and re-seeds its registers."""
    cfg, app, c = _app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    cfg.save_yaml_config()
    tries = []
    ds_start = app.state.reconcile_devices
    reg = app.state.registry
    orig_replace = reg.replace

    def counting_replace(did, new_cfg, *a, **k):
        if 'client' in k:
            tries.append(did)
        return orig_replace(did, new_cfg, *a, **k)
    reg.replace = counting_replace
    doc = _doc(cfg)
    reg._pairs = [(d, None if d.id == 'pv-u2' else cl) for d, cl in reg._pairs]   # pv-u2: no client
    import multibus.devices.device_runtime as rt
    real = rt.build_device_client
    rt.build_device_client = lambda *a, **k: None
    try:
        for _ in range(3):
            assert c.post('/api/config/import?lock_writes=false', content=_bundle(doc)).status_code == 200
    finally:
        rt.build_device_client = real
    assert tries.count('pv-u2') == 1 and ds_start is not None
