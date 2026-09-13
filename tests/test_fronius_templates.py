# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Fronius SunSpec templates — programmatic diff against the reference
collector's register map, in the CANONICAL naming (2026-09-11 rework).

Two invariants are pinned:
1. ADDRESSES: derived INDEPENDENTLY from the collector's parser offsets
   (fronius-modbus-mqtt/fronius/register_parser.py — model 103 block read at
   documented 40072, model 203 at 40072, model 160 at 40254): PDU =
   documented − 1 + offset. The same map passed live raw-frame decode parity
   against all 4 site units on 2026-09-11.
2. NAMING: every routed register carries a CANONICAL name (same topics/
   fields/measurements as the Janitza reference), and the legacy collector
   leaf for each one lives in scripts/fronius_legacy_leaves.json — the map
   that builds the mqtt.compat_aliases layer for the migration.
"""
import json

from multibus.canonical_fields import is_canonical, mqtt_topic_for
from multibus.device_template import parse_template, validate_template

INV = "multibus/device_templates/fronius_sunspec_inverter.json"
MET = "multibus/device_templates/fronius_sunspec_meter.json"
LEAVES = "scripts/fronius_legacy_leaves.json"

# collector offsets relative to documented 40072 (PDU base 40071)
INV_BASE = 40071
INV_OFFSETS = {
    "current_total": (0, "uint16"), "current_l1": (1, "uint16"),
    "current_l2": (2, "uint16"), "current_l3": (3, "uint16"),
    "a_sf": (4, "int16"),
    "voltage_l1_l2": (5, "uint16"), "voltage_l2_l3": (6, "uint16"),
    "voltage_l3_l1": (7, "uint16"), "voltage_l1_n": (8, "uint16"),
    "voltage_l2_n": (9, "uint16"), "voltage_l3_n": (10, "uint16"),
    "v_sf": (11, "int16"),
    "power_active_total": (12, "int16"), "w_sf": (13, "int16"),
    "frequency": (14, "uint16"), "hz_sf": (15, "int16"),
    "power_apparent_total": (16, "int16"), "va_sf": (17, "int16"),
    "power_reactive_total": (18, "int16"), "var_sf": (19, "int16"),
    "power_factor_total": (20, "int16"), "pf_sf": (21, "int16"),
    "energy_active_generated": (22, "uint32"), "wh_sf": (24, "int16"),
    "current_dc": (25, "uint16"), "dca_sf": (26, "int16"),
    "voltage_dc": (27, "uint16"), "dcv_sf": (28, "int16"),
    "power_dc": (29, "int16"), "dcw_sf": (30, "int16"),
    "temperature_cabinet": (31, "int16"), "temperature_heatsink": (32, "int16"),
    "temperature_transformer": (33, "int16"), "temperature_other": (34, "int16"),
    "tmp_sf": (35, "int16"),
    "operating_state": (36, "uint16"), "vendor_state": (37, "uint16"),
    "event_flags_1": (38, "uint32"), "event_flags_2": (40, "uint32"),
    "vendor_event_flags_1": (42, "uint32"), "vendor_event_flags_2": (44, "uint32"),
    "vendor_event_flags_3": (46, "uint32"), "vendor_event_flags_4": (48, "uint32"),
}

# model 160 read at documented 40254 (PDU base 40253); module 1 block at
# offset 10, module 2 at offset 30; DCA/DCV/DCW/DCWH/Tmp at module offsets
# 9/10/11/12-13/16 (collector _parse_mppt_module_optimized)
MPPT_BASE = 40253
MPPT_OFFSETS = {
    "dca_mppt_sf": (2, "int16"), "dcv_mppt_sf": (3, "int16"),
    "dcw_mppt_sf": (4, "int16"), "dcwh_mppt_sf": (5, "int16"),
    "mppt_modules": (8, "uint16"),
    "current_dc_mppt1": (10 + 9, "uint16"), "voltage_dc_mppt1": (10 + 10, "uint16"),
    "power_dc_mppt1": (10 + 11, "uint16"), "energy_dc_mppt1": (10 + 12, "uint32"),
    "temperature_mppt1": (10 + 16, "int16"),
    "current_dc_mppt2": (30 + 9, "uint16"), "voltage_dc_mppt2": (30 + 10, "uint16"),
    "power_dc_mppt2": (30 + 11, "uint16"), "energy_dc_mppt2": (30 + 12, "uint32"),
    "temperature_mppt2": (30 + 16, "int16"),
}

MET_BASE = 40071
MET_OFFSETS = {
    "current_total": (0, "int16"), "current_l1": (1, "int16"),
    "current_l2": (2, "int16"), "current_l3": (3, "int16"),
    "a_sf": (4, "int16"),
    "voltage_ln_avg": (5, "int16"), "voltage_l1_n": (6, "int16"),
    "voltage_l2_n": (7, "int16"), "voltage_l3_n": (8, "int16"),
    "voltage_ll_avg": (9, "int16"), "voltage_l1_l2": (10, "int16"),
    "voltage_l2_l3": (11, "int16"), "voltage_l3_l1": (12, "int16"),
    "v_sf": (13, "int16"),
    "frequency": (14, "int16"), "hz_sf": (15, "int16"),
    "power_active_total": (16, "int16"), "power_active_l1": (17, "int16"),
    "power_active_l2": (18, "int16"), "power_active_l3": (19, "int16"),
    "w_sf": (20, "int16"),
    "power_apparent_total": (21, "int16"), "power_apparent_l1": (22, "int16"),
    "power_apparent_l2": (23, "int16"), "power_apparent_l3": (24, "int16"),
    "va_sf": (25, "int16"),
    "power_reactive_total": (26, "int16"), "power_reactive_l1": (27, "int16"),
    "power_reactive_l2": (28, "int16"), "power_reactive_l3": (29, "int16"),
    "var_sf": (30, "int16"),
    "power_factor_total": (31, "int16"), "power_factor_l1": (32, "int16"),
    "power_factor_l2": (33, "int16"), "power_factor_l3": (34, "int16"),
    "pf_sf": (35, "int16"),
    "energy_active_export": (36, "uint32"), "energy_active_export_l1": (38, "uint32"),
    "energy_active_export_l2": (40, "uint32"), "energy_active_export_l3": (42, "uint32"),
    "energy_active_import": (44, "uint32"), "energy_active_import_l1": (46, "uint32"),
    "energy_active_import_l2": (48, "uint32"), "energy_active_import_l3": (50, "uint32"),
    "wh_sf": (52, "int16"),
}

# the retired collector's MQTT point names — the compat-alias layer must map
# every canonical topic onto exactly these (the parity harness and the F2
# cutover both speak this tree)
INV_LEGACY = {
    "current_total": "A", "current_l1": "AphA", "current_l2": "AphB",
    "current_l3": "AphC", "voltage_l1_l2": "PPVphAB", "voltage_l2_l3": "PPVphBC",
    "voltage_l3_l1": "PPVphCA", "voltage_l1_n": "PhVphA", "voltage_l2_n": "PhVphB",
    "voltage_l3_n": "PhVphC", "power_active_total": "W", "frequency": "Hz",
    "power_apparent_total": "VA", "power_reactive_total": "VAr",
    "power_factor_total": "PF", "energy_active_generated": "WH",
    "current_dc": "DCA", "voltage_dc": "DCV", "power_dc": "DCW",
    "temperature_cabinet": "TmpCab", "temperature_heatsink": "TmpSnk",
    "temperature_transformer": "TmpTrns", "temperature_other": "TmpOt",
    "operating_state": "St", "vendor_state": "StVnd",
    "power_dc_mppt1": "mppt/string1/DCW", "power_dc_mppt2": "mppt/string2/DCW",
    "mppt_modules": "mppt/num_modules",
}
MET_LEGACY = {
    "current_total": "A", "voltage_ln_avg": "PhV", "voltage_l1_n": "PhVphA",
    "voltage_ll_avg": "PPV", "frequency": "Hz", "power_active_total": "W",
    "power_active_l1": "WphA", "power_apparent_total": "VA",
    "power_reactive_total": "VAR", "power_factor_total": "PF",
    "energy_active_export": "TotWhExp", "energy_active_export_l1": "TotWhExpPhA",
    "energy_active_import": "TotWhImp", "energy_active_import_l3": "TotWhImpPhC",
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
    for name, (off, dtype) in INV_OFFSETS.items():
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
        "a_sf": ["current_total", "current_l1", "current_l2", "current_l3"],
        "v_sf": ["voltage_l1_l2", "voltage_l2_l3", "voltage_l3_l1",
                 "voltage_l1_n", "voltage_l2_n", "voltage_l3_n"],
        "w_sf": ["power_active_total"], "hz_sf": ["frequency"],
        "va_sf": ["power_apparent_total"], "var_sf": ["power_reactive_total"],
        "pf_sf": ["power_factor_total"], "wh_sf": ["energy_active_generated"],
        "dca_sf": ["current_dc"], "dcv_sf": ["voltage_dc"],
        "dcw_sf": ["power_dc"],
        "tmp_sf": ["temperature_cabinet", "temperature_heatsink",
                   "temperature_transformer", "temperature_other"],
        "dca_mppt_sf": ["current_dc_mppt1", "current_dc_mppt2"],
        "dcv_mppt_sf": ["voltage_dc_mppt1", "voltage_dc_mppt2"],
        "dcw_mppt_sf": ["power_dc_mppt1", "power_dc_mppt2"],
        "dcwh_mppt_sf": ["energy_dc_mppt1", "energy_dc_mppt2"],
    }
    for sf, dependents in expect.items():
        for name in dependents:
            assert regs[name].scale_from == sf, name
    # raw registers stay raw
    for name in ("operating_state", "vendor_state", "event_flags_1",
                 "vendor_event_flags_4", "mppt_modules", "temperature_mppt1"):
        assert regs[name].scale_from == "", name


def test_every_routed_register_is_canonical():
    """The whole point of the rework: a Fronius endpoint speaks EXACTLY the same
    naming as the Janitza reference — canonical names, dictionary-derived
    topics and measurements. Only unrouted SF plumbing may deviate."""
    for path in (INV, MET):
        _t, regs = _by_name(path)
        for r in regs.values():
            d = r.defaults or {}
            # a register with NO defaults is not in the curated set, so nothing
            # routes it anywhere — it is documentation of a point the model
            # has, for hardware that implements it
            routed = bool(d) and ((d.get("mqtt") or {}).get("enabled", True)
                                  or (d.get("influxdb") or {}).get("enabled", True))
            if routed:
                assert is_canonical(r.name), (path, r.name)
                # topic/measurement DERIVE from the dictionary (empty defaults)
                assert not (d.get("mqtt") or {}).get("topic"), r.name
                assert not (d.get("influxdb") or {}).get("measurement"), r.name
            else:
                # unrouted is either SF plumbing, or a real point this hardware
                # does not implement — kept in the map, out of the curated set.
                # Naming discipline still applies to the latter.
                assert r.name.endswith("_sf") or is_canonical(r.name), (path, r.name)


def test_legacy_leaf_map_covers_the_collector_tree():
    leaves = _load(LEAVES)
    for path, tid, legacy in ((INV, "fronius_sunspec_inverter", INV_LEGACY),
                              (MET, "fronius_sunspec_meter", MET_LEGACY)):
        m = leaves[tid]
        _t, regs = _by_name(path)
        # every routed register has an alias leaf (nothing silently dropped
        # from the legacy tree)
        for r in regs.values():
            d = r.defaults or {}
            if d and (d.get("mqtt") or {}).get("enabled", True):
                assert (mqtt_topic_for(r.name) or r.name) in m, (tid, r.name)
        # spot-pin the collector's exact point names
        for name, leaf in legacy.items():
            assert m[mqtt_topic_for(name)] == leaf, (tid, name)


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
    for path, names in ((INV, ["energy_active_generated"]),
                        (MET, ["energy_active_export", "energy_active_import"])):
        _t, regs = _by_name(path)
        for n in names:
            assert regs[n].monotonic is True, (path, n)


def test_identity_block_present_and_static():
    for path in (INV, MET):
        _t, regs = _by_name(path)
        for name, doc in (("manufacturer", 40005), ("model", 40021),
                          ("serial", 40053)):
            r = regs[name]
            assert r.address == doc - 1
            assert r.data_type == "string:16"
            assert r.poll_group == "static"


# ── P3: power factor is a fraction, and the status ships decoded ─────────────

def _tpl(path):
    with open(path) as f:
        return parse_template(json.load(f))


def test_power_factor_carries_the_percent_to_fraction_conversion():
    """SunSpec reports PF as a PERCENTAGE. Without the fixed scale on top of
    the dynamic scale factor, a Fronius unit's power_factor/total read ±100
    while the Janitza next to it read ±1 — one canonical topic, two meanings."""
    for path, names in ((INV, ["power_factor_total"]),
                        (MET, ["power_factor_total", "power_factor_l1",
                               "power_factor_l2", "power_factor_l3"])):
        t = _tpl(path)
        for n in names:
            r = next(x for x in t.registers if x.name == n)
            assert r.scale_from == "pf_sf", (path, n)
            assert r.scale == 100, (path, n)


def test_the_inverter_ships_its_status_decoded():
    t = _tpl(INV)
    by_name = {c.name: c for c in t.calculated}
    assert set(by_name) == {"status_text", "status_alarm", "status_active"}
    assert by_name["status_text"].topic == "status/text"
    assert by_name["status_alarm"].topic == "status/alarm"
    assert by_name["status_active"].topic == "status/active"
    # text is MQTT-only; InfluxDB has no use for a string state
    assert by_name["status_text"].influxdb is False
    # the flags land in the same measurement as the code they derive from
    assert by_name["status_alarm"].measurement == "status"
    # every derived name is in the canonical dictionary, like every other leaf
    for n, c in by_name.items():
        assert is_canonical(n), n
        assert mqtt_topic_for(n) == c.topic, n


def test_status_text_matches_the_reference_collector_wording():
    """Anything reading the old collector's `status` leaf must see the same
    words on the new one — the migration moves a topic, not a vocabulary."""
    expected = {
        1: "Off", 2: "Sleeping (auto-shutdown)", 3: "Starting up",
        4: "Tracking power point", 5: "Forced power reduction",
        6: "Shutting down", 7: "One or more faults exist", 8: "Standby",
        9: "No SolarNet communication", 10: "No communication with inverter",
        11: "Overcurrent on SolarNet plug", 12: "Inverter is being updated",
        13: "AFCI Event",
    }
    enum = next(c for c in _tpl(INV).calculated if c.name == "status_text").enum
    assert {int(k): v for k, v in enum.items()} == expected


def test_alarm_and_active_cover_the_vendor_code_sets():
    from multibus import expressions
    calcs = {c.name: expressions.compile_expression(c.expr)
             for c in _tpl(INV).calculated if c.name != "status_text"}
    # the vendor table flags these as alarms; 4/5 are the producing states
    alarm, active = {5, 7, 9, 10, 11, 13}, {4, 5}
    for code in range(1, 14):
        got = {n: int(expressions.evaluate_tree(tree, lambda _r, c=code: c))
               for n, tree in calcs.items()}
        assert got["status_alarm"] == (1 if code in alarm else 0), code
        assert got["status_active"] == (1 if code in active else 0), code


# ── the map is what the hardware actually answers ───────────────────────────

# Verified against a production Symo Advanced 20.0-3-M on 2026-09-12: every one
# of these answers with the SunSpec not-implemented sentinel, always.
UNIMPLEMENTED = {
    "current_dc", "dca_sf", "voltage_dc", "dcv_sf",
    "temperature_cabinet", "temperature_heatsink", "temperature_transformer",
    "temperature_other", "tmp_sf", "temperature_mppt1", "temperature_mppt2",
}


def test_points_this_hardware_never_answers_are_not_curated():
    """They stay in the map — a Primo or a GEN24 may well implement them — but
    nobody polls a hole by default."""
    t = _tpl(INV)
    by_name = {r.name: r for r in t.registers}
    for n in UNIMPLEMENTED:
        assert n in by_name, f"{n} should stay documented in the map"
        assert not by_name[n].defaults, f"{n} must not be in the curated set"
    # and the points that DO answer are still curated
    for n in ("power_dc", "dcw_sf", "power_active_total", "energy_active_generated",
              "voltage_l1_n", "current_l1", "power_dc_mppt1"):
        assert by_name[n].defaults, n


def test_the_mppt_block_polls_on_its_own_cadence():
    """It sits 135 registers past the AC block, so it can never share a read
    with it (Modbus caps one read at 125). Leaving it in the fast group made
    every AC sweep cost two transactions — and transactions, not registers, are
    what a slow master charges for."""
    t = _tpl(INV)
    groups = {g: t.poll_groups[g]["interval"] for g in t.poll_groups}
    assert groups == {"normal": 5, "slow": 30, "static": 3600, "controls": 3600}
    for r in t.registers:
        if not r.defaults:
            continue
        if r.category == "mppt" or r.name.endswith("_mppt_sf"):
            assert r.poll_group == "slow", r.name
        elif r.category == "identity":
            assert r.poll_group == "static", r.name
        elif r.category == "controls" or r.name == "wmaxlimpct_sf":
            # model 123 sits between the AC block and the MPPT block; read on
            # its own so a controller's read-back never rides the AC sweep
            assert r.poll_group == "controls", r.name
        else:
            assert r.poll_group == "normal", r.name


def test_every_curated_group_costs_exactly_one_transaction():
    """The whole point of the tiering: one fast read and one slow read per
    inverter, never more."""
    from multibus.config import ModbusConfig, SelectedRegister
    from multibus.modbus_client import ModbusConnection, RegisterPoller
    from multibus.register_parser import RegisterParser

    t = _tpl(INV)
    conn = ModbusConnection(ModbusConfig(host="192.0.2.5", max_gap=20))
    by = {}
    for r in t.registers:
        if r.defaults:
            by.setdefault(r.poll_group or "normal", []).append(r)
    assert set(by) == {"normal", "slow", "static", "controls"}
    for g, rs in by.items():
        sel = [SelectedRegister(address=r.address, name=r.name, label=r.label,
                                unit=r.unit, data_type=r.data_type, poll_group=g,
                                scale=r.scale, scale_from=r.scale_from, nan=r.nan,
                                register_type=r.register_type) for r in rs]
        p = RegisterPoller(g, t.poll_groups[g]["interval"], sel, conn,
                           RegisterParser("big"), lambda *a: None, "x")
        assert len(p._read_groups) == 1, (g, p._read_groups)
