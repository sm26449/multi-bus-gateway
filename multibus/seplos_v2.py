# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Seplos V2 BMS: the ASCII "telecom" protocol, read-only, into a virtual map.

Seplos V2 packs do not speak Modbus. Their RS485 port speaks the
telecom-style ASCII protocol (the YD/T 1363 family, CID1 0x46 = battery):

    ~ VER ADR CID1 CID2 LENGTH INFO CHKSUM \\r

every byte between ``~`` and ``\\r`` written as two upper-case ASCII hex
characters. LENGTH is LCHKSUM (4 bits) + LENID (12 bits), LENID being the
number of ASCII characters of INFO; CHKSUM is the two's complement of the sum
of the ASCII characters between ``~`` and the checksum (mod 65536). A request
carries a command in CID2; the answer carries a return code (RTN) there.

What this module guarantees (ADR-0014):

* **Read-only by construction.** :func:`encode` builds a frame only for the
  read commands in :data:`READ_COMMANDS` and raises :class:`CommandNotAllowed`
  for anything else. There is no other encoder here, and no configuration
  reaches past it: the gateway cannot send a set-parameter or control command.
* **A fixed virtual register map** (the ``MAP_*`` constants below). Each answer is
  decoded semantically and written as 16-bit words at fixed addresses of the
  ``input`` table, so a template uses the normal register machinery and one
  template serves a 15-cell and a 16-cell pack. A value the pack does not
  report (a cell past its count, a 0xFFFF "not available") is ABSENT — no
  word is written and the register publishes nothing, never a zero.

Two ways to read a bus (``connection.mode``; the API asks it of every new
device, a stored one without it reads as poll, and a value it cannot trust
starts nothing):

* ``poll`` — the gateway is the master: one transport per serial
  port (``seplos_v2``) or per bridge host:port (``seplos_v2-tcp``), shared by
  every pack on it under one lock; pack ADR = the device's ``unit_id``.
* ``listen`` — passive, the ``rtu_tap`` model: one reader per port that NEVER
  writes; it pairs the master pack's requests with the answers on the
  inter-pack bus and feeds the pack whose ADR registered. Never poll a bus a
  master pack already polls: listen there. Besides the read commands it
  pairs the master's 0x5A summary exchange (:data:`CMD_PARALLEL_SUMMARY`),
  the only traffic a V2 master puts on that bus (bench, 2026-10-26).

Everything protocol-shaped here comes from the published Seplos "BMS
communication protocol V2.0" and the community decoders built on it; nothing
is verified on hardware yet (bench lot A). The decoders read by the counts in
the frame and never by an assumed length, and every guess is listed in the doc.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .register_parser import RegisterParser
from .value_decode import apply_corrections

logger = logging.getLogger(__name__)

SOI, EOI = 0x7E, 0x0D              # '~' and '\r'
VER = 0x20                         # protocol version 2.0
CID1_BATTERY = 0x46

CMD_TELEMETRY = 0x42               # analog values: cells, temps, current, SOC…
CMD_ALARMS = 0x44                  # telesignalling: alarm and status bytes
CMD_SYSTEM_PARAMS = 0x47           # system parameters (no decoder yet)
CMD_MANUFACTURER = 0x51            # device name, software version, manufacturer
# The ONLY commands this gateway may ever encode. Everything else the protocol
# knows (control 0x45, set parameters 0x49, …) is refused by encode().
READ_COMMANDS = frozenset({CMD_TELEMETRY, CMD_ALARMS, CMD_SYSTEM_PARAMS, CMD_MANUFACTURER})
# The master pack's per-slave summary on the inter-pack bus (bench capture
# 2026-10-26). Undocumented: NOT a command this gateway may send — it is
# only heard. Its answer echoes 0x5A in CID2 (no RTN) and carries the INFO.
CMD_PARALLEL_SUMMARY = 0x5A
# what a listener pairs (requests it recognises); encode() never sees 0x5A
LISTEN_COMMANDS = READ_COMMANDS | {CMD_PARALLEL_SUMMARY}

RTN_TEXT = {
    0x00: 'ok', 0x01: 'VER error', 0x02: 'CHKSUM error', 0x03: 'LCHKSUM error',
    0x04: 'CID2 invalid', 0x05: 'command format error', 0x06: 'invalid data',
    # telecom-family codes Seplos is reported to use; unverified on hardware
    0x90: 'ADR error', 0x91: 'internal communication error',
}

MAX_LENID = 0x0FFF
_MAX_FRAME = 1 + 12 + MAX_LENID + 4 + 1
_HEX = frozenset(b'0123456789ABCDEFabcdef')


# ── checksums and the read-only encoder ──────────────────────────────────────

def lchksum(lenid: int) -> int:
    """The LENGTH field's 4-bit checksum: the two's complement of the sum of
    LENID's three nibbles, mod 16 (0 when LENID is 0)."""
    if not 0 <= lenid <= MAX_LENID:
        raise ValueError(f"LENID {lenid} out of range 0..{MAX_LENID}")
    s = (lenid & 0xF) + ((lenid >> 4) & 0xF) + ((lenid >> 8) & 0xF)
    return (~s + 1) & 0xF


def length_field(lenid: int) -> int:
    """LCHKSUM:4 + LENID:12."""
    return (lchksum(lenid) << 12) | lenid


def chksum(body: bytes) -> int:
    """The frame checksum over the ASCII characters between '~' and CHKSUM."""
    return (~sum(body) + 1) & 0xFFFF


class CommandNotAllowed(ValueError):
    """A CID2 outside the read-only allow-list: the gateway never sends it."""


def _hex(b: Iterable[int]) -> bytes:
    return ''.join(f'{x:02X}' for x in b).encode('ascii')


def encode(adr: int, cid2: int, info: bytes = b'') -> bytes:
    """One request frame, ``~…\\r``. Refuses every CID2 that is not a read."""
    if cid2 not in READ_COMMANDS:
        raise CommandNotAllowed(
            f"CID2 0x{int(cid2) & 0xFF:02X} is not a read command — the Seplos V2 "
            f"driver only encodes {', '.join(f'0x{c:02X}' for c in sorted(READ_COMMANDS))}")
    if not 0 <= int(adr) <= 0xFF:
        raise ValueError(f"ADR {adr} out of range 0..255")
    info_hex = _hex(info)
    body = _hex((VER, adr, CID1_BATTERY, cid2)) + f'{length_field(len(info_hex)):04X}'.encode() + info_hex
    return bytes([SOI]) + body + f'{chksum(body):04X}'.encode() + bytes([EOI])


def request(adr: int, cid2: int) -> bytes:
    """The request for one read command as the community tools send it: the
    pack address in INFO for 0x42/0x44/0x47, no INFO for 0x51."""
    return encode(adr, cid2, b'' if cid2 == CMD_MANUFACTURER else bytes([adr & 0xFF]))


# ── frames ───────────────────────────────────────────────────────────────────

class FrameError(ValueError):
    kind = 'format'


class FormatError(FrameError):
    kind = 'format'


class LengthError(FrameError):
    kind = 'length'


class LengthChecksumError(FrameError):
    kind = 'lchksum'


class ChecksumError(FrameError):
    kind = 'chksum'


class DecodeError(ValueError):
    """A valid frame whose INFO does not hold what its counts promise."""


@dataclass
class Frame:
    ver: int
    adr: int
    cid1: int
    cid2: int
    info: bytes
    raw: bytes = b''

    @property
    def is_request(self) -> bool:
        """Commands sit in 0x40..0x7F; answers carry an RTN (0x00.., 0x80..)."""
        return 0x40 <= self.cid2 <= 0x7F


def parse(raw: bytes) -> Frame:
    """Validate one ``~…\\r`` frame (SOI, EOI, hex, LENGTH, LCHKSUM, CHKSUM)."""
    raw = bytes(raw)
    if len(raw) < 18 or raw[0] != SOI or raw[-1] != EOI:
        raise FormatError(f"not a frame ({len(raw)} bytes)")
    body = raw[1:-1]
    if any(c not in _HEX for c in body):
        raise FormatError("non-hex characters inside the frame")
    try:
        head = bytes.fromhex(body[:12].decode('ascii'))
    except ValueError as e:
        raise FormatError(str(e)) from None
    ver, adr, cid1, cid2 = head[0], head[1], head[2], head[3]
    length = (head[4] << 8) | head[5]
    lenid = length & 0x0FFF
    if len(body) != 12 + lenid + 4:
        raise LengthError(f"LENID {lenid} but {len(body) - 16} INFO characters")
    if length >> 12 != lchksum(lenid):
        raise LengthChecksumError(f"LCHKSUM {length >> 12:X} for LENID {lenid}")
    if lenid % 2:
        raise LengthError(f"odd LENID {lenid}")
    want = int(body[-4:].decode('ascii'), 16)
    if chksum(body[:-4]) != want:
        raise ChecksumError(f"CHKSUM {want:04X}, computed {chksum(body[:-4]):04X}")
    info = bytes.fromhex(body[12:12 + lenid].decode('ascii'))
    return Frame(ver, adr, cid1, cid2, info, raw)


