# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The bench inverter maps (Deye single-phase hybrid, Huawei SUN2000 with its
DTSU666 meter and LUNA2000 battery) load from the registry, validate on save,
say what they are, and decode to the gateway's canonical units and signs."""
import json
from pathlib import Path

import pytest

from multibus.bus_scan import _check, _decode
from multibus.device_template import TemplateRegistry, validate_template
from multibus.expressions import evaluate
from multibus.register_parser import RegisterParser
from multibus.value_decode import apply_corrections

TPL_DIR = Path("multibus/device_templates")
NEW = {"deye_hybrid_1p": "inverter", "huawei_sun2000": "inverter",
       "huawei_dtsu666_via_sun2000": "meter", "huawei_luna2000": "battery"}


@pytest.fixture(scope="module")
def registry(tmp_path_factory):
    reg = TemplateRegistry(user_dir=tmp_path_factory.mktemp("user"))
    reg.reload()
    return reg


def _raw(tid):
    return json.loads((TPL_DIR / f"{tid}.json").read_text(encoding="utf-8"))


def _row(tpl, name):
    return next(r for r in tpl.registers if r.name == name)


def _value(tpl, name, words):
    """Decode a row the way the poller does: parser (template word order),
    then the shared correction pipeline (scale, offset, sign)."""
    r = _row(tpl, name)
    raw = RegisterParser(tpl.protocol.get("byte_order", "big")).parse_value(words, r.data_type, r.nan)
    return apply_corrections(raw, r)


@pytest.mark.parametrize("tid,kind", sorted(NEW.items()))
def test_bench_templates_load_validate_and_say_their_kind(registry, tid, kind):
    assert validate_template(_raw(tid)) == []                 # the strict (save) checks
    assert tid in {t.id for t in registry.list()}
    assert registry.load_errors == {} and registry.load_warnings == {}
    t = registry.get(tid)
    assert t.builtin and t.kind == kind and t.summary()["kind"] == kind
    assert all(r.register_type == "holding" for r in t.registers)    # FC03 only
    for r in t.registers:
        if r.name.startswith("energy_"):
            assert r.unit in ("Wh", "varh") and (r.monotonic or r.daily), r.name
    # a template a bus scan could propose twice would suggest nothing
    assert not t.components


# ── Lot C: Deye single-phase hybrid ─────────────────────────────────────────

def test_deye_is_read_only_through_and_through(registry):
    t = registry.get("deye_hybrid_1p")
    assert not any(r.writable for r in t.registers)
    assert all(r.access == "RD" for r in t.registers)
    assert not t.commands and not t.setup and not t.rules and not t.channels
    raw = _raw("deye_hybrid_1p")["device_template"]
    assert not any(k.startswith("write") for r in raw["registers"] for k in r)
    assert t.protocol["default_unit_id"] == 1 and set(t.protocol["transports"]) == {"rtu", "tcp"}
    assert "read-only" in t.description.lower() and "9600 8N1" in t.description
    assert "verified on site" in t.description.lower()
    # each row says whether the bench checked it (test_deye_rows_say_whether_the_bench_checked_them)
    assert all("checked on hardware" in r.description.lower() for r in t.registers)


def test_deye_rows_sit_at_the_published_addresses(registry):
    t = registry.get("deye_hybrid_1p")
    by = {r.name: r.address for r in t.registers}
    assert by["serial"] == 3 and _row(t, "serial").data_type == "string:5"
    assert by["vendor_state"] == 59 and by["frequency"] == 79
    assert (by["temperature_transformer"], by["temperature_heatsink"], by["battery_temperature"]) == (90, 91, 182)
    assert (by["pack_voltage"], by["soc"], by["battery_power"], by["battery_current"]) == (183, 184, 190, 191)
    assert (by["voltage_l1_n"], by["current_l1"], by["power_grid"], by["grid_ct_power"]) == (150, 160, 169, 172)
    assert (by["power_active_total"], by["power_load"]) == (175, 178)
    assert [by[f"{q}_dc_mppt{n}"] for n in (1, 2) for q in ("voltage", "current", "power")] == \
        [109, 110, 186, 111, 112, 187]
    assert [by[f"fault_word_{i}"] for i in (1, 2, 3, 4)] == [103, 104, 105, 106]
    assert by["energy_active_export"] == 81 and by["energy_active_generated"] == 96


