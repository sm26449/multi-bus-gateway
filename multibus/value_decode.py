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


# ── the ONE wire→value correction pipeline ───────────────────────────────────

def is_sentinel_value(value, data_type: str, nan) -> bool:
    """Not-available sentinel test, shared across transports — the same
    semantics as the Modbus parser (``nan`` is True → the standard sentinel
    for the type, a number, or a list of values)."""
    from .register_parser import RegisterParser
    if nan is True:
        s = RegisterParser._NOT_IMPLEMENTED.get(str(data_type).lower())
        return s is not None and value == s
    if isinstance(nan, (list, tuple, set)):
        return value in nan
    if isinstance(nan, (int, float)) and not isinstance(nan, bool):
        return value == nan
    return False


def apply_corrections(value, reg, *, counter_filter=None, info=None,
                      siblings=None):
    """The wire→value correction pipeline, shared by every transport.

    This logic used to exist in six drifted copies (external audit's
    highest-leverage finding): the Modbus poll path had every stage, while
    HTTP/MQTT silently skipped nan/enum/monotonic and the diagnostic views
    skipped offset. The order matches the authoritative Modbus path exactly:

      1. ``nan`` sentinel on the RAW value → None (absence; the caller holds
         last-good). Compared BEFORE scaling, like the parser always did.
      2. ``enum``/``bits`` → text (scale/offset/monotonic are numeric-only,
         so they are skipped for status registers).
      3. engineering = raw/scale + offset — or, when the register declares
         ``scale_from`` (SunSpec dynamic scale factor), engineering =
         raw × 10^SF where SF is the referenced sibling's current raw value
         taken from ``siblings`` ({name: value}, supplied by polling callers
         from the batch's own reads + a last-good bridge). No valid SF
         (missing, non-numeric or |SF| > 10) → the value is MISSING — a
         wrongly-scaled reading is worse than no reading. Negative SF also
         rounds to −SF decimals to kill float noise (parity with the SunSpec
         collectors this replaces).
      4. monotonic counter filter — STATEFUL: only POLLING callers own a
         per-register MonotonicFilter and pass it. Diagnostic reads
         (query/read-back/views) must NOT pass one — a debug read must never
         advance filter state.

    Every stage engages only when the register declares it; an undeclared
    register passes through unchanged — which is why extending a transport
    with this helper cannot change behavior for existing configs.

    ``info`` (optional dict) reports WHY a None was returned
    (``stage``: ``sentinel`` | ``decode_failed`` | ``filter_drop``) so the
    polling callers keep their edge-triggered diagnostics (DP-6/DP-10).
    Returns the corrected value, or None meaning "treat as missing".
    """
    if value is None:
        return None

    def _f(name, default=None):
        if isinstance(reg, dict):
            return reg.get(name, default)
        return getattr(reg, name, default)

    nan = _f("nan")
    if (nan is not None and nan is not False
            and isinstance(value, (int, float)) and not isinstance(value, bool)
            and is_sentinel_value(value, _f("data_type") or "float", nan)):
        if info is not None:
            info["stage"] = "sentinel"
        return None

    if _f("enum") or _f("bits"):
        decoded = decode_register(value, reg)
        if decoded is None and info is not None:
            info["stage"] = "decode_failed"
        return decoded

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        sf_ref = _f("scale_from", "") or ""
        if sf_ref:
            sf = (siblings or {}).get(sf_ref)
            if (not isinstance(sf, (int, float)) or isinstance(sf, bool)
                    or abs(sf) > 10):
                if info is not None:
                    info["stage"] = "sf_missing"
                return None
            sf = int(sf)
            value = value * (10.0 ** sf)
            if sf < 0:
                value = round(value, -sf)
        else:
            sc = _f("scale", 1.0) or 1.0
            if sc != 1.0:
                value = value / sc
        off = _f("offset", 0.0) or 0.0
        if off:
            value = value + off

    if counter_filter is not None and _f("monotonic"):
        value = counter_filter.feed(value)
        if value is None and info is not None:
            info["stage"] = "filter_drop"
    return value
