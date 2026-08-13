# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Community YAML template import: upstream register map → MBG template, with
field aliases + full-fidelity passthrough of MBG-native fields."""
from multibus.yaml_import import parse_yaml
from multibus.device_template import validate_template


def _tpl(regs):
    return {"device_template": {"id": "xdev", "name": "X", "protocol": {}, "registers": regs}}


def test_basic_registers_list():
    y = """
registers:
  - { address: 0, name: voltage_l1_n, unit: V, data_type: uint16, scale: 10 }
  - { address: 2, name: power_active_total, unit: W, data_type: int32 }
"""
    p = parse_yaml(y)
    assert p['errors'] == [] and len(p['registers']) == 2
    r0 = p['registers'][0]
    assert r0['address'] == 0 and r0['name'] == 'voltage_l1_n' and r0['scale'] == 10.0
    assert validate_template(_tpl(p['registers'])) == []


def test_field_aliases():
    # reg/addr, type/datatype, factor/gain, desc→label
    y = """
registers:
  - { reg: 10, name: current_l1, units: A, type: float, factor: 1, description: "L1 current" }
  - { addr: 0x0A, name: freq, uom: Hz, dtype: word, gain: 100 }
"""
    p = parse_yaml(y)
    r0, r1 = p['registers']
    assert r0['address'] == 10 and r0['data_type'] == 'float' and r0['label'] == 'L1 current'
    assert r1['address'] == 10 and r1['data_type'] == 'uint16' and r1['scale'] == 100.0  # 0x0A hex, word→uint16


def test_mbg_native_fields_pass_through():
    y = """
registers:
  - address: 40
    name: inverter_state
    data_type: uint16
    enum: { 1: "Off", 4: "MPPT", 7: "Fault" }
  - address: 50
    name: setpoint
    data_type: uint16
    writable: true
    write_min: 0
    write_max: 5000
    offset: -273.15
    monotonic: false
"""
    p = parse_yaml(y)
    a, b = p['registers']
    assert a['enum'] == {1: "Off", 4: "MPPT", 7: "Fault"}
    assert b['writable'] is True and b['write_min'] == 0 and b['write_max'] == 5000
    assert b['offset'] == -273.15


def test_register_type_alias():
    y = "registers:\n  - { address: 5, name: x, data_type: uint16, register_type: input }\n"
    assert parse_yaml(y)['registers'][0]['register_type'] == 'input'
    y2 = "registers:\n  - { address: 5, name: x, data_type: uint16, fc: 4 }\n"
    assert parse_yaml(y2)['registers'][0]['register_type'] == 'input'


def test_bare_list_and_device_template_wrapper():
    assert len(parse_yaml("- {address: 0, name: a, data_type: uint16}\n")['registers']) == 1
    wrapped = """
device_template:
  id: acme_meter
  name: Acme Meter
  vendor: Acme
  registers:
    - { address: 0, name: v, data_type: uint16 }
"""
    p = parse_yaml(wrapped)
    assert p['meta']['id'] == 'acme_meter' and p['meta']['vendor'] == 'Acme'
    assert len(p['registers']) == 1


def test_warnings_and_skips():
    y = """
registers:
  - { name: no_addr }
  - { address: 0, name: ok, data_type: uint16 }
  - { address: 0, name: ok }
  - "not a mapping"
  - { address: bad, name: badaddr, data_type: uint16 }
  - { address: 9, name: weird, data_type: frobnicate }
"""
    p = parse_yaml(y)
    names = [r['name'] for r in p['registers']]
    assert 'ok' in names and 'weird' in names            # weird kept (type→default float)
    assert 'no_addr' not in names and 'badaddr' not in names
    assert any('duplicate' in w for w in p['warnings'])
    assert any('unknown type' in w for w in p['warnings'])
    assert p['registers'][[r['name'] for r in p['registers']].index('weird')]['data_type'] == 'float'


def test_errors():
    assert parse_yaml("")['errors']
    assert parse_yaml("just: a scalar\nno: registers")['errors']
    assert parse_yaml(": : : invalid")['errors']