def test_deye_totals_are_low_word_first_and_served_in_wh(registry):
    t = registry.get("deye_hybrid_1p")
    assert t.protocol["byte_order"] == "little"
    # 0x0001_86A0 = 100000 x 0.1 kWh = 10 000 kWh: low word 0x86A0 on the wire first
    assert _value(t, "energy_active_generated", [0x86A0, 0x0001]) == pytest.approx(10_000_000)
    assert _value(t, "battery_energy_charged_today", [123]) == pytest.approx(12_300)
    assert _row(t, "energy_active_generated").monotonic and _row(t, "energy_today").daily
    # the total import is split across 78 and 80 (79 is the frequency)
    calc = next(c for c in t.calculated if c.name == "energy_active_import")
    vals = {"grid_import_total_low": 0x86A0, "grid_import_total_high": 1}
    assert evaluate(calc.expr, vals.get) == pytest.approx(10_000_000)


def test_deye_signs_follow_the_canonical_conventions(registry):
    t = registry.get("deye_hybrid_1p")
    # Deye: + discharging -> canonical battery_power / battery_current + charging
    assert _value(t, "battery_power", [1500]) == -1500
    assert _value(t, "battery_power", [0x10000 - 800]) == 800            # -800 W = charging
    assert _value(t, "battery_current", [2550]) == pytest.approx(-25.5)
    # grid: Deye + buying is already power_grid's + import (negative = exporting)
    assert _value(t, "power_grid", [0x10000 - 2000]) == -2000
    # load: Deye + consuming -> power_load negative = consuming
    assert _value(t, "power_load", [900]) == -900
    # temperatures carry a +100 degC offset at 0.1 degC
    assert _value(t, "temperature_heatsink", [1253]) == pytest.approx(25.3)
    assert _value(t, "battery_temperature", [950]) == pytest.approx(-5.0)


def test_deye_fault_bits_name_the_display_code(registry):
    t = registry.get("deye_hybrid_1p")
    f1, f4 = _row(t, "fault_word_1"), _row(t, "fault_word_4")
    assert apply_corrections(1 << 0, f1).startswith("F01")
    assert apply_corrections(1 << 15, f4).startswith("F64")


def test_deye_identify_reads_the_device_type(registry):
    idf = registry.get("deye_hybrid_1p").identify["registers"]
    dev_type = next(x for x in idf if x["address"] == 0)
    assert _check(3, dev_type) and _check(0x0300, dev_type) and not _check(2, dev_type)


# ── Deye bench check: SUN-8K-SG05LP1, RS485 9600 8N1, unit 1, no BMS (2026-10-27) ──

def _s16(v):
    return v & 0xFFFF


def _u32(v):
    return [v & 0xFFFF, v >> 16]          # low word first, as the inverter sends it


# Raw words the bench inverter answered: every value the bench gateway
# published, inverted back through the template's scale and offset.
DEYE_BENCH = {
    "device_type": [3], "protocol_version": [513], "vendor_state": [2],
    "voltage_l1_n": [2313], "frequency": [4998], "current_l1": [751], "inverter_current": [50],
    "power_grid": [1572], "grid_ct_power": [24], "power_active_total": [0], "power_load": [1573],
    "voltage_dc_mppt1": [2455], "current_dc_mppt1": [4], "power_dc_mppt1": [115],
    "voltage_dc_mppt2": [2455], "current_dc_mppt2": [7], "power_dc_mppt2": [181],
    "pack_voltage": [5227], "battery_current": [_s16(-362)], "battery_power": [_s16(-189)],
    "soc": [0], "battery_temperature": [0],
    "temperature_heatsink": [1462], "temperature_transformer": [1486],
    "battery_energy_charged_today": [1], "battery_energy_discharged_today": [2],
    "battery_energy_charged_total": _u32(112127), "battery_energy_discharged_total": _u32(97918),
    "grid_energy_import_today": [67], "grid_energy_export_today": [0],
    "grid_import_total_low": [31400], "grid_import_total_high": [3],
    "energy_active_export": _u32(253), "energy_active_generated": _u32(190458),
    "load_energy_today": [66], "load_energy_total": _u32(398145), "energy_today": [4],
}

