# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Listen-only Modbus RTU tap: read a bus someone ELSE is mastering.

A classic Modbus RTU bus has exactly one master. Plenty of valuable buses
already have theirs — a BMS master pack polling its slaves, a vendor
datalogger polling its meters, a PLC owning its drives — and putting a second
active master on the wire corrupts frames for both. The tap mode makes MBG a
silent observer instead: it opens the serial port, NEVER transmits a byte,
reassembles the frames the existing master/slave exchange produces, pairs
requests with responses, and feeds the decoded register values through the
same correction/store/publish pipeline a polled device uses.

Topology: one :class:`TapReader` per serial port (the port cannot be shared),
dispatching to one :class:`RtuTapClient` per observed unit id. Devices declare
``connection: {protocol: rtu_tap, serial_port: /dev/ttyX, unit_id: N, ...}``;
several devices on the same port share the reader automatically.

What the tap understands:
  * FC 03/04 request→response pairs → a window of holding/input registers
    (the bread and butter of any polling master);
  * FC 06 single-register writes and FC 16 multi-register write REQUESTS →
    the written values (watching a master push setpoints is half the reason
    to tap a bus while debugging);
  * exception responses (counted, surfaced in stats).

Framing is timing-based like the spec says (3.5 character silence ends a
frame) with CRC-16 as the arbiter; a CRC failure drops the buffered bytes and
the stream re-synchronizes at the next silent gap.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Tuple

from .register_parser import RegisterParser
from .value_decode import apply_corrections

logger = logging.getLogger(__name__)

_RESPONSE_WINDOW_S = 1.0        # a response later than this is an orphan
_MIN_GAP_S = 0.00175            # 3.5 chars at 19200+; slower bauds compute more


def crc16(data: bytes) -> int:
    """Modbus CRC-16 (poly 0xA001), little-endian on the wire."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            lsb = crc & 1
            crc >>= 1
            if lsb:
                crc ^= 0xA001
    return crc


def frame_crc_ok(frame: bytes) -> bool:
    if len(frame) < 4:
        return False
    return crc16(frame[:-2]) == (frame[-2] | (frame[-1] << 8))


class RtuFramer:
    """Incremental RTU frame splitter: protocol-aware + CRC-validated.

    Timing alone cannot split frames here: a tap reads the port in chunks
    (pyserial returns ~50 ms batches) and at 19200 baud a whole
    request→response exchange fits inside ONE chunk, so inter-frame silence
    is invisible to us. Instead, frames are extracted greedily from the head
    of the buffer by trying each PLAUSIBLE length for the function code and
    letting CRC-16 arbitrate — the same approach every serious bus sniffer
    uses. The 3.5-character silence remains the RESYNC signal: a head that
    validates at no candidate length (we attached mid-frame, or a byte got
    corrupted) is dropped once the bus goes quiet, and the stream realigns
    on the next exchange.

    Pure — fed with ``(bytes, monotonic_time)`` so it is testable without a
    serial port."""

    def __init__(self, baudrate: int = 9600, bytesize: int = 8,
                 parity: str = 'N', stopbits: int = 1):
        bits = 1 + bytesize + (0 if parity in ('N', 'n', '') else 1) + stopbits
        char_time = bits / float(baudrate or 9600)
        self.gap = max(_MIN_GAP_S, 3.5 * char_time)
        self._buf = bytearray()
        self._last_byte_t: Optional[float] = None
        self.dropped_bytes = 0            # resync cost, surfaced in stats

    def _candidates(self) -> List[int]:
        """Plausible total frame lengths for the buffer head's function code."""
        b = self._buf
        fc = b[1]
        out = []
        if fc & 0x80:
            out.append(5)                                  # exception response
        elif fc in (1, 2, 3, 4):
            out.append(8)                                  # read request
            if len(b) > 2:
                out.append(5 + b[2])                       # read response
        elif fc in (5, 6):
            out.append(8)                                  # write single (req == echo)
        elif fc in (15, 16):
            out.append(8)                                  # write-multi response
            if len(b) > 6:
                out.append(9 + b[6])                       # write-multi request
        else:                                              # unknown FC: best guesses
            out.append(8)
            if len(b) > 2:
                out.append(5 + b[2])
        return sorted({n for n in out if 4 <= n <= 260})

    def _extract(self, final: bool = False) -> List[bytes]:
        """Greedy head extraction with one-byte-slide resync.

        Wall-clock gaps are useless here — the serial layer hands us ~20-50 ms
        chunks, so every chunk boundary looks like silence. Resync is purely
        CRC-driven instead: when every candidate length for the head is
        already buffered and none validates, the head byte is junk — slide one
        byte and retry. ``final`` (true silence: an empty read) also slides
        through a PARTIAL head rather than waiting for bytes that will not
        come."""
        frames = []
        while len(self._buf) >= 4:
            cands = self._candidates()
            matched = None
            for n in cands:
                if len(self._buf) >= n and frame_crc_ok(bytes(self._buf[:n])):
                    matched = bytes(self._buf[:n])
                    break
            if matched is not None:
                frames.append(matched)
                del self._buf[:len(matched)]
                continue
            if max(cands) <= len(self._buf) or final:
                del self._buf[:1]          # junk head — slide to realign
                self.dropped_bytes += 1
                continue
            break                          # head plausible but incomplete — wait
        return frames

    def feed(self, data: bytes, now: float) -> List[bytes]:
        """Append bytes and return every complete frame at the buffer head."""
        if data:
            self._buf.extend(data)
            self._last_byte_t = now
        return self._extract()

    def flush(self, now: float) -> List[bytes]:
        """On bus silence (an empty read): slide through any junk head too."""
        return self._extract(final=True)


