# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Fronius SunSpec templates (F0.4) — programmatic diff against the reference
collector's register map.

The expected addresses below are derived INDEPENDENTLY from the collector's
parser offsets (fronius-modbus-mqtt/fronius/register_parser.py — model 103
block read at documented 40072, model 203 at 40072, model 160 at 40254) so a
template typo cannot hide: PDU = documented − 1 + offset. The same map passed
live raw-frame decode parity against all 4 site units on 2026-09-11.
"""
import json

from multibus.device_template import parse_template, validate_template

INV = "multibus/device_templates/fronius_sunspec_inverter.json"
MET = "multibus/device_templates/fronius_sunspec_meter.json"

# collector offsets relative to documented 40072 (PDU base 40071)
INV_BASE = 40071
INV_OFFSETS = {
    "ac_current": (0, "uint16"), "ac_current_a": (1, "uint16"),
    "ac_current_b": (2, "uint16"), "ac_current_c": (3, "uint16"),
    "a_sf": (4, "int16"),
    "ac_voltage_ab": (5, "uint16"), "ac_voltage_bc": (6, "uint16"),
    "ac_voltage_ca": (7, "uint16"), "ac_voltage_an": (8, "uint16"),
    "ac_voltage_bn": (9, "uint16"), "ac_voltage_cn": (10, "uint16"),
    "v_sf": (11, "int16"),
    "ac_power": (12, "int16"), "w_sf": (13, "int16"),
    "ac_frequency": (14, "uint16"), "hz_sf": (15, "int16"),
    "apparent_power": (16, "int16"), "va_sf": (17, "int16"),
    "reactive_power": (18, "int16"), "var_sf": (19, "int16"),
    "power_factor": (20, "int16"), "pf_sf": (21, "int16"),
    "lifetime_energy": (22, "uint32"), "wh_sf": (24, "int16"),
    "dc_current": (25, "uint16"), "dca_sf": (26, "int16"),
    "dc_voltage": (27, "uint16"), "dcv_sf": (28, "int16"),
    "dc_power": (29, "int16"), "dcw_sf": (30, "int16"),
    "temp_cabinet": (31, "int16"), "temp_heatsink": (32, "int16"),
    "temp_transformer": (33, "int16"), "temp_other": (34, "int16"),
    "tmp_sf": (35, "int16"),
    "status_code": (36, "uint16"), "status_vendor": (37, "uint16"),
    "evt1": (38, "uint32"), "evt2": (40, "uint32"),
    "evt_vnd1": (42, "uint32"), "evt_vnd2": (44, "uint32"),
    "evt_vnd3": (46, "uint32"), "evt_vnd4": (48, "uint32"),
}

# model 160 read at documented 40254 (PDU base 40253); module 1 block at
# offset 10, module 2 at offset 30; DCA/DCV/DCW/DCWH/Tmp at module offsets
# 9/10/11/12-13/16 (collector _parse_mppt_module_optimized)
MPPT_BASE = 40253
MPPT_OFFSETS = {
    "dca_mppt_sf": (2, "int16"), "dcv_mppt_sf": (3, "int16"),
    "dcw_mppt_sf": (4, "int16"), "dcwh_mppt_sf": (5, "int16"),
    "mppt_num_modules": (8, "uint16"),
    "mppt1_dc_current": (10 + 9, "uint16"), "mppt1_dc_voltage": (10 + 10, "uint16"),
    "mppt1_dc_power": (10 + 11, "uint16"), "mppt1_dc_energy": (10 + 12, "uint32"),
    "mppt1_temperature": (10 + 16, "int16"),
    "mppt2_dc_current": (30 + 9, "uint16"), "mppt2_dc_voltage": (30 + 10, "uint16"),
    "mppt2_dc_power": (30 + 11, "uint16"), "mppt2_dc_energy": (30 + 12, "uint32"),
    "mppt2_temperature": (30 + 16, "int16"),
}

MET_BASE = 40071
MET_OFFSETS = {
    "current_total": (0, "int16"), "current_a": (1, "int16"),
    "current_b": (2, "int16"), "current_c": (3, "int16"),
    "a_sf": (4, "int16"),
    "voltage_ln_avg": (5, "int16"), "voltage_an": (6, "int16"),
    "voltage_bn": (7, "int16"), "voltage_cn": (8, "int16"),
    "voltage_ll_avg": (9, "int16"), "voltage_ab": (10, "int16"),
    "voltage_bc": (11, "int16"), "voltage_ca": (12, "int16"),
    "v_sf": (13, "int16"),
    "frequency": (14, "int16"), "hz_sf": (15, "int16"),
    "power_total": (16, "int16"), "power_a": (17, "int16"),
    "power_b": (18, "int16"), "power_c": (19, "int16"),
    "w_sf": (20, "int16"),
    "va_total": (21, "int16"), "va_a": (22, "int16"),
    "va_b": (23, "int16"), "va_c": (24, "int16"),
    "va_sf": (25, "int16"),
    "var_total": (26, "int16"), "var_a": (27, "int16"),
    "var_b": (28, "int16"), "var_c": (29, "int16"),
    "var_sf": (30, "int16"),
    "pf_avg": (31, "int16"), "pf_a": (32, "int16"),
    "pf_b": (33, "int16"), "pf_c": (34, "int16"),
    "pf_sf": (35, "int16"),
    "energy_exported": (36, "uint32"), "energy_exported_a": (38, "uint32"),
    "energy_exported_b": (40, "uint32"), "energy_exported_c": (42, "uint32"),
    "energy_imported": (44, "uint32"), "energy_imported_a": (46, "uint32"),
    "energy_imported_b": (48, "uint32"), "energy_imported_c": (50, "uint32"),
    "wh_sf": (52, "int16"),
}

# the collector's MQTT point names (INVERTER_FIELD_MAP / METER_FIELD_MAP) —
# the shadow-parity harness compares topic-for-topic against these
INV_TOPICS = {
    "ac_current": "A", "ac_current_a": "AphA", "ac_current_b": "AphB",
    "ac_current_c": "AphC", "ac_voltage_ab": "PPVphAB",
    "ac_voltage_bc": "PPVphBC", "ac_voltage_ca": "PPVphCA",
    "ac_voltage_an": "PhVphA", "ac_voltage_bn": "PhVphB",
    "ac_voltage_cn": "PhVphC", "ac_power": "W", "ac_frequency": "Hz",
    "apparent_power": "VA", "reactive_power": "VAr", "power_factor": "PF",
    "lifetime_energy": "WH", "dc_current": "DCA", "dc_voltage": "DCV",
    "dc_power": "DCW", "temp_cabinet": "TmpCab", "temp_heatsink": "TmpSnk",
    "temp_transformer": "TmpTrns", "temp_other": "TmpOt",
    "status_code": "St", "status_vendor": "StVnd",
}
MET_TOPICS = {
    "current_total": "A", "current_a": "AphA", "current_b": "AphB",
    "current_c": "AphC", "voltage_ln_avg": "PhV", "voltage_an": "PhVphA",
    "voltage_bn": "PhVphB", "voltage_cn": "PhVphC", "voltage_ll_avg": "PPV",
    "voltage_ab": "PPVphAB", "voltage_bc": "PPVphBC", "voltage_ca": "PPVphCA",
    "frequency": "Hz", "power_total": "W", "power_a": "WphA",
    "power_b": "WphB", "power_c": "WphC", "va_total": "VA", "va_a": "VAphA",
    "va_b": "VAphB", "va_c": "VAphC", "var_total": "VAR", "var_a": "VARphA",
    "var_b": "VARphB", "var_c": "VARphC", "pf_avg": "PF", "pf_a": "PFphA",
    "pf_b": "PFphB", "pf_c": "PFphC", "energy_exported": "TotWhExp",
    "energy_exported_a": "TotWhExpPhA", "energy_exported_b": "TotWhExpPhB",
    "energy_exported_c": "TotWhExpPhC", "energy_imported": "TotWhImp",
    "energy_imported_a": "TotWhImpPhA", "energy_imported_b": "TotWhImpPhB",
    "energy_imported_c": "TotWhImpPhC",
}


def _load(path):
    with open(path) as f:
        return json.load(f)


def _by_name(path):
    t = parse_template(_load(path))
    return t, {r.name: r for r in t.registers}


def test_templates_validate_and_parse():
    for path in (INV, MET):
        assert validate_template(_load(path)) == [], path
        t = parse_template(_load(path))
        assert t.registers
        assert t.protocol.get("byte_order") == "big"


def test_inverter_addresses_match_collector_offsets():
    _t, regs = _by_name(INV)
    for name, (off, dtype) in {**INV_OFFSETS}.items():
        r = regs[name]
        assert r.address == INV_BASE + off, f"{name}: {r.address} != {INV_BASE + off}"
        assert r.data_type == dtype, name
    for name, (off, dtype) in MPPT_OFFSETS.items():
        r = regs[name]
        assert r.address == MPPT_BASE + off, f"{name}: {r.address} != {MPPT_BASE + off}"
        assert r.data_type == dtype, name


def test_meter_addresses_match_collector_offsets():
    _t, regs = _by_name(MET)
    for name, (off, dtype) in MET_OFFSETS.items():
        r = regs[name]
        assert r.address == MET_BASE + off, f"{name}: {r.address} != {MET_BASE + off}"
        assert r.data_type == dtype, name


def test_scale_from_wiring_matches_collector_sf_assignment():
    _t, regs = _by_name(INV)
    expect = {
        "a_sf": ["ac_current", "ac_current_a", "ac_current_b", "ac_current_c"],
        "v_sf": ["ac_voltage_ab", "ac_voltage_bc", "ac_voltage_ca",
                 "ac_voltage_an", "ac_voltage_bn", "ac_voltage_cn"],
        "w_sf": ["ac_power"], "hz_sf": ["ac_frequency"],
        "va_sf": ["apparent_power"], "var_sf": ["reactive_power"],
        "pf_sf": ["power_factor"], "wh_sf": ["lifetime_energy"],
        "dca_sf": ["dc_current"], "dcv_sf": ["dc_voltage"],
        "dcw_sf": ["dc_power"],
        "tmp_sf": ["temp_cabinet", "temp_heatsink", "temp_transformer",
                   "temp_other"],
        "dca_mppt_sf": ["mppt1_dc_current", "mppt2_dc_current"],
        "dcv_mppt_sf": ["mppt1_dc_voltage", "mppt2_dc_voltage"],
        "dcw_mppt_sf": ["mppt1_dc_power", "mppt2_dc_power"],
        "dcwh_mppt_sf": ["mppt1_dc_energy", "mppt2_dc_energy"],
    }
    for sf, dependents in expect.items():
        for name in dependents:
            assert regs[name].scale_from == sf, name
    # raw registers stay raw
    for name in ("status_code", "status_vendor", "evt1", "evt_vnd4",
                 "mppt_num_modules", "mppt1_temperature"):
        assert regs[name].scale_from == "", name


def test_mqtt_topic_defaults_match_collector_point_names():
    for path, topics in ((INV, INV_TOPICS), (MET, MET_TOPICS)):
        _t, regs = _by_name(path)
        for name, topic in topics.items():
            d = regs[name].defaults or {}
            assert (d.get("mqtt") or {}).get("topic") == topic, (path, name)
        # SF registers are selected (same-batch prescan) but never routed
        sf_names = [n for n in regs if n.endswith("_sf")]
        assert sf_names
        for n in sf_names:
            d = regs[n].defaults or {}
            assert (d.get("mqtt") or {}).get("enabled") is False, n
            assert (d.get("influxdb") or {}).get("enabled") is False, n


def test_batch_spans_fit_the_datamanager_read_cap():
    """The DataManager rejects reads much beyond ~50-55 registers. Model the
    poller's grouping (same poll group, gap <= max_gap 10) and assert every
    contiguous span stays within the proven cap (the collector reads 53)."""
    from multibus.register_parser import RegisterParser
    parser = RegisterParser()
    for path in (INV, MET):
        t, regs = _by_name(path)
        by_group = {}
        for r in t.registers:
            by_group.setdefault(r.poll_group or "normal", []).append(r)
        for gname, rs in by_group.items():
            rs = sorted(rs, key=lambda r: r.address)
            span_start = prev_end = None
            for r in rs:
                cnt = parser.get_register_count(r.data_type)
                if span_start is None or r.address - prev_end > 10:
                    span_start = r.address
                end = r.address + cnt - 1
                width = end - span_start + 1
                assert width <= 55, (path, gname, span_start, end, width)
                prev_end = end


def test_energy_counters_are_monotonic():
    for path, names in ((INV, ["lifetime_energy"]),
                        (MET, ["energy_exported", "energy_imported"])):
        _t, regs = _by_name(path)
        for n in names:
            assert regs[n].monotonic is True, (path, n)


def test_identity_block_present_and_static():
    for path in (INV, MET):
        _t, regs = _by_name(path)
        for name, doc in (("manufacturer", 40005), ("model", 40021),
                          ("serial_number", 40053)):
            r = regs[name]
            assert r.address == doc - 1
            assert r.data_type == "string:16"
            assert r.poll_group == "static"