# rows whose bench value agreed with the physics and with the other rows
DEYE_CHECKED = {
    "device_type", "protocol_version", "vendor_state", "voltage_l1_n", "frequency", "current_l1",
    "power_grid", "power_load", "pack_voltage", "battery_power", "battery_current",
    "temperature_heatsink", "temperature_transformer",
    *(f"{q}_dc_mppt{n}" for q in ("voltage", "current", "power") for n in (1, 2)),
    "battery_energy_charged_today", "battery_energy_discharged_today",
    "battery_energy_charged_total", "battery_energy_discharged_total",
    "grid_energy_import_today", "grid_energy_export_today",
    "grid_import_total_low", "grid_import_total_high", "energy_active_export",
    "energy_active_generated", "load_energy_today", "load_energy_total", "energy_today",
    # the inverter display (2026-10-27 09:31) confirmed 172 is the external CT
    # ("Total CT Power", noise near 0 with no CT fitted)
    "grid_ct_power",
}
CHECKED_MARK = "checked on hardware (sg05lp1, 2026-10-27)"


def test_deye_bench_words_decode_to_what_the_inverter_was_doing(registry):
    t = registry.get("deye_hybrid_1p")
    v = {n: _value(t, n, w) for n, w in DEYE_BENCH.items()}
    assert v["voltage_l1_n"] == pytest.approx(231.3) and v["frequency"] == pytest.approx(49.98)
    # importing 1572 W at the GRID port: + import, the canonical power_grid sign
    assert v["power_grid"] == 1572
    # the load is 1573 W; power_load is canonically negative while consuming
    # (canonical_fields: "House load (negative = consuming)", as Fronius P_Load)
    assert v["power_load"] == -1573
    # PV charges the battery: raw -189 W (Deye: negative = charging) ->
    # canonical battery_power / battery_current + charging
    assert v["battery_power"] == 189 and v["battery_current"] == pytest.approx(3.62)
    # the direct raw read half an hour earlier, also while charging
    assert _value(t, "battery_power", [_s16(-244)]) == 244
    assert _value(t, "battery_current", [_s16(-467)]) == pytest.approx(4.67)
    assert v["pack_voltage"] == pytest.approx(52.27)
    # raw 0 at 182 = no BMS / no sensor: absent, never -100 degC
    assert v["battery_temperature"] is None
    assert _value(t, "battery_temperature", [1253]) == pytest.approx(25.3)
    assert v["temperature_heatsink"] == pytest.approx(46.2)
    assert v["temperature_transformer"] == pytest.approx(48.6)
    # the import total rebuilt from 78 (low) and 80 (high): 22 800.8 kWh
    calc = next(c for c in t.calculated if c.name == "energy_active_import")
    imp = evaluate(calc.expr, v.get)
    assert imp == pytest.approx(22_800_800)
    # the 32-bit totals are low word first: years of service balance within a
    # few percent (load = import + PV - export - net battery charge - losses)
    net_charge = v["battery_energy_charged_total"] - v["battery_energy_discharged_total"]
    supplied = imp + v["energy_active_generated"] - v["energy_active_export"] - net_charge
    assert v["load_energy_total"] == pytest.approx(39_814_500)
    assert 0.95 < v["load_energy_total"] / supplied <= 1.0
    # today's import fed today's load
    assert v["load_energy_today"] == pytest.approx(6600) and v["grid_energy_import_today"] == pytest.approx(6700)


def test_deye_import_halves_feed_the_total_and_are_not_published(registry):
    t = registry.get("deye_hybrid_1p")
    halves = {"grid_import_total_low", "grid_import_total_high"}
    plumbing = halves | {"gen_energy_total_low", "gen_energy_total_high"}     # 92/95, same split
    for r in t.registers:
        sinks = (r.defaults.get("mqtt", {}).get("enabled"), r.defaults.get("influxdb", {}).get("enabled"))
        # every row is curated (seeding picks rows with defaults), the halves are plumbing
        assert sinks == ((False, False) if r.name in plumbing else (True, True)), r.name
    calc = next(c for c in t.calculated if c.name == "energy_active_import")
    assert halves <= set(calc.expr.replace("(", " ").replace(")", " ").split())


