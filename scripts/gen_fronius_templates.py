# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Generate the Fronius SunSpec (int + scale factor) device templates.

CANONICAL OUTPUT (2026-09-11 rework): register names come from the canonical
dictionary (multibus/canonical_fields.py), so a Fronius plant publishes the
SAME topics, fields and InfluxDB measurements as every other MBG device
(meters/<device>/power/active/total, measurement `power_active`, field
`power_active_total` — exactly like the Janitza reference). The LEGACY
SunSpec-collector tree (fronius/inverter/N/W …) is NOT baked into the
template: it is a compatibility layer, expressed as `mqtt.compat_aliases`
built from the LEGACY leaf map this script also emits
(scripts/fronius_legacy_leaves.json).

The register maps are the ones a dedicated Fronius collector has run in
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
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from multibus.canonical_fields import CANONICAL_FIELDS, mqtt_topic_for  # noqa: E402

OUT = _ROOT / "multibus" / "device_templates"
LEAVES_OUT = _ROOT / "scripts" / "fronius_legacy_leaves.json"

# ── compact spec tables ──────────────────────────────────────────────────────
# (doc_addr, canonical_name, dtype, sf_ref, LEGACY_leaf, cat)
# doc_addr = SunSpec documented address; PDU = doc_addr − 1 is emitted.
# sf_ref "" = no dynamic scaling; None = raw (no nan either).
# LEGACY_leaf = the topic tail the retired collector used under
# fronius/inverter/N/ (drives the compat alias map, NOT the template).

INVERTER_103 = [
    (40072, "current_total",   "uint16", "a_sf",   "A",       "ac"),
    (40073, "current_l1",      "uint16", "a_sf",   "AphA",    "ac"),
    (40074, "current_l2",      "uint16", "a_sf",   "AphB",    "ac"),
    (40075, "current_l3",      "uint16", "a_sf",   "AphC",    "ac"),
    (40076, "a_sf",            "int16",  "",       None,      "sf"),
    (40077, "voltage_l1_l2",   "uint16", "v_sf",   "PPVphAB", "ac"),
    (40078, "voltage_l2_l3",   "uint16", "v_sf",   "PPVphBC", "ac"),
    (40079, "voltage_l3_l1",   "uint16", "v_sf",   "PPVphCA", "ac"),
    (40080, "voltage_l1_n",    "uint16", "v_sf",   "PhVphA",  "ac"),
    (40081, "voltage_l2_n",    "uint16", "v_sf",   "PhVphB",  "ac"),
    (40082, "voltage_l3_n",    "uint16", "v_sf",   "PhVphC",  "ac"),
    (40083, "v_sf",            "int16",  "",       None,      "sf"),
    (40084, "power_active_total", "int16", "w_sf", "W",       "ac"),
    (40085, "w_sf",            "int16",  "",       None,      "sf"),
    (40086, "frequency",       "uint16", "hz_sf",  "Hz",      "ac"),
    (40087, "hz_sf",           "int16",  "",       None,      "sf"),
    (40088, "power_apparent_total", "int16", "va_sf", "VA",   "ac"),
    (40089, "va_sf",           "int16",  "",       None,      "sf"),
    (40090, "power_reactive_total", "int16", "var_sf", "VAr", "ac"),
    (40091, "var_sf",          "int16",  "",       None,      "sf"),
    (40092, "power_factor_total", "int16", "pf_sf", "PF",     "ac"),
    (40093, "pf_sf",           "int16",  "",       None,      "sf"),
    (40094, "energy_active_generated", "uint32", "wh_sf", "WH", "energy"),
    (40096, "wh_sf",           "int16",  "",       None,      "sf"),
    (40097, "current_dc",      "uint16", "dca_sf", "DCA",     "dc"),
    (40098, "dca_sf",          "int16",  "",       None,      "sf"),
    (40099, "voltage_dc",      "uint16", "dcv_sf", "DCV",     "dc"),
    (40100, "dcv_sf",          "int16",  "",       None,      "sf"),
    (40101, "power_dc",        "int16",  "dcw_sf", "DCW",     "dc"),
    (40102, "dcw_sf",          "int16",  "",       None,      "sf"),
    (40103, "temperature_cabinet",     "int16", "tmp_sf", "TmpCab",  "temperature"),
    (40104, "temperature_heatsink",    "int16", "tmp_sf", "TmpSnk",  "temperature"),
    (40105, "temperature_transformer", "int16", "tmp_sf", "TmpTrns", "temperature"),
    (40106, "temperature_other",       "int16", "tmp_sf", "TmpOt",   "temperature"),
    (40107, "tmp_sf",          "int16",  "",       None,      "sf"),
    # St/StVnd are published RAW (the dashboard's offline detection reads the
    # numeric SunSpec code) — so no enum decode and no nan sentinel here.
    (40108, "operating_state", "uint16", None,     "St",      "status"),
    (40109, "vendor_state",    "uint16", None,     "StVnd",   "status"),
    (40110, "event_flags_1",   "uint32", None,     "evt1",    "status"),
    (40112, "event_flags_2",   "uint32", None,     "evt2",    "status"),
    (40114, "vendor_event_flags_1", "uint32", None, "evt_vnd1", "status"),
    (40116, "vendor_event_flags_2", "uint32", None, "evt_vnd2", "status"),
    (40118, "vendor_event_flags_3", "uint32", None, "evt_vnd3", "status"),
    (40120, "vendor_event_flags_4", "uint32", None, "evt_vnd4", "status"),
]

