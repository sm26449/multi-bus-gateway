"""Explicit-run adversarial contract probes; unresolved contracts fail on purpose.

Run this file explicitly with pytest. Its probe_ prefix keeps it outside normal
test discovery. No southbound I/O; in-memory clients only.
"""
import io
import json
import threading
import zipfile
from unittest.mock import Mock

import pytest
import yaml

from tests.test_commands_api import _app, _Mqtt
from tests.test_commands import _Inverter


def test_waiting_named_command_obeys_real_lock_route(tmp_path):
    inv = _Inverter(sf=-2)
    _, app, client = _app(tmp_path, {'pv-u1': inv})
    dev, driver = app.state.registry.find('pv-u1')[1:]
    lock_for = app.state.cmd_lock_for
    lock = lock_for(driver, dev)
    entered = threading.Event()

    class BarrierLock:
        def acquire(self, *a, **kw):
            entered.set()
            return lock.acquire(*a, **kw)

        def release(self):
            lock.release()

    table = next(c.cell_contents for c in lock_for.__closure__
                 if isinstance(c.cell_contents, dict))
    key = next(k for k, v in table.items() if v is lock)
    table[key] = BarrierLock()
    answer = []
    lock.acquire()
    worker = threading.Thread(target=lambda: answer.append(client.post(
        '/api/devices/pv-u1/commands/power_limit', json={'value': 60})))
    try:
        worker.start()
        assert entered.wait(5), 'command did not reach bus lock'
        r = client.post('/api/devices/pv-u1/write-lock', json={'locked': True})
        assert r.status_code == 200, r.text
        assert app.state.registry.find('pv-u1')[1].write_locked
        assert app.state.registry.find('pv-u1')[1] is not dev
    finally:
        lock.release()
        worker.join(10)
        app.state.command_dispatch.drain(10)
    assert not worker.is_alive()
    assert inv.writes == [], (answer[0].json(), inv.writes)


@pytest.mark.parametrize('kind', ['tap', 'seplos'])
def test_monotonic_register_decodes(kind):
    if kind == 'tap':
        from tests.test_rtu_tap import make_pair, reg
        _, client = make_pair('/dev/review-not-opened', registers=[reg(0, 'energy', monotonic=True)])
        client._ingest_window(0, [100], 1.0)
    else:
        from tests.test_seplos_v2 import conn, reg
        from multibus.seplos_v2 import SeplosV2Client
        r = reg(0, 'energy', monotonic=True)
        client = SeplosV2Client(conn(), [r], {}, device_id='review')
        client.words[0] = 100
        client._values([r], 1.0)


def test_sanitized_top_level_url_keeps_secret(tmp_path):
    from multibus.snapshots import write_bundle_files
    from multibus.core.redact import redact_url
    original = 'http://192.168.1.2/ingest?token=private-value'
    assert redact_url(original) != original
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump({'rest_push': {'url': original}}))
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as z:
        z.writestr('config.yaml', yaml.safe_dump({'rest_push': {'url': redact_url(original)}}))
    with zipfile.ZipFile(io.BytesIO(data.getvalue())) as z:
        write_bundle_files(z, cfg_dir=tmp_path, user_tpl_dir=tmp_path / 'tpl',
                           registers_path_for=lambda d: tmp_path / d, replace_config=False)
    assert yaml.safe_load((tmp_path / 'config.yaml').read_text())['rest_push']['url'] == original


def test_bad_numeric_ha_command_publishes_rejection(tmp_path):
    mq = _Mqtt()
    _, app, _ = _app(tmp_path, {'pv-u1': _Inverter(sf=-2)}, mqtt=mq)
    from multibus.config import SelectedRegister
    reg = SelectedRegister(address=40232, name='power_limit_pct', label='', unit='',
                           data_type='uint16', poll_group='normal')
    app.state.mqtt_write_command('pv-u1', reg, 'abc')
    results = [json.loads(body) for topic, body, _ in mq.sent if topic.endswith('/cmd/result')]
    assert results and results[-1]['status'] == 'rejected', mq.sent


