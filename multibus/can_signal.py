# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""CAN signals in a template row — the schema, its checks and the decoder.

A CAN frame has no registers: an id, up to 8 data bytes, and signals placed
on bits. A template row describes one signal with DBC's words, as optional
fields beside the ones every row has::

    can_id            int or "0x…" string; several rows per id is normal
    can_id_extended   true for a 29-bit id (required for ids >= 0x800)
    start_bit         DBC start bit (Intel: the LSB; Motorola: the MSB, in
                      DBC's "sawtooth" numbering)
    bit_length        1..64
    byte_order        little (DBC Intel, @1) | big (DBC Motorola, @0)

``data_type`` stays the gateway's vocabulary (no new value types): it says
the signedness and the container (``uint16`` holds up to 16 bits, ``int32``
a signed signal of up to 32, ``float32`` exactly 32 IEEE bits, ``string:N``
N*2 ASCII bytes). ``scale`` is a divisor and ``offset`` is added after it,
``enum``/``bits``/``mask`` decode as on any other row — the shared
correction pipeline (value_decode.apply_corrections) does all of that, so
this module only turns bytes into the raw number or text.

``address`` stays synthetic (as on MQTT rows): the value store, calcs,
outputs and UI are keyed by it and never see a CAN coordinate.
"""
from __future__ import annotations

import re
import struct
from typing import Any, Dict, Iterable, List, Optional, Tuple

# the row fields this module owns, in the order they are written out
CAN_FIELDS = ('can_id', 'can_id_extended', 'start_bit', 'bit_length', 'byte_order')
# the template transports that read a map of CAN signals
CAN_TRANSPORTS = ('can', 'can_tap')

STD_ID_MAX = 0x7FF
EXT_ID_MAX = 0x1FFFFFFF
MAX_FRAME_BITS = 64            # classic CAN: 8 data bytes (no CAN FD in v1)
BYTE_ORDERS = ('little', 'big')

# data_type -> (container bits, signed). Floats need their exact width.
_INT_TYPES = {
    'uint16': (16, False), 'uint32': (32, False), 'uint64': (64, False),
    'int16': (16, True), 'short': (16, True),
    'int32': (32, True), 'int64': (64, True), 'long64': (64, True),
}
_FLOAT_TYPES = {'float': 32, 'float32': 32, 'double': 64}


def parse_can_id(v: Any) -> Optional[int]:
    """An id as a template writes it — an integer or a ``0x…``/decimal
    string — or None when it is not one."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return int(v.strip(), 0)
        except ValueError:
            return None
    return None


def _get(row: Any, name: str, default=None):
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def is_can_row(row: Any) -> bool:
    """Does this row describe a CAN signal (it carries a ``can_id``)?"""
    return _get(row, 'can_id') not in (None, '')


def can_fields(row: Any) -> Dict[str, Any]:
    """A row's CAN fields as they are written to a template or a selection
    file (``can_id`` as ``0x…``), or ``{}`` for a row that has none. What
    every copy of a row — seed, API, export — carries along."""
    cid = parse_can_id(_get(row, 'can_id'))
    if cid is None:
        return {}
    out: Dict[str, Any] = {'can_id': f"0x{cid:X}"}
    if _get(row, 'can_id_extended'):
        out['can_id_extended'] = True
    for k in ('start_bit', 'bit_length'):
        v = _get(row, k)
        if v is not None:
            out[k] = int(v)
    bo = _get(row, 'byte_order')
    if bo:
        out['byte_order'] = str(bo).lower()
    return out


def frame_key(row: Any) -> Optional[Tuple[int, bool]]:
    """(id, extended) — the frame a row's signal rides on."""
    cid = parse_can_id(_get(row, 'can_id'))
    if cid is None:
        return None
    return cid, bool(_get(row, 'can_id_extended', False))


# ── bit positions ────────────────────────────────────────────────────────────

def _motorola_pos(start_bit: int) -> int:
    """DBC Motorola start bit (MSB, sawtooth numbering) → the signal's MSB
    position counted from the first transmitted bit (byte 0 bit 7 = 0)."""
    return (start_bit // 8) * 8 + (7 - start_bit % 8)


def signal_bits(start_bit: int, bit_length: int, byte_order: str) -> List[int]:
    """The frame bits a signal occupies, in DBC's numbering (byte*8 + bit,
    bit 0 = the byte's LSB) — what the overlap check compares."""
    if byte_order == 'big':
        pos = _motorola_pos(start_bit)
        lin = range(pos, pos + bit_length)
        return [(p // 8) * 8 + (7 - p % 8) for p in lin]
    return list(range(start_bit, start_bit + bit_length))


def bits_needed(start_bit: int, bit_length: int, byte_order: str) -> int:
    """How many data bits a frame must carry for the signal to be in it."""
    if byte_order == 'big':
        return _motorola_pos(start_bit) + bit_length
    return start_bit + bit_length


def extract_raw(data: bytes, start_bit: int, bit_length: int, byte_order: str = 'little') -> int:
    """The signal's unsigned raw bits, DBC semantics. ValueError when the
    frame is too short to hold them."""
    total = 8 * len(data)
    if bits_needed(start_bit, bit_length, byte_order) > total:
        raise ValueError(f"frame of {len(data)} bytes is too short for bits "
                         f"{start_bit}+{bit_length} ({byte_order})")
    mask = (1 << bit_length) - 1
    if byte_order == 'big':
        v = int.from_bytes(data, 'big')
        return (v >> (total - _motorola_pos(start_bit) - bit_length)) & mask
    return (int.from_bytes(data, 'little') >> start_bit) & mask


def _string_len(dt: str) -> Optional[int]:
    """'string:N' → N registers (2 bytes each), as the Modbus maps say it."""
    m = re.match(r'^string:?(\d+)$', dt)
    return int(m.group(1)) if m else None


def decode_signal(data: bytes, row: Any) -> Any:
    """One row's raw value out of a frame's bytes: an int (sign applied for
    a signed type), a float for float32/double, or text for a string. The
    caller runs the shared correction pipeline (scale, offset, enum, bits)
    on a number. ValueError when the frame cannot hold it."""
    sb, bl = int(_get(row, 'start_bit')), int(_get(row, 'bit_length'))
    bo = str(_get(row, 'byte_order') or 'little').lower()
    dt = str(_get(row, 'data_type') or 'uint16').lower()
    if dt.startswith('string'):
        if sb % 8 or bl % 8:
            raise ValueError("a string starts and ends on a byte boundary")
        # text is bytes in wire order whatever the byte order says; start_bit
        # names its first byte (a multiple of 8)
        chunk = data[sb // 8:sb // 8 + bl // 8]
        if len(chunk) < bl // 8:
            raise ValueError(f"frame of {len(data)} bytes is too short for the text")
        return chunk.decode('ascii', 'replace').replace('\x00', ' ').strip()
    raw = extract_raw(data, sb, bl, bo)
    if dt in _FLOAT_TYPES:
        fmt, width = ('>f', 4) if _FLOAT_TYPES[dt] == 32 else ('>d', 8)
        return struct.unpack(fmt, raw.to_bytes(width, 'big'))[0]
    _, signed = _INT_TYPES.get(dt, (64, False))
    if signed and raw & (1 << (bl - 1)):
        raw -= 1 << bl
    return raw


# ── template checks ──────────────────────────────────────────────────────────

def row_errors(r: Dict[str, Any], where: str) -> List[str]:
    """What is wrong with one row's CAN fields (empty = fine)."""
    present = [k for k in CAN_FIELDS if r.get(k) is not None]
    if not present:
        return []
    errs: List[str] = []
    if 'can_id' not in present:
        return [f"{where}: {', '.join(present)} without can_id — a CAN signal names its frame"]
    cid = parse_can_id(r.get('can_id'))
    ext = r.get('can_id_extended', False)
    if ext is not None and not isinstance(ext, bool):
        errs.append(f"{where}: can_id_extended must be true or false")
        ext = False
    if cid is None or cid < 0:
        errs.append(f"{where}: can_id must be a number (e.g. 849 or \"0x351\")")
    elif ext and cid > EXT_ID_MAX:
        errs.append(f"{where}: can_id 0x{cid:X} is over the 29-bit range (0x1FFFFFFF)")
    elif not ext and cid > STD_ID_MAX:
        errs.append(f"{where}: can_id 0x{cid:X} is over 0x7FF — a 29-bit id needs can_id_extended: true")
    sb, bl = r.get('start_bit'), r.get('bit_length')
    ok_sb = isinstance(sb, int) and not isinstance(sb, bool) and 0 <= sb < MAX_FRAME_BITS
    ok_bl = isinstance(bl, int) and not isinstance(bl, bool) and 1 <= bl <= MAX_FRAME_BITS
    if not ok_sb:
        errs.append(f"{where}: start_bit must be a whole number 0..63")
    if not ok_bl:
        errs.append(f"{where}: bit_length must be a whole number 1..64")
    bo = r.get('byte_order')
    if bo is not None and str(bo).lower() not in BYTE_ORDERS:
        errs.append(f"{where}: byte_order must be little (DBC Intel) or big (DBC Motorola)")
        bo = 'little'
    if bo is None and ok_bl and bl > 8:
        errs.append(f"{where}: byte_order is required for a signal over 8 bits (little or big)")
    bo = str(bo or 'little').lower()
    dt = str(r.get('data_type', 'float')).lower()
    if dt.startswith('string') and bo == 'big':
        # text is bytes in wire order: DBC's Motorola numbering says nothing
        # useful about it, and its start bit would read as the wrong byte
        errs.append(f"{where}: CAN strings are little-endian only (bytes in wire order) — "
                    f"byte_order little, start_bit the first byte × 8")
        return errs
    if ok_sb and ok_bl and bits_needed(sb, bl, bo) > MAX_FRAME_BITS:
        errs.append(f"{where}: bits {sb}+{bl} ({bo}) do not fit in 8 data bytes")
    if ok_bl:
        errs.extend(_type_errors(dt, sb if ok_sb else 0, bl, where))
    for k in ('topic', 'json_path'):
        if r.get(k):
            errs.append(f"{where}: a CAN signal has no {k} — it is read from its frame")
    return errs


def _type_errors(dt: str, sb: int, bl: int, where: str) -> List[str]:
    if dt.startswith('string'):
        n = _string_len(dt)
        if n is None or n * 16 != bl:
            return [f"{where}: a CAN string 'string:N' is N*2 bytes — bit_length {bl} needs "
                    f"'string:{bl // 16}'" + ("" if bl % 16 == 0 else " (an even number of bytes)")]
        if sb % 8:
            return [f"{where}: a CAN string starts on a byte boundary (start_bit a multiple of 8)"]
        return []
    if dt in _FLOAT_TYPES:
        w = _FLOAT_TYPES[dt]
        return [] if bl == w else [f"{where}: data_type {dt} is {w} IEEE bits — bit_length must be {w}"]
    if dt in _INT_TYPES:
        w, _ = _INT_TYPES[dt]
        return [] if bl <= w else [f"{where}: data_type {dt} holds {w} bits — bit_length {bl} "
                                   f"needs a wider type"]
    return [f"{where}: data_type {dt} is not used on CAN signals "
            f"(uint16/32/64, int16/32/64, float32, double, string:N)"]


def template_errors(t: Dict[str, Any]) -> List[str]:
    """The CAN checks of a whole template: every row's fields, and a CAN
    map whose rows all carry their frame (a row without one would never
    receive a value)."""
    proto = t.get('protocol') if isinstance(t.get('protocol'), dict) else {}
    transports = [str(x).lower() for x in (proto.get('transports') or [])]
    is_can = any(x in CAN_TRANSPORTS for x in transports)
    errs: List[str] = []
    regs = t.get('registers') if isinstance(t.get('registers'), list) else []
    for i, r in enumerate(regs):
        if not isinstance(r, dict):
            continue
        where = f"register #{i} (addr {r.get('address')!r}, name {r.get('name')!r})"
        errs.extend(row_errors(r, where))
        if is_can and r.get('can_id') in (None, ''):
            errs.append(f"{where}: a CAN template's rows each name their frame (can_id, start_bit, bit_length)")
        elif not is_can and transports and r.get('can_id') not in (None, ''):
            errs.append(f"{where}: can_id on a {'/'.join(transports)} template — "
                        f"declare protocol.transports: [can]")
    br = proto.get('bitrate')
    if br is not None and (isinstance(br, bool) or not isinstance(br, int) or not 10_000 <= br <= 1_000_000):
        errs.append("protocol.bitrate: bits per second, 10000..1000000 (e.g. 500000)")
    return errs


def overlap_warnings(regs: Iterable[Any]) -> List[str]:
    """Two signals on the same bits of one frame. A warning, not an error:
    some protocols overlap on purpose (a byte and its flags)."""
    seen: Dict[Tuple[int, bool], List[Tuple[str, set]]] = {}
    out: List[str] = []
    for r in regs:
        key = frame_key(r)
        if key is None:
            continue
        try:
            bits = set(signal_bits(int(_get(r, 'start_bit')), int(_get(r, 'bit_length')),
                                   str(_get(r, 'byte_order') or 'little').lower()))
        except (TypeError, ValueError):
            continue
        name = str(_get(r, 'name') or '?')
        for other, obits in seen.get(key, []):
            if bits & obits:
                out.append(f"CAN 0x{key[0]:X}: {name!r} overlaps {other!r} "
                           f"(fine if intentional — a byte and its flags)")
        seen.setdefault(key, []).append((name, bits))
    return out


# ── candump log lines (test fixtures, the e2e replay) ────────────────────────

_CANDUMP_RE = re.compile(r'^\((\d+\.\d+)\)\s+(\S+)\s+([0-9A-Fa-f]{3}|[0-9A-Fa-f]{8})#(R|[0-9A-Fa-f]*)\s*$')


def parse_candump_line(line: str) -> Optional[Tuple[float, str, int, bool, bytes]]:
    """``(ts) iface ID#DATA`` (``candump -L``) → (ts, iface, id, extended,
    data), or None for a line that is not a classic frame. A 3-digit id is
    11-bit, an 8-digit one 29-bit; a remote request (``#R``) carries no data."""
    m = _CANDUMP_RE.match(line.strip())
    if not m:
        return None
    ts, iface, cid, data = m.groups()
    if data == 'R':
        return float(ts), iface, int(cid, 16), len(cid) == 8, b''
    if len(data) % 2 or len(data) > 16:
        return None
    return float(ts), iface, int(cid, 16), len(cid) == 8, bytes.fromhex(data)