def test_deye_grid_rows_say_where_they_measure(registry):
    t = registry.get("deye_hybrid_1p")
    assert "GRID port" in _row(t, "power_grid").label
    assert "external CT" in _row(t, "grid_ct_power").label
    assert _value(t, "grid_ct_power", [_s16(-300)]) == -300          # + import, like power_grid


def test_deye_rows_say_whether_the_bench_checked_them(registry):
    t = registry.get("deye_hybrid_1p")
    names = {r.name for r in t.registers}
    assert DEYE_CHECKED <= names
    for r in t.registers:
        d = r.description.lower()
        if r.name in DEYE_CHECKED:
            assert CHECKED_MARK in d and "not yet checked" not in d, r.name
        else:
            assert "not yet checked on hardware" in d and CHECKED_MARK not in d, r.name


def test_deye_notes_say_how_a_bms_loss_looks(registry):
    t = registry.get("deye_hybrid_1p")
    note = next(n for n in t.notes if "BMS" in n["text"] and "soc" in n.get("fields", []))
    assert {"soc", "battery_temperature", "vendor_state"} <= set(note["fields"])
    for word in ("SOC 0", "raw 0", "Normal", "ADR-0023"):
        assert word in note["text"], word


# ── Deye GEN port (AC-coupled microinverter), the inverter's BMS block and the ──
# ── battery settings: words derived from the inverter display, 2026-10-27 09:31 ──
# (BMS disconnected: the display shows the BMS block at 0, its temperature -100)

DEYE_DISPLAY = {
    "gen_power": [0], "gen_voltage": [2266], "gen_energy_today": [0],
    "gen_energy_total_low": [30724], "gen_energy_total_high": [0],
    "energy_lifetime": _u32(221183), "load_voltage": [2289],
    "bms_voltage": [0], "bms_current": [0], "bms_temperature": [0], "bms_soc": [0],
    "charge_current_limit": [0], "discharge_current_limit": [0],
    "bms_max_charge_current": [0], "bms_max_discharge_current": [0],
    "charge_voltage_limit": [0], "discharge_voltage_limit": [0],
    "battery_rated_capacity": [500], "battery_type": [1],
}
DEYE_GEN_ADDR = {
    "gen_energy_today": 62, "energy_lifetime": 63, "gen_energy_total_low": 92, "gen_energy_total_high": 95,
    "load_voltage": 157, "gen_power": 166, "gen_voltage": 181,
    "battery_type": 200, "battery_rated_capacity": 204,
    "charge_voltage_limit": 312, "discharge_voltage_limit": 313,
    "charge_current_limit": 314, "discharge_current_limit": 315,
    "bms_soc": 316, "bms_voltage": 317, "bms_current": 318, "bms_temperature": 319,
    "bms_max_charge_current": 320, "bms_max_discharge_current": 321,
}


def test_deye_gen_port_and_bms_rows_sit_at_the_published_addresses(registry):
    t = registry.get("deye_hybrid_1p")
    by = {r.name: r for r in t.registers}
    assert {n: by[n].address for n in DEYE_GEN_ADDR if n in by} == DEYE_GEN_ADDR
    assert by["energy_lifetime"].data_type == "uint32" and by["energy_lifetime"].monotonic
    assert by["gen_energy_today"].daily and by["gen_energy_today"].unit == "Wh"
    for n in ("gen_power", "bms_current"):
        assert by[n].data_type == "int16", n                     # signed on the wire
    assert by["battery_type"].enum == {"0": "Lead-acid", "1": "Lithium"}
    assert by["battery_rated_capacity"].poll_group == "static" and by["battery_type"].poll_group == "static"
    assert not any(r.writable for r in t.registers)               # settings are read, never written


