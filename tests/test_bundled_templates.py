# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""The bundled real device maps must validate and describe the right transport."""
import json

from multibus.device_template import parse_template, validate_template

SDM630 = "multibus/device_templates/eastron_sdm630.json"
SDM120 = "multibus/device_templates/eastron_sdm120.json"
EM24 = "multibus/device_templates/carlo_gavazzi_em24.json"
B23 = "multibus/device_templates/abb_b23.json"
B21 = "multibus/device_templates/abb_b21.json"
IEM3000 = "multibus/device_templates/schneider_iem3000.json"


def _load(path):
    with open(path) as f:
        return json.load(f)


def test_eastron_templates_validate_and_parse():
    for path in (SDM630, SDM120, EM24, B23, B21, IEM3000):
        data = _load(path)
        assert validate_template(data) == [], path
        t = parse_template(data)
        assert t.registers


def test_sdm630_is_fc4_bigendian_float_with_canonical_addresses():
    t = parse_template(_load(SDM630))
    assert t.protocol.get("byte_order") == "big"
    assert all(r.register_type == "input" for r in t.registers)
    assert all(r.data_type in ("float", "float32") for r in t.registers)
    by_addr = {r.address: r for r in t.registers}
    # canonical SDM630 addresses -> canonical field names
    assert by_addr[0].name == "voltage_l1_n"
    assert by_addr[6].name == "current_l1"
    assert by_addr[12].name == "power_active_l1"
    assert by_addr[52].name == "power_active_total"
    assert by_addr[70].name == "frequency"
    assert by_addr[72].name == "energy_active_import"
    assert by_addr[342].name == "energy_active_total"


def test_sdm120_single_phase_subset():
    t = parse_template(_load(SDM120))
    assert all(r.register_type == "input" for r in t.registers)
    by_addr = {r.address: r for r in t.registers}
    assert (by_addr[0].name == "voltage_l1_n" and by_addr[12].name == "power_active_total"
            and by_addr[72].name == "energy_active_import")


def test_em24_is_fc3_littleendian_int32_matching_victron():
    """EM24 must match Victron dbus-modbus-client/carlo_gavazzi.py exactly:
    holding (FC03), little-endian (Reg_s32l), int32 with divisor scales."""
    t = parse_template(_load(EM24))
    assert t.protocol.get("byte_order") == "little"
    assert all(r.register_type == "holding" for r in t.registers)
    by_addr = {r.address: r for r in t.registers}
    # (addr, type, scale) verbatim from EM24_Meter phase_regs + data_regs
    assert (by_addr[0].data_type, by_addr[0].scale) == ("int32", 10)     # V L1 /10
    assert (by_addr[12].data_type, by_addr[12].scale) == ("int32", 1000)  # I L1 /1000
    assert (by_addr[18].data_type, by_addr[18].scale) == ("int32", 10)    # P L1 /10
    assert (by_addr[40].data_type, by_addr[40].scale) == ("int32", 10)    # P total 0x28
    assert (by_addr[51].data_type, by_addr[51].scale) == ("uint16", 10)   # Freq 0x33
    assert by_addr[52].name == "energy_active_import"   # Energy Forward 0x34
    assert by_addr[78].name == "energy_active_export"   # Energy Reverse 0x4e
    assert by_addr[64].name == "energy_active_import_l1"  # per-phase forward 0x40


def test_abb_b23_is_fc3_bigendian_integer_with_verified_map():
    """ABB B23 must match the cross-verified B-series map: holding (FC03),
    big-endian, integer registers, divisor scales (factor = 1/scale)."""
    t = parse_template(_load(B23))
    assert t.protocol.get("byte_order") == "big"
    assert all(r.register_type == "holding" for r in t.registers)
    by_addr = {r.address: r for r in t.registers}
    # instantaneous block @ 0x5B00 = 23296
    assert (by_addr[23296].name, by_addr[23296].data_type, by_addr[23296].scale) == ("voltage_l1_n", "uint32", 10)
    assert (by_addr[23308].name, by_addr[23308].data_type, by_addr[23308].scale) == ("current_l1", "uint32", 100)
    assert (by_addr[23316].name, by_addr[23316].data_type, by_addr[23316].scale) == ("power_active_total", "int32", 100)
    assert (by_addr[23324].data_type, by_addr[23324].scale) == ("int32", 100)   # Q total
    assert (by_addr[23340].name, by_addr[23340].data_type, by_addr[23340].scale) == ("frequency", "uint16", 100)
    assert (by_addr[23354].name, by_addr[23354].data_type, by_addr[23354].scale) == ("power_factor_total", "int16", 1000)
    # energy block @ 0x5000 = 20480 (64-bit); raw kWh*100 -> canonical Wh
    assert (by_addr[20480].name, by_addr[20480].data_type, by_addr[20480].scale) == ("energy_active_import", "uint64", 0.1)
    assert by_addr[20480].unit == "Wh"
    assert (by_addr[20484].data_type) == "uint64"
    assert (by_addr[20488].name, by_addr[20488].data_type) == ("energy_active_net", "int64")   # net is signed


