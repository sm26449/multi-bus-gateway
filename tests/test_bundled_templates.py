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
    # canonical SDM630 addresses
    assert by_addr[0].name == "V_L1"
    assert by_addr[6].name == "I_L1"
    assert by_addr[12].name == "P_L1"
    assert by_addr[52].name == "P_total"
    assert by_addr[70].name == "Freq"
    assert by_addr[72].name == "Import_kWh"
    assert by_addr[342].name == "Total_kWh"


def test_sdm120_single_phase_subset():
    t = parse_template(_load(SDM120))
    assert all(r.register_type == "input" for r in t.registers)
    by_addr = {r.address: r for r in t.registers}
    assert by_addr[0].name == "V" and by_addr[12].name == "P" and by_addr[72].name == "Import_kWh"


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
    assert by_addr[52].name == "Import_kWh"   # Energy Forward 0x34
    assert by_addr[78].name == "Export_kWh"   # Energy Reverse 0x4e
    assert by_addr[64].name == "Energy_L1_Import"  # per-phase forward 0x40


def test_abb_b23_is_fc3_bigendian_integer_with_verified_map():
    """ABB B23 must match the cross-verified B-series map: holding (FC03),
    big-endian, integer registers, divisor scales (factor = 1/scale)."""
    t = parse_template(_load(B23))
    assert t.protocol.get("byte_order") == "big"
    assert all(r.register_type == "holding" for r in t.registers)
    by_addr = {r.address: r for r in t.registers}
    # instantaneous block @ 0x5B00 = 23296
    assert (by_addr[23296].name, by_addr[23296].data_type, by_addr[23296].scale) == ("V_L1", "uint32", 10)
    assert (by_addr[23308].name, by_addr[23308].data_type, by_addr[23308].scale) == ("I_L1", "uint32", 100)
    assert (by_addr[23316].name, by_addr[23316].data_type, by_addr[23316].scale) == ("P_total", "int32", 100)
    assert (by_addr[23324].data_type, by_addr[23324].scale) == ("int32", 100)   # Q total
    assert (by_addr[23340].name, by_addr[23340].data_type, by_addr[23340].scale) == ("Freq", "uint16", 100)
    assert (by_addr[23354].name, by_addr[23354].data_type, by_addr[23354].scale) == ("PF_total", "int16", 1000)
    # energy block @ 0x5000 = 20480 (64-bit)
    assert (by_addr[20480].name, by_addr[20480].data_type, by_addr[20480].scale) == ("Import_kWh", "uint64", 100)
    assert (by_addr[20484].data_type) == "uint64"
    assert (by_addr[20488].name, by_addr[20488].data_type) == ("Net_kWh", "int64")   # net is signed


def test_abb_b21_single_phase_subset():
    t = parse_template(_load(B21))
    assert t.protocol.get("byte_order") == "big"
    by_addr = {r.address: r for r in t.registers}
    assert (by_addr[23296].name, by_addr[23296].data_type) == ("V", "uint32")
    assert (by_addr[23316].name, by_addr[23316].data_type) == ("P", "int32")
    assert (by_addr[20480].name, by_addr[20480].data_type) == ("Import_kWh", "uint64")


def test_schneider_iem3000_matches_mbmd_verified_map():
    """iEM3000 must match the field-tested volkszaehler/mbmd iem3000 map:
    holding (FC03), big-endian, float32 instantaneous + int64 energy,
    0-based addresses (= Schneider register - 1). Power float32 is kW -> W
    (scale 0.001); energy int64 is Wh -> kWh (scale 1000)."""
    t = parse_template(_load(IEM3000))
    assert t.protocol.get("byte_order") == "big"
    assert all(r.register_type == "holding" for r in t.registers)
    by_addr = {r.address: r for r in t.registers}
    # 0x0BB7 = 2999 (Current L1, register 3000)
    assert (by_addr[2999].name, by_addr[2999].data_type, by_addr[2999].scale) == ("I_L1", "float", 1)
    # 0x0BD3 = 3027 (Voltage L1-N, register 3028)
    assert (by_addr[3027].name, by_addr[3027].data_type) == ("V_L1", "float")
    # 0x0BF3 = 3059 (Active power total, register 3060) -> kW to W
    assert (by_addr[3059].name, by_addr[3059].data_type, by_addr[3059].scale) == ("P_total", "float", 0.001)
    # 0x0C25 = 3109 (Frequency, register 3110)
    assert (by_addr[3109].name, by_addr[3109].data_type, by_addr[3109].scale) == ("Freq", "float", 1)
    # 0x0C83 = 3203 (Active energy import total, register 3204) int64 Wh -> kWh
    assert (by_addr[3203].name, by_addr[3203].data_type, by_addr[3203].scale) == ("Import_kWh", "int64", 1000)
    assert by_addr[3207].name == "Export_kWh"


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


def test_boot_and_runtime_use_the_same_resolver():
    """Regression for the restart-asymmetry defect: both main.py (boot) and
    api.py (runtime) must go through TemplateRegistry.byte_order_for, so a
    device's decode order survives a restart byte-identically."""
    import inspect
    import main as _main
    import multibus.api as _api
    boot_src = inspect.getsource(_main.GatewayApp.setup)
    assert "byte_order_for(device.template)" in boot_src
    api_src = inspect.getsource(_api.create_api)
    assert "byte_order_for(dev_cfg.template)" in api_src