class Framer:
    """Splits ``~…\\r`` frames out of a byte stream. Garbage-tolerant: bytes
    before a '~', a frame cut by a new '~', or a head that never ends are
    dropped (counted in ``dropped_bytes``) and the stream realigns at the next
    '~'. Pure — no port, no clock."""

    def __init__(self, max_frame: int = _MAX_FRAME):
        self._buf = bytearray()
        self.max_frame = max_frame
        self.dropped_bytes = 0

    def feed(self, data: bytes) -> List[bytes]:
        if data:
            self._buf.extend(data)
        out: List[bytes] = []
        b = self._buf
        while b:
            soi = b.find(SOI)
            if soi < 0:
                self.dropped_bytes += len(b)
                b.clear()
                break
            if soi:
                self.dropped_bytes += soi
                del b[:soi]
            eoi = b.find(EOI, 1)
            nxt = b.find(SOI, 1)
            if nxt != -1 and (eoi == -1 or nxt < eoi):
                # a new frame started before this one ended: the head is torn
                self.dropped_bytes += nxt
                del b[:nxt]
                continue
            if eoi == -1:
                if len(b) > self.max_frame:
                    self.dropped_bytes += 1
                    del b[:1]
                    continue
                break
            out.append(bytes(b[:eoi + 1]))
            del b[:eoi + 1]
        return out


# ── semantic decoders ────────────────────────────────────────────────────────

class _Cursor:
    def __init__(self, info: bytes, what: str):
        self.b, self.i, self.what = info, 0, what

    @property
    def left(self) -> int:
        return len(self.b) - self.i

    def u8(self, name: str) -> int:
        if self.left < 1:
            raise DecodeError(f"{self.what}: ends before {name}")
        v = self.b[self.i]
        self.i += 1
        return v

    def u16(self, name: str) -> int:
        if self.left < 2:
            raise DecodeError(f"{self.what}: ends before {name}")
        v = (self.b[self.i] << 8) | self.b[self.i + 1]
        self.i += 2
        return v

    def take(self, n: int, name: str) -> bytes:
        if self.left < n:
            raise DecodeError(f"{self.what}: ends inside {name}")
        v = self.b[self.i:self.i + n]
        self.i += n
        return v


# the user-defined words after the fixed 0x42 part, in protocol order
TELEMETRY_CUSTOM = ('full_capacity', 'soc', 'rated_capacity', 'cycles', 'soh', 'port_voltage')


