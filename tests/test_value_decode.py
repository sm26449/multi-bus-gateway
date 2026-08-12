# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Enum / bitfield decode: a raw status register → human-readable text."""
from types import SimpleNamespace

from multibus.value_decode import (decode_bits, decode_enum, decode_register,
                                   is_textual)

# SunSpec-style inverter operating state (JSON map → string keys)
STATE = {"1": "Off", "2": "Sleeping", "3": "Starting", "4": "MPPT",
         "5": "Throttled", "7": "Fault", "8": "Standby"}
# a BMS-style alarm word (bit → name)
ALARM = {"0": "overvoltage", "1": "undervoltage", "2": "overtemp", "3": "overcurrent"}


# ── enum ─────────────────────────────────────────────────────────────────────

def test_enum_maps_known_codes():
    assert decode_enum(4, STATE) == "MPPT"
    assert decode_enum(7, STATE) == "Fault"


def test_enum_unknown_is_labelled_not_bare_number():
    assert decode_enum(99, STATE) == "unknown (99)"
    assert decode_enum(99, STATE, unknown="?") == "?"


def test_enum_mask_and_shift_extract_a_subfield():
    # state packed in bits 8..11 of a status word: 0x0400 → nibble 4 → MPPT
    assert decode_enum(0x0400, STATE, mask=0x0F00, shift=8) == "MPPT"


def test_enum_non_numeric_returns_none():
    assert decode_enum("x", STATE) is None


# ── bitfield ─────────────────────────────────────────────────────────────────

def test_bits_lists_active_flags_in_order():
    # 0b1101 = bits 0,2,3 set
    assert decode_bits(0b1101, ALARM) == "overvoltage, overtemp, overcurrent"


def test_bits_no_flags_is_the_idle_label():
    assert decode_bits(0, ALARM) == ""
    assert decode_bits(0, ALARM, none_label="ok") == "ok"


def test_bits_custom_glue():
    assert decode_bits(0b0110, ALARM, glue="|") == "undervoltage|overtemp"


# ── decode_register dispatch + is_textual ────────────────────────────────────

def test_decode_register_prefers_enum_then_bits_else_passthrough():
    assert decode_register(4, SimpleNamespace(enum=STATE, bits=None)) == "MPPT"
    assert decode_register(0b0100, SimpleNamespace(enum=None, bits=ALARM)) == "overtemp"
    assert decode_register(230.0, SimpleNamespace(enum=None, bits=None)) == 230.0
    assert decode_register(None, SimpleNamespace(enum=STATE)) is None


def test_decode_register_works_on_dicts_too():
    assert decode_register(7, {"enum": STATE}) == "Fault"


def test_is_textual():
    assert is_textual(SimpleNamespace(enum=STATE, bits=None)) is True
    assert is_textual({"bits": ALARM}) is True
    assert is_textual(SimpleNamespace(enum=None, bits=None)) is False


# ── it reaches the poll path + HA typing ─────────────────────────────────────

def _u16(v):
    return [v & 0xFFFF]


def test_poller_decodes_enum_register():
    from unittest.mock import MagicMock
    from multibus.config import SelectedRegister
    from multibus.modbus_client import RegisterPoller
    from multibus.register_parser import RegisterParser

    reg = SelectedRegister(address=40, name="inverter_state", label="State",
                           unit="", data_type="uint16", poll_group="normal",
                           enum=STATE)
    conn = MagicMock()
    conn.read_registers.return_value = _u16(7)          # raw 7
    poller = RegisterPoller("normal", 5, [reg], conn, RegisterParser(),
                            publish_callback=lambda *a: None)
    assert poller._poll_registers()[40]["value"] == "Fault"


def test_enum_register_gets_no_measurement_state_class():
    from multibus.mqtt_publisher import apply_ha_typing
    cfg = {}
    apply_ha_typing(cfg, SimpleNamespace(
        unit="", device_class="", state_class="", entity_category="",
        enabled_by_default=None, icon="", suggested_display_precision=None,
        enum=STATE, bits=None))
    assert "state_class" not in cfg      # text sensor — never a measurement
    assert "device_class" not in cfg


def test_round_trip_through_template():
    from multibus.device_template import parse_template, validate_template
    tpl = {"device_template": {
        "id": "x_enum", "name": "X", "protocol": {},
        "registers": [{"address": 40, "name": "inverter_state", "unit": "",
                       "data_type": "uint16", "enum": STATE},
                      {"address": 41, "name": "alarms", "unit": "",
                       "data_type": "uint16", "bits": ALARM}]}}
    assert validate_template(tpl) == []
    t = parse_template(tpl)
    assert t.registers[0].enum == STATE
    assert t.registers[0].to_dict()["enum"] == STATE
    assert t.registers[1].to_dict()["bits"] == ALARM
