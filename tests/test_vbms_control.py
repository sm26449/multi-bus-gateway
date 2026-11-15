# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Virtual BMS EMS/MQTT control plane (ADR-0037 D4): an EMS command is a ceiling clamped to the
safe envelope, a stale command reverts to the floor, and the result drives the CAN frames."""
import json
import pathlib

import pytest

from multibus.outputs.vbms import VirtualBms
from multibus.outputs.vbms_bank import aggregate_bank
from multibus.outputs.vbms_control import VbmsControl

# safe envelope (datasheet caps ∩ aggregation floor) and the floor used with no fresh command
BOUNDS = {"charge_current_limit": (0, 80), "discharge_current_limit": (0, 180),
          "charge_voltage_limit": (40, 57.6), "discharge_voltage_limit": (40, 50),
          "charge_enable": (0, 1), "discharge_enable": (0, 1)}
FLOOR = {"charge_current_limit": 20, "discharge_current_limit": 40,
         "charge_voltage_limit": 55.0, "discharge_voltage_limit": 46.4,
         "charge_enable": 1, "discharge_enable": 1}


def _ctl(t0=0.0):
    clk = {"t": t0}
    c = VbmsControl(BOUNDS, FLOOR, max_age_s=60, clock=lambda: clk["t"])
    return c, clk


def test_command_is_a_ceiling_clamped_to_the_envelope():
    c, _ = _ctl()
    assert c.command("charge_current_limit", 200) == 80      # EMS asked 200, envelope caps at 80
    assert c.command("charge_current_limit", -5) == 0        # never negative
    assert c.command("discharge_current_limit", 90) == 90    # within (0,180): honoured


def test_enables_clamp_to_0_or_1():
    c, _ = _ctl()
    assert c.command("charge_enable", 1) == 1
    assert c.command("discharge_enable", 0.3) == 0


def test_aliases_and_unknown_field():
    c, _ = _ctl()
    assert c.command("ccl", 50) == 50                        # alias → charge_current_limit
    assert c.resolve()["charge_current_limit"] == 50
    with pytest.raises(ValueError):
        c.command("soc", 100)                                # measured, not controllable


def test_resolve_uses_command_while_fresh_then_floors():
    c, clk = _ctl()
    c.command("charge_current_limit", 60)
    assert c.resolve()["charge_current_limit"] == 60         # fresh
    clk["t"] = 100                                           # > max_age 60
    assert c.resolve()["charge_current_limit"] == FLOOR["charge_current_limit"]   # stale → floor


def test_handle_command_routes_a_cmd_topic():
    c, _ = _ctl()
    v = c.handle_command("mbg/devices/deye-vbms/cmd/discharge_current_limit", "150")
    assert v == 150 and c.resolve()["discharge_current_limit"] == 150


def test_floor_holds_for_uncommanded_fields():
    c, _ = _ctl()
    c.command("charge_current_limit", 70)
    r = c.resolve()
    assert r["charge_current_limit"] == 70
    assert r["discharge_current_limit"] == FLOOR["discharge_current_limit"]   # untouched → floor


# ── EMS → control → aggregation → frames (the whole D4 loop) ───────────────────
def test_ems_command_reaches_the_can_frame():
    from multibus.codec.can_signal import decode_signal, parse_can_id
    tpl = pathlib.Path(__file__).resolve().parent.parent / "multibus" / "device_templates" / "pylontech_can_inverter.json"
    rows = json.loads(tpl.read_text())["device_template"]["registers"]
    vb = VirtualBms(rows)
    c, _ = _ctl()
    c.command("charge_current_limit", 80)        # EMS says 80 A charge
    packs = [{"soc": 90, "voltage": 53.5, "current": 10, "capacity_total": 280},
             {"soc": 70, "voltage": 53.4, "current": 8, "capacity_total": 280}]
    values = {**aggregate_bank(packs), **c.resolve(), "manufacturer": "SHEnergy"}
    frames = vb.build_frames(values)
    ccl = next(r for r in rows if r.get("name") == "charge_current_limit")
    assert decode_signal(frames[0x351], ccl) / ccl["scale"] == 80     # the EMS limit on the wire
    soc = next(r for r in rows if r.get("name") == "soc" and parse_can_id(r.get("can_id")) == 0x355)
    assert decode_signal(frames[0x355], soc) == 80                     # live-aggregated SOC (90&70)