# SunSpec model 160 (MPPT) — SFs at doc 40256-40259, module 1 at 40264,
# module 2 at 40284. IDStr/Tms omitted (never consumed).
MPPT_160 = [
    (40256, "dca_mppt_sf",     "int16",  "",            None,                 "sf"),
    (40257, "dcv_mppt_sf",     "int16",  "",            None,                 "sf"),
    (40258, "dcw_mppt_sf",     "int16",  "",            None,                 "sf"),
    (40259, "dcwh_mppt_sf",    "int16",  "",            None,                 "sf"),
    (40262, "mppt_modules",    "uint16", None,          "mppt/num_modules",   "mppt"),
    (40273, "current_dc_mppt1", "uint16", "dca_mppt_sf", "mppt/string1/DCA",  "mppt"),
    (40274, "voltage_dc_mppt1", "uint16", "dcv_mppt_sf", "mppt/string1/DCV",  "mppt"),
    (40275, "power_dc_mppt1",   "uint16", "dcw_mppt_sf", "mppt/string1/DCW",  "mppt"),
    (40276, "energy_dc_mppt1",  "uint32", "dcwh_mppt_sf", "mppt/string1/DCWH", "mppt"),
    (40280, "temperature_mppt1", "int16", "",            "mppt/string1/Tmp",  "mppt"),
    (40293, "current_dc_mppt2", "uint16", "dca_mppt_sf", "mppt/string2/DCA",  "mppt"),
    (40294, "voltage_dc_mppt2", "uint16", "dcv_mppt_sf", "mppt/string2/DCV",  "mppt"),
    (40295, "power_dc_mppt2",   "uint16", "dcw_mppt_sf", "mppt/string2/DCW",  "mppt"),
    (40296, "energy_dc_mppt2",  "uint32", "dcwh_mppt_sf", "mppt/string2/DCWH", "mppt"),
    (40300, "temperature_mppt2", "int16", "",            "mppt/string2/Tmp",  "mppt"),
]

