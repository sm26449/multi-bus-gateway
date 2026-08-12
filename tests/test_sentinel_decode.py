# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""A device 'not available' sentinel must decode to None (missing), not the raw
max/min value — else garbage (e.g. 65535) is published to MQTT/InfluxDB and can
reach a downstream ESS. Opt-in per register via `nan`; float NaN always dropped.
"""
from multibus.register_parser import RegisterParser


def _p():
    return RegisterParser(byte_order="big")


def test_integer_sentinels_dropped_only_when_opted_in():
    p = _p()
    cases = [
        ("int16", [0x8000], -32768),
        ("uint16", [0xFFFF], 65535),
        ("int32", [0x8000, 0x0000], -2147483648),
        ("uint32", [0xFFFF, 0xFFFF], 4294967295),
        ("uint64", [0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF], 2 ** 64 - 1),
    ]
    for dtype, regs, raw in cases:
        # without opt-in the raw value is returned as-is (65535 can be legitimate)
        assert p.parse_value(regs, dtype) == raw, dtype
        # with nan=True the type's not-implemented sentinel reads as missing
        assert p.parse_value(regs, dtype, nan=True) is None, dtype


def test_real_value_survives_nan_true():
    p = _p()
    assert p.parse_value([500], "uint16", nan=True) == 500      # not the sentinel
    assert p.parse_value([0x4248, 0x0000], "float", nan=True) == 50.0


def test_float_nan_always_dropped():
    p = _p()
    assert p.parse_value([0x7FC0, 0x0000], "float") is None     # NaN, no opt-in needed
    assert p.parse_value([0x7F80, 0x0000], "float") is None     # +Inf


def test_explicit_sentinel_value_and_list():
    p = _p()
    assert p.parse_value([100], "uint16", nan=100) is None
    assert p.parse_value([101], "uint16", nan=100) == 101
    assert p.parse_value([0], "uint16", nan=[0, 65535]) is None
    assert p.parse_value([0xFFFF], "uint16", nan=[0, 65535]) is None
    assert p.parse_value([42], "uint16", nan=[0, 65535]) == 42


def test_nan_false_or_none_is_a_noop():
    p = _p()
    assert p.parse_value([0xFFFF], "uint16", nan=False) == 65535
    assert p.parse_value([0xFFFF], "uint16", nan=None) == 65535


def test_nan_round_trips_through_template():
    from multibus.device_template import parse_template, validate_template
    tpl = {"device_template": {
        "id": "x_nan", "name": "X", "protocol": {},
        "registers": [
            {"address": 0, "name": "voltage_l1_n", "unit": "V",
             "data_type": "uint16", "scale": 10, "nan": True},
        ]}}
    assert validate_template(tpl) == []
    t = parse_template(tpl)
    assert t.registers[0].nan is True
    assert t.registers[0].to_dict()["nan"] is True
