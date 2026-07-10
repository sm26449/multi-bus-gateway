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
"""Byte/word-order matrix for RegisterEncoder <-> RegisterParser.

Covers all four classic orderings (ABCD/big, CDAB/little, BADC, DCBA):
- every order round-trips for every data type (encoder -> parser -> value),
- 'big'/'little' stay byte-for-byte identical to their historical layout
  (protects the Janitza input reader and the EM24/Fronius emulations),
- the previously-inconsistent little-endian *integer* decode now agrees with
  the unsigned decode on the same registers (regression guard for that fix).
"""
import pytest

from multibus.encoder import RegisterEncoder
from multibus.register_parser import RegisterParser

ORDERS = ['big', 'little', 'abcd', 'cdab', 'badc', 'dcba']
CASES = [
    ('int16', -1234), ('int16', 32767), ('int16', -32768),
    ('uint16', 54321), ('uint16', 0),
    ('int32', -2_000_000), ('int32', 2147483647), ('uint32', 3_000_000_000),
    ('int64', -9_000_000_000), ('uint64', 18_000_000_000_000_000_000),
    ('float', -5794.0), ('float', 0.0), ('double', 3.14159265),
]


@pytest.mark.parametrize('order', ORDERS)
@pytest.mark.parametrize('dtype,value', CASES)
def test_roundtrip_every_order_every_type(order, dtype, value):
    regs = RegisterEncoder(order).encode(value, dtype)
    back = RegisterParser(order).parse_value(regs, dtype)
    if dtype in ('float', 'double'):
        assert back == pytest.approx(value, abs=1e-3)
    else:
        assert back == value


def test_big_little_layouts_are_byte_identical_to_history():
    # 0x12345678: big=[0x1234,0x5678], little(=CDAB word-swap)=[0x5678,0x1234]
    assert RegisterEncoder('big').encode(0x12345678, 'uint32') == [0x1234, 0x5678]
    assert RegisterEncoder('little').encode(0x12345678, 'uint32') == [0x5678, 0x1234]
    assert RegisterParser('big').parse_value([0x1234, 0x5678], 'uint32') == 0x12345678
    assert RegisterParser('little').parse_value([0x5678, 0x1234], 'uint32') == 0x12345678
    # 16-bit stays put for big/little (word order irrelevant, no byte swap)
    assert RegisterEncoder('big').encode(-1234, 'int16') == [0xFB2E]
    assert RegisterEncoder('little').encode(-1234, 'int16') == [0xFB2E]


def test_little_int_matches_uint_after_fix():
    # Previously the little-endian signed path byte-swapped inconsistently.
    regs = [0x5678, 0x1234]           # little/CDAB layout of 0x12345678
    u = RegisterParser('little').parse_value(regs, 'uint32')
    i = RegisterParser('little').parse_value(regs, 'int32')
    assert u == 0x12345678
    assert i == (u if u < 2**31 else u - 2**32)


def test_four_orders_are_distinct():
    layouts = {o: tuple(RegisterEncoder(o).encode(0x12345678, 'uint32'))
               for o in ('abcd', 'cdab', 'badc', 'dcba')}
    assert len(set(layouts.values())) == 4, layouts


# ── P2: ambiguous le/littleendian aliases removed ────────────────────────────

def test_le_littleendian_aliases_removed():
    from multibus.register_parser import RegisterParser
    # 'little' stays (word-swap = CDAB); the ambiguous ones fall back to big
    assert RegisterParser.resolve_order("little") == (False, True)
    assert RegisterParser.resolve_order("cdab") == (False, True)
    assert RegisterParser.resolve_order("dcba") == (True, True)
    # 'le'/'littleendian' no longer silently mean DCBA — unknown → big default
    assert RegisterParser.resolve_order("le") == (False, False)
    assert RegisterParser.resolve_order("littleendian") == (False, False)