METER_203 = [
    (40072, "current_total",   "int16",  "a_sf",  "A",       "ac"),
    (40073, "current_l1",      "int16",  "a_sf",  "AphA",    "ac"),
    (40074, "current_l2",      "int16",  "a_sf",  "AphB",    "ac"),
    (40075, "current_l3",      "int16",  "a_sf",  "AphC",    "ac"),
    (40076, "a_sf",            "int16",  "",      None,      "sf"),
    (40077, "voltage_ln_avg",  "int16",  "v_sf",  "PhV",     "ac"),
    (40078, "voltage_l1_n",    "int16",  "v_sf",  "PhVphA",  "ac"),
    (40079, "voltage_l2_n",    "int16",  "v_sf",  "PhVphB",  "ac"),
    (40080, "voltage_l3_n",    "int16",  "v_sf",  "PhVphC",  "ac"),
    (40081, "voltage_ll_avg",  "int16",  "v_sf",  "PPV",     "ac"),
    (40082, "voltage_l1_l2",   "int16",  "v_sf",  "PPVphAB", "ac"),
    (40083, "voltage_l2_l3",   "int16",  "v_sf",  "PPVphBC", "ac"),
    (40084, "voltage_l3_l1",   "int16",  "v_sf",  "PPVphCA", "ac"),
    (40085, "v_sf",            "int16",  "",      None,      "sf"),
    (40086, "frequency",       "int16",  "hz_sf", "Hz",      "ac"),
    (40087, "hz_sf",           "int16",  "",      None,      "sf"),
    (40088, "power_active_total", "int16", "w_sf", "W",      "ac"),
    (40089, "power_active_l1", "int16",  "w_sf",  "WphA",    "ac"),
    (40090, "power_active_l2", "int16",  "w_sf",  "WphB",    "ac"),
    (40091, "power_active_l3", "int16",  "w_sf",  "WphC",    "ac"),
    (40092, "w_sf",            "int16",  "",      None,      "sf"),
    (40093, "power_apparent_total", "int16", "va_sf", "VA",  "ac"),
    (40094, "power_apparent_l1", "int16", "va_sf", "VAphA",  "ac"),
    (40095, "power_apparent_l2", "int16", "va_sf", "VAphB",  "ac"),
    (40096, "power_apparent_l3", "int16", "va_sf", "VAphC",  "ac"),
    (40097, "va_sf",           "int16",  "",      None,      "sf"),
    (40098, "power_reactive_total", "int16", "var_sf", "VAR", "ac"),
    (40099, "power_reactive_l1", "int16", "var_sf", "VARphA", "ac"),
    (40100, "power_reactive_l2", "int16", "var_sf", "VARphB", "ac"),
    (40101, "power_reactive_l3", "int16", "var_sf", "VARphC", "ac"),
    (40102, "var_sf",          "int16",  "",      None,      "sf"),
    (40103, "power_factor_total", "int16", "pf_sf", "PF",    "ac"),
    (40104, "power_factor_l1", "int16",  "pf_sf", "PFphA",   "ac"),
    (40105, "power_factor_l2", "int16",  "pf_sf", "PFphB",   "ac"),
    (40106, "power_factor_l3", "int16",  "pf_sf", "PFphC",   "ac"),
    (40107, "pf_sf",           "int16",  "",      None,      "sf"),
    (40108, "energy_active_export",    "uint32", "wh_sf", "TotWhExp",    "energy"),
    (40110, "energy_active_export_l1", "uint32", "wh_sf", "TotWhExpPhA", "energy"),
    (40112, "energy_active_export_l2", "uint32", "wh_sf", "TotWhExpPhB", "energy"),
    (40114, "energy_active_export_l3", "uint32", "wh_sf", "TotWhExpPhC", "energy"),
    (40116, "energy_active_import",    "uint32", "wh_sf", "TotWhImp",    "energy"),
    (40118, "energy_active_import_l1", "uint32", "wh_sf", "TotWhImpPhA", "energy"),
    (40120, "energy_active_import_l2", "uint32", "wh_sf", "TotWhImpPhB", "energy"),
    (40122, "energy_active_import_l3", "uint32", "wh_sf", "TotWhImpPhC", "energy"),
    (40124, "wh_sf",           "int16",  "",      None,      "sf"),
]