def decode_telemetry(info: bytes) -> Dict[str, Any]:
    """CID2 0x42 answer: DATAFLAG, pack, N, N×cell mV, M, M×temp (0.1 K),
    current (int16, 0.01 A), pack voltage (0.01 V), remaining (0.01 Ah),
    custom count P, then P words: full capacity, SOC (0.1 %), rated capacity,
    cycles, SOH (0.1 %), port voltage, reserved…. Words past what the frame
    holds are left out (a short custom block is decoded as far as it goes)."""
    c = _Cursor(info, '0x42')
    out: Dict[str, Any] = {'data_flag': c.u8('DATAFLAG'), 'pack': c.u8('pack byte')}
    n = c.u8('cell count')
    out['cells'] = [c.u16(f'cell {i + 1}') for i in range(n)]
    m = c.u8('temperature count')
    out['temps'] = [c.u16(f'temperature {i + 1}') for i in range(m)]
    cur = c.u16('current')
    out['current'] = cur - 0x10000 if cur & 0x8000 else cur
    out['voltage'] = c.u16('pack voltage')
    out['remaining_capacity'] = c.u16('remaining capacity')
    p = c.u8('custom count') if c.left else 0
    out['custom_count'] = p
    words = [c.u16('custom word') for _ in range(min(p, c.left // 2))]
    out['custom_short'] = len(words) < p
    for name, w in zip(TELEMETRY_CUSTOM, words):
        out[name] = w
    out['reserved'] = words[len(TELEMETRY_CUSTOM):]
    out['trailing'] = c.left
    return out


# the 0x44 user-defined bytes, in protocol order (V2.0 table; bench-checked later)
ALARM_CUSTOM = ('alarm_event_1', 'alarm_event_2', 'alarm_event_3', 'alarm_event_4',
                'alarm_event_5', 'alarm_event_6', 'switch_state', 'balance_state_1',
                'balance_state_2', 'system_state', 'disconnection_state_1',
                'disconnection_state_2', 'alarm_event_7', 'alarm_event_8')


def decode_alarms(info: bytes) -> Dict[str, Any]:
    """CID2 0x44 answer: DATAFLAG, pack, N, N×cell alarm, M, M×temp alarm,
    then the current/voltage alarm bytes, custom count P and P bytes (alarm
    events 1–6, switch state, balance 1–2, system state, disconnection 1–2,
    alarm events 7–8, reserved).

    The V2.0 document lists TWO bytes between the temperatures and P (charge/
    discharge current alarm, pack voltage alarm); some readings of it list
    three (charge current, pack voltage, discharge current). The frame says
    which: the layout is the one whose P matches the bytes left. When neither
    does, two is assumed and ``layout_uncertain`` is set."""
    c = _Cursor(info, '0x44')
    out: Dict[str, Any] = {'data_flag': c.u8('DATAFLAG'), 'pack': c.u8('pack byte')}
    n = c.u8('cell count')
    out['cell_alarms'] = list(c.take(n, 'cell alarms'))
    m = c.u8('temperature count')
    out['temp_alarms'] = list(c.take(m, 'temperature alarms'))
    rest = info[c.i:]
    k_bytes, uncertain = 2, True
    for k in (2, 3):
        if len(rest) > k and rest[k] == len(rest) - k - 1:
            k_bytes, uncertain = k, False
            break
    pre = c.take(min(k_bytes, c.left), 'current/voltage alarms')
    out['current_alarm'] = pre[0] if len(pre) > 0 else None
    out['voltage_alarm'] = pre[1] if len(pre) > 1 else None
    out['discharge_current_alarm'] = pre[2] if len(pre) > 2 else None
    p = c.u8('custom count') if c.left else 0
    out['custom_count'] = p
    out['custom'] = list(c.take(min(p, c.left), 'custom bytes'))
    out['layout_uncertain'] = uncertain
    return out


def decode_manufacturer(info: bytes) -> Dict[str, Any]:
    """CID2 0x51 answer (telecom layout): device name (10 ASCII bytes),
    software version (2 bytes), manufacturer name (20 ASCII bytes). A shorter
    or longer INFO is split the same way as far as it goes."""
    return {'name': info[:10], 'version': info[10:12], 'manufacturer': info[12:32],
            'length': len(info)}


# The 0x5A summary, in word order. Nothing published documents it (the V2.0
# document stops at 0x51; no community decoder handles it): the layout is
# read off the bench capture by the units 0x42 uses, and checked against the
# CAN link of the same bank.
SUMMARY_FIELDS = ('head', 'cell_max', 'cell_min', 'temp_max', 'temp_min', 'current', 'voltage',
                  'remaining_capacity', 'full_capacity', 'soc', 'port_voltage')
# The one layout seen: 32 bytes, head 0x0001 (the bench, 2026-10-26). Any
# other INFO is kept raw only — a firmware with another layout must not
# publish plausible-looking SOC or voltages read by position (SV2-8).
SUMMARY_LENGTH = 32
SUMMARY_HEAD = 0x0001
# 0.1 K words a pack can mean: −40 … +100 °C; 0 and 0xFFFF are "none"
_KELVIN10_RANGE = (2731 - 400, 2731 + 1000)


def _summary_problem(d: Dict[str, Any]) -> str:
    """Why the named words of a 0x5A answer cannot be trusted, or ''."""
    if d['head'] != SUMMARY_HEAD:
        return f"head 0x{d['head']:04X}, not 0x{SUMMARY_HEAD:04X}"
    if NA not in (d['cell_max'], d['cell_min']) and d['cell_min'] > d['cell_max']:
        return f"lowest cell {d['cell_min']} mV above the highest {d['cell_max']} mV"
    if d['soc'] != NA and d['soc'] > 1000:
        return f"SOC {d['soc'] / 10:g} %"
    if NA not in (d['remaining_capacity'], d['full_capacity']) \
            and d['remaining_capacity'] > d['full_capacity']:
        return "remaining capacity above full capacity"
    for k in ('temp_max', 'temp_min'):
        v = d[k]
        if v not in (0, NA) and not _KELVIN10_RANGE[0] <= v <= _KELVIN10_RANGE[1]:
            return f"{k} {(v - 2731) / 10:g} °C"
    return ''


def decode_summary(info: bytes) -> Dict[str, Any]:
    """CID2 0x5A answer (32 bytes on the bench): 16 big-endian words — head,
    highest / lowest cell (mV), highest / lowest temperature (0.1 K), current
    (int16, 0.01 A), pack voltage (0.01 V), remaining and full capacity
    (0.01 Ah), SOC (0.1 %), port voltage (0.01 V, assumed), then words not
    understood yet, kept raw. Never raises: an INFO of another length or head,
    or one whose values cannot be (``_summary_problem``), comes back with
    ``problem`` set — its words are kept raw, nothing is named from it."""
    words = [(info[i] << 8) | info[i + 1] for i in range(0, len(info) - 1, 2)]
    out: Dict[str, Any] = {'words': words, 'length': len(info), 'trailing': len(info) % 2}
    if len(info) != SUMMARY_LENGTH:
        out['problem'] = f"{len(info)} bytes, not the {SUMMARY_LENGTH}-byte layout"
        return out
    named = dict(zip(SUMMARY_FIELDS, words))
    cur = named['current']
    named['current'] = cur - 0x10000 if cur & 0x8000 else cur
    out.update(named)
    out['problem'] = _summary_problem(out)
    return out


# ── the virtual register map (input table) — the contract templates read ──

MAP_CELLS = 0x0000            # 32 words, mV
MAP_CELL_COUNT = 0x0020
MAP_TEMP_COUNT = 0x0021
MAP_TEMPS = 0x0022            # 8 words, int16 0.1 °C
MAP_TELEMETRY_CUSTOM_COUNT = 0x002A
MAP_DATA_FLAG = 0x002B
MAP_CURRENT = 0x0030
MAP_VOLTAGE = 0x0031
MAP_REMAINING = 0x0032
MAP_FULL = 0x0033
MAP_SOC = 0x0034
MAP_RATED = 0x0035
MAP_CYCLES = 0x0036
MAP_SOH = 0x0037
MAP_PORT_VOLTAGE = 0x0038
MAP_RESERVED = 0x0039         # up to 7 words
MAP_CELL_MAX = 0x0040
MAP_CELL_MIN = 0x0041
MAP_CELL_AVG = 0x0042
MAP_CELL_DELTA = 0x0043
MAP_CELL_TEMP_MAX = 0x0044
MAP_CELL_TEMP_MIN = 0x0045
MAP_CELL_TEMP_AVG = 0x0046
MAP_AMBIENT_TEMP = 0x0047
MAP_POWER_TEMP = 0x0048
MAP_CELL_MAX_INDEX = 0x0049
MAP_CELL_MIN_INDEX = 0x004A

MAP_CELL_ALARMS = 0x0100      # 32 bytes, one per word
MAP_ALARM_CELL_COUNT = 0x0120
MAP_ALARM_TEMP_COUNT = 0x0121
MAP_TEMP_ALARMS = 0x0122      # 8 bytes
MAP_CURRENT_ALARM = 0x0130
MAP_VOLTAGE_ALARM = 0x0131
MAP_DISCHARGE_CURRENT_ALARM = 0x0132
MAP_ALARM_CUSTOM_COUNT = 0x0133
MAP_ALARM_CUSTOM = 0x0134     # up to 32 bytes: alarm event 1 at 0x0134 …
MAP_ALARM_BITS = 0x0160       # custom bytes 1..16 bit by bit: 0x0160 + 8*i + bit
MAP_BALANCE_BITS = 0x01E0     # balance 1 | balance 2 << 8 (cells 1–16)
MAP_DISCONNECT_BITS = 0x01E1
MAP_CELL_UNDERVOLT_BITS = 0x01E2   # cells whose alarm byte is 0x01 (below the lower limit)
MAP_CELL_OVERVOLT_BITS = 0x01E3    # cells whose alarm byte is 0x02 (above the upper limit)
MAP_TEMP_ALARM_BITS = 0x01E4       # temperatures with any alarm byte ≠ 0
MAP_ALARM_LAYOUT_UNCERTAIN = 0x01E5

MAP_DEVICE_NAME = 0x0200      # 5 words, 10 ASCII chars
MAP_SOFTWARE_VERSION = 0x0205  # major << 8 | minor
MAP_MANUFACTURER = 0x0206     # 10 words, 20 ASCII chars
MAP_MANUFACTURER_LENGTH = 0x0210

MAP_SUM_HEAD = 0x0400         # 0x5A summary (listen only)
MAP_SUM_CELL_MAX = 0x0401     # mV
MAP_SUM_CELL_MIN = 0x0402     # mV
MAP_SUM_CELL_DELTA = 0x0403   # mV, derived from the two above
MAP_SUM_TEMP_MAX = 0x0404     # int16 0.1 °C (Kelvin offset removed)
MAP_SUM_TEMP_MIN = 0x0405
MAP_SUM_CURRENT = 0x0406      # int16 0.01 A
MAP_SUM_VOLTAGE = 0x0407      # 0.01 V
MAP_SUM_REMAINING = 0x0408    # 0.01 Ah
MAP_SUM_FULL = 0x0409         # 0.01 Ah
MAP_SUM_SOC = 0x040A          # 0.1 %
MAP_SUM_PORT_VOLTAGE = 0x040B  # 0.01 V (assumed)
MAP_SUM_RAW = 0x0410          # every INFO word as received, up to 32 (0x0410–0x042F)
MAP_SUM_LENGTH = 0x043F       # INFO length in bytes

MAP_DIAG = 0x0F00
MAP_DIAG_RESPONSE_MS = 0x0F00
MAP_DIAG_CHECKSUM_ERRORS = 0x0F01
MAP_DIAG_TIMEOUTS = 0x0F02
MAP_DIAG_RTN_ERRORS = 0x0F03
MAP_DIAG_LAST_RTN = 0x0F04
MAP_DIAG_GOOD = 0x0F05
MAP_DIAG_DECODE_ERRORS = 0x0F06

AREA_DIAG = 'diag'
# which command fills which block of the map (the high byte of the address)
_AREA_BY_PAGE = {0x00: CMD_TELEMETRY, 0x01: CMD_ALARMS, 0x02: CMD_MANUFACTURER,
                 0x03: CMD_SYSTEM_PARAMS, 0x04: CMD_PARALLEL_SUMMARY, 0x0F: AREA_DIAG}

NA = 0xFFFF                   # "not available" as the packs write it
MODES = ('poll', 'listen')


def area_of(address: int):
    """The command whose answer fills this map address (or 'diag'), else None."""
    return _AREA_BY_PAGE.get((int(address) >> 8) & 0xFF)


def _s16(v: int) -> int:
    return int(v) & 0xFFFF


def _put(out: Dict[int, int], addr: int, value: Optional[int]) -> None:
    if value is not None and value != NA:
        out[addr] = _s16(value)


def telemetry_words(d: Dict[str, Any]) -> Dict[int, int]:
    out: Dict[int, int] = {}
    cells = d['cells']
    for i, mv in enumerate(cells[:32]):
        _put(out, MAP_CELLS + i, mv)
    out[MAP_CELL_COUNT] = len(cells)
    temps = d['temps']
    out[MAP_TEMP_COUNT] = len(temps)
    celsius = [None if t == NA else t - 2731 for t in temps]
    for i, c in enumerate(celsius[:8]):
        _put(out, MAP_TEMPS + i, c)
    out[MAP_TELEMETRY_CUSTOM_COUNT] = d['custom_count']
    out[MAP_DATA_FLAG] = d['data_flag']
    out[MAP_CURRENT] = _s16(d['current'])           # int16 — 0xFFFF is −0.01 A, a value
    _put(out, MAP_VOLTAGE, d['voltage'])
    _put(out, MAP_REMAINING, d['remaining_capacity'])
    for addr, key in ((MAP_FULL, 'full_capacity'), (MAP_SOC, 'soc'), (MAP_RATED, 'rated_capacity'),
                      (MAP_CYCLES, 'cycles'), (MAP_SOH, 'soh'), (MAP_PORT_VOLTAGE, 'port_voltage')):
        _put(out, addr, d.get(key))
    for i, w in enumerate(d.get('reserved', [])[:7]):
        out[MAP_RESERVED + i] = _s16(w)
    # derived from this same frame (consistent by construction)
    present = [(i + 1, mv) for i, mv in enumerate(cells) if mv != NA]
    if present:
        hi = max(present, key=lambda x: x[1])
        lo = min(present, key=lambda x: x[1])
        out[MAP_CELL_MAX], out[MAP_CELL_MAX_INDEX] = hi[1], hi[0]
        out[MAP_CELL_MIN], out[MAP_CELL_MIN_INDEX] = lo[1], lo[0]
        out[MAP_CELL_AVG] = int(round(sum(mv for _i, mv in present) / len(present)))
        out[MAP_CELL_DELTA] = hi[1] - lo[1]
    # ASSUMED (V2.0 16S layout: 4 cell sensors, ambient, power/MOSFET): with
    # 3+ sensors the last two are ambient and power, the rest are cells
    if len(celsius) >= 3:
        cell_t, amb, pwr = celsius[:-2], celsius[-2], celsius[-1]
    else:
        cell_t, amb, pwr = celsius, None, None
    cell_t = [t for t in cell_t if t is not None]
    if cell_t:
        out[MAP_CELL_TEMP_MAX] = _s16(max(cell_t))
        out[MAP_CELL_TEMP_MIN] = _s16(min(cell_t))
        out[MAP_CELL_TEMP_AVG] = _s16(int(round(sum(cell_t) / len(cell_t))))
    _put(out, MAP_AMBIENT_TEMP, amb)
    _put(out, MAP_POWER_TEMP, pwr)
    return out


def _bits(values: List[int], pred: Callable[[int], bool], width: int = 16) -> int:
    return sum(1 << i for i, v in enumerate(values[:width]) if pred(v))


def alarm_words(d: Dict[str, Any]) -> Dict[int, int]:
    out: Dict[int, int] = {}
    ca, ta = d['cell_alarms'], d['temp_alarms']
    for i, v in enumerate(ca[:32]):
        out[MAP_CELL_ALARMS + i] = v
    out[MAP_ALARM_CELL_COUNT] = len(ca)
    out[MAP_ALARM_TEMP_COUNT] = len(ta)
    for i, v in enumerate(ta[:8]):
        out[MAP_TEMP_ALARMS + i] = v
    for addr, key in ((MAP_CURRENT_ALARM, 'current_alarm'), (MAP_VOLTAGE_ALARM, 'voltage_alarm'),
                      (MAP_DISCHARGE_CURRENT_ALARM, 'discharge_current_alarm')):
        if d.get(key) is not None:
            out[addr] = d[key]
    out[MAP_ALARM_CUSTOM_COUNT] = d['custom_count']
    custom = d['custom']
    for i, v in enumerate(custom[:32]):
        out[MAP_ALARM_CUSTOM + i] = v
    for i, v in enumerate(custom[:16]):
        for bit in range(8):
            out[MAP_ALARM_BITS + 8 * i + bit] = (v >> bit) & 1
    named = dict(zip(ALARM_CUSTOM, custom))
    if 'balance_state_1' in named:
        out[MAP_BALANCE_BITS] = named['balance_state_1'] | (named.get('balance_state_2', 0) << 8)
    if 'disconnection_state_1' in named:
        out[MAP_DISCONNECT_BITS] = named['disconnection_state_1'] | (named.get('disconnection_state_2', 0) << 8)
    out[MAP_CELL_UNDERVOLT_BITS] = _bits(ca, lambda v: v == 0x01)
    out[MAP_CELL_OVERVOLT_BITS] = _bits(ca, lambda v: v == 0x02)
    out[MAP_TEMP_ALARM_BITS] = _bits(ta, lambda v: v != 0)
    out[MAP_ALARM_LAYOUT_UNCERTAIN] = 1 if d.get('layout_uncertain') else 0
    return out


def _str_words(b: bytes, n_words: int) -> List[int]:
    b = bytes(b[:2 * n_words]).ljust(2 * n_words, b'\x00')
    return [(b[2 * i] << 8) | b[2 * i + 1] for i in range(n_words)]


def manufacturer_words(d: Dict[str, Any]) -> Dict[int, int]:
    out: Dict[int, int] = {}
    if d['name']:
        for i, w in enumerate(_str_words(d['name'], 5)):
            out[MAP_DEVICE_NAME + i] = w
    v = d['version']
    if len(v) == 2:
        out[MAP_SOFTWARE_VERSION] = (v[0] << 8) | v[1]
    if d['manufacturer']:
        for i, w in enumerate(_str_words(d['manufacturer'], 10)):
            out[MAP_MANUFACTURER + i] = w
    out[MAP_MANUFACTURER_LENGTH] = d['length']
    return out


def _kelvin10(v: Optional[int]) -> Optional[int]:
    """0.1 K → 0.1 °C; 0, 0xFFFF and anything outside −40 … +100 °C are none
    (a zero word would otherwise read −273.1 °C, SV2-11)."""
    if v is None or not _KELVIN10_RANGE[0] <= v <= _KELVIN10_RANGE[1]:
        return None
    return v - 2731


def summary_words(d: Dict[str, Any]) -> Dict[int, int]:
    """Raw words and the length always; the named words only from the
    bench's layout with values that can be (``decode_summary``)."""
    out: Dict[int, int] = {}
    for i, w in enumerate(d['words'][:32]):
        out[MAP_SUM_RAW + i] = w
    out[MAP_SUM_LENGTH] = d['length']
    if d.get('problem'):
        return out
    out[MAP_SUM_HEAD] = d['head']
    _put(out, MAP_SUM_CELL_MAX, d['cell_max'])
    _put(out, MAP_SUM_CELL_MIN, d['cell_min'])
    if MAP_SUM_CELL_MAX in out and MAP_SUM_CELL_MIN in out and d['cell_max'] >= d['cell_min']:
        out[MAP_SUM_CELL_DELTA] = d['cell_max'] - d['cell_min']
    _put(out, MAP_SUM_TEMP_MAX, _kelvin10(d['temp_max']))
    _put(out, MAP_SUM_TEMP_MIN, _kelvin10(d['temp_min']))
    out[MAP_SUM_CURRENT] = _s16(d['current'])       # int16 — 0xFFFF is −0.01 A, a value
    for addr, key in ((MAP_SUM_VOLTAGE, 'voltage'), (MAP_SUM_REMAINING, 'remaining_capacity'),
                      (MAP_SUM_FULL, 'full_capacity'), (MAP_SUM_SOC, 'soc'),
                      (MAP_SUM_PORT_VOLTAGE, 'port_voltage')):
        _put(out, addr, d.get(key))
    return out


_DECODERS = {CMD_TELEMETRY: (decode_telemetry, telemetry_words),
             CMD_ALARMS: (decode_alarms, alarm_words),
             CMD_MANUFACTURER: (decode_manufacturer, manufacturer_words),
             # heard only (listen): never in a poll plan, never encoded
             CMD_PARALLEL_SUMMARY: (decode_summary, summary_words)}


def words_for(cid2: int, info: bytes) -> Dict[int, int]:
    """The map words one answer fills. Raises DecodeError (or KeyError for a
    command with no decoder, 0x47)."""
    dec, to_words = _DECODERS[cid2]
    return to_words(dec(info))


# ── transports ───────────────────────────────────────────────────────────────

class _SerialPort:
    def __init__(self, path: str, line: Tuple[int, str, int, int], listen: bool = False):
        import serial
        baud, parity, stop, bits = line
        if not listen:
            self._s = serial.Serial(port=path, baudrate=baud, parity=parity, stopbits=stop,
                                    bytesize=bits, timeout=0.05)
            return
        # listening: RTS and DTR are set low BEFORE the port opens, so an
        # adapter whose driver-enable follows RTS never drives the bus (SV2-13)
        s = serial.Serial(baudrate=baud, parity=parity, stopbits=stop, bytesize=bits, timeout=0.05)
        s.rts = False
        s.dtr = False
        s.port = path
        s.open()
        self._s = s

    def read(self, n: int) -> bytes:
        return self._s.read(n)

    def write(self, data: bytes) -> None:
        self._s.write(data)
        self._s.flush()

    def reset_input_buffer(self) -> None:
        self._s.reset_input_buffer()

    def close(self) -> None:
        self._s.close()


class _TcpPort:
    """A bridge's raw byte stream, read like a serial port (b'' = quiet)."""

    def __init__(self, host: str, port: int):
        import socket
        self._sock = socket.create_connection((host, port), timeout=5)
        self._sock.settimeout(0.05)

    def read(self, n: int) -> bytes:
        import socket
        try:
            data = self._sock.recv(n)
        except socket.timeout:
            return b''
        if not data:
            raise ConnectionError("the bridge closed the connection")
        return data

    def write(self, data: bytes) -> None:
        self._sock.sendall(data)

    def reset_input_buffer(self) -> None:
        import socket
        self._sock.settimeout(0)
        try:
            while self._sock.recv(4096):
                pass
        except (BlockingIOError, socket.timeout, OSError):
            pass
        finally:
            self._sock.settimeout(0.05)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def line_of(conn_cfg) -> Tuple[int, str, int, int]:
    return (int(getattr(conn_cfg, 'baudrate', 9600) or 9600),
            str(getattr(conn_cfg, 'parity', 'N') or 'N').upper(),
            int(getattr(conn_cfg, 'stopbits', 1) or 1),
            int(getattr(conn_cfg, 'bytesize', 8) or 8))


def _is_net(conn_cfg) -> bool:
    return str(getattr(conn_cfg, 'protocol', '') or '').lower().endswith('-tcp') \
        or not str(getattr(conn_cfg, 'serial_port', '') or '')


def bus_key(conn_cfg) -> str:
    """One bus, whatever name reaches it (the bus claims' identity): a by-id
    link or an alias node of the line, a host in another case."""
    if _is_net(conn_cfg):
        host = str(getattr(conn_cfg, 'host', '') or '').strip().lower()
        return f"tcp:{host}:{int(getattr(conn_cfg, 'port', 0) or 0)}"
    from .bus_claims import canonical_serial
    sp = str(getattr(conn_cfg, 'serial_port', '') or '')
    return canonical_serial(sp) or sp


def bus_name(conn_cfg) -> str:
    if _is_net(conn_cfg):
        return f"{getattr(conn_cfg, 'host', '')}:{int(getattr(conn_cfg, 'port', 0) or 0)}"
    return str(getattr(conn_cfg, 'serial_port', ''))


def open_port(conn_cfg):
    """The byte stream of one bus: a serial port here or a bridge's socket.
    Tests replace this function; nothing else opens a port."""
    if _is_net(conn_cfg):
        from . import bridge_guard
        # through our serial bridge: only the bus this device was bound to
        if not bridge_guard.allow(conn_cfg):
            st = bridge_guard.state(str(getattr(conn_cfg, 'host', '')),
                                    int(getattr(conn_cfg, 'port', 0) or 0)) or {}
            raise ConnectionError(st.get("detail") or "the bridge serves another adapter here")
        return _TcpPort(str(getattr(conn_cfg, 'host', '')), int(getattr(conn_cfg, 'port', 0) or 0))
    return _SerialPort(str(conn_cfg.serial_port), line_of(conn_cfg),
                       listen=str(getattr(conn_cfg, 'mode', '') or '').lower() == 'listen')


class ResponseTimeout(TimeoutError):
    pass


class RtnError(Exception):
    def __init__(self, code: int):
        super().__init__(f"RTN 0x{code:02X} ({RTN_TEXT.get(code, 'unknown')})")
        self.code = code


class _Bus:
    """One poll transport: a port (or a bridge socket) and its lock, shared by
    every pack on it. Opens on first use and again after a failure."""

    def __init__(self, conn_cfg):
        self.key = bus_key(conn_cfg)
        self.name = bus_name(conn_cfg)
        self.conn = conn_cfg
        self.line = line_of(conn_cfg)
        self.lock = threading.Lock()
        self._port = None
        self.open_error = ''
        self.users = 0
        self.dropped_bytes = 0
        self.recent: deque = deque(maxlen=200)

    @property
    def is_open(self) -> bool:
        return self._port is not None

    def ensure_open(self) -> bool:
        if self._port is not None:
            return True
        try:
            self._port = open_port(self.conn)
        except Exception as e:  # noqa: BLE001 — missing port / refused bridge: say why, retry next cycle
            if str(e) != self.open_error:
                logger.warning("seplos_v2 %s: cannot open — %s", self.name, e)
            self.open_error = str(e)
            self._port = None
            return False
        self.open_error = ''
        logger.info("seplos_v2 %s: open (poll, read commands only)", self.name)
        return True

    def close(self) -> None:
        if self._port is not None:
            try:
                self._port.close()
            except Exception:  # noqa: BLE001
                pass
        self._port = None

    def exchange(self, adr: int, cid2: int, timeout: float) -> Tuple[Frame, float]:
        """Send one read request and wait for the pack's answer. Returns the
        frame and the response time in ms. Raises ResponseTimeout,
        FrameError (checksum/length/format), RtnError or ConnectionError."""
        req = request(adr, cid2)                      # the allow-list is checked HERE
        with self.lock:
            if not self.ensure_open():
                raise ConnectionError(self.open_error or 'port not open')
            port = self._port
            framer = Framer()
            try:
                port.reset_input_buffer()
                t0 = time.monotonic()
                port.write(req)
                self._note('tx', adr, cid2)
                deadline = t0 + max(0.05, float(timeout))
                while True:
                    data = port.read(512)
                    for raw in framer.feed(data):
                        if raw == req:
                            continue                  # an adapter's echo of our own request
                        try:
                            fr = parse(raw)
                        except FrameError as e:
                            self._note('bad', adr, cid2, e.kind)
                            raise
                        if fr.is_request or fr.adr != adr:
                            continue                  # somebody else's traffic
                        ms = (time.monotonic() - t0) * 1000.0
                        self._note('rx', fr.adr, fr.cid2, f"{len(fr.info)} B")
                        if fr.cid2 != 0:
                            raise RtnError(fr.cid2)
                        return fr, ms
                    if time.monotonic() >= deadline:
                        self._note('timeout', adr, cid2)
                        raise ResponseTimeout(f"no answer from pack {adr} within {timeout:g} s")
            except (FrameError, RtnError, ResponseTimeout):
                raise
            except Exception as e:  # noqa: BLE001 — the port went away: reopen next time
                self.open_error = str(e)
                logger.warning("seplos_v2 %s: I/O failed — %s (reopening)", self.name, e)
                self.close()
                raise ConnectionError(str(e)) from e
            finally:
                self.dropped_bytes += framer.dropped_bytes

    def _note(self, kind: str, adr: int, cid2: int, detail: str = '') -> None:
        self.recent.append({'t': time.time(), 'unit': adr, 'fc': cid2, 'kind': kind, 'detail': detail})


_BUSES: Dict[str, _Bus] = {}
_LISTENERS: Dict[str, '_Listener'] = {}
_REG_LOCK = threading.Lock()


def bus_for(conn_cfg) -> _Bus:
    key = bus_key(conn_cfg)
    with _REG_LOCK:
        b = _BUSES.get(key)
        if b is None or (b.users == 0 and b.line != line_of(conn_cfg)):
            b = _BUSES[key] = _Bus(conn_cfg)
        return b


_RESPONSE_WINDOW_S = 2.0
ALL_UNITS = -1          # observe(ALL_UNITS, w): w._heard(adr, cid2, kind, detail, now) for every frame


class _Listener:
    """One port listened to, one reader thread, the packs registered by ADR.
    It never writes: the port is opened, read, and closed — nothing else."""

    REOPEN_S = 3.0

    def __init__(self, conn_cfg):
        self.key = bus_key(conn_cfg)
        self.name = bus_name(conn_cfg)
        self.conn = conn_cfg
        self.line = line_of(conn_cfg)
        self._clients: Dict[int, 'SeplosV2Client'] = {}
        # receive-only listeners beside the packs' devices (the read preview):
        # they never take a pack's ADR, and keep the port open while they listen
        self._observers: Dict[int, List[Any]] = {}
        self._pending: Dict[int, Tuple[int, float]] = {}
        self._port = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.framer = Framer()
        self.frames = 0
        self.bad_frames = 0
        self.orphans = 0
        self.commands_seen = 0             # requests other than reads (a master's set/control)
        self.summary_unpaired = 0          # 0x5A answers whose question was not heard (SV2-10)
        self.last_frame_mono: Optional[float] = None
        self.open_error = ''
        self.recent: deque = deque(maxlen=200)

    def register(self, adr: int, client: 'SeplosV2Client') -> bool:
        """Attach a pack; False when the port is not open yet, or when another
        device already listens for this ADR (``client.refusal`` says which —
        two devices on one pack would split its answers, SV2-14)."""
        with self._lock:
            other = self._clients.get(adr)
            if other is not None and other is not client:
                client.refusal = (f"pack ADR {adr} on {self.name} is already heard by device "
                                  f"'{other.device_id}' — one device per pack")
                logger.error("seplos_v2 %s: %s", client.device_id, client.refusal)
                return False
            self._clients[adr] = client
            self._start_locked()
            return self._port is not None

    def _start_locked(self) -> None:
        if self._thread is None:
            self._open()
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name=f"seplos-v2-listen:{self.name}")
            self._thread.start()

    def _stop_if_idle_locked(self) -> None:
        if not self._clients and not self._observers:
            self._stop.set()
            self._close()
            self._thread = None

    def unregister(self, adr: int, client: Optional['SeplosV2Client'] = None) -> None:
        with self._lock:
            if client is not None and self._clients.get(adr) is not client:
                return                          # not ours: the pack's own device keeps listening
            self._clients.pop(adr, None)
            self._stop_if_idle_locked()

    def observe(self, adr: int, observer: Any) -> bool:
        """Hear pack ``adr``'s answers too (``observer._ingest_answer``),
        beside the device that reads that pack — the read preview (ADR-0031
        §5). The port is opened when nobody had it open and stays open until
        the last device or observer leaves. Nothing is written."""
        with self._lock:
            self._observers.setdefault(int(adr), []).append(observer)
            self._start_locked()
            return self._port is not None

    def unobserve(self, adr: int, observer: Any) -> None:
        with self._lock:
            lst = self._observers.get(int(adr)) or []
            if observer in lst:
                lst.remove(observer)
            if not lst:
                self._observers.pop(int(adr), None)
            self._stop_if_idle_locked()

    def _open(self) -> bool:
        # a new stream: half a frame and a question from before the gap mean nothing now
        dropped = self.framer.dropped_bytes
        self.framer = Framer()
        self.framer.dropped_bytes = dropped
        self._pending = {}
        try:
            self._port = open_port(self.conn)
        except Exception as e:  # noqa: BLE001 — missing now, plugged in later: try again
            if str(e) != self.open_error:
                logger.warning("seplos_v2 %s: cannot open — %s (trying again every %g s)",
                               self.name, e, self.REOPEN_S)
            self.open_error = str(e)
            self._port = None
            return False
        self.open_error = ''
        logger.info("seplos_v2 %s: listening (read-only)", self.name)
        return True

    def _close(self) -> None:
        if self._port is not None:
            try:
                self._port.close()
            except Exception:  # noqa: BLE001
                pass
        self._port = None

    def _run(self) -> None:
        stop = self._stop
        while not stop.is_set():
            port = self._port
            if port is None:
                if stop.wait(self.REOPEN_S):
                    break
                with self._lock:
                    if stop.is_set():
                        break
                    self._open()
                continue
            try:
                data = port.read(512)
            except Exception as e:  # noqa: BLE001 — adapter pulled / bridge dropped: reopen
                if stop.is_set():
                    break
                self.open_error = str(e)
                logger.warning("seplos_v2 %s: read failed — %s (opening it again)", self.name, e)
                with self._lock:
                    if self._port is port:
                        self._close()
                continue
            if not data:
                continue
            now = time.monotonic()
            for raw in self.framer.feed(data):
                self.on_frame(raw, now)

    def on_frame(self, raw: bytes, now: float) -> None:
        try:
            fr = parse(raw)
        except FrameError as e:
            self.bad_frames += 1
            self._note('bad', None, None, e.kind)
            # the frame's ADR is unknowable once it fails; charge the pack
            # whose answer was awaited (one request in flight on a bus)
            if len(self._pending) == 1:
                adr = next(iter(self._pending))
                cl = self._clients.get(adr)
                if cl is not None:
                    cl._count_error(e.kind)
            return
        self.frames += 1
        self.last_frame_mono = now
        if fr.cid2 == CMD_PARALLEL_SUMMARY:
            self._on_summary(fr, now)
            return
        if fr.is_request:
            if fr.cid2 in READ_COMMANDS:
                self._pending = {fr.adr: (fr.cid2, now)}
                self._note('request', fr.adr, fr.cid2)
            else:
                self.commands_seen += 1
                self._pending = {}
                self._note('command', fr.adr, fr.cid2)
            return
        self._answer(fr, now, fr.cid2)

    def _on_summary(self, fr: Frame, now: float) -> None:
        """0x5A both ways: the master's question carries no INFO; the answer
        echoes 0x5A in CID2 (no RTN) and carries the summary. An answer is
        taken only right after the question to the same ADR; one whose
        question was lost (a collision, a torn frame) is an orphan."""
        if not fr.info:
            self._pending = {fr.adr: (CMD_PARALLEL_SUMMARY, now)}
            self._note('request', fr.adr, fr.cid2)
            return
        pend = self._pending.get(fr.adr)
        if pend is None or pend[0] != CMD_PARALLEL_SUMMARY:
            self.orphans += 1
            self.summary_unpaired += 1
            self._note('orphan', fr.adr, fr.cid2)
            return
        self._answer(fr, now, 0)

    def _answer(self, fr: Frame, now: float, rtn: int) -> None:
        pend = self._pending.pop(fr.adr, None)
        if pend is None or now - pend[1] > _RESPONSE_WINDOW_S:
            self.orphans += 1
            self._note('orphan', fr.adr, fr.cid2)
            return
        cid2, t_req = pend
        self._note('response', fr.adr, cid2, f"RTN {rtn:02X}, {len(fr.info)} B")
        cl = self._clients.get(fr.adr)
        for c in ([cl] if cl is not None else []) + list(self._observers.get(fr.adr) or ()):
            try:
                c._ingest_answer(cid2, fr, now, (now - t_req) * 1000.0, rtn=rtn)
            except Exception as e:  # noqa: BLE001 — a decode bug must not kill the reader
                logger.error("seplos_v2 %s pack %d: ingest failed — %s", self.name, fr.adr, e)

    def _note(self, kind: str, adr, cid2, detail: str = '') -> None:
        self.recent.append({'t': time.time(), 'unit': adr, 'fc': cid2, 'kind': kind, 'detail': detail})
        # every frame to whoever hears the whole bus (Listen for addresses,
        # bus_scan.py): observe(ALL_UNITS, watcher) — nothing is ever written
        for w in list(self._observers.get(ALL_UNITS) or ()):
            try:
                w._heard(adr, cid2, kind, detail, time.monotonic())
            except Exception as e:  # noqa: BLE001 — a watcher's bug must not kill the reader
                logger.error("seplos_v2 %s: a bus watcher failed — %s", self.name, e)


def listener_for(conn_cfg) -> _Listener:
    key = bus_key(conn_cfg)
    with _REG_LOCK:
        r = _LISTENERS.get(key)
        if r is None or (not r._clients and not r._observers and r.line != line_of(conn_cfg)):
            r = _LISTENERS[key] = _Listener(conn_cfg)
        return r


# ── the device-facing client ─────────────────────────────────────────────────

@dataclass
class _UnitStats:
    good: int = 0
    timeouts: int = 0
    checksum_errors: int = 0
    frame_errors: int = 0
    rtn_errors: int = 0
    decode_errors: int = 0
    io_errors: int = 0
    loop_errors: int = 0
    last_rtn: Optional[int] = None
    last_response_ms: Optional[float] = None
    by_command: Dict[str, int] = field(default_factory=dict)


def _table_of(r) -> str:
    from .config import normalize_register_type
    return normalize_register_type(getattr(r, 'register_type', '') or 'holding')


def _key(r) -> int:
    from .config import store_key
    return store_key(r.address, getattr(r, 'register_type', '') or 'holding')


class SeplosV2Client:
    """One Seplos V2 pack (ADR = ``unit_id``), with the interface every
    driver has (connect / start_polling / disconnect / connected /
    update_registers / reload_registers / get_stats / data_health /
    recent_frames / publish_callback). It has no ``write_value``: nothing can
    be written through it (MultiSourceClient refuses on its behalf)."""

    def __init__(self, conn_cfg, registers: list, poll_groups: dict = None,
                 byte_order: str = 'big', device_id: str = ''):
        self.conn = conn_cfg
        self.device_id = device_id
        # a configuration that did not pass the API (YAML, an import, a mirror)
        # FAILS CLOSED: a mode or an address it cannot trust never transmits
        self.config_error = ''
        mode = str(getattr(conn_cfg, 'mode', '') or 'poll').lower()
        self.mode = mode
        if mode not in MODES:
            self.config_error = f"connection.mode {mode!r} is not poll or listen — not started"
        try:
            self.unit_id = int(getattr(conn_cfg, 'unit_id', 0) or 0)
        except (TypeError, ValueError):
            self.unit_id = -1
        if not 0 <= self.unit_id <= 0xFF and not self.config_error:
            self.config_error = (f"connection.unit_id {getattr(conn_cfg, 'unit_id', None)!r} is not a pack "
                                 f"address 0..255 — not started")
        if self.config_error:
            logger.error("seplos_v2 %s: %s", device_id, self.config_error)
        # a map that is only heard (0x5A rows and diagnostics) can never be
        # polled: it listens whatever the configuration said (SV2-9)
        areas = {area_of(r.address) for r in (registers or [])}
        if (mode == 'poll' and not self.config_error and CMD_PARALLEL_SUMMARY in areas
                and areas <= {CMD_PARALLEL_SUMMARY, AREA_DIAG}):
            logger.warning("seplos_v2 %s: every row is the master's 0x5A summary, which is only heard — "
                           "listening, not polling", device_id)
            import copy
            conn_cfg = copy.copy(conn_cfg)
            conn_cfg.mode = mode = self.mode = 'listen'
            self.conn = conn_cfg
        self.refusal = ''                          # another device holds this ADR (listen)
        self.registers = registers or []
        self.poll_groups = poll_groups or {}
        self.parser = RegisterParser(byte_order)
        self.publish_callback: Optional[Callable] = None
        self.timeout = float(getattr(conn_cfg, 'timeout', 3) or 3)
        self.attempts = max(1, int(getattr(conn_cfg, 'retry_attempts', 3) or 1))
        self.retry_delay = min(0.5, max(0.0, float(getattr(conn_cfg, 'retry_delay', 0.2) or 0)))
        self.stats = _UnitStats()
        self.words: Dict[int, int] = {}
        self.updates = 0
        self.last_rx_mono: Optional[float] = None
        self.last_rx_by_cmd: Dict[int, float] = {}   # freshness per command the map reads (SV2-7)
        self.unused_answers = 0                      # heard for this ADR, read by no row
        self._summary_problem = ''
        self.last_fail_mono: Optional[float] = None
        self._counter_filters: Dict[int, Any] = {}
        self._lock = threading.Lock()
        self._registered = False
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._plan_dirty = True
        self.poll_count = 0
        if self.config_error:
            self._listener = None
            self._bus = None
        elif self.mode == 'listen':
            self._listener = listener_for(conn_cfg)
            self._bus = None
        else:
            self._listener = None
            self._bus = bus_for(conn_cfg)

    # ── lifecycle ──────────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        if not self._registered or self.config_error:
            return False
        if self._listener is not None:
            return self._listener._port is not None and not self._listener.open_error
        return self._bus.is_open and not self._bus.open_error

    def connect(self) -> bool:
        if self.config_error:
            return False
        if self._listener is not None:
            self.refusal = ''
            self._registered = True
            ok = self._listener.register(self.unit_id, self)
            if self.refusal:
                self._registered = False
            return ok
        if not self._registered:
            self._registered = True
            with _REG_LOCK:
                self._bus.users += 1
        with self._bus.lock:
            return self._bus.ensure_open()

    def start_polling(self) -> None:
        if self._listener is not None or self.config_error:
            return                                   # the bus's own master sets the rhythm
        if self._thread is not None and self._thread.is_alive():
            self._stop.set()
            self._thread.join(timeout=5)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._poll_loop, args=(self._stop,), daemon=True,
                                        name=f"seplos-v2:{self.device_id}")
        self._thread.start()

    def disconnect(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5)
        self._thread = None
        if self._listener is not None:
            self._listener.unregister(self.unit_id, self)
        elif self._registered and self._bus is not None:
            with _REG_LOCK:
                self._bus.users = max(0, self._bus.users - 1)
                last = self._bus.users == 0
            if last:
                with self._bus.lock:
                    self._bus.close()
        self._registered = False

    stop = disconnect

    def update_registers(self, registers: list, poll_groups: dict = None) -> None:
        with self._lock:
            self.registers = registers or []
            if poll_groups is not None:
                self.poll_groups = poll_groups
            self._plan_dirty = True

    def reload_registers(self) -> None:
        """The poll loop re-plans on its next tick; nothing to restart."""

    # ── poll mode ──────────────────────────────────────────────────────────
    def _plan(self) -> List[Tuple[str, float, list, list]]:
        """(group, interval, registers, commands) for every group in use."""
        with self._lock:
            regs = list(self.registers)
            groups = dict(self.poll_groups)
            self._plan_dirty = False
        by_group: Dict[str, list] = {}
        for r in regs:
            g = getattr(r, 'poll_group', '') or 'normal'
            if g not in groups:
                g = 'normal'
            by_group.setdefault(g, []).append(r)
        plan = []
        for g, rs in by_group.items():
            pg = groups.get(g)
            iv = float(getattr(pg, 'interval', pg if isinstance(pg, (int, float)) else 5) or 5)
            cmds = []
            for r in rs:
                a = area_of(r.address)
                # only what may be ASKED: a 0x5A row is heard, never polled
                if a in _DECODERS and a in READ_COMMANDS and a not in cmds:
                    cmds.append(a)
            plan.append((g, max(0.05, iv), rs, sorted(cmds)))
        return plan

    def _poll_loop(self, stop: threading.Event) -> None:
        plan = self._plan()
        due = {g: 0.0 for g, *_ in plan}
        while not stop.is_set():
            if self._plan_dirty:
                plan = self._plan()
                due = {g: due.get(g, 0.0) for g, *_ in plan}
            if not plan:
                if stop.wait(1.0):
                    break
                continue
            now = time.monotonic()
            ready = [p for p in plan if due[p[0]] <= now]
            if not ready:
                stop.wait(max(0.02, min(min(due.values()) - now, 1.0)))
                continue
            try:
                if not self._poll_cycle(stop, ready, now):
                    return
            except Exception as e:  # noqa: BLE001 — one bad cycle must not end polling for good
                self.stats.loop_errors += 1
                logger.exception("seplos_v2 %s: poll cycle failed — %s (continuing)", self.device_id, e)
            for g, iv, _rs, _cs in ready:
                due[g] = max(due[g] + iv, time.monotonic()) if due[g] else time.monotonic() + iv
            self.poll_count += 1

    def _poll_cycle(self, stop: threading.Event, ready, now: float) -> bool:
        """Ask what the due groups need and publish them; False when stopped."""
        cmds: List[int] = []
        for _g, _iv, _rs, cs in ready:
            cmds.extend(c for c in cs if c not in cmds)
        fresh = set()
        for cid2 in cmds:
            if stop.is_set():
                return False
            if self.poll_once(cid2):
                fresh.add(cid2)
        if stop.is_set():
            return False
        fresh.add(AREA_DIAG)
        for g, iv, rs, _cs in ready:
            data = self._values([r for r in rs if area_of(r.address) in fresh], now, interval=iv)
            if data:
                self._publish(g, data)
        return True

    def poll_once(self, cid2: int) -> bool:
        """One command to this pack, with retries; True when the map got fresh
        words from it. Every failure lands in the unit's stats."""
        for attempt in range(self.attempts):
            if attempt and self._stop.wait(self.retry_delay):
                return False
            try:
                fr, ms = self._bus.exchange(self.unit_id, cid2, self.timeout)
            except ResponseTimeout:
                self._count_error('timeout')
                continue
            except FrameError as e:
                self._count_error(e.kind)
                continue
            except RtnError as e:
                self._count_error('rtn', rtn=e.code)
                return False                     # it answered: asking again changes nothing
            except ConnectionError:
                self._count_error('io')
                return False
            return self._ingest_answer(cid2, fr, time.monotonic(), ms, publish=False)
        return False

    # ── the shared data path ───────────────────────────────────────────────
    def _wanted(self) -> set:
        """The commands whose answers this map reads (every decoded one when
        the map is empty). Only those count as answers and as freshness: a
        0x42 map must not look alive on 0x5A summaries (SV2-7)."""
        with self._lock:
            regs = list(self.registers)
        got = {area_of(r.address) for r in regs} & set(_DECODERS)
        return got if regs else set(_DECODERS)

    def _oldest_wanted(self) -> Tuple[Optional[int], Optional[float]]:
        """(command, last answer) of the stalest command the map reads that
        has been heard at least once — the one that decides freshness."""
        heard = {c: t for c, t in self.last_rx_by_cmd.items() if c in self._wanted()}
        if not heard:
            return None, None
        c = min(heard, key=heard.get)
        return c, heard[c]

    def _count_error(self, kind: str, rtn: Optional[int] = None) -> None:
        s = self.stats
        if kind == 'timeout':
            s.timeouts += 1
        elif kind in ('chksum', 'lchksum'):
            s.checksum_errors += 1
        elif kind == 'rtn':
            s.rtn_errors += 1
            s.last_rtn = rtn
        elif kind == 'decode':
            s.decode_errors += 1
        elif kind == 'io':
            s.io_errors += 1
        else:
            s.frame_errors += 1
        self.last_fail_mono = time.monotonic()
        self._diag_words()

    def _diag_words(self) -> None:
        s = self.stats
        with self._lock:
            w = self.words
            if s.last_response_ms is not None:
                w[MAP_DIAG_RESPONSE_MS] = min(0xFFFF, int(round(s.last_response_ms)))
            w[MAP_DIAG_CHECKSUM_ERRORS] = s.checksum_errors & 0xFFFF
            w[MAP_DIAG_TIMEOUTS] = s.timeouts & 0xFFFF
            w[MAP_DIAG_RTN_ERRORS] = s.rtn_errors & 0xFFFF
            if s.last_rtn is not None:
                w[MAP_DIAG_LAST_RTN] = s.last_rtn
            w[MAP_DIAG_GOOD] = s.good & 0xFFFF
            w[MAP_DIAG_DECODE_ERRORS] = s.decode_errors & 0xFFFF

    def _ingest_answer(self, cid2: int, fr: Frame, now: float, ms: float, publish: bool = True,
                       rtn: Optional[int] = None) -> bool:
        """``rtn``: the return code when the frame does not carry it in CID2
        (a 0x5A answer echoes the command there); None = read it from CID2."""
        code = fr.cid2 if rtn is None else rtn
        if code != 0:
            self._count_error('rtn', rtn=code)
            return False
        if cid2 not in _DECODERS:
            return False                         # 0x47: heard, not decoded yet
        if cid2 not in self._wanted():
            self.unused_answers += 1             # heard, but no row reads it: not an answer for us
            return False
        problem = ''
        try:
            if cid2 == CMD_PARALLEL_SUMMARY:
                d = decode_summary(fr.info)
                new, problem = summary_words(d), d['problem']
            else:
                new = words_for(cid2, fr.info)
        except DecodeError as e:
            logger.warning("seplos_v2 %s: 0x%02X answer not decoded — %s", self.device_id, cid2, e)
            self._count_error('decode')
            return False
        s = self.stats
        if problem:
            # kept raw for the bench; nothing named, not counted as an answer (SV2-8)
            if problem != self._summary_problem:
                logger.warning("seplos_v2 %s: 0x5A answer not decoded by name — %s (raw words kept)",
                               self.device_id, problem)
            self._summary_problem = problem
            self._count_error('decode')
        else:
            s.good += 1
            s.last_response_ms = ms
            s.by_command[f'0x{cid2:02X}'] = s.by_command.get(f'0x{cid2:02X}', 0) + 1
            self.last_rx_mono = now
            self.last_rx_by_cmd[cid2] = now
        with self._lock:
            # the command's block is replaced whole: a cell that disappeared
            # must not keep its last voltage
            for a in [a for a in self.words if area_of(a) == cid2]:
                del self.words[a]
            self.words.update(new)
        self._diag_words()
        if publish:
            with self._lock:
                regs = [r for r in self.registers if area_of(r.address) in (cid2, AREA_DIAG)]
            data = self._values(regs, now)
            if data:
                self._publish('tap', data)       # the push group calcs match (calc_engine)
        return not problem

    def _values(self, regs: list, now: float, interval: Optional[float] = None) -> Dict[int, Dict]:
        with self._lock:
            words = dict(self.words)
        siblings: Dict[str, Any] = {}
        rows = []
        for r in regs:
            if _table_of(r) != 'input':
                continue                         # the map is the input table only
            n = self.parser.get_register_count(getattr(r, 'data_type', 'uint16') or 'uint16')
            raw = [words.get(r.address + i) for i in range(n)]
            if any(w is None for w in raw):
                continue                         # absent: nothing published, never a zero
            rows.append((r, raw))
            if n == 1:
                siblings[r.name] = raw[0]
        data: Dict[int, Dict] = {}
        ts = time.time()
        for r, raw in rows:
            val = self.parser.parse_value(raw, getattr(r, 'data_type', 'uint16') or 'uint16',
                                          nan=getattr(r, 'nan', None))
            if val is None:
                continue
            cf = None
            if getattr(r, 'monotonic', False):
                cf = self._counter_filters.get(_key(r))
                if cf is None:
                    from .counter_filter import CounterFilter
                    cf = self._counter_filters[_key(r)] = CounterFilter()
            val = apply_corrections(val, r, counter_filter=cf, siblings=siblings)
            if val is None:
                continue
            row = {'value': val, 'register': r, 'ts': ts, 'mono': now}
            if interval is not None:
                row['interval'] = interval
            data[_key(r)] = row
        return data

    def _publish(self, group: str, data: Dict[int, Dict]) -> None:
        if not self.publish_callback:
            return
        if self.updates == 0:
            logger.info("seplos_v2 %s: first %d values (pack %d, %s)",
                        self.device_id, len(data), self.unit_id, self.mode)
        self.updates += len(data)
        try:
            self.publish_callback(group, data)
        except Exception as e:  # noqa: BLE001 — never kill the poll/reader thread
            logger.error("seplos_v2 fan-out failed for %s: %s", self.device_id, e)

    # ── observability ──────────────────────────────────────────────────────
    def _where(self):
        return self._listener if self._listener is not None else self._bus

    def get_stats(self) -> Dict:
        s = self.stats
        w = self._where()
        if w is None:
            return {'connected': False, 'mode': f"seplos_v2 (not started: {self.config_error})",
                    'unit_id': self.unit_id, 'successful_reads': 0, 'failed_reads': 0, 'errors': 0,
                    'updates': 0, 'poll_rate': None, 'staleness_age_s': None, 'last_latency_ms': None,
                    'error_counts': {}, 'bus': {'port': bus_name(self.conn), 'open_error': self.config_error}}
        last = self.last_rx_mono
        if self.mode == 'listen':
            last = self._oldest_wanted()[1]       # the stalest command the map reads (SV2-7)
        age = None if last is None else round(time.monotonic() - last, 1)
        failed = s.timeouts + s.checksum_errors + s.frame_errors + s.rtn_errors + s.decode_errors + s.io_errors
        rate = None
        if self.mode == 'poll':
            ivs = {}
            for r in self.registers:
                g = getattr(r, 'poll_group', '') or 'normal'
                pg = self.poll_groups.get(g) or self.poll_groups.get('normal')
                ivs[g] = float(getattr(pg, 'interval', 5) or 5)
            rate = round(sum(1.0 / v for v in ivs.values() if v > 0), 2)
        bus: Dict[str, Any] = {'port': w.name, 'open_error': w.open_error}
        if self._listener is not None:
            bus.update(frames=w.frames, bad_frames=w.bad_frames, orphans=w.orphans,
                       commands_seen=w.commands_seen, resync_dropped_bytes=w.framer.dropped_bytes,
                       summary_unpaired=w.summary_unpaired, unused_answers=self.unused_answers)
            warn = self._listen_warning()
            if warn:
                bus['warning'] = warn
            if self.refusal:
                bus['open_error'] = self.refusal
        else:
            bus.update(resync_dropped_bytes=w.dropped_bytes)
        return {
            'connected': self.connected,
            'mode': f"seplos_v2 ({'listen-only' if self.mode == 'listen' else 'poll'})",
            'unit_id': self.unit_id,
            'successful_reads': s.good,
            'failed_reads': failed,
            'errors': failed,
            'updates': self.updates,
            'poll_rate': rate,
            'staleness_age_s': age,
            'last_latency_ms': None if s.last_response_ms is None else round(s.last_response_ms, 1),
            'error_counts': {
                'timeouts': s.timeouts, 'checksum_errors': s.checksum_errors,
                'frame_errors': s.frame_errors, 'rtn_errors': s.rtn_errors,
                'decode_errors': s.decode_errors, 'io_errors': s.io_errors,
                'loop_errors': s.loop_errors,
            },
            'last_rtn': None if s.last_rtn is None else f"0x{s.last_rtn:02X} ({RTN_TEXT.get(s.last_rtn, 'unknown')})",
            'answers_by_command': dict(s.by_command),
            'bus': bus,
        }

    def _listen_warning(self) -> str:
        """Something heard but nothing usable, said in words (SV2-10)."""
        w = self._listener
        if w is None or self.stats.good:
            return ''
        if w.summary_unpaired:
            return (f"{w.summary_unpaired} 0x5A answers heard but none paired with the master's question — "
                    "its question may carry INFO (another firmware), or the questions are lost on the line")
        if self._summary_problem:
            return f"0x5A answers heard but not decoded: {self._summary_problem}"
        if self.unused_answers:
            return (f"{self.unused_answers} answers heard for pack {self.unit_id}, but this map reads none of "
                    "their commands — the master asks 0x5A? use seplos_bms_v2_tap")
        return ''

    def data_health(self, stale_threshold_s: float = 30) -> Dict:
        if self.config_error:
            return {'status': 'down', 'reason': self.config_error}
        if self.refusal:
            return {'status': 'down', 'reason': self.refusal}
        w = self._where()
        if w.open_error or not self._registered:
            return {'status': 'down', 'reason': w.open_error or 'not attached'}
        bound = float(getattr(self.conn, 'stale_after_s', None) or stale_threshold_s)
        if self.mode == 'poll' and self.registers:
            fastest = min((float(getattr(self.poll_groups.get(getattr(r, 'poll_group', '') or 'normal'),
                                         'interval', 5) or 5) for r in self.registers), default=5)
            bound = max(bound, 3 * fastest + 2)
        if self.mode == 'listen':
            cmd, last = self._oldest_wanted()
            if last is None:
                warn = self._listen_warning()
                return {'status': 'stale', 'reason': 'no answer heard yet' + (f" — {warn}" if warn else '')}
            age = time.monotonic() - last
            if age > max(1.0, bound):
                return {'status': 'stale', 'reason': f'no 0x{cmd:02X} answer for {int(age)}s'}
            return {'status': 'ok'}
        if self.last_rx_mono is None:
            if self.last_fail_mono is not None:
                return {'status': 'down', 'reason': 'the pack does not answer'}
            return {'status': 'ok'}
        age = time.monotonic() - self.last_rx_mono
        if age > max(1.0, bound):
            return {'status': 'down', 'reason': f'no answer for {int(age)}s'}
        return {'status': 'ok'}

    def recent_frames(self, limit: int = 50) -> List[Dict]:
        w = self._where()
        return [] if w is None else list(w.recent)[-limit:]


def probe(conn_cfg, timeout: float = 3.0) -> Dict[str, Any]:
    """One telemetry request (0x42) to one pack, through the shared transport
    (a running device on the same port takes turns with it). For Test."""
    mode = str(getattr(conn_cfg, 'mode', '') or 'poll').lower()
    if mode == 'listen':
        return {'ok': None, 'message': 'listen-only — nothing to ask; save the device, '
                                       'then Test shows what it has heard'}
    if mode not in MODES:
        return {'ok': False, 'message': f"mode {mode!r} is not poll or listen — nothing sent"}
    with _REG_LOCK:
        heard = _LISTENERS.get(bus_key(conn_cfg))
        busy = bool(heard is not None and (heard._clients or heard._observers))
    if busy:
        # the gateway listens there: another master owns that bus
        return {'ok': False, 'message': f"{bus_name(conn_cfg)} is listened to by the gateway — another "
                                        "master polls it, so a question from here would collide; nothing sent"}
    bus = bus_for(conn_cfg)
    adr = int(getattr(conn_cfg, 'unit_id', 0) or 0)
    try:
        fr, ms = bus.exchange(adr, CMD_TELEMETRY, timeout)
        d = decode_telemetry(fr.info)
    except ResponseTimeout:
        return {'ok': False, 'message': f"no answer from pack {adr} within {timeout:g} s "
                                        "(address, A/B wiring, baud rate?)"}
    except FrameError as e:
        return {'ok': False, 'message': f"a corrupted answer ({e.kind}: {e}) — line noise, baud rate?"}
    except RtnError as e:
        return {'ok': False, 'message': f"the pack refused: {e}"}
    except DecodeError as e:
        return {'ok': False, 'message': f"an answer that does not decode: {e}"}
    except ConnectionError as e:
        return {'ok': False, 'message': f"cannot open {bus.name}: {e}"}
    finally:
        if bus.users == 0:
            with bus.lock:
                bus.close()
    soc = d.get('soc')
    return {'ok': True, 'latency_ms': round(ms, 1),
            'message': f"pack {adr}: {len(d['cells'])} cells, {d['voltage'] / 100:.2f} V"
                       + (f", SOC {soc / 10:.1f} %" if soc is not None else '')}