def test_snapshot_failure_aborts_before_bundle_write(tmp_path):
    from multibus.snapshots import apply_bundle
    from multibus.lifecycle.snapshots import PreopSnapshotFailed
    (tmp_path / 'config.yaml').write_text('mqtt: {topic_prefix: original}\n')
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as z:
        z.writestr('config.yaml', 'mqtt: {topic_prefix: changed}\n')
    reload = Mock()
    store = Mock()
    store.create.side_effect = OSError('simulated snapshot failure')
    with zipfile.ZipFile(io.BytesIO(data.getvalue())) as z:
        try:
            apply_bundle(z, cfg_dir=tmp_path, user_tpl_dir=tmp_path / 'tpl',
                         registers_path_for=lambda d: tmp_path / d, replace_config=True,
                         allow_nonlan=False, snapshot_store=store, pre_label='pre-restore',
                         user='review', reload_from_disk=reload, apply=False, restore_identity=False)
        except (OSError, PreopSnapshotFailed):   # C3 fail-closed raises the latter
            pass
    assert 'original' in (tmp_path / 'config.yaml').read_text()
    reload.assert_not_called()


def test_waiting_raw_write_obeys_real_lock_route(tmp_path, monkeypatch):
    from tests.test_raw_dispatch import _gw, _reg, _results
    g = _gw(tmp_path, monkeypatch)
    g.cfg.upsert_raw_device({'id': 'ctrl', 'template': 'ctrl_tpl',
                             'connection': {'protocol': 'tcp', 'host': '192.0.2.1'},
                             'mqtt': {'topic_prefix': 't/ctrl'}})
    g.app.state.registry.resync(g.cfg.devices, None)
    dev, driver = g.app.state.registry.find('ctrl')[1:]
    lock_for = g.app.state.cmd_lock_for
    lock = lock_for(driver, dev)
    entered = threading.Event()

    class BarrierLock:
        def acquire(self, *a, **kw):
            entered.set()
            return lock.acquire(*a, **kw)

        def release(self):
            lock.release()

    table = next(c.cell_contents for c in lock_for.__closure__ if isinstance(c.cell_contents, dict))
    key = next(k for k, v in table.items() if v is lock)
    table[key] = BarrierLock()
    lock.acquire()
    try:
        g.handler('ctrl', _reg(200, 'setpoint'), '5')
        assert entered.wait(5), 'raw write did not reach bus lock'
        r = g.client.post('/api/devices/ctrl/write-lock', json={'locked': True})
        assert r.status_code == 200, r.text
        assert g.app.state.registry.find('ctrl')[1].write_locked
        assert g.app.state.registry.find('ctrl')[1] is not dev
    finally:
        lock.release()
        assert g.cd.drain(10)
    assert not g.fakes['ctrl'].writes, (g.fakes['ctrl'].writes, _results(g))


@pytest.mark.parametrize('operation', ['import', 'restore'])
def test_bundle_lock_binds_running_nonprimary(tmp_path, operation):
    inv = _Inverter(sf=-2)
    cfg, app, client = _app(tmp_path, {'pv-u1': inv})
    assert client.post('/api/devices/pv-u1/write-lock', json={'locked': True}).status_code == 200
    snap = client.post('/api/config/snapshots', json={'label': 'locked'})
    assert snap.status_code == 200, snap.text
    sid = snap.json()['snapshot']['id']
    assert client.post('/api/devices/pv-u1/write-lock', json={'locked': False}).status_code == 200
    if operation == 'restore':
        r = client.post(f'/api/config/snapshots/{sid}/restore?apply=false')
    else:
        data = io.BytesIO()
        doc = yaml.safe_load(cfg.config_path.read_text())
        doc['endpoints'][0]['write_locked'] = True
        with zipfile.ZipFile(data, 'w') as z:
            z.writestr('config.yaml', yaml.safe_dump(doc))
        r = client.post('/api/config/import?apply=false', content=data.getvalue())
    assert r.status_code == 200, r.text
    assert cfg.get_device('pv-u1').write_locked
    command = client.post('/api/devices/pv-u1/commands/power_limit', json={'value': 60})
    assert command.status_code == 403 and not inv.writes, {
        'config_locked': cfg.get_device('pv-u1').write_locked,
        'registry_locked': app.state.registry.find('pv-u1')[1].write_locked,
        'command': command.json(), 'writes': inv.writes}