def test_abb_b21_single_phase_subset():
    t = parse_template(_load(B21))
    assert t.protocol.get("byte_order") == "big"
    by_addr = {r.address: r for r in t.registers}
    assert (by_addr[23296].name, by_addr[23296].data_type) == ("voltage_l1_n", "uint32")
    assert (by_addr[23316].name, by_addr[23316].data_type) == ("power_active_total", "int32")
    assert (by_addr[20480].name, by_addr[20480].data_type) == ("energy_active_import", "uint64")


def test_schneider_iem3000_matches_mbmd_verified_map():
    """iEM3000 must match the field-tested volkszaehler/mbmd iem3000 map:
    holding (FC03), big-endian, float32 instantaneous + int64 energy,
    0-based addresses (= Schneider register - 1). Power float32 is kW -> W
    (scale 0.001); energy int64 is already canonical Wh (scale 1)."""
    t = parse_template(_load(IEM3000))
    assert t.protocol.get("byte_order") == "big"
    assert all(r.register_type == "holding" for r in t.registers)
    by_addr = {r.address: r for r in t.registers}
    # 0x0BB7 = 2999 (Current L1, register 3000)
    assert (by_addr[2999].name, by_addr[2999].data_type, by_addr[2999].scale) == ("current_l1", "float", 1)
    # 0x0BD3 = 3027 (Voltage L1-N, register 3028)
    assert (by_addr[3027].name, by_addr[3027].data_type) == ("voltage_l1_n", "float")
    # 0x0BF3 = 3059 (Active power total, register 3060) -> kW to W
    assert (by_addr[3059].name, by_addr[3059].data_type, by_addr[3059].scale) == ("power_active_total", "float", 0.001)
    # 0x0C25 = 3109 (Frequency, register 3110)
    assert (by_addr[3109].name, by_addr[3109].data_type, by_addr[3109].scale) == ("frequency", "float", 1)
    # 0x0C83 = 3203 (Active energy import total, register 3204) int64 native Wh
    assert (by_addr[3203].name, by_addr[3203].data_type, by_addr[3203].scale) == ("energy_active_import", "int64", 1)
    assert by_addr[3203].unit == "Wh"
    assert by_addr[3207].name == "energy_active_export"


def test_sensor_templates_zigbee_and_ble_validate():
    # Zigbee (zigbee2mqtt) + BLE (Theengs/BTHome→MQTT) sensor presets: valid,
    # MQTT-transport (so the wizard offers them at the MQTT step), json_path on
    # every row (push-driven JSON, no Modbus addresses), documented field names.
    from multibus.device_template import template_transport
    for tid, fields in (("zigbee2mqtt_sensor",
                         {"temperature", "humidity", "battery", "linkquality"}),
                        ("ble_theengs_sensor", {"tempc", "hum", "batt", "rssi"})):
        raw = _load(f"multibus/device_templates/{tid}.json")
        assert validate_template(raw) == []
        t = parse_template(raw)
        assert template_transport(t) == "mqtt"
        names = {r.name for r in t.registers}
        assert fields <= names, f"{tid}: missing {fields - names}"
        assert all(r.json_path for r in t.registers), f"{tid}: every row needs json_path"


# ── P0: byte_order resolves identically at boot and runtime ──────────────────

def test_byte_order_for_matches_template():
    from multibus.device_template import TemplateRegistry
    reg = TemplateRegistry()
    # EM24 is word-swapped (little/cdab) — the boot path MUST resolve this,
    # else a restart decodes word-swapped garbage
    assert reg.byte_order_for("carlo_gavazzi_em24") in ("little", "cdab")
    assert reg.byte_order_for("carlo_gavazzi_em24") != "big"
    # SDM630 is big
    assert reg.byte_order_for("eastron_sdm630") == "big"
    # unknown / none → safe default big (never crashes)
    assert reg.byte_order_for("does_not_exist") == "big"
    assert reg.byte_order_for(None) == "big"


