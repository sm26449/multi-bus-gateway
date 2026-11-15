"""Explicit-run safety probe: imported target changes must not write the old wire.

Uses in-memory devices only; no production/network I/O.
"""
import io
import zipfile
import yaml
import pytest
from tests.test_commands_api import _app
from tests.test_commands import _Inverter


@pytest.mark.parametrize("apply", [False, True])
def test_import_retarget_cannot_write_old_wire(tmp_path, apply):
    old_wire = _Inverter(sf=-2)
    cfg, app, client = _app(tmp_path, {'pv-u1': old_wire})
    cfg.save_yaml_config()  # keep the same authenticated write policy across import
    old_client = app.state.registry.find('pv-u1')[2]
    doc = yaml.safe_load(cfg.config_path.read_text())
    for src in doc['endpoints'][0]['groups'][0]['sources']:
        if src.get('protocol') == 'tcp':
            assert src['host'] == '192.0.2.9'
            src['host'] = '192.0.2.99'
    b = io.BytesIO()
    with zipfile.ZipFile(b, 'w') as z:
        z.writestr('config.yaml', yaml.safe_dump(doc))
    r = client.post('/api/config/import?apply=' + str(apply).lower(), content=b.getvalue())
    assert r.status_code == 200, r.text
    new_cfg, running_client = app.state.registry.find('pv-u1')[1:]
    assert r.json()['restart_required'] is True
    assert next(s.connection.host for s in new_cfg.sources if s.protocol == 'tcp') == '192.0.2.99'
    assert running_client is old_client
    command = client.post('/api/devices/pv-u1/commands/power_limit', json={'value': 60})
    assert not old_wire.writes, {'import': r.json(), 'command': command.json(), 'old_wire_writes': old_wire.writes}
