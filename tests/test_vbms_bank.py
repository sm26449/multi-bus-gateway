# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Virtual BMS live bank aggregation (ADR-0037 inc.2, design §4): parallel packs combine
conservatively into one bank, and the result feeds the profile frame builder."""
import json
import pathlib

from multibus.codec.can_signal import decode_signal, parse_can_id
from multibus.outputs.vbms import VirtualBms
from multibus.outputs.vbms_bank import aggregate_bank, eligible

PACK_A = {"soc": 90, "soh": 100, "voltage": 53.5, "current": 10.0, "temp_max": 23, "temp_min": 22,
          "cell_v_max": 3.35, "cell_v_min": 3.33, "capacity_total": 280, "modules": 1, "protection_flags": 0}
PACK_B = {"soc": 50, "soh": 98, "voltage": 53.4, "current": 8.0, "temp_max": 24, "temp_min": 21,
          "cell_v_max": 3.30, "cell_v_min": 3.16, "capacity_total": 280, "modules": 1, "protection_flags": 0x08}


def test_parallel_packs_aggregate_conservatively():
    b = aggregate_bank([PACK_A, PACK_B])
    assert b["battery_current"] == 18.0               # summed
    assert b["pack_voltage"] == 53.45                 # mean
    assert b["soc"] == 70                             # capacity-weighted mean (equal caps → mean)
    assert b["soh"] == 98                             # min (conservative)
    assert b["max_cell_temp"] == 24 and b["min_cell_temp"] == 21
    assert b["battery_temperature"] == 24             # the hottest
    assert b["max_cell_voltage"] == 3.35 and b["min_cell_voltage"] == 3.16
    assert b["total_capacity"] == 560 and b["module_count"] == 2
    assert b["protection_flags"] == 0x08              # OR across packs
    assert b["packs_live"] == 2


def test_capacity_weighted_soc():
    big = {**PACK_A, "soc": 80, "capacity_total": 300}
    small = {**PACK_B, "soc": 40, "capacity_total": 100}
    # (80*300 + 40*100) / 400 = 28000/400 = 70
    assert aggregate_bank([big, small])["soc"] == 70


def test_eligibility_drops_dead_packs():
    dead = {"soh": 100}                                # no soc/voltage → not eligible
    assert eligible([PACK_A, dead]) == [PACK_A]
    assert aggregate_bank([dead]) == {}               # nothing eligible → empty (caller safe-defaults)
    # a dropped pack doesn't skew the sum
    assert aggregate_bank([PACK_A, dead])["battery_current"] == 10.0


def test_single_pack():
    b = aggregate_bank([PACK_A])
    assert b["soc"] == 90 and b["battery_current"] == 10.0 and b["module_count"] == 1


# ── the loop closes: packs → bank → profile frames ────────────────────────────
def test_aggregated_bank_feeds_the_frame_builder():
    tpl = pathlib.Path(__file__).resolve().parent.parent / "multibus" / "device_templates" / "pylontech_can_inverter.json"
    rows = json.loads(tpl.read_text())["device_template"]["registers"]
    vb = VirtualBms(rows)
    # measured from aggregation + control fields from the (future) EMS/control plane
    values = {**aggregate_bank([PACK_A, PACK_B]),
              "charge_voltage_limit": 57.6, "charge_current_limit": 80,
              "discharge_current_limit": 180, "discharge_voltage_limit": 46.4,
              "manufacturer": "SHEnergy", "charge_enable": 1, "discharge_enable": 1}
    frames = vb.build_frames(values)
    soc_row = next(r for r in rows if r.get("name") == "soc" and parse_can_id(r.get("can_id")) == 0x355)
    assert decode_signal(frames[0x355], soc_row) == 70       # the aggregated SOC on the wire
    assert frames[0x351].hex().upper() == "400220030807D001"  # the control-plane limits
