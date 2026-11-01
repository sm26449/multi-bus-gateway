# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""QA-R1-04: changing one poll group's interval keeps the description the
device page showed for it.

A device whose registers file does not hold a group yet shows the gateway's
group (interval and description). Saving a new interval writes only the
interval for that group; reading it back used to build the group from the
file alone, so its description became '' — the page lost "Standard
measurements" on a plain interval edit (devices_step3_e2e)."""
from tests.test_devices import write_config

EXTRA = """
devices:
  - id: rs
    name: Serial meter
    template: fronius_smart_meter_65a
    enabled: false
    connection: {protocol: tcp, host: 127.0.0.1, port: 15995, unit_id: 1}
"""


def _groups(cfg, dev_id):
    dev = next(d for d in cfg.devices if d.id == dev_id)
    _regs, groups = cfg.load_device_registers(dev)
    return {n: (g.interval, g.description) for n, g in groups.items()}


def test_an_interval_edit_keeps_the_description_shown(tmp_path):
    cfg = write_config(tmp_path, EXTRA)
    before = _groups(cfg, "rs")
    assert before["normal"][1]                      # the page shows a description

    cfg.save_device_poll_groups("rs", {"normal": {"interval": 7}})
    after = _groups(cfg, "rs")

    assert after["normal"] == (7, before["normal"][1])
    assert after["realtime"] == before["realtime"] and after["slow"] == before["slow"]


def test_a_description_sent_still_wins(tmp_path):
    cfg = write_config(tmp_path, EXTRA)
    cfg.save_device_poll_groups("rs", {"normal": {"interval": 7, "description": "mine"}})
    assert _groups(cfg, "rs")["normal"] == (7, "mine")
    cfg.save_device_poll_groups("rs", {"normal": {"interval": 7, "description": ""}})
    assert _groups(cfg, "rs")["normal"] == (7, "")      # cleared on purpose stays cleared