def test_deye_display_words_decode_to_what_the_screen_showed(registry):
    t = registry.get("deye_hybrid_1p")
    v = {n: _value(t, n, w) for n, w in DEYE_DISPLAY.items()}
    # Microinverter: MI Voltage 226.60 V, MI Power 0 W, Daily Production MI 0.00 kWh
    assert v["gen_voltage"] == pytest.approx(226.6) and v["gen_power"] == 0
    assert v["gen_energy_today"] == 0
    # Total Production MI 3.07240 MWh, rebuilt from 92 (low) and 95 (high)
    calc = next(c for c in t.calculated if c.name == "gen_energy_total")
    gen_total = evaluate(calc.expr, v.get)
    assert gen_total == pytest.approx(3_072_400)
    # Cumulative Production (Active) 22.11830 MWh = PV 19.04590 MWh + MI 3.07240 MWh
    assert v["energy_lifetime"] == pytest.approx(22_118_300)
    pv_total = _value(t, "energy_active_generated", _u32(190459))
    assert v["energy_lifetime"] == pytest.approx(pv_total + gen_total)
    assert v["load_voltage"] == pytest.approx(228.9)
    # Battery Rated Capacity 500 Ah, Battery Type lithium (enum text)
    assert v["battery_rated_capacity"] == 500 and v["battery_type"] == "Lithium"
    # BMS disconnected: 0 V and -100 degC cannot be real, so they are absent;
    # currents, limits and SOC 0 stay numbers (a BMS may legitimately send 0)
    for n in ("bms_voltage", "bms_temperature", "charge_voltage_limit", "discharge_voltage_limit"):
        assert v[n] is None, n
    for n in ("bms_current", "bms_soc", "charge_current_limit", "discharge_current_limit",
              "bms_max_charge_current", "bms_max_discharge_current"):
        assert v[n] == 0, n
    # with a BMS: 0.01 V voltages, 0.1 degC temperature with the +100 degC offset
    assert _value(t, "bms_voltage", [5310]) == pytest.approx(53.1)
    assert _value(t, "charge_voltage_limit", [5680]) == pytest.approx(56.8)
    assert _value(t, "bms_temperature", [1253]) == pytest.approx(25.3)
    assert _value(t, "bms_current", [_s16(-40)]) == -40


def test_deye_gen_total_halves_feed_the_total_and_are_not_published(registry):
    t = registry.get("deye_hybrid_1p")
    halves = {"gen_energy_total_low", "gen_energy_total_high"}
    assert halves <= {r.name for r in t.registers}
    calc = next(c for c in t.calculated if c.name == "gen_energy_total")
    assert halves <= set(calc.expr.replace("(", " ").replace(")", " ").split())
    assert calc.unit == "Wh"


def test_deye_notes_describe_the_gen_port_and_the_bms_block(registry):
    t = registry.get("deye_hybrid_1p")
    gen = next(n for n in t.notes if "gen_power" in n.get("fields", []))
    assert "microinverter" in gen["text"].lower() and "GEN" in gen["text"]
    bms = next(n for n in t.notes if "bms_voltage" in n.get("fields", []))
    assert "ADR-0023" in bms["text"]
    # the first (warning) note no longer lists the external CT as unchecked
    assert "external CT" not in t.notes[0]["text"]


# ── Lot D: Huawei SUN2000 + DTSU666 + LUNA2000 ──────────────────────────────

def _words(text, n):
    b = text.encode().ljust(2 * n, b"\0")
    return [(b[i] << 8) | b[i + 1] for i in range(0, 2 * n, 2)]


def test_huawei_identify_matches_the_sun2000_model_string(registry):
    t = registry.get("huawei_sun2000")
    rules = t.identify["registers"]

    def matches(model):
        w = _words(model, 15)
        return all(_check(_decode(w[r["address"] - 30000:], r["data_type"], "big"), r) for r in rules)
    assert matches("SUN2000-10KTL-M1") and matches("SUN2000-5KTL-L1") and matches("SUN2000-12K-MB0")
    assert matches("SUN2000L-5KTL")                            # the older L series is a SUN2000 too
    assert not matches("SUN5000-10KTL") and not matches("LUNA2000") and not matches("SUN200")
    # only the inverter is proposed by a scan: the meter and battery sit on its unit ID
    assert not registry.get("huawei_dtsu666_via_sun2000").identify
    assert not registry.get("huawei_luna2000").identify


