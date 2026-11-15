# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The in-process Virtual BMS executor (ADR-0037): VbmsManager builds instances from config,
sources live pack values through an injected read_value, applies the EMS control plane, and runs
an arm-gated executor thread per instance."""
import json
import pathlib
import time

import pytest

from multibus.codec.can_signal import decode_signal, parse_can_id
from multibus.outputs.vbms_manager import VbmsManager

_ROWS = json.loads((pathlib.Path(__file__).resolve().parent.parent / "multibus" / "device_templates"
                    / "pylontech_can_inverter.json").read_text())["device_template"]["registers"]

CONFIG = {"virtual_bms": [{
    "id": "deye-vbms", "channel": "can0", "profile": "pylontech_can_inverter",
    "packs": ["seplos-adr1", "seplos-adr2"], "period_s": 0.05,
    "bounds": {"charge_current_limit": [0, 80], "discharge_current_limit": [0, 180],
               "charge_enable": [0, 1], "discharge_enable": [0, 1]},
    "floor": {"charge_current_limit": 20, "discharge_current_limit": 40,
              "charge_voltage_limit": 57.6, "discharge_voltage_limit": 46.4,
              "charge_enable": 1, "discharge_enable": 1},
}]}

STORE = {}
for dev, soc, v, i in [("seplos-adr1", 90, 53.5, 10.0), ("seplos-adr2", 70, 53.4, 8.0)]:
    STORE.update({(dev, "soc"): soc, (dev, "pack_voltage"): v, (dev, "battery_current"): i,
                  (dev, "total_capacity"): 280, (dev, "max_cell_voltage"): 3.35,
                  (dev, "min_cell_voltage"): 3.33, (dev, "temp_max"): 23, (dev, "temp_min"): 22})


def _read(dev, field):
    return STORE.get((dev, field))


def _load(_name):
    return _ROWS


class FakeTx:
    instances = []
    def __init__(self, channel):
        self.channel = channel
        self.opened = self.closed = False
        self.sent = []
        FakeTx.instances.append(self)
    def open(self):
        self.opened = True
    def send(self, can_id, data):
        self.sent.append((can_id, data))
    def close(self):
        self.closed = True


def _mgr():
    return VbmsManager(CONFIG, _read, _load, tx_factory=FakeTx)


def test_configure_builds_instances_disarmed():
    m = _mgr()
    assert list(m.instances) == ["deye-vbms"]
    inst = m.instances["deye-vbms"]
    assert inst.armed is False and inst.packs == ["seplos-adr1", "seplos-adr2"]


def test_pack_values_read_from_the_store_via_field_map():
    m = _mgr()
    packs = m.pack_values(m.instances["deye-vbms"])
    assert len(packs) == 2
    assert packs[0]["soc"] == 90 and packs[0]["voltage"] == 53.5 and packs[0]["current"] == 10.0
    assert packs[0]["capacity_total"] == 280 and packs[0]["cell_v_max"] == 3.35


def test_values_aggregate_live_plus_control_floor():
    m = _mgr()
    vals = m.values(m.instances["deye-vbms"])
    assert vals["soc"] == 80                       # capacity-weighted (90 & 70)
    assert vals["battery_current"] == 18.0         # summed
    assert vals["charge_current_limit"] == 20      # floor (no EMS command yet)
    assert vals["manufacturer"] == "SHEnergy"


def test_ems_command_flows_into_values_and_frames():
    m = _mgr()
    m.handle_command("deye-vbms", "mbg/devices/deye-vbms/cmd/charge_current_limit", "200")
    vals = m.values(m.instances["deye-vbms"])
    assert vals["charge_current_limit"] == 80      # EMS 200 clamped to the 80 envelope
    frames = m.instances["deye-vbms"].vbms.build_frames(vals)
    ccl = next(r for r in _ROWS if r.get("name") == "charge_current_limit")
    assert decode_signal(frames[0x351], ccl) / ccl["scale"] == 80
    soc = next(r for r in _ROWS if r.get("name") == "soc" and parse_can_id(r.get("can_id")) == 0x355)
    assert decode_signal(frames[0x355], soc) == 80


def test_command_clamps_and_routes_by_field():
    m = _mgr()
    assert m.command("deye-vbms", "charge_current_limit", "200") == 80     # clamped to the envelope
    assert m.command("deye-vbms", "ccl", "15") == 15                       # alias, within envelope
    assert m.values(m.instances["deye-vbms"])["charge_current_limit"] == 15
    with pytest.raises(ValueError):
        m.command("deye-vbms", "manufacturer", "x")                        # not a control field
    with pytest.raises(KeyError):
        m.command("nope", "charge_current_limit", "10")                    # unknown instance


def test_command_does_not_transmit_on_a_disarmed_instance():
    FakeTx.instances.clear()
    m = _mgr()
    m.command("deye-vbms", "charge_current_limit", "50")
    assert m.instances["deye-vbms"].running is False     # a command never arms
    assert FakeTx.instances == []                        # and never opens a transmitter


def test_command_topics_enumerate_every_field_per_instance():
    m = _mgr()
    topics = m.command_topics("mbg/devices/{device}")
    assert topics["mbg/devices/deye-vbms/cmd/charge_current_limit"] == ("deye-vbms", "charge_current_limit")
    assert len(topics) == 6                               # the 6 CONTROL_FIELDS
    assert all(t.startswith("mbg/devices/deye-vbms/cmd/") for t in topics)
    # the pattern is honoured (no hardcoding of the base)
    alt = m.command_topics("site7/bms/{device}")
    assert "site7/bms/deye-vbms/cmd/discharge_current_limit" in alt


def test_preview_shows_per_pack_and_aggregate():
    m = _mgr()
    pv = m.preview(m.instances["deye-vbms"])
    assert pv["packs_total"] == 2 and pv["packs_live"] == 2
    p1 = next(p for p in pv["packs"] if p["id"] == "seplos-adr1")
    assert p1["eligible"] is True and p1["values"]["soc"] == 90 and p1["values"]["voltage"] == 53.5
    assert pv["sends"]["soc"] == 80                 # the aggregated value sent
    assert pv["send_optional"] is True and "0x351" in pv["frames"]


def test_preview_send_optional_false_narrows_the_frames():
    cfg = {"virtual_bms": [{**CONFIG["virtual_bms"][0], "fields": {"send_optional": False}}]}
    m = VbmsManager(cfg, _read, _load, tx_factory=FakeTx)
    pv = m.preview(m.instances["deye-vbms"])
    assert "0x351" in pv["frames"] and "0x370" not in pv["frames"]   # mandatory kept, cell-extremes dropped


def test_preview_cross_checks_the_named_inverter():
    cfg = {"virtual_bms": [{**CONFIG["virtual_bms"][0], "inverter": "deye"}]}
    store = dict(STORE); store[("deye", "bms_soc")] = 80; store[("deye", "bms_voltage")] = 53.4
    m = VbmsManager(cfg, lambda d, f: store.get((d, f)), _load, tx_factory=FakeTx)
    xc = m.preview(m.instances["deye-vbms"])["inverter_check"]
    assert xc["inverter"] == "deye"
    assert xc["soc"]["sent"] == 80 and xc["soc"]["received"] == 80 and xc["soc"]["match"] is True
    assert xc["voltage"]["match"] is True                      # 53.45 sent vs 53.4 read, within 1 V


def test_preview_has_no_cross_check_without_an_inverter():
    assert "inverter_check" not in _mgr().preview(_mgr().instances["deye-vbms"])


def test_state_snapshot_carries_values_and_status():
    m = _mgr()
    st = m.state(m.instances["deye-vbms"])
    assert st["values"]["soc"] == 80 and st["values"]["charge_current_limit"] == 20   # measured + floor
    assert st["armed"] is False and st["running"] is False
    assert st["channel"] == "can0"
    assert st["packs_total"] == 2 and st["packs_live"] == 2                            # both packs fresh


def test_disarmed_manager_runs_no_thread():
    m = _mgr()
    m.start_all()
    assert m.instances["deye-vbms"].running is False     # disarmed → nothing starts
    assert m.status()["deye-vbms"]["running"] is False


def test_arm_starts_a_thread_and_disarm_stops_it_safely():
    FakeTx.instances.clear()
    m = _mgr()
    m.arm("deye-vbms")
    inst = m.instances["deye-vbms"]
    assert inst.armed and inst.running
    deadline = time.monotonic() + 1
    while not (FakeTx.instances and FakeTx.instances[-1].sent) and time.monotonic() < deadline:
        time.sleep(0.01)
    tx = FakeTx.instances[-1]
    assert tx.opened and len(tx.sent) > 0            # the loop transmitted
    assert tx.sent[0][0] in (c for c in inst.vbms.can_ids())
    m.disarm("deye-vbms")
    assert inst.running is False and tx.closed       # stopped + tx closed (safe BMS-loss)
