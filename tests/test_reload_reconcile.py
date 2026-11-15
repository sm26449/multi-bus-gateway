# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The reload → resync → reconcile wiring through a real import (ADR-0047 P2; FINDINGS
RECON-TEST-1). `tests/test_reconcile.py` holds the pure diff; these hold what the running
gateway does with it, in the order it does it."""
import io
import zipfile

import pytest
import yaml

from tests.test_commands import _Inverter
from tests.test_commands_api import _app
from tests.test_devices_api import needs_tc


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


def _spy_on_reconcile(app, device_id):
    """What the registry holds for ``device_id`` right AFTER the reconcile ran."""
    seen = {}
    orig = app.state.reconcile_devices

    def spy(*a, **k):
        out = orig(*a, **k)
        _i, dev, cl = app.state.registry.find(device_id)
        seen.update(present=dev is not None, locked=bool(dev and dev.write_locked),
                    client=cl is not None)
        return out
    app.state.reconcile_devices = spy
    return seen


# ── RECON-F20-1: the forced lock lands before the reconcile starts anything ──────────────

@needs_tc
def test_a_device_added_by_an_import_is_locked_before_its_client_starts(tmp_path):
    """F20 default (lock_writes on). The reconcile starts the ADDED device inside the reload;
    the forced lock must already be on it then — not only once the reload has returned."""
    cfg, app, client = _app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    cfg.save_yaml_config()
    seen = _spy_on_reconcile(app, 'pv-u3')
    r = client.post('/api/config/import', content=_bundle(_units(_doc(cfg), [1, 2, 3])))
    assert r.status_code == 200, r.text
    assert seen == {'present': True, 'locked': True, 'client': True}, seen


@needs_tc
def test_the_registry_is_never_rebound_to_an_unlocked_import(tmp_path):
    """The running registry is re-bound (resync) to the reloaded config only after the
    forced lock: no already-running device is ever paired with the bundle's unlocked state."""
    inv = _Inverter(sf=-2)
    cfg, app, client = _app(tmp_path, {'pv-u1': inv})
    cfg.save_yaml_config()
    registry = app.state.registry
    bound = []
    orig = registry.resync

    def spy(device_cfgs, primary_client):
        bound.append({d.id: d.write_locked for d in device_cfgs})
        return orig(device_cfgs, primary_client)
    registry.resync = spy
    r = client.post('/api/config/import', content=_bundle(_doc(cfg)))
    assert r.status_code == 200, r.text
    assert bound and all(all(state.values()) for state in bound), bound
    command = client.post('/api/devices/pv-u1/commands/power_limit', json={'value': 60})
    assert command.status_code == 403 and 'lock' in command.json()['reason'].lower()
    assert not inv.writes


@needs_tc
def test_an_import_that_opts_out_of_the_lock_leaves_the_added_device_unlocked(tmp_path):
    """lock_writes=false is the operator's explicit choice — the hook must not lock then."""
    cfg, app, client = _app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    cfg.save_yaml_config()
    seen = _spy_on_reconcile(app, 'pv-u3')
    r = client.post('/api/config/import?lock_writes=false',
                    content=_bundle(_units(_doc(cfg), [1, 2, 3])))
    assert r.status_code == 200, r.text
    assert seen == {'present': True, 'locked': False, 'client': True}, seen


# ── membership through the reload (RECON-REMOVE-1 / RECON-REMOVE-2) ──────────────────────

@needs_tc
@pytest.mark.parametrize('apply_flag', ['true', 'false'])
def test_a_removed_device_is_stopped_whether_or_not_the_import_applies(tmp_path, apply_flag):
    """resync drops a removed device from the registry on EVERY reload, so its client must be
    disconnected on every reload too — else it runs on with nothing able to reach it."""
    cfg, app, client = _app(tmp_path, {'pv-u1': _Inverter(sf=-2), 'pv-u2': _Inverter(sf=-2)})
    cfg.save_yaml_config()
    old_client = app.state.registry.find('pv-u2')[2]
    assert old_client.connected
    r = client.post(f'/api/config/import?lock_writes=false&apply={apply_flag}',
                    content=_bundle(_units(_doc(cfg), [1])))
    assert r.status_code == 200, r.text
    assert app.state.registry.find('pv-u2')[1] is None
    assert old_client.connected is False
    assert app.state.registry.store_for('pv-u2') is None
    assert app.state.registry.find('pv-u1')[2] is not None          # the kept device runs on