def test_single_nonprimary_import_requests_restart(tmp_path):
    from fastapi.testclient import TestClient
    from multibus.api import create_api
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml='''
primary_device: false
devices:
  - id: ctrl
    connection: {protocol: tcp, host: 192.168.1.2}
''')
    fake = Mock()
    app, _ = create_api(cfg, None, None, None, devices=[(cfg.get_device('ctrl'), fake)])
    client = TestClient(app, raise_server_exceptions=False)
    assert len(cfg.devices) == 1 and not cfg.devices[0].primary
    data = io.BytesIO()
    doc = yaml.safe_load(cfg.config_path.read_text())
    doc['devices'][0]['connection']['host'] = '192.168.1.3'
    with zipfile.ZipFile(data, 'w') as z:
        z.writestr('config.yaml', yaml.safe_dump(doc))
    r = client.post('/api/config/import?apply=false', content=data.getvalue())
    assert r.status_code == 200, r.text
    assert r.json()['restart_required'] is True, r.json()


def test_failure_after_first_file_leaves_no_partial_bundle(tmp_path, monkeypatch):
    import multibus.lifecycle.snapshots as snapshots
    (tmp_path / 'config.yaml').write_text('mqtt: {topic_prefix: original}\n')
    (tmp_path / 'attributes.yaml').write_text('original attributes')
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as z:
        z.writestr('config.yaml', 'mqtt: {topic_prefix: changed}\n')
        z.writestr('attributes.yaml', '{}')
    original_replace = snapshots.os.replace

    def fail_second(src, dst):
        if str(dst).endswith('attributes.yaml'):
            raise OSError('simulated second-file failure')
        return original_replace(src, dst)

    monkeypatch.setattr(snapshots.os, 'replace', fail_second)
    reload = Mock()
    with zipfile.ZipFile(io.BytesIO(data.getvalue())) as z:
        with pytest.raises(OSError, match='second-file failure'):
            snapshots.apply_bundle(z, cfg_dir=tmp_path, user_tpl_dir=tmp_path / 'tpl',
                                   registers_path_for=lambda d: tmp_path / d, replace_config=True,
                                   allow_nonlan=False, snapshot_store=Mock(), pre_label='pre-restore',
                                   user='review', reload_from_disk=reload, apply=False, restore_identity=False)
    reload.assert_not_called()
    assert 'original' in (tmp_path / 'config.yaml').read_text()


def test_control_lock_before_admission_blocks_command(tmp_path):
    inv = _Inverter(sf=-2)
    _, _, client = _app(tmp_path, {'pv-u1': inv})
    assert client.post('/api/devices/pv-u1/write-lock', json={'locked': True}).status_code == 200
    result = client.post('/api/devices/pv-u1/commands/power_limit', json={'value': 60})
    assert result.status_code == 403, result.text
    assert not inv.writes


def test_control_raw_numeric_refusal_publishes(tmp_path, monkeypatch):
    from tests.test_raw_dispatch import _gw, _reg, _results
    g = _gw(tmp_path, monkeypatch)
    g.handler('ctrl', _reg(200, 'setpoint'), 'abc')
    results = _results(g)
    assert results and results[-1]['status'] == 'rejected'
    assert not g.fakes['ctrl'].writes


@pytest.mark.parametrize('kind', ['tap', 'seplos'])
def test_control_nonmonotonic_register_decodes(kind):
    if kind == 'tap':
        from tests.test_rtu_tap import make_pair, reg
        _, client = make_pair('/dev/review-control-not-opened', registers=[reg(0, 'energy')])
        client.publish_callback = Mock()
        client._ingest_window(0, [100], 1.0)
        client.publish_callback.assert_called_once()
    else:
        from tests.test_seplos_v2 import conn, reg
        from multibus.seplos_v2 import SeplosV2Client
        r = reg(0, 'energy')
        client = SeplosV2Client(conn(), [r], {}, device_id='review-control')
        client.words[0] = 100
        assert client._values([r], 1.0)