def test_boot_and_runtime_build_devices_the_same_way():
    """Regression for the restart-asymmetry defect.

    There used to be two device constructors — one in main.py for boot, one in
    api.py for a device added at runtime — and they drifted, invisibly, until a
    restart brought a device back decoding with a different byte order. The fix
    is not a test that keeps two copies in step; it is one copy. Both callers
    now go through device_runtime.build_device_client, which resolves the decode
    order through the single TemplateRegistry.byte_order_for."""
    import inspect
    import main as _main
    import multibus.api as _api
    from multibus import device_runtime

    assert "build_device_client" in inspect.getsource(_main.GatewayApp.setup)
    assert "build_device_client" in inspect.getsource(_api.create_api)
    # and the one builder resolves the order through the one resolver
    assert "byte_order_for(" in inspect.getsource(device_runtime.driver_for)


def test_catalog_energy_units_are_canonical_wh_family():
    """EVERY bundled map must deliver energy in the canonical Wh family
    (Wh/varh/VAh) — a kWh row under a canonical energy_* name is a silent
    1000x error the moment the device feeds a vmeter or a fallback twin.
    Native-kWh meters convert in the template scale (kWh map -> scale/1000)."""
    import glob
    import json
    for path in sorted(glob.glob("multibus/device_templates/*.json")):
        with open(path) as fh:
            regs = json.load(fh).get("device_template", {}).get("registers", [])
        for r in regs:
            if str(r.get("name", "")).startswith("energy_"):
                assert str(r.get("unit", "")) in ("Wh", "varh", "VAh"), (
                    f"{path}: {r.get('name')} declares {r.get('unit')!r}; "
                    "canonical energy units are Wh/varh/VAh")


# ── scale_from (SunSpec dynamic SF, F0.2) — template contract ────────────────

def _sunspec_stub(regs):
    return {"device_template": {
        "id": "x_sunspec", "name": "X",
        "protocol": {"byte_order": "big", "word_order": "big"},
        "registers": regs,
    }}


def test_scale_from_roundtrips_through_parse_and_to_dict():
    regs = [
        {"address": 40092, "name": "ac_power", "label": "AC Power",
         "unit": "W", "data_type": "int16", "register_type": "holding",
         "scale_from": "ac_power_sf", "nan": True},
        {"address": 40093, "name": "ac_power_sf", "label": "AC Power SF",
         "unit": "", "data_type": "int16", "register_type": "holding",
         "nan": True},
    ]
    data = _sunspec_stub(regs)
    assert validate_template(data) == []
    t = parse_template(data)
    dep = next(r for r in t.registers if r.name == "ac_power")
    assert dep.scale_from == "ac_power_sf"
    out = [r.to_dict() for r in t.registers]
    assert next(r for r in out if r["name"] == "ac_power")["scale_from"] == "ac_power_sf"
    # unset stays absent, not "" noise
    assert "scale_from" not in next(r for r in out if r["name"] == "ac_power_sf")


def test_scale_from_dangling_referent_is_a_validation_error():
    data = _sunspec_stub([
        {"address": 40092, "name": "ac_power", "label": "AC Power",
         "unit": "W", "data_type": "int16", "register_type": "holding",
         "scale_from": "nope_sf"},
    ])
    errs = validate_template(data)
    assert any("scale_from" in e and "nope_sf" in e for e in errs)


def test_scale_may_accompany_scale_from_as_a_unit_conversion():
    """A fixed scale on top of a dynamic SF is the unit conversion the exponent
    cannot express — SunSpec power factor is a PERCENTAGE, so `scale: 100`
    turns the spec's +-100 into the +-1 fraction every other device publishes."""
    data = _sunspec_stub([
        {"address": 40092, "name": "power_factor_total", "label": "PF",
         "unit": "", "data_type": "int16", "register_type": "holding",
         "scale": 100, "scale_from": "pf_sf"},
        {"address": 40093, "name": "pf_sf", "label": "SF",
         "unit": "", "data_type": "int16", "register_type": "holding"},
    ])
    assert validate_template(data) == []


def test_zero_scale_is_rejected():
    data = _sunspec_stub([
        {"address": 40092, "name": "ac_power", "label": "AC Power",
         "unit": "W", "data_type": "int16", "register_type": "holding",
         "scale": 0},
    ])
    assert any("scale must not be 0" in e for e in validate_template(data))
