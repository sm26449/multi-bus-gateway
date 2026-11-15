# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Build a CAN frame's 8 data bytes from signal values — the exact inverse of
``can_signal.extract_raw`` / ``decode_signal`` (Virtual BMS V0, ADR-0037).

This is the frame builder a Virtual BMS needs to speak Pylontech-LV (or any DBC
map) to an inverter. It is **pure**: bytes in, bytes out. It owns NO socket, NO
serial port, NO CAN interface, and it never transmits — a Virtual BMS executor is
a SEPARATE, later lot that must pass an explicit arm/no-transmit gate. The marker
below (and the test that reads this file) make that guarantee checkable.

Round-trip property, for any real captured frame and its template rows:

    encode_frame((row, decode_signal(frame, row)) for row in rows) == frame   # for the bits the rows cover

so an encoder that reproduces a real Seplos→Deye frame byte-for-byte is proven
against the wire before anything is ever sent.
"""
from __future__ import annotations

import re
import struct
from typing import Any, Iterable, List, Tuple

from multibus.codec.can_signal import (
    _FLOAT_TYPES,
    _INT_TYPES,
    _get,
    _motorola_pos,
    bits_needed,
)

# ── no-transmit guarantee (read by test_can_encode) ───────────────────────────
# This module turns values into bytes and nothing else. It must never grow a
# transmit path; the Virtual BMS executor (socket/arm/send) is its own lot.
TRANSMITS = False


def place_raw(buf: bytearray, start_bit: int, bit_length: int,
              raw: int, byte_order: str = "little") -> None:
    """Write ``raw`` (unsigned, masked to ``bit_length``) into ``buf`` at the DBC
    position — the exact inverse of ``can_signal.extract_raw``. Mutates ``buf``."""
    total = 8 * len(buf)
    if bits_needed(start_bit, bit_length, byte_order) > total:
        raise ValueError(f"frame of {len(buf)} bytes is too short for bits "
                         f"{start_bit}+{bit_length} ({byte_order})")
    mask = (1 << bit_length) - 1
    raw &= mask
    if byte_order == "big":
        shift = total - _motorola_pos(start_bit) - bit_length
        cur = int.from_bytes(buf, "big")
        cur = (cur & ~(mask << shift)) | (raw << shift)
        buf[:] = cur.to_bytes(len(buf), "big")
    else:
        cur = int.from_bytes(buf, "little")
        cur = (cur & ~(mask << start_bit)) | (raw << start_bit)
        buf[:] = cur.to_bytes(len(buf), "little")


def _string_bytes(dt: str) -> int:
    m = re.match(r"^string:?(\d+)$", dt)
    return int(m.group(1)) * 2 if m else 0          # 'string:4' → 8 wire bytes


def encode_signal(buf: bytearray, row: Any, value: Any) -> None:
    """Place one row's ``value`` into ``buf`` — the inverse of ``decode_signal``.
    ``value`` is what ``decode_signal`` returns (signed int / float / text).
    Mutates ``buf``. A string is NUL-padded to its width (decode strips padding,
    so a frame whose text fills the field round-trips exactly; a padded one may
    differ in the pad byte — reported, not hidden, by the round-trip)."""
    sb, bl = int(_get(row, "start_bit")), int(_get(row, "bit_length"))
    bo = str(_get(row, "byte_order") or "little").lower()
    dt = str(_get(row, "data_type") or "uint16").lower()
    if dt.startswith("string"):
        if sb % 8 or bl % 8:
            raise ValueError("a string starts and ends on a byte boundary")
        width = bl // 8
        text = ("" if value is None else str(value)).encode("ascii", "replace")[:width]
        chunk = text + b"\x00" * (width - len(text))
        buf[sb // 8:sb // 8 + width] = chunk
        return
    if dt in _FLOAT_TYPES:
        fmt, w = (">f", 4) if _FLOAT_TYPES[dt] == 32 else (">d", 8)
        raw = int.from_bytes(struct.pack(fmt, float(value)), "big")
        place_raw(buf, sb, bl, raw, bo)
        return
    _, signed = _INT_TYPES.get(dt, (64, False))
    iv = int(value)
    if signed and iv < 0:
        iv += 1 << bl                               # two's complement in bit_length
    place_raw(buf, sb, bl, iv & ((1 << bl) - 1), bo)


def encode_frame(rows_values: Iterable[Tuple[Any, Any]], length: int = 8) -> bytes:
    """Build a ``length``-byte frame from ``(row, value)`` pairs. Unset bits stay
    zero (so a frame whose rows do not cover every byte will differ from a real
    one there — the round-trip report surfaces exactly which bits)."""
    buf = bytearray(length)
    for row, value in rows_values:
        encode_signal(buf, row, value)
    return bytes(buf)


def patch_frame(frame: bytes, rows_values: Iterable[Tuple[Any, Any]]) -> bytes:
    """Overlay field changes onto an existing frame: start from ``frame``'s bytes
    and re-encode ONLY the given ``(row, value)`` pairs, leaving every other bit
    (vendor/reserved bytes included) exactly as captured. This is the Virtual BMS
    replay primitive — send roughly the real battery data, change one field, and
    confirm the inverter reacts — so byte-identity does not depend on the template
    modelling every vendor byte."""
    buf = bytearray(frame)
    for row, value in rows_values:
        encode_signal(buf, row, value)
    return bytes(buf)


# ── round-trip verification (used by the bench script + tests) ────────────────
def roundtrip(frame: bytes, rows: List[Any]) -> bytes:
    """Decode every row out of ``frame`` and re-encode it; the result equals
    ``frame`` for every bit the rows cover."""
    from multibus.codec.can_signal import decode_signal
    return encode_frame(((r, decode_signal(frame, r)) for r in rows), length=len(frame))


def uncovered_bits(frame: bytes, rebuilt: bytes) -> List[int]:
    """DBC bit indices (byte*8 + bit) where ``frame`` and its re-encode differ —
    i.e. set bits the template rows do not account for. Empty = byte-exact."""
    diff = int.from_bytes(frame, "little") ^ int.from_bytes(rebuilt, "little")
    return [i for i in range(8 * len(frame)) if (diff >> i) & 1]
