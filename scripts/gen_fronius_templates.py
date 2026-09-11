# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Generate the Fronius SunSpec (int + scale factor) device templates.

The register maps below are the ones a dedicated Fronius collector has run in
production for years, verified live against 4 Symo units + 1 Smart Meter
behind a DataManager (raw-frame decode parity on every field, 2026-09-11).
Regenerate with:  python scripts/gen_fronius_templates.py

Address convention: template addresses are PDU (0-indexed, passed to pymodbus
verbatim) = the SunSpec documented "4xxxx" address − 1. Example: the model-103
measurement block documented at 40072 lives at PDU **40071**.

Vendor deltas NOT expressible in a generic template (handled by consumers /
documented for the shadow-parity harness):
- Power factor: Fronius reports PF raw ±10000 with SF −2 (spec says −4 for
  that magnitude) and sometimes SF 0 meaning −2. The generic decode yields
  ±100 where the collector forces ±1.0.
- Derived topics (`status` text, `alarm`, `active`, `events` JSON) are
  computed, not registers.
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "multibus" / "device_templates"

# ── compact spec tables: (doc_addr, name, dtype, sf_ref, mqtt_topic, unit, cat)
# doc_addr is the SunSpec documented address; PDU = doc_addr - 1 is emitted.
# sf_ref "" = no dynamic scaling. mqtt_topic "" = SF/aux register (not routed).

INVERTER_103 = [
    (40072, "ac_current",      "uint16", "a_sf",   "A",       "A",  "ac"),
    (40073, "ac_current_a",    "uint16", "a_sf",   "AphA",    "A",  "ac"),
    (40074, "ac_current_b",    "uint16", "a_sf",   "AphB",    "A",  "ac"),
    (40075, "ac_current_c",    "uint16", "a_sf",   "AphC",    "A",  "ac"),
    (40076, "a_sf",            "int16",  "",       "",        "",   "sf"),
    (40077, "ac_voltage_ab",   "uint16", "v_sf",   "PPVphAB", "V",  "ac"),
    (40078, "ac_voltage_bc",   "uint16", "v_sf",   "PPVphBC", "V",  "ac"),
    (40079, "ac_voltage_ca",   "uint16", "v_sf",   "PPVphCA", "V",  "ac"),
    (40080, "ac_voltage_an",   "uint16", "v_sf",   "PhVphA",  "V",  "ac"),
    (40081, "ac_voltage_bn",   "uint16", "v_sf",   "PhVphB",  "V",  "ac"),
    (40082, "ac_voltage_cn",   "uint16", "v_sf",   "PhVphC",  "V",  "ac"),
    (40083, "v_sf",            "int16",  "",       "",        "",   "sf"),
    (40084, "ac_power",        "int16",  "w_sf",   "W",       "W",  "ac"),
    (40085, "w_sf",            "int16",  "",       "",        "",   "sf"),
    (40086, "ac_frequency",    "uint16", "hz_sf",  "Hz",      "Hz", "ac"),
    (40087, "hz_sf",           "int16",  "",       "",        "",   "sf"),
    (40088, "apparent_power",  "int16",  "va_sf",  "VA",      "VA", "ac"),
    (40089, "va_sf",           "int16",  "",       "",        "",   "sf"),
    (40090, "reactive_power",  "int16",  "var_sf", "VAr",     "var", "ac"),
    (40091, "var_sf",          "int16",  "",       "",        "",   "sf"),
    (40092, "power_factor",    "int16",  "pf_sf",  "PF",      "",   "ac"),
    (40093, "pf_sf",           "int16",  "",       "",        "",   "sf"),
    (40094, "lifetime_energy", "uint32", "wh_sf",  "WH",      "Wh", "energy"),
    (40096, "wh_sf",           "int16",  "",       "",        "",   "sf"),
    (40097, "dc_current",      "uint16", "dca_sf", "DCA",     "A",  "dc"),
    (40098, "dca_sf",          "int16",  "",       "",        "",   "sf"),
    (40099, "dc_voltage",      "uint16", "dcv_sf", "DCV",     "V",  "dc"),
    (40100, "dcv_sf",          "int16",  "",       "",        "",   "sf"),
    (40101, "dc_power",        "int16",  "dcw_sf", "DCW",     "W",  "dc"),
    (40102, "dcw_sf",          "int16",  "",       "",        "",   "sf"),
    (40103, "temp_cabinet",    "int16",  "tmp_sf", "TmpCab",  "°C", "temperature"),
    (40104, "temp_heatsink",   "int16",  "tmp_sf", "TmpSnk",  "°C", "temperature"),
    (40105, "temp_transformer", "int16", "tmp_sf", "TmpTrns", "°C", "temperature"),
    (40106, "temp_other",      "int16",  "tmp_sf", "TmpOt",   "°C", "temperature"),
    (40107, "tmp_sf",          "int16",  "",       "",        "",   "sf"),
    # St/StVnd are published RAW (the dashboard's offline detection reads the
    # numeric SunSpec code) — so no enum decode and no nan sentinel here.
    (40108, "status_code",     "uint16", None,     "St",      "",   "status"),
    (40109, "status_vendor",   "uint16", None,     "StVnd",   "",   "status"),
    (40110, "evt1",            "uint32", None,     "evt1",    "",   "status"),
    (40112, "evt2",            "uint32", None,     "evt2",    "",   "status"),
    (40114, "evt_vnd1",        "uint32", None,     "evt_vnd1", "",  "status"),
    (40116, "evt_vnd2",        "uint32", None,     "evt_vnd2", "",  "status"),
    (40118, "evt_vnd3",        "uint32", None,     "evt_vnd3", "",  "status"),
    (40120, "evt_vnd4",        "uint32", None,     "evt_vnd4", "",  "status"),
]

