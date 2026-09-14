# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""A unit read through several sources has one register list for its
consumers — HA discovery published only the connectivity sensor of the pv
units because it read the (empty) device-level file."""
import json

from tests.test_commands_api import PLANT_YAML
from tests.test_devices import write_config


def _sel(path, regs):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": "1.0", "poll_groups": {}, "registers": [
        {"address": a, "name": n, "label": n, "unit": "", "data_type": "uint16", "poll_group": "normal",
         "mqtt": {"enabled": True, "topic": t}} for a, n, t in regs]}))


def test_unit_registers_merge_the_sources_one_entry_per_name(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    u1 = next(d for d in cfg.devices if d.id == 'pv-u1')
    _sel(cfg.source_registers_path('pv-u1', 'solar_api'), [(1, 'power_active_total', 'power/active/total'), (2, 'energy_active_generated', 'energy/active/generated')])
    _sel(cfg.source_registers_path('pv-u1', 'sunspec'), [(40083, 'power_active_total', 'power/active/total'), (40092, 'voltage_l1_n', 'voltage/l1_n')])
    regs = cfg.unit_registers(u1)
    assert [r.name for r in regs] == ['power_active_total', 'energy_active_generated', 'voltage_l1_n']
    assert regs[0].address == 1                       # the first source that declares a name wins
    assert cfg.load_device_registers(u1)[0] == []     # the device-level file is still empty
    # a unit with no sources is its own single list
    u2 = next(d for d in cfg.devices if d.id == 'pv-u2')
    _sel(cfg.device_registers_path('pv-u2'), [(40083, 'power_active_total', 'power/active/total')])
    u2.sources = []
    assert [r.name for r in cfg.unit_registers(u2)] == ['power_active_total']
