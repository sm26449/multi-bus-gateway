# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Enum / bitfield decode — turn a raw status register into meaning.

Many registers are not measurements but **state**: an inverter operating state
(``4`` → ``MPPT``, ``7`` → ``Fault``), or a packed **status word** where each
bit is a flag (``0b1101`` → ``overvoltage, overcurrent, overtemp``). Served raw
they are useless to a human, to Home Assistant, and to the threshold engine.

Both decoders are pure and take the raw integer plus a map declared on the
register (``enum`` / ``bits``), returning a **string** that flows to MQTT /
InfluxDB / HA exactly like the existing string registers. A status word with
packed sub-fields is handled by ``mask`` + ``shift`` (extract ``(raw & mask) >>
shift`` before the enum lookup).
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def _int_keys(m: Dict) -> Dict[int, str]:
    """Normalize a JSON-sourced map (string keys) to ``{int: str}``; entries
    whose key is not an integer are dropped rather than raising."""
    out: Dict[int, str] = {}
    for k, v in (m or {}).items():
        try:
            out[int(k)] = str(v)
        except (TypeError, ValueError):
            continue
    return out


def _extract(raw: int, mask: Optional[int], shift: int) -> int:
    v = int(raw)
    if mask is not None:
        v &= int(mask)
    if shift:
        v >>= int(shift)
    return v


def decode_enum(value: Any, enum_map: Dict, *, mask: Optional[int] = None,
                shift: int = 0, unknown: Optional[str] = None) -> Optional[str]:
    """Map a raw integer to its label. An unmapped value returns ``unknown`` if
    given, else ``"unknown (<n>)"`` — never silently wrong, and never the bare
    number (which would read as a measurement downstream)."""
    try:
        code = _extract(value, mask, shift)
    except (TypeError, ValueError):
        return None
    label = _int_keys(enum_map).get(code)
    if label is not None:
        return label
    return unknown if unknown is not None else f"unknown ({code})"


def decode_bits(value: Any, bit_map: Dict, *, glue: str = ", ",
                none_label: str = "") -> Optional[str]:
    """Expand a status word to the ``glue``-joined names of its set bits, in bit
    order. No bit set → ``none_label`` (default ``""``, i.e. the healthy/idle
    state). Keys are bit positions (``0`` = LSB)."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    names = _int_keys(bit_map)
    active = [names[bit] for bit in sorted(names) if v & (1 << bit)]
    return glue.join(active) if active else none_label


def decode_register(value: Any, reg) -> Any:
    """Apply whatever decode a register declares (``enum`` wins over ``bits``);
    return the value unchanged when neither is set or the value is missing.
    ``reg`` may be a dataclass or a dict-like — attributes are read defensively
    so this is safe on the poll path."""
    if value is None:
        return None

    def _f(name):
        if isinstance(reg, dict):
            return reg.get(name)
        return getattr(reg, name, None)

    enum_map = _f("enum")
    if enum_map:
        return decode_enum(value, enum_map, mask=_f("mask"), shift=_f("shift") or 0)
    bit_map = _f("bits")
    if bit_map:
        return decode_bits(value, bit_map)
    return value


def is_textual(reg) -> bool:
    """True if the register decodes to text (enum/bits) — used to keep an
    invalid numeric HA state_class/device_class off a state sensor."""
    if isinstance(reg, dict):
        return bool(reg.get("enum") or reg.get("bits"))
    return bool(getattr(reg, "enum", None) or getattr(reg, "bits", None))