# SunSpec model 160 (MPPT) — SFs at doc 40256-40259, module 1 at 40264,
# module 2 at 40284. IDStr/Tms omitted (never consumed).
MPPT_160 = [
    (40256, "dca_mppt_sf",       "int16",  "",            "",                 "",   "sf"),
    (40257, "dcv_mppt_sf",       "int16",  "",            "",                 "",   "sf"),
    (40258, "dcw_mppt_sf",       "int16",  "",            "",                 "",   "sf"),
    (40259, "dcwh_mppt_sf",      "int16",  "",            "",                 "",   "sf"),
    (40262, "mppt_num_modules",  "uint16", None,          "mppt/num_modules", "",   "mppt"),
    (40273, "mppt1_dc_current",  "uint16", "dca_mppt_sf", "mppt/string1/DCA", "A",  "mppt"),
    (40274, "mppt1_dc_voltage",  "uint16", "dcv_mppt_sf", "mppt/string1/DCV", "V",  "mppt"),
    (40275, "mppt1_dc_power",    "uint16", "dcw_mppt_sf", "mppt/string1/DCW", "W",  "mppt"),
    (40276, "mppt1_dc_energy",   "uint32", "dcwh_mppt_sf", "mppt/string1/DCWH", "Wh", "mppt"),
    (40280, "mppt1_temperature", "int16",  "",          "mppt/string1/Tmp", "°C", "mppt"),
    (40293, "mppt2_dc_current",  "uint16", "dca_mppt_sf", "mppt/string2/DCA", "A",  "mppt"),
    (40294, "mppt2_dc_voltage",  "uint16", "dcv_mppt_sf", "mppt/string2/DCV", "V",  "mppt"),
    (40295, "mppt2_dc_power",    "uint16", "dcw_mppt_sf", "mppt/string2/DCW", "W",  "mppt"),
    (40296, "mppt2_dc_energy",   "uint32", "dcwh_mppt_sf", "mppt/string2/DCWH", "Wh", "mppt"),
    (40300, "mppt2_temperature", "int16",  "",          "mppt/string2/Tmp", "°C", "mppt"),
]