# SunSpec common block (model 1) — static identity, polled hourly.
# Opt/Vr are omitted on purpose: including them would make the identity span
# one contiguous 64-register batch, over the DataManager's ~50-register cap.
IDENTITY_1 = [
    (40005, "manufacturer", "string:16", None, "manufacturer",  "identity"),
    (40021, "model",        "string:16", None, "model",         "identity"),
    (40053, "serial",       "string:16", None, "serial_number", "identity"),
]

MONOTONIC = {"energy_active_generated", "energy_dc_mppt1", "energy_dc_mppt2",
             "energy_active_export", "energy_active_export_l1",
             "energy_active_export_l2", "energy_active_export_l3",
             "energy_active_import", "energy_active_import_l1",
             "energy_active_import_l2", "energy_active_import_l3"}

DASHBOARD = {"power_active_total", "power_dc", "energy_active_generated",
             "operating_state", "energy_active_export", "energy_active_import"}

# non-canonical display labels (canonical names get the dictionary description
# as their label; these cover the SunSpec plumbing + non-dictionary extras)
_SF_LABEL = lambda n: n.upper().replace("_SF", "").replace("_MPPT", " MPPT") + " Scale Factor"  # noqa: E731


def label_for(name: str, is_sf: bool) -> str:
    if is_sf:
        return _SF_LABEL(name)
    e = CANONICAL_FIELDS.get(name)
    if e:
        return e[3]
    return name.replace("_", " ").title()


def unit_for(name: str) -> str:
    e = CANONICAL_FIELDS.get(name)
    return e[1] if e else ""