class TapReader:
    """One serial port, one reader thread, many per-unit clients."""

    @staticmethod
    def line_of(conn_cfg) -> Tuple[int, str, int, int]:
        """(baud, parity, stopbits, bytesize) as the port is opened."""
        return (int(getattr(conn_cfg, 'baudrate', 9600) or 9600),
                str(getattr(conn_cfg, 'parity', 'N') or 'N').upper(),
                int(getattr(conn_cfg, 'stopbits', 1) or 1),
                int(getattr(conn_cfg, 'bytesize', 8) or 8))

    def __init__(self, conn_cfg):
        # a serial port on this host, or a bus behind a bridge: the bridge
        # (our ser2net, a transparent converter) passes on every byte the bus
        # carries — the framing is CRC-driven, so TCP's lost gaps do not matter
        self.host = str(getattr(conn_cfg, 'host', '') or '') if not getattr(conn_cfg, 'serial_port', '') else ''
        self.tcp_port = int(getattr(conn_cfg, 'port', 0) or 0)
        self.port = conn_cfg.serial_port or f"{self.host}:{self.tcp_port}"
        self.line = self.line_of(conn_cfg)
        self.baudrate, self.parity, self.stopbits, self.bytesize = self.line
        self._clients: Dict[int, 'RtuTapClient'] = {}
        self._pending: Dict[int, Tuple[int, int, int, float]] = {}  # unit -> (fc, addr, count, t)
        # learned window shapes: (fc, byte_count) -> start address. On a
        # multi-drop bus the MASTER device answers no requests — it emits its
        # own blocks unsolicited (the Seplos master pack does exactly this).
        # Every paired exchange teaches us what a block of that shape means,
        # so an unpaired response dispatches at the learned address. A shape
        # seen with two different addresses becomes ambiguous and never infers.
        self._shape: Dict[Tuple[int, int], Optional[int]] = {}
        self.inferred = 0
        self._serial = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._framer = RtuFramer(self.baudrate, self.bytesize,
                                 self.parity, self.stopbits)
        # port-level observability (every client reports these)
        self.frames = 0
        self.crc_errors = 0
        self.orphans = 0
        self.exceptions = 0
        self.writes_seen = 0
        self.open_error = ""
        # last decoded frames, newest last — the debug half of the feature
        self.recent: deque = deque(maxlen=200)

    # ── lifecycle ──────────────────────────────────────────────────────────
    def register(self, unit_id: int, client: 'RtuTapClient') -> bool:
        with self._lock:
            self._clients[unit_id] = client
            if self._thread is None:
                return self._start_locked()
            return self._serial is not None

    def unregister(self, unit_id: int) -> None:
        with self._lock:
            self._clients.pop(unit_id, None)
            if not self._clients:
                self._stop_locked()

    def _start_locked(self) -> bool:
        if self.host:
            # over the network: the wire loop connects (and reconnects) itself
            self.open_error = ""
            self._stop.clear()
            self._serial = _TcpSource(self.host, self.tcp_port)
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name=f"rtu-tap:{self.port}")
            self._thread.start()
            logger.info("rtu_tap %s: listening through the bridge (read-only)", self.port)
            return True
        try:
            import serial
            self._serial = serial.Serial(
                port=self.port, baudrate=self.baudrate, parity=self.parity,
                stopbits=self.stopbits, bytesize=self.bytesize,
                timeout=0.05)                      # poll the gap at 20 Hz
        except Exception as e:  # noqa: BLE001 — port missing/busy: say why, stay down
            self.open_error = str(e)
            self._serial = None
            logger.warning("rtu_tap %s: cannot open port — %s", self.port, e)
            return False
        self.open_error = ""                       # a past failure is not today's
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"rtu-tap:{self.port}")
        self._thread.start()
        logger.info("rtu_tap %s: listening (%d baud, read-only)", self.port, self.baudrate)
        return True

    def _stop_locked(self) -> None:
        self._stop.set()
        if self._serial:
            try:
                self._serial.close()
            except Exception:  # noqa: BLE001
                pass
        self._serial = None
        self._thread = None

    # ── the wire loop ──────────────────────────────────────────────────────
    def _run(self) -> None:
        framer = self._framer
        ser = self._serial
        while not self._stop.is_set():
            try:
                data = ser.read(512)
            except Exception as e:  # noqa: BLE001 — adapter yanked mid-read
                if self._stop.is_set():
                    break              # our own close() interrupted the read
                self.open_error = str(e)
                if isinstance(ser, _TcpSource):
                    # a network hiccup: say so, wait, connect again
                    logger.warning("rtu_tap %s: bridge connection lost — %s", self.port, e)
                    ser.close()
                    if self._stop.wait(3):
                        break
                    continue
                logger.warning("rtu_tap %s: read failed — %s", self.port, e)
                break
            if isinstance(ser, _TcpSource) and data:
                self.open_error = ""
            now = time.monotonic()
            for frame in framer.feed(data, now) + ([] if data else framer.flush(now)):
                self._on_frame(frame, now)

    # ── frame classification + request/response pairing ────────────────────
    def _on_frame(self, frame: bytes, now: float) -> None:
        if not frame_crc_ok(frame):
            self.crc_errors += 1
            return
        self.frames += 1
        unit, fc = frame[0], frame[1]
        if self.frames == 1:
            logger.info("rtu_tap %s: first valid frame (unit %d, fc %d, %d bytes)",
                        self.port, unit, fc, len(frame))
        if self.frames % 1000 == 0:
            logger.info("rtu_tap %s: %d frames (crc_err %d, orphans %d, exc %d, "
                        "writes %d, resync %dB)", self.port, self.frames,
                        self.crc_errors, self.orphans, self.exceptions,
                        self.writes_seen, self._framer.dropped_bytes)
        body = frame[:-2]
        if fc & 0x80:
            self.exceptions += 1
            self._note(now, unit, fc & 0x7F, kind='exception',
                       detail=f"code {body[2] if len(body) > 2 else '?'}")
            return
        if fc in (1, 2):
            # coil/discrete reads: the request counts BITS, the response packs
            # them 8 to a byte LSB-first. Dispatched in BIT-address space.
            if len(frame) == 8:                            # request
                addr = (body[2] << 8) | body[3]
                count = (body[4] << 8) | body[5]
                self._pending[unit] = (fc, addr, count, now)
                self._note(now, unit, fc, kind='request', detail=f"@{addr} x{count} bits")
                return
            pend = self._pending.get(unit)
            nbytes = body[2] if len(body) > 2 else -1
            if (pend and pend[0] == fc and len(frame) == 5 + nbytes
                    and nbytes == (pend[2] + 7) // 8
                    and now - pend[3] <= _RESPONSE_WINDOW_S):
                addr, count = pend[1], pend[2]
                del self._pending[unit]
                key = (fc, nbytes)
                if self._shape.get(key, addr) != addr:
                    self._shape[key] = None
                else:
                    self._shape[key] = addr
                bits = [(body[3 + i // 8] >> (i % 8)) & 1 for i in range(count)]
                self._note(now, unit, fc, kind='response', detail=f"@{addr} x{count} bits")
                self._dispatch_bits(unit, addr, bits, now, fc)
                return
            if len(frame) == 5 + nbytes and nbytes > 0:
                addr = self._shape.get((fc, nbytes))
                if addr is not None:       # the master's own unsolicited block
                    bits = [(body[3 + i // 8] >> (i % 8)) & 1 for i in range(nbytes * 8)]
                    self.inferred += 1
                    self._note(now, unit, fc, kind='inferred', detail=f"@{addr} x{len(bits)} bits")
                    self._dispatch_bits(unit, addr, bits, now, fc)
                    return
            self.orphans += 1
            self._note(now, unit, fc, kind='orphan')
            return
        if fc in (3, 4):
            if len(frame) == 8:                            # request
                addr = (body[2] << 8) | body[3]
                count = (body[4] << 8) | body[5]
                self._pending[unit] = (fc, addr, count, now)
                self._note(now, unit, fc, kind='request', detail=f"@{addr} x{count}")
                return
            pend = self._pending.get(unit)
            nbytes = body[2] if len(body) > 2 else -1
            if (pend and pend[0] == fc and len(frame) == 5 + nbytes
                    and nbytes == 2 * pend[2] and now - pend[3] <= _RESPONSE_WINDOW_S):
                addr, count = pend[1], pend[2]
                del self._pending[unit]
                # teach the shape map (first write wins; a conflict poisons it)
                key = (fc, nbytes)
                if self._shape.get(key, addr) != addr:
                    self._shape[key] = None               # ambiguous — never infer
                else:
                    self._shape[key] = addr
                words = [(body[3 + 2 * i] << 8) | body[4 + 2 * i] for i in range(count)]
                self._note(now, unit, fc, kind='response', detail=f"@{addr} x{count}")
                self._dispatch(unit, addr, words, now, fc)
                return
            # unpaired response — the emitting unit may BE the bus master
            # (nobody asks it anything): infer the window from the shape the
            # other units' paired exchanges taught us
            if len(frame) == 5 + nbytes and nbytes > 0 and nbytes % 2 == 0:
                addr = self._shape.get((fc, nbytes))
                if addr is not None:
                    count = nbytes // 2
                    words = [(body[3 + 2 * i] << 8) | body[4 + 2 * i] for i in range(count)]
                    self.inferred += 1
                    self._note(now, unit, fc, kind='inferred', detail=f"@{addr} x{count}")
                    self._dispatch(unit, addr, words, now, fc)
                    return
            self.orphans += 1
            self._note(now, unit, fc, kind='orphan')
            return
        if fc == 6 and len(frame) == 8:                    # write single (req == echo)
            addr = (body[2] << 8) | body[3]
            value = (body[4] << 8) | body[5]
            # the echo repeats the request a few ms later — dispatch once
            key = (unit, addr, value)
            if getattr(self, '_last_w', None) == key and now - getattr(self, '_last_w_t', 0) < 0.5:
                return
            self._last_w, self._last_w_t = key, now
            self.writes_seen += 1
            self._note(now, unit, fc, kind='write', detail=f"@{addr} = {value}")
            self._dispatch(unit, addr, [value], now, fc)
            return
        if fc == 16 and len(body) >= 7 and len(frame) == 9 + body[6]:  # write-multi request
            addr = (body[2] << 8) | body[3]
            count = (body[4] << 8) | body[5]
            if body[6] == 2 * count:
                words = [(body[7 + 2 * i] << 8) | body[8 + 2 * i] for i in range(count)]
                self.writes_seen += 1
                self._note(now, unit, fc, kind='write', detail=f"@{addr} x{count}")
                self._dispatch(unit, addr, words, now, fc)
            return
        self._note(now, unit, fc, kind='other')

    def _note(self, now: float, unit: int, fc: int, kind: str, detail: str = "") -> None:
        self.recent.append({'t': time.time(), 'unit': unit, 'fc': fc,
                            'kind': kind, 'detail': detail})

    def _dispatch(self, unit: int, addr: int, words: List[int], now: float, fc: int = 3) -> None:
        client = self._clients.get(unit)
        if client:
            try:
                client._ingest_window(addr, words, now, _FC_TABLE.get(fc, 'holding'))
            except Exception as e:  # noqa: BLE001 — a decode bug must not kill the reader
                logger.error("rtu_tap %s unit %d: ingest failed — %s", self.port, unit, e)

    def _dispatch_bits(self, unit: int, addr: int, bits: List[int], now: float, fc: int = 1) -> None:
        client = self._clients.get(unit)
        if client:
            try:
                client._ingest_coils(addr, bits, now, _FC_TABLE.get(fc, 'coil'))
            except Exception as e:  # noqa: BLE001 — a decode bug must not kill the reader
                logger.error("rtu_tap %s unit %d: coil ingest failed — %s", self.port, unit, e)


# which table a function code reads or writes: a window decodes only the
# rows of ITS table (coil 0, holding 0 and input 0 are three different things)
_FC_TABLE = {1: 'coil', 5: 'coil', 15: 'coil', 2: 'discrete',
             3: 'holding', 6: 'holding', 16: 'holding', 4: 'input'}


def _table_of(r) -> str:
    from .config import normalize_register_type
    return normalize_register_type(getattr(r, 'register_type', '') or 'holding')


def _key(r) -> int:
    from .config import store_key
    return store_key(r.address, getattr(r, 'register_type', '') or 'holding')


# one reader per port, shared by every tap device on it
class _TcpSource:
    """The bytes of a bus behind a bridge, read like a serial port: ``read``
    returns what arrived within 50 ms (b"" on a quiet bus), connecting first
    when needed. The tap only ever reads — it never writes to the bus."""

    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self._sock = None

    def read(self, n: int) -> bytes:
        import socket
        if self._sock is None:
            self._sock = socket.create_connection((self.host, self.port), timeout=5)
            self._sock.settimeout(0.05)
        try:
            data = self._sock.recv(n)
        except socket.timeout:
            return b""
        if not data:
            raise ConnectionError("the bridge closed the connection")
        return data

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None


_READERS: Dict[str, TapReader] = {}
_READERS_LOCK = threading.Lock()


def _reader_key(conn_cfg) -> str:
    sp = getattr(conn_cfg, 'serial_port', '') or ''
    return sp or f"tcp:{getattr(conn_cfg, 'host', '')}:{getattr(conn_cfg, 'port', 0)}"


def reader_for(conn_cfg) -> TapReader:
    """The port's shared reader. An idle one (nobody attached) whose line
    settings no longer match is rebuilt — it was opened for devices that are
    gone, and an edited device may have changed its baud. Only on a MISMATCH:
    units are constructed before any of them connects, so "idle" alone would
    hand each its own reader, and the tty would be opened N times."""
    key = _reader_key(conn_cfg)
    with _READERS_LOCK:
        r = _READERS.get(key)
        if r is None or (not r._clients and r.line != TapReader.line_of(conn_cfg)):
            r = _READERS[key] = TapReader(conn_cfg)
        return r


class RtuTapClient:
    """ModbusClient-compatible facade for one OBSERVED unit on a tapped bus.

    No polling, no writes — values arrive when the bus's own master reads (or
    writes) them. Interface parity with the other drivers: connect /
    start_polling / update_registers / get_stats / data_health /
    publish_callback.
    """

    def __init__(self, conn_cfg, registers: list, poll_groups: dict = None,
                 byte_order: str = 'big', device_id: str = ''):
        self.conn = conn_cfg
        self.device_id = device_id
        self.unit_id = int(getattr(conn_cfg, 'unit_id', 1) or 1)
        self.registers = registers or []
        self.poll_groups = poll_groups or {}
        self.parser = RegisterParser(byte_order)
        self.publish_callback: Optional[Callable] = None
        self.connected = False
        self.windows = 0                      # paired windows dispatched to us
        self.table_mismatch: Optional[Dict] = None   # heard FC4, map says holding (or the reverse)
        self.updates = 0                      # register values stored
        self.last_rx_mono: Optional[float] = None
        self._reader = reader_for(conn_cfg)
        self._sibling_raw: Dict[str, Any] = {}     # name -> raw (SunSpec SF refs)
        self._counter_filters: Dict[int, Any] = {}
        self._lock = threading.Lock()

    # ── lifecycle (ModbusClient-compatible) ────────────────────────────────
    def connect(self) -> bool:
        self.connected = self._reader.register(self.unit_id, self)
        return self.connected

    def start_polling(self) -> None:
        """Nothing to start — the bus's own master sets the rhythm."""

    def disconnect(self) -> None:
        self._reader.unregister(self.unit_id)
        self.connected = False

    stop = disconnect

    def update_registers(self, registers: list, poll_groups: dict = None) -> None:
        with self._lock:
            self.registers = registers or []
            if poll_groups is not None:
                self.poll_groups = poll_groups

    def reload_registers(self) -> None:
        """No pollers to bounce — update_registers already took effect."""

    # ── data path ──────────────────────────────────────────────────────────
    def _ingest_window(self, addr: int, words: List[int], now: float, table: str = 'holding') -> None:
        self.windows += 1
        self.last_rx_mono = now
        if self.windows == 1:
            logger.info("rtu_tap %s: first paired window — unit %d @%d x%d "
                        "(callback %s)", self.device_id, self.unit_id, addr,
                        len(words), 'set' if self.publish_callback else 'MISSING')
        with self._lock:
            regs = [r for r in self.registers if _table_of(r) == table]
            other = [r for r in self.registers if _table_of(r) in ('holding', 'input') and _table_of(r) != table]
        end = addr + len(words)
        hit = lambda r: addr <= r.address < end   # noqa: E731
        if not any(hit(r) for r in regs) and any(hit(r) for r in other):
            # the master reads these addresses in the OTHER table than the map
            # says — the commonest tap mistake; Test names it
            self.table_mismatch = {'heard': table, 'map': _table_of(next(r for r in other if hit(r))),
                                   'address': addr}
        # raw sibling values first, so a scale factor read in the SAME window
        # resolves for the registers that reference it
        in_window = []
        for r in regs:
            n = self.parser.get_register_count(getattr(r, 'data_type', 'uint16') or 'uint16')
            if r.address >= addr and r.address + n <= end:
                raw_words = words[r.address - addr: r.address - addr + n]
                in_window.append((r, raw_words))
                if n == 1:
                    self._sibling_raw[r.name] = raw_words[0]
        data = {}
        ts = time.time()
        for r, raw_words in in_window:
            val = self.parser.parse_value(raw_words, getattr(r, 'data_type', 'uint16') or 'uint16',
                                          nan=getattr(r, 'nan', None))
            if val is None:
                continue
            cf = None
            if getattr(r, 'monotonic', False):
                cf = self._counter_filters.get(_key(r))
                if cf is None:
                    from .counter_filter import CounterFilter
                    cf = self._counter_filters[_key(r)] = CounterFilter()
            val = apply_corrections(val, r, counter_filter=cf,
                                    siblings=self._sibling_raw)
            if val is None:
                continue
            data[_key(r)] = {'value': val, 'register': r, 'ts': ts, 'mono': now}
        if data and self.publish_callback:
            if self.updates == 0:
                logger.info("rtu_tap %s: first %d values decoded from window @%d",
                            self.device_id, len(data), addr)
            self.updates += len(data)
            try:
                self.publish_callback('tap', data)
            except Exception as e:  # noqa: BLE001 — see mqtt_input: never kill the reader thread
                logger.error("rtu_tap fan-out failed for %s: %s", self.device_id, e)

    def _ingest_coils(self, addr: int, bits: List[int], now: float, table: str = 'coil') -> None:
        """A window of coils/discretes, in BIT-address space. A register with
        ``register_type: coil`` reads one bit (data_type bit/bool) or packs 16
        consecutive bits LSB-first into a word (data_type uint16) — the layout
        the Seplos PIC block and most alarm bitmaps use. mask/shift/enum then
        apply through the shared correction pipeline, so a status byte decodes
        to its label and a FET bit to ON/OFF exactly like any other register."""
        self.windows += 1
        self.last_rx_mono = now
        with self._lock:
            regs = [r for r in self.registers if _table_of(r) == table]
        end = addr + len(bits)
        data = {}
        ts = time.time()
        for r in regs:
            # uint16 packs 16 bits LSB-first; `mask: 1` marks a SINGLE-bit
            # register (the template validator has no bool type, and the
            # shared pipeline applies mask only inside enum decode)
            dt16 = str(getattr(r, 'data_type', '') or '').lower() == 'uint16'
            width = 1 if getattr(r, 'mask', None) == 1 else (16 if dt16 else 1)
            if not (addr <= r.address < end):
                continue
            off = r.address - addr
            take = min(width, end - r.address)   # high bits past the window read 0
            raw = sum(bits[off + i] << i for i in range(take))
            val = apply_corrections(raw, r, siblings=self._sibling_raw)
            if val is None:
                continue
            data[_key(r)] = {'value': val, 'register': r, 'ts': ts, 'mono': now}
        if data and self.publish_callback:
            if self.updates == 0:
                logger.info("rtu_tap %s: first %d coil values from window @%d",
                            self.device_id, len(data), addr)
            self.updates += len(data)
            try:
                self.publish_callback('tap', data)
            except Exception as e:  # noqa: BLE001
                logger.error("rtu_tap coil fan-out failed for %s: %s", self.device_id, e)

    # ── observability ──────────────────────────────────────────────────────
    def get_stats(self) -> Dict:
        age = None
        if self.last_rx_mono is not None:
            age = round(time.monotonic() - self.last_rx_mono, 1)
        return {
            'connected': self.connected and not self._reader.open_error,
            'mode': 'rtu_tap (listen-only)',
            'successful_reads': self.windows,
            'failed_reads': 0,
            'updates': self.updates,
            'poll_rate': None,                 # push-driven: the master's rhythm
            'staleness_age_s': age,
            'last_latency_ms': None,
            'error_counts': {
                'crc_errors': self._reader.crc_errors,
                'orphan_frames': self._reader.orphans,
                'exception_responses': self._reader.exceptions,
                'resync_dropped_bytes': self._reader._framer.dropped_bytes,
            },
            'inferred_windows': self._reader.inferred,
            'table_mismatch': self.table_mismatch,
            'bus': {
                'port': self._reader.port,
                'frames': self._reader.frames,
                'writes_seen': self._reader.writes_seen,
                'open_error': self._reader.open_error,
            },
        }

    def data_health(self, stale_threshold_s: float = 30) -> Dict:
        if self._reader.open_error or not self.connected:
            return {'status': 'down', 'reason': self._reader.open_error or 'not attached'}
        bound = getattr(self.conn, 'stale_after_s', None) or stale_threshold_s
        if self.last_rx_mono is None:
            return {'status': 'stale', 'reason': 'no traffic observed yet'}
        age = time.monotonic() - self.last_rx_mono
        if age > max(1, bound):
            return {'status': 'stale', 'reason': f'no traffic for {int(age)}s'}
        return {'status': 'ok'}

    def recent_frames(self, limit: int = 50) -> List[Dict]:
        """The port's last decoded frames, newest last — the debug view."""
        return list(self._reader.recent)[-limit:]