METER_203 = [
    (40072, "current_total",   "int16",  "a_sf",  "A",       "A",  "ac"),
    (40073, "current_a",       "int16",  "a_sf",  "AphA",    "A",  "ac"),
    (40074, "current_b",       "int16",  "a_sf",  "AphB",    "A",  "ac"),
    (40075, "current_c",       "int16",  "a_sf",  "AphC",    "A",  "ac"),
    (40076, "a_sf",            "int16",  "",      "",        "",   "sf"),
    (40077, "voltage_ln_avg",  "int16",  "v_sf",  "PhV",     "V",  "ac"),
    (40078, "voltage_an",      "int16",  "v_sf",  "PhVphA",  "V",  "ac"),
    (40079, "voltage_bn",      "int16",  "v_sf",  "PhVphB",  "V",  "ac"),
    (40080, "voltage_cn",      "int16",  "v_sf",  "PhVphC",  "V",  "ac"),
    (40081, "voltage_ll_avg",  "int16",  "v_sf",  "PPV",     "V",  "ac"),
    (40082, "voltage_ab",      "int16",  "v_sf",  "PPVphAB", "V",  "ac"),
    (40083, "voltage_bc",      "int16",  "v_sf",  "PPVphBC", "V",  "ac"),
    (40084, "voltage_ca",      "int16",  "v_sf",  "PPVphCA", "V",  "ac"),
    (40085, "v_sf",            "int16",  "",      "",        "",   "sf"),
    (40086, "frequency",       "int16",  "hz_sf", "Hz",      "Hz", "ac"),
    (40087, "hz_sf",           "int16",  "",      "",        "",   "sf"),
    (40088, "power_total",     "int16",  "w_sf",  "W",       "W",  "ac"),
    (40089, "power_a",         "int16",  "w_sf",  "WphA",    "W",  "ac"),
    (40090, "power_b",         "int16",  "w_sf",  "WphB",    "W",  "ac"),
    (40091, "power_c",         "int16",  "w_sf",  "WphC",    "W",  "ac"),
    (40092, "w_sf",            "int16",  "",      "",        "",   "sf"),
    (40093, "va_total",        "int16",  "va_sf", "VA",      "VA", "ac"),
    (40094, "va_a",            "int16",  "va_sf", "VAphA",   "VA", "ac"),
    (40095, "va_b",            "int16",  "va_sf", "VAphB",   "VA", "ac"),
    (40096, "va_c",            "int16",  "va_sf", "VAphC",   "VA", "ac"),
    (40097, "va_sf",           "int16",  "",      "",        "",   "sf"),
    (40098, "var_total",       "int16",  "var_sf", "VAR",    "var", "ac"),
    (40099, "var_a",           "int16",  "var_sf", "VARphA", "var", "ac"),
    (40100, "var_b",           "int16",  "var_sf", "VARphB", "var", "ac"),
    (40101, "var_c",           "int16",  "var_sf", "VARphC", "var", "ac"),
    (40102, "var_sf",          "int16",  "",      "",        "",   "sf"),
    (40103, "pf_avg",          "int16",  "pf_sf", "PF",      "",   "ac"),
    (40104, "pf_a",            "int16",  "pf_sf", "PFphA",   "",   "ac"),
    (40105, "pf_b",            "int16",  "pf_sf", "PFphB",   "",   "ac"),
    (40106, "pf_c",            "int16",  "pf_sf", "PFphC",   "",   "ac"),
    (40107, "pf_sf",           "int16",  "",      "",        "",   "sf"),
    (40108, "energy_exported",   "uint32", "wh_sf", "TotWhExp",    "Wh", "energy"),
    (40110, "energy_exported_a", "uint32", "wh_sf", "TotWhExpPhA", "Wh", "energy"),
    (40112, "energy_exported_b", "uint32", "wh_sf", "TotWhExpPhB", "Wh", "energy"),
    (40114, "energy_exported_c", "uint32", "wh_sf", "TotWhExpPhC", "Wh", "energy"),
    (40116, "energy_imported",   "uint32", "wh_sf", "TotWhImp",    "Wh", "energy"),
    (40118, "energy_imported_a", "uint32", "wh_sf", "TotWhImpPhA", "Wh", "energy"),
    (40120, "energy_imported_b", "uint32", "wh_sf", "TotWhImpPhB", "Wh", "energy"),
    (40122, "energy_imported_c", "uint32", "wh_sf", "TotWhImpPhC", "Wh", "energy"),
    (40124, "wh_sf",           "int16",  "",      "",        "",   "sf"),
]

# SunSpec common block (model 1) — static identity, polled hourly.
# Opt/Vr are omitted on purpose: including them would make the identity span
# one contiguous 64-register batch, over the DataManager's ~50-register cap.
IDENTITY_1 = [
    (40005, "manufacturer",  "string:16", None, "manufacturer",  "", "identity"),
    (40021, "model",         "string:16", None, "model",         "", "identity"),
    (40053, "serial_number", "string:16", None, "serial_number", "", "identity"),
]

MONOTONIC = {"lifetime_energy", "mppt1_dc_energy", "mppt2_dc_energy",
             "energy_exported", "energy_exported_a", "energy_exported_b",
             "energy_exported_c", "energy_imported", "energy_imported_a",
             "energy_imported_b", "energy_imported_c"}

DASHBOARD = {"ac_power", "dc_power", "lifetime_energy", "status_code",
             "power_total", "energy_exported", "energy_imported"}


