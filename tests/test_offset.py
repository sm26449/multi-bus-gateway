# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Per-register offset: engineering value = raw / scale + offset (zero-point /
unit shift, e.g. Kelvin×10 → °C). Applied on every transport after scale."""
from unittest.mock import MagicMock

import pytest

from multibus.config import SelectedRegister
from multibus.modbus_client import RegisterPoller
from multibus.register_parser import RegisterParser


def _poll(reg, raw):
    conn = MagicMock()
    conn.read_registers.return_value = [raw & 0xFFFF]
    p = RegisterPoller("normal", 5, [reg], conn, RegisterParser(),
                       publish_callback=lambda *a: None)
    return p._poll_registers().get(reg.address, {}).get("value")


def _reg(**kw):
    base = dict(address=10, name="t", label="t", unit="", data_type="uint16",
                poll_group="normal")
    base.update(kw)
    return SelectedRegister(**base)


def test_offset_alone():
    assert _poll(_reg(offset=5), 100) == 105


def test_scale_then_offset():
    # raw 2985, /10 = 298.5, + (-273.15) = 25.35  (Kelvin×10 → °C)
    assert _poll(_reg(scale=10, offset=-273.15), 2985) == pytest.approx(25.35)


def test_no_offset_is_unchanged():
    assert _poll(_reg(scale=10), 2500) == 250.0
    assert _poll(_reg(), 42) == 42


def test_offset_skipped_for_enum_register():
    # a status register decodes to text — scale/offset are numeric-only
    r = _reg(data_type="uint16", enum={"7": "Fault"}, offset=100)
    assert _poll(r, 7) == "Fault"


def test_offset_round_trips_through_template():
    from multibus.device_template import parse_template, validate_template
    tpl = {"device_template": {
        "id": "x_off", "name": "X", "protocol": {},
        "registers": [{"address": 0, "name": "temperature", "unit": "°C",
                       "data_type": "uint16", "scale": 10, "offset": -273.15}]}}
    assert validate_template(tpl) == []
    r = parse_template(tpl).registers[0]
    assert r.offset == -273.15
    assert r.to_dict()["offset"] == -273.15


def test_offset_absent_by_default_stays_lean():
    from multibus.device_template import parse_template
    r = parse_template({"device_template": {
        "id": "x_p", "name": "X", "protocol": {},
        "registers": [{"address": 0, "name": "v", "unit": "V",
                       "data_type": "uint16"}]}}).registers[0]
    assert r.offset == 0.0
    assert "offset" not in r.to_dict()


def test_config_parses_offset():
    from multibus.config import Config
    reg = Config._parse_selected_payload(
        {"registers": [{"address": 0, "name": "t", "data_type": "uint16",
                        "scale": 10, "offset": -273.15}]})[0]
    assert reg.offset == -273.15
