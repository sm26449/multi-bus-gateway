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
"""Round-trip proof for RegisterEncoder against STANDARD Modbus word order.

The virtual meter's OUTPUT must match what the consumer (Victron's
dbus-modbus-client) decodes: registers are 16-bit big-endian on the wire;
`Reg_*b` = big word order (high word first), `Reg_*l` = little (low word first).
We validate against that standard. (The parser's little-endian signed-int path
was historically inconsistent; it is now fixed and cross-checked against this
encoder in tests/test_byte_order.py.)
"""
import math
import struct
import pytest
from multibus.encoder import RegisterEncoder

ORDERS = ['big', 'little']
INT_CASES = {
    'int16':  [0, 1, -1, 32767, -32768, 1234],
    'uint16': [0, 1, 65535, 4096],
    'int32':  [0, 1, -1, 2147483647, -2147483648, -57940, 123456],
    'uint32': [0, 1, 4294967295, 87990],
    'int64':  [0, -1, 9223372036854775807, -9223372036854775808],
    'uint64': [0, 1, 18446744073709551615],
}


def ref_decode_int(regs, dtype, order):
    words = regs if order == 'big' else list(reversed(regs))   # → [high..low]
    u = 0
    for w in words:
        u = (u << 16) | (w & 0xffff)
    bits = len(regs) * 16
    if not dtype.startswith('u') and u >= (1 << (bits - 1)):
        u -= (1 << bits)
    return u


def ref_decode_float(regs, dtype, order):
    words = regs if order == 'big' else list(reversed(regs))
    raw = b''.join(int(w & 0xffff).to_bytes(2, 'big') for w in words)
    return struct.unpack('>f' if dtype != 'double' else '>d', raw)[0]


@pytest.mark.parametrize('order', ORDERS)
@pytest.mark.parametrize('dtype', list(INT_CASES))
def test_int_roundtrip(order, dtype):
    enc = RegisterEncoder(order)
    for v in INT_CASES[dtype]:
        regs = enc.encode(v, dtype)
        assert ref_decode_int(regs, dtype, order) == v, f"{dtype}/{order} v={v} regs={regs}"


@pytest.mark.parametrize('order', ORDERS)
@pytest.mark.parametrize('dtype', ['float', 'double'])
def test_float_roundtrip(order, dtype):
    enc = RegisterEncoder(order)
    for v in [0.0, 1.5, -1.5, 236.816, -5794.778, 49.995, 12345.678]:
        regs = enc.encode(v, dtype)
        back = ref_decode_float(regs, dtype, order)
        assert math.isclose(back, v, rel_tol=1e-6, abs_tol=1e-3), f"{dtype}/{order} v={v} back={back}"


def test_scale_convention():
    # EM24 Reg_s32l scale=10: -5794 W -> raw -57940 -> consumer reads /10 = -5794
    enc = RegisterEncoder('little')
    regs = enc.encode(-5794, 'int32', scale=10)
    assert ref_decode_int(regs, 'int32', 'little') == -57940


def test_clamp_no_overflow():
    enc = RegisterEncoder('big')
    regs = enc.encode(10**12, 'int16')              # far over range
    assert ref_decode_int(regs, 'int16', 'big') == 32767   # clamped, no overflow


@pytest.mark.parametrize('order', ORDERS)
@pytest.mark.parametrize('dtype', ['int64', 'uint64', 'long64'])
def test_large_int_exact_precision(order, dtype):
    # Values above 2^53 (float's mantissa) that are NOT at the type boundary —
    # the old int(round(float(v))) path corrupted the low bits (e.g. lost the +1).
    # Real case: large energy counters (Wh) served through a virtual meter.
    enc = RegisterEncoder(order)
    cases = [2**53 + 1, 2**53 + 7, 10**15 + 3, 9223372036854775806]  # all < 2^63
    for v in cases:
        regs = enc.encode(v, dtype)
        assert ref_decode_int(regs, dtype, order) == v, f"{dtype}/{order} v={v} regs={regs}"


def test_integral_scale_keeps_precision():
    # scale is integral and value is int → pure-int multiply, no float round-trip.
    enc = RegisterEncoder('big')
    v = 10**12 + 7
    regs = enc.encode(v, 'int64', scale=10)
    assert ref_decode_int(regs, 'int64', 'big') == v * 10


def test_bool_still_encodes_as_int():
    # bool is an int subclass — must not take the exact-int path with scale games,
    # but must still encode to 0/1 correctly.
    enc = RegisterEncoder('big')
    assert ref_decode_int(enc.encode(True, 'uint16'), 'uint16', 'big') == 1
    assert ref_decode_int(enc.encode(False, 'uint16'), 'uint16', 'big') == 0


def test_clamp_logs_warning(caplog):
    import logging
    enc = RegisterEncoder('big')
    with caplog.at_level(logging.WARNING, logger='janitza.encoder'):
        enc.encode(10**12, 'int16')          # far over range → clamped
    assert any('out of range' in r.message for r in caplog.records)


def test_in_range_does_not_warn(caplog):
    import logging
    enc = RegisterEncoder('big')
    with caplog.at_level(logging.WARNING, logger='janitza.encoder'):
        enc.encode(1234, 'int16')
    assert not caplog.records


def test_string_roundtrip():
    enc = RegisterEncoder('big')
    regs = enc.encode_string('JNZ001', 7)
    assert len(regs) == 7
    raw = b''.join(int(r).to_bytes(2, 'big') for r in regs)
    assert raw.rstrip(b'\x00').decode('ascii') == 'JNZ001'