def build_registers(rows, poll_group, measurement):
    out = []
    for doc_addr, name, dtype, sf_ref, topic, unit, cat in rows:
        is_sf = cat == "sf"
        r = {
            "address": doc_addr - 1,          # PDU = documented SunSpec − 1
            "name": name,
            "label": name.replace("_", " ").title() if not is_sf
                     else name.upper().replace("_SF", " SF"),
            "unit": unit,
            "data_type": dtype,
            "register_type": "holding",
            "category": cat,
            "poll_group": poll_group,
        }
        if sf_ref:
            r["scale_from"] = sf_ref
        # sentinel semantics mirror the reference collector: every measurement
        # decodes 0xFFFF/0x8000/0xFFFFFFFF as missing; St/StVnd/evt are raw
        if sf_ref is not None or is_sf:
            r["nan"] = True
        if name in MONOTONIC:
            r["monotonic"] = True
        r["defaults"] = {
            "mqtt": ({"enabled": True, "topic": topic} if topic
                     else {"enabled": False, "topic": ""}),
            "influxdb": ({"enabled": True, "measurement": measurement,
                          "tags": {}} if topic
                         else {"enabled": False, "measurement": "", "tags": {}}),
            "ui": {"show_on_dashboard": name in DASHBOARD, "widget": "value"},
        }
        out.append(r)
    return out


def template(tid, name, model, description, registers, poll_groups):
    return {"device_template": {
        "schema_version": 1,
        "id": tid,
        "name": name,
        "vendor": "Fronius",
        "model": model,
        "version": "1.0.0",
        "description": description,
        "source_document": "SunSpec Information Models (int+SF); register map "
                           "verified live against a production Fronius fleet "
                           "(raw-frame decode parity, 2026-09-11)",
        "protocol": {"byte_order": "big", "default_register_type": "holding"},
        "poll_groups": poll_groups,
        "categories": {
            "ac": "AC measurements", "dc": "DC side", "energy": "Energy",
            "temperature": "Temperatures", "status": "Status & events",
            "mppt": "MPPT strings", "identity": "Device identity",
            "sf": "SunSpec scale factors",
        },
        "registers": registers,
        "canonical": True,
    }}


def main():
    inv = template(
        "fronius_sunspec_inverter",
        "Fronius SunSpec inverter (int+SF, via datalogger)",
        "Symo / Primo / Eco (SunSpec 103)",
        "Fronius inverter read THROUGH its DataManager/datalogger over Modbus "
        "TCP, in the SunSpec int + scale-factor register mode (models "
        "1/103/160: AC block, DC block, temperatures, status/events, per-"
        "string MPPT, identity). Use this when the datalogger's Modbus TCP "
        "slave is enabled and set to 'int+SF' (the Fronius default). For "
        "SEVERAL inverters behind one datalogger, add a `plants:` entry with "
        "this template and the unit IDs (1, 2, ...) — each unit becomes its "
        "own device. CAUTION: dataloggers serve only a few concurrent Modbus "
        "clients; if another system polls the same datalogger, keep poll "
        "intervals modest (10-15 s) or reduce the number of units read in "
        "parallel. Known vendor quirk: PF is published with the generic "
        "SunSpec decode (±100); Fronius units report PF raw ±10000 with an "
        "out-of-spec scale factor, so dedicated drivers show ±1.0.",
        build_registers(INVERTER_103, "normal", "fronius_inverter")
        + build_registers(MPPT_160, "normal", "fronius_inverter")
        + build_registers(IDENTITY_1, "static", "fronius_inverter"),
        {"normal": {"interval": 5, "description": "Measurements + MPPT"},
         "static": {"interval": 3600, "description": "Identity strings"}},
    )
    met = template(
        "fronius_sunspec_meter",
        "Fronius SunSpec meter (int+SF, via datalogger)",
        "Smart Meter 63A/50kA (SunSpec 203)",
        "Fronius Smart Meter read THROUGH the DataManager/datalogger over "
        "Modbus TCP (SunSpec model 203, int + scale factor; full 3-phase set "
        "+ per-phase import/export energies). The meter appears on the "
        "datalogger at unit ID 240 (default; 241/242 for additional meters). "
        "Use this when the meter hangs off a Fronius datalogger — for a "
        "Smart Meter wired DIRECTLY to your own RS-485 (RTU or an RTU-TCP "
        "bridge), use the separate 'Fronius Smart Meter 65A-3 (RTU)' "
        "template instead: same hardware, completely different register "
        "map. PF quirk as on the inverter template (generic ±100).",
        build_registers(METER_203, "normal", "fronius_meter")
        + build_registers(IDENTITY_1, "static", "fronius_meter"),
        {"normal": {"interval": 2, "description": "Measurements"},
         "static": {"interval": 3600, "description": "Identity strings"}},
    )
    for t in (inv, met):
        p = OUT / f"{t['device_template']['id']}.json"
        p.write_text(json.dumps(t, indent=2, ensure_ascii=False) + "\n",
                     encoding="utf-8")
        print(f"wrote {p} ({len(t['device_template']['registers'])} registers)")


if __name__ == "__main__":
    main()
