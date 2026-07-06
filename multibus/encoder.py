"""Register encoder — engineering value → Modbus registers.

Turns an engineering value into the 16-bit Modbus registers a consumer expects,
honouring data type, word/byte order and scale. The word order follows the
**standard Modbus convention used by real consumers** (Victron dbus-modbus-client
`Reg_s32l`/`Reg_*b`: little = low word at the lower address). Encoder and
`RegisterParser` share `RegisterParser.resolve_order()` and are now consistent
across all four orderings (ABCD/big, CDAB/little, BADC, DCBA) — round-trip and
back-compat are proven in tests/test_byte_order.py. (Historically the parser's
little-endian *signed integer* path byte-swapped inconsistently; that is fixed.)

Scale convention (matches Victron's dbus-modbus-client `Reg_*` definitions):
    raw_register = round(engineering_value * scale)
    consumer reads back: engineering_value = raw_register / scale
e.g. EM24 power Reg_s32l(..., scale=10): W=-5794 -> raw=-57940 -> reads -5794 W.
"""
from __future__ import annotations

import logging
import struct
from typing import Any

from .register_parser import RegisterParser

logger = logging.getLogger(__name__)

_INT_RANGES = {
    'int16':  (-32768, 32767),          'short':  (-32768, 32767),
    'uint16': (0, 65535),
    'int32':  (-2**31, 2**31 - 1),      'uint32': (0, 2**32 - 1),
    'int64':  (-2**63, 2**63 - 1),      'long64': (-2**63, 2**63 - 1),
    'uint64': (0, 2**64 - 1),
}


class RegisterEncoder:
    REGISTER_COUNTS = RegisterParser.REGISTER_COUNTS

    def __init__(self, byte_order: str = 'big'):
        self.byte_order = byte_order
        # Same four orderings the parser understands (big/abcd, little/cdab,
        # badc, dcba). 'big'/'little' keep their historical word-only meaning.
        self._byteswap, self._word_swap = RegisterParser.resolve_order(byte_order)

    def _apply_order(self, regs: list[int]) -> list[int]:
        """Turn canonical big-endian (high word first) registers into the wire
        layout for this byte order — the inverse of RegisterParser._canon."""
        if self._byteswap:
            regs = [((r & 0xff) << 8) | ((r >> 8) & 0xff) for r in regs]
        if self._word_swap:
            regs = list(reversed(regs))
        return regs

    def register_count(self, data_type: str) -> int:
        return self.REGISTER_COUNTS.get(data_type.lower(), 2)

    def encode(self, value: Any, data_type: str, scale: float = 1.0) -> list[int]:
        """Encode ``value`` into a list of 16-bit registers for ``data_type``."""
        dt = data_type.lower()
        if dt in ('float', 'float32'):
            return self._enc_float(float(value) * scale)
        if dt == 'double':
            return self._enc_double(float(value) * scale)

        # Preserve full integer precision for large counters (energy Wh, etc.):
        # float() has a 53-bit mantissa, so int(round(float(value)*scale)) corrupts
        # the low bits of int64/uint64 values > 2^53. When both operands are already
        # integral, multiply in pure-int space and skip the float round-trip.
        if isinstance(value, int) and not isinstance(value, bool) and float(scale).is_integer():
            raw = value * int(scale)
        else:
            raw = int(round(float(value) * scale))
        lo, hi = _INT_RANGES.get(dt, _INT_RANGES['int32'])
        if raw < lo or raw > hi:                # over-range: clamp, but never silently
            logger.warning("encode: %s (scale %s) → %s out of range for %s [%s, %s]; clamping",
                           value, scale, raw, dt, lo, hi)
            raw = max(lo, min(hi, raw))

        if dt in ('int16', 'short', 'uint16'):
            return self._apply_order([raw & 0xffff])   # byte-swap orders swap the word
        if dt in ('int32', 'uint32'):
            return self._split32(raw & 0xffffffff)
        if dt in ('int64', 'long64', 'uint64'):
            return self._split64(raw & 0xffffffffffffffff)
        return self._split32(raw & 0xffffffff)

    # ── helpers (canonical big-endian, then apply the wire order) ──────────
    def _split32(self, u: int) -> list[int]:
        return self._apply_order([(u >> 16) & 0xffff, u & 0xffff])

    def _split64(self, u: int) -> list[int]:
        return self._apply_order([(u >> 48) & 0xffff, (u >> 32) & 0xffff,
                                  (u >> 16) & 0xffff, u & 0xffff])

    def _enc_float(self, v: float) -> list[int]:
        return self._apply_order(list(struct.unpack('>HH', struct.pack('>f', v))))

    def _enc_double(self, v: float) -> list[int]:
        return self._apply_order(list(struct.unpack('>HHHH', struct.pack('>d', v))))

    # SunSpec "not implemented / not accessible" sentinels — the industry
    # convention for "this register has no valid value right now". Used by the
    # virtual-meter 'sentinel' staleness policy so absence is NEVER encodable
    # as a plausible measurement (0 is a valid reading; NaN/type-min are not).
    _SENTINELS = {
        'int16': -0x8000, 'short': -0x8000,
        'uint16': 0xFFFF,
        'int32': -0x8000_0000, 'uint32': 0xFFFF_FFFF,
        'int64': -0x8000_0000_0000_0000, 'long64': -0x8000_0000_0000_0000,
        'uint64': 0xFFFF_FFFF_FFFF_FFFF,
    }

    def sentinel_words(self, data_type: str, length: int = 1) -> list[int]:
        """Register words meaning "value not available" for ``data_type``
        (SunSpec NA convention): float/double → NaN, ints → type-edge sentinel,
        string → NULs. Same wire ordering as a normal encode."""
        dt = data_type.lower()
        if dt in ('float', 'float32'):
            return self._enc_float(float('nan'))
        if dt == 'double':
            return self._enc_double(float('nan'))
        if dt == 'string':
            return self.encode_string('', max(1, int(length)))
        raw = self._SENTINELS.get(dt, self._SENTINELS['int32'])
        if dt in ('int16', 'short', 'uint16'):
            return self._apply_order([raw & 0xffff])
        if dt in ('int64', 'long64', 'uint64'):
            return self._split64(raw & 0xffffffffffffffff)
        return self._split32(raw & 0xffffffff)

    def encode_string(self, text: str, length_regs: int) -> list[int]:
        """ASCII string into ``length_regs`` registers (2 chars/reg, null-padded).
        Word order is sequential; byte-swap orders swap the two chars per word."""
        raw = text.encode('ascii', 'replace')[: length_regs * 2]
        raw = raw.ljust(length_regs * 2, b'\x00')
        regs = [int.from_bytes(raw[i:i + 2], 'big') for i in range(0, len(raw), 2)]
        if self._byteswap:
            regs = [((r & 0xff) << 8) | ((r >> 8) & 0xff) for r in regs]
        return regs