def test_huawei_inverter_map(registry):
    t = registry.get("huawei_sun2000")
    assert t.protocol["byte_order"] == "big"
    by = {r.name: r for r in t.registers}
    assert (by["model"].address, by["model"].data_type) == (30000, "string:15")
    assert (by["serial"].address, by["part_number"].address, by["model_id"].address) == (30015, 30025, 30070)
    assert (by["pv_string_count"].address, by["mppt_modules"].address) == (30071, 30072)
    assert (by["rated_power"].address, by["rated_power"].data_type) == (30073, "uint32")
    assert [by[f"state_{i}"].address for i in (1, 2, 3)] == [32000, 32002, 32003]
    assert [by[f"alarm_{i}"].address for i in (1, 2, 3)] == [32008, 32009, 32010]
    assert all(by[f"alarm_{i}"].bits for i in (1, 2, 3))
    for n in range(1, 9):
        v, i = by[f"voltage_dc_mppt{n}"], by[f"current_dc_mppt{n}"]
        assert (v.address, v.data_type, v.scale) == (32014 + 2 * n, "int16", 10)
        assert (i.address, i.data_type, i.scale) == (32015 + 2 * n, "int16", 100)
        # PV5-8 are a read of their own: a 4-string model refusing them keeps PV1-4
        assert v.poll_group == i.poll_group == ("fast" if n <= 4 else "pv_extra")
    assert (by["power_dc"].address, by["power_dc"].data_type) == (32064, "int32")
    assert (by["power_active_total"].address, by["power_reactive_total"].address) == (32080, 32082)
    assert (by["power_factor_total"].scale, by["frequency"].scale) == (1000, 100)
    assert (by["temperature_cabinet"].address, by["temperature_cabinet"].scale) == (32087, 10)
    assert (by["vendor_state"].address, by["fault_code"].address) == (32089, 32090)
    status = next(c for c in t.calculated if c.name == "status_text")
    assert status.enum[str(0x0200)] == "On-grid" and status.enum[str(0x0300)] == "Shutdown: fault"
    assert status.enum["0"] == "Standby: initializing"
    assert not any(r.writable for r in t.registers) and not t.commands
    for text in ("Enable unrestricted", "6607", "502", "one modbus client", "9600 8N1", "rtu_tap", "SunSpec"):
        assert text.lower() in t.description.lower(), text


def test_huawei_yield_counters_are_wh(registry):
    t = registry.get("huawei_sun2000")
    total, day = _row(t, "energy_active_generated"), _row(t, "energy_today")
    assert (total.address, day.address) == (32106, 32114)
    assert total.monotonic and day.daily
    # 0.01 kWh: 123456 -> 1234.56 kWh -> 1 234 560 Wh
    assert _value(t, "energy_active_generated", [0x0001, 0xE240]) == pytest.approx(1_234_560)


def test_huawei_meter_power_is_plus_import(registry):
    t = registry.get("huawei_dtsu666_via_sun2000")
    assert _row(t, "power_active_total").address == 37113
    # Huawei: + 2500 W = feeding in -> the gateway's meter: -2500 W
    assert _value(t, "power_active_total", [0x0000, 2500]) == -2500
    assert _value(t, "power_active_total", [0xFFFF, 0x10000 - 700]) == 700      # importing
    assert _value(t, "power_active_l1", [0x0000, 100]) == -100
    exp, imp = _row(t, "energy_active_export"), _row(t, "energy_active_import")
    assert (exp.address, imp.address) == (37119, 37121) and exp.monotonic and imp.monotonic
    assert _value(t, "energy_active_import", [0x0000, 1000]) == pytest.approx(10_000)


def test_huawei_battery_power_is_plus_charging(registry):
    t = registry.get("huawei_luna2000")
    by = {r.name: r.address for r in t.registers}
    assert (by["soc"], by["battery_power"], by["unit1_status"], by["unit1_power"]) == (37760, 37765, 37000, 37001)
    assert _value(t, "battery_power", [0x0000, 3000]) == 3000                    # charging stays +
    assert _value(t, "soc", [875]) == pytest.approx(87.5)
    assert _value(t, "battery_energy_charged_total", [0x0000, 250]) == pytest.approx(2_500)
    assert apply_corrections(2, _row(t, "battery_status")) == "Running"


def test_huawei_templates_settle_after_connect():
    """The SDongle ignores the first seconds after a TCP connect (ADR-0015):
    every Huawei template behind it declares the pause."""
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / 'multibus' / 'device_templates'
    for name in ('huawei_sun2000', 'huawei_dtsu666_via_sun2000', 'huawei_luna2000'):
        t = json.loads((root / f'{name}.json').read_text())['device_template']
        assert t['protocol'].get('connect_settle_s') == 3, name