def build_registers(rows, poll_group):
    out = []
    for doc_addr, name, dtype, sf_ref, _legacy, cat in rows:
        is_sf = cat == "sf"
        routed = not is_sf
        r = {
            "address": doc_addr - 1,          # PDU = documented SunSpec − 1
            "name": name,
            "label": label_for(name, is_sf),
            "unit": unit_for(name),
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
        # Canonical seeding: topic + measurement DERIVE from the dictionary
        # (empty here) — a Fronius unit publishes exactly like the Janitza.
        r["defaults"] = {
            "mqtt": {"enabled": routed, "topic": ""},
            "influxdb": {"enabled": routed, "measurement": "", "tags": {}},
            "ui": {"show_on_dashboard": name in DASHBOARD, "widget": "value"},
        }
        out.append(r)
    return out


def legacy_leaves(rows_groups) -> dict:
    """canonical MQTT topic → legacy collector leaf, for mqtt.compat_aliases.
    Only routed rows with a legacy leaf participate."""
    leaves = {}
    for rows in rows_groups:
        for _addr, name, _dt, _sf, legacy, cat in rows:
            if cat == "sf" or legacy is None:
                continue
            topic = mqtt_topic_for(name) or name
            leaves[topic] = legacy
    return leaves


def template(tid, name, model, description, registers, poll_groups):
    return {"device_template": {
        "schema_version": 1,
        "id": tid,
        "name": name,
        "vendor": "Fronius",
        "model": model,
        "version": "2.0.0",
        "description": description,
        "source_document": "SunSpec Information Models (int+SF); register map "
                           "verified live against a production Fronius fleet "
                           "(raw-frame decode parity, 2026-09-11)",
        "protocol": {"byte_order": "big", "default_register_type": "holding"},
        "poll_groups": poll_groups,
        "categories": {
            "ac": {"label": "AC measurements", "order": 1},
            "dc": {"label": "DC side", "order": 2},
            "mppt": {"label": "MPPT strings", "order": 3},
            "energy": {"label": "Energy", "order": 4},
            "temperature": {"label": "Temperatures", "order": 5},
            "status": {"label": "Status & events", "order": 6},
            "identity": {"label": "Device identity", "order": 7},
            "sf": {"label": "SunSpec scale factors", "order": 8},
        },
        "registers": registers,
        # register names ARE canonical now (dictionary-extended for the PV
        # domain); the lint skips unrouted plumbing (SF registers).
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
        "string MPPT, identity). Registers are CANONICAL: this device "
        "publishes the same topics/fields/measurements as every other MBG "
        "device (power/active/total, dc/power, mppt/1/power …); a legacy "
        "SunSpec-name tree (…/W, …/PhVphA) is available via "
        "mqtt.compat_aliases. Use this when the datalogger's Modbus TCP "
        "slave is enabled and set to 'int+SF' (the Fronius default). For "
        "SEVERAL inverters behind one datalogger, add a `plants:` entry with "
        "this template and the unit IDs (1, 2, ...) — each unit becomes its "
        "own device. CAUTION: dataloggers serve only a few concurrent Modbus "
        "clients; if another system polls the same datalogger, keep poll "
        "intervals modest (10-15 s) or reduce the number of units read in "
        "parallel. Known vendor quirk: PF is published with the generic "
        "SunSpec decode (±100); Fronius units report PF raw ±10000 with an "
        "out-of-spec scale factor, so dedicated drivers show ±1.0.",
        build_registers(INVERTER_103, "normal")
        + build_registers(MPPT_160, "normal")
        + build_registers(IDENTITY_1, "static"),
        {"normal": {"interval": 5, "description": "Measurements + MPPT"},
         "static": {"interval": 3600, "description": "Identity strings"}},
    )
    met = template(
        "fronius_sunspec_meter",
        "Fronius SunSpec meter (int+SF, via datalogger)",
        "Smart Meter 63A/50kA (SunSpec 203)",
        "Fronius Smart Meter read THROUGH the DataManager/datalogger over "
        "Modbus TCP (SunSpec model 203, int + scale factor; full 3-phase set "
        "+ per-phase import/export energies). Registers are CANONICAL "
        "(voltage/l1_n, power/active/total, energy/active/import …) — same "
        "output shape as every other MBG meter; the legacy SunSpec-name tree "
        "is available via mqtt.compat_aliases. The meter appears on the "
        "datalogger at unit ID 240 (default; 241/242 for additional meters). "
        "Use this when the meter hangs off a Fronius datalogger — for a "
        "Smart Meter wired DIRECTLY to your own RS-485 (RTU or an RTU-TCP "
        "bridge), use the separate 'Fronius Smart Meter 65A-3 (RTU)' "
        "template instead: same hardware, completely different register "
        "map. PF quirk as on the inverter template (generic ±100).",
        build_registers(METER_203, "normal")
        + build_registers(IDENTITY_1, "static"),
        {"normal": {"interval": 2, "description": "Measurements"},
         "static": {"interval": 3600, "description": "Identity strings"}},
    )
    for t in (inv, met):
        p = OUT / f"{t['device_template']['id']}.json"
        p.write_text(json.dumps(t, indent=2, ensure_ascii=False) + "\n",
                     encoding="utf-8")
        print(f"wrote {p} ({len(t['device_template']['registers'])} registers)")

    # legacy alias leaf maps (canonical topic → collector leaf) — feed these
    # into mqtt.compat_aliases per unit:
    #   {from: meters/<unit-device-id>, to: fronius/inverter/<unit>,  # or mbg/… in shadow
    #    leaves: <inverter map below>}
    leaves = {
        "fronius_sunspec_inverter": legacy_leaves([INVERTER_103, MPPT_160, IDENTITY_1]),
        "fronius_sunspec_meter": legacy_leaves([METER_203, IDENTITY_1]),
    }
    LEAVES_OUT.write_text(json.dumps(leaves, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {LEAVES_OUT} "
          f"({', '.join(f'{k}:{len(v)}' for k, v in leaves.items())} leaves)")


if __name__ == "__main__":
    main()