@needs_tc
def test_an_added_device_is_started_on_apply(tmp_path):
    cfg, app, client = _app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    cfg.save_yaml_config()
    kept = app.state.registry.find('pv-u1')[2]
    r = client.post('/api/config/import?lock_writes=false',
                    content=_bundle(_units(_doc(cfg), [1, 2, 3])))
    assert r.status_code == 200, r.text
    assert app.state.registry.find('pv-u3')[2] is not None
    assert app.state.registry.find('pv-u1')[2] is kept              # not rebuilt


@needs_tc
def test_an_added_device_waits_without_a_client_when_the_import_does_not_apply(tmp_path):
    """apply=false saves the bundle; nothing new is started until an apply or a restart."""
    cfg, app, client = _app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    cfg.save_yaml_config()
    r = client.post('/api/config/import?lock_writes=false&apply=false',
                    content=_bundle(_units(_doc(cfg), [1, 2, 3])))
    assert r.status_code == 200, r.text
    _i, dev3, cl = app.state.registry.find('pv-u3')
    assert dev3 is not None and cl is None
    assert 'pv-u3' not in app.state.applied_wire


# ── RECON-TRUNC-1: a load that failed is not a statement about which devices exist ───────

@needs_tc
@pytest.mark.parametrize('keep_good', [True, False])
def test_a_reload_whose_load_failed_stops_nothing(tmp_path, keep_good):
    """The file is unreadable by the time the reload loads it (a torn write, a bad disk). The
    load is refused and the running config kept (ADR-0047 §1); the reload must then answer 500
    and leave the runtime alone — never read the running devices as removed."""
    cfg, app, client = _app(tmp_path, {'pv-u1': _Inverter(sf=-2), 'pv-u2': _Inverter(sf=-2)})
    cfg.save_yaml_config()
    bundle = _bundle(_doc(cfg))
    running = {i: app.state.registry.find(i)[2] for i in ('pv-u1', 'pv-u2')}
    orig = cfg.load

    def torn_load():
        if not keep_good:
            cfg.config_path.with_suffix('.yaml.good').unlink(missing_ok=True)
        cfg.config_path.write_text("modbus: [unclosed\n")
        return orig()
    cfg.load = torn_load
    r = client.post('/api/config/import?lock_writes=false', content=bundle)
    assert r.status_code == 500, r.text
    assert r.json()['detail']['written'] is True and r.json()['detail']['snapshot'] == 'pre-import'
    for did, cl in running.items():
        assert app.state.registry.find(did)[2] is cl, did
        assert cl.connected, did


# ── the same wiring through a SNAPSHOT RESTORE (it shares apply_bundle + the reload) ─────

def _snapshot(client, note):
    r = client.post('/api/config/snapshots', json={'note': note})
    assert r.status_code == 200, r.text
    return r.json()['snapshot']['id']


@needs_tc
@pytest.mark.parametrize('apply_flag', ['true', 'false'])
def test_a_restore_stops_a_device_the_snapshot_does_not_have(tmp_path, apply_flag):
    cfg, app, client = _app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    cfg.save_yaml_config()
    assert client.post('/api/config/import?lock_writes=false',
                       content=_bundle(_units(_doc(cfg), [1]))).status_code == 200
    sid = _snapshot(client, 'one unit')
    assert client.post('/api/config/import?lock_writes=false',
                       content=_bundle(_units(_doc(cfg), [1, 2, 3]))).status_code == 200
    started = app.state.registry.find('pv-u3')[2]
    assert started is not None
    stopped = []
    orig = started.disconnect
    started.disconnect = lambda: (stopped.append(True), orig())[1]
    r = client.post(f'/api/config/snapshots/{sid}/restore?apply={apply_flag}&lock_writes=false')
    assert r.status_code == 200, r.text
    assert stopped and app.state.registry.find('pv-u3')[1] is None
    assert app.state.registry.store_for('pv-u3') is None
    assert 'pv-u3' not in app.state.applied_wire


@needs_tc
def test_a_restore_starts_a_device_the_snapshot_adds_and_locks_it_first(tmp_path):
    cfg, app, client = _app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    cfg.save_yaml_config()
    assert client.post('/api/config/import?lock_writes=false',
                       content=_bundle(_units(_doc(cfg), [1, 2, 3]))).status_code == 200
    sid = _snapshot(client, 'three units')
    assert client.post('/api/config/import?lock_writes=false',
                       content=_bundle(_units(_doc(cfg), [1]))).status_code == 200
    assert app.state.registry.find('pv-u3')[1] is None
    seen = _spy_on_reconcile(app, 'pv-u3')
    r = client.post(f'/api/config/snapshots/{sid}/restore')          # lock_writes defaults on
    assert r.status_code == 200, r.text
    assert seen == {'present': True, 'locked': True, 'client': True}, seen
    assert all(d.write_locked for d in cfg.devices)
    assert not cfg.lock_pending_path().exists()
