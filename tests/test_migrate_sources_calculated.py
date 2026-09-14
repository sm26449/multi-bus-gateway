# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Units read through sources get their template's derived measurements at
the device level — where the calc engine reads them — without touching the
source files or a hand-written formula."""
import json

from multibus.device_template import TemplateRegistry
from scripts.migrate_sources_calculated import plan
from tests.test_commands_api import PLANT_YAML
from tests.test_devices import write_config


def test_sources_units_gain_the_templates_calculated_entries(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    reg = TemplateRegistry()
    p = plan(cfg, reg)
    by = {d: (path, data, tpl, missing) for d, path, data, tpl, missing in p}
    assert set(by) == {'pv-u1', 'pv-u2'}
    path, data, tpl, missing = by['pv-u1']
    assert tpl == 'fronius_sunspec_inverter' and missing == ['status_text', 'status_alarm', 'status_active']
    assert path == cfg.device_registers_path('pv-u1') and data['registers'] == []
    # write as --apply would, then the loader sees them and the source files are untouched
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    assert [c['name'] for c in cfg.load_calculated('pv-u1')] == ['status_text', 'status_alarm', 'status_active']
    assert cfg.load_calculated('pv-u1')[0]['topic'] == 'status/text'
    # re-running changes nothing; a hand-written formula stays first
    data['calculated'].insert(0, {'name': 'mine', 'expr': 'power_active_total * 2', 'poll_group': 'normal'})
    path.write_text(json.dumps(data))
    p2 = {d: missing for d, _p, _d, _t, missing in plan(cfg, reg)}
    assert p2['pv-u1'] == [] and p2['pv-u2'] == ['status_text', 'status_alarm', 'status_active']
    assert cfg.load_calculated('pv-u1')[0]['name'] == 'mine'
