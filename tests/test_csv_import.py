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
"""CSV register-map importer: parsing, aliases, and template round-trip."""
from multibus.csv_import import parse_csv
from multibus.device_template import validate_template


def _template(regs):
    return {"device_template": {"id": "csv_dev", "name": "CSV Device", "registers": regs}}


def test_basic_comma_with_aliases_and_hex():
    csv = ("Register,Name,Description,Unit,Type,Factor\n"
           "0x0000,V_L1,Voltage L1-N,V,float32,1\n"
           "40,P_total,Total power,W,int32,10\n"
           "19000,Freq,Frequency,Hz,u16,100\n")
    r = parse_csv(csv)
    assert r['errors'] == []
    regs = {x['name']: x for x in r['registers']}
    assert regs['V_L1']['address'] == 0 and regs['V_L1']['data_type'] == 'float'
    assert regs['P_total']['address'] == 40 and regs['P_total']['data_type'] == 'int32'
    assert regs['P_total']['scale'] == 10.0
    assert regs['Freq']['data_type'] == 'uint16'          # 'u16' alias
    assert regs['V_L1']['label'] == 'Voltage L1-N'         # 'Description' -> label


def test_semicolon_delimiter_and_type_aliases():
    r = parse_csv("addr;name;type\n100;A;dword\n102;B;word\n")
    regs = {x['name']: x for x in r['registers']}
    assert regs['A']['data_type'] == 'uint32'
    assert regs['B']['data_type'] == 'uint16'


def test_bad_rows_are_skipped_with_warnings_not_fatal():
    csv = "addr,name,type\n1,Good,float\nbad,BadAddr,float\n,,\n5,,float\n"
    r = parse_csv(csv)
    assert [x['name'] for x in r['registers']] == ['Good']
    assert len(r['warnings']) >= 2 and r['errors'] == []


def test_http_json_path_gets_synthetic_addresses():
    csv = "name,json_path,unit\nP,Body.Data.PowerReal_P_Sum,W\nF,Body.Data.Frequency,Hz\n"
    r = parse_csv(csv)
    addrs = [x['address'] for x in r['registers']]
    assert addrs == [0, 1]
    assert r['registers'][0]['json_path'] == 'Body.Data.PowerReal_P_Sum'


def test_missing_required_columns_is_fatal():
    r = parse_csv("foo,bar\n1,2\n")
    assert r['registers'] == [] and r['errors']


def test_duplicate_address_name_skipped():
    r = parse_csv("addr,name\n1,A\n1,A\n2,B\n")
    assert len(r['registers']) == 2


def test_parsed_registers_validate_as_a_template():
    csv = ("address,name,label,unit,type,scale,category\n"
           "0,volt,Voltage,V,float,1,voltage\n"
           "2,power,Power,W,int32,10,power\n")
    r = parse_csv(csv)
    assert validate_template(_template(r['registers'])) == []


def test_default_data_type_applied_when_type_missing_or_unknown():
    r = parse_csv("addr,name\n1,A\n2,B\n", default_data_type='uint16')
    assert all(x['data_type'] == 'uint16' for x in r['registers'])
