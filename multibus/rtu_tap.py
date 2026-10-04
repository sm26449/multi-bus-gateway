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
    """Incremental, timing-based RTU frame splitter. Pure — fed with
    ``(bytes, monotonic_time)`` so it is testable without a serial port."""

    def __init__(self, baudrate: int = 9600, bytesize: int = 8,
                 parity: str = 'N', stopbits: int = 1):
        bits = 1 + bytesize + (0 if parity in ('N', 'n', '') else 1) + stopbits
        char_time = bits / float(baudrate or 9600)
        self.gap = max(_MIN_GAP_S, 3.5 * char_time)
        self._buf = bytearray()
        self._last_byte_t: Optional[float] = None

    def feed(self, data: bytes, now: float) -> List[bytes]:
        """Append bytes; return frames CLOSED by the silence before them."""
        frames = []
        if (self._buf and self._last_byte_t is not None
                and now - self._last_byte_t >= self.gap):
            frames.append(bytes(self._buf))
            self._buf.clear()
        if data:
            self._buf.extend(data)
            self._last_byte_t = now
        return frames

    def flush(self, now: float) -> List[bytes]:
        """Close the pending frame if the bus has been silent long enough."""
        if (self._buf and self._last_byte_t is not None
                and now - self._last_byte_t >= self.gap):
            out = [bytes(self._buf)]
            self._buf.clear()
            return out
        return []


class TapReader:
    """One serial port, one reader thread, many per-unit clients."""

    def __init__(self, conn_cfg):
        self.port = conn_cfg.serial_port
        self.baudrate = int(getattr(conn_cfg, 'baudrate', 9600) or 9600)
        self.parity = str(getattr(conn_cfg, 'parity', 'N') or 'N')
        self.stopbits = int(getattr(conn_cfg, 'stopbits', 1) or 1)
        self.bytesize = int(getattr(conn_cfg, 'bytesize', 8) or 8)
        self._clients: Dict[int, 'RtuTapClient'] = {}
        self._pending: Dict[int, Tuple[int, int, int, float]] = {}  # unit -> (fc, addr, count, t)
        self._serial = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
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
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"rtu-tap:{self.port}")
        self._thread.start()
        logger.info("rtu_tap %s: listening (%d baud, read-only)", self.port, self.baudrate)
        return True

    def _stop_locked(self) -> None:
        self._stop.set()
        if self._serial:
            try: self._serial.close()
            except Exception: pass  # noqa: BLE001
        self._serial = None
        self._thread = None

    # ── the wire loop ──────────────────────────────────────────────────────
    def _run(self) -> None:
        framer = RtuFramer(self.baudrate, self.bytesize, self.parity, self.stopbits)
        ser = self._serial
        while not self._stop.is_set():
            try:
                data = ser.read(512)
            except Exception as e:  # noqa: BLE001 — adapter yanked mid-read
                if self._stop.is_set():
                    break              # our own close() interrupted the read
                self.open_error = str(e)
                logger.warning("rtu_tap %s: read failed — %s", self.port, e)
                break
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
        body = frame[:-2]
        if fc & 0x80:
            self.exceptions += 1
            self._note(now, unit, fc & 0x7F, kind='exception',
                       detail=f"code {body[2] if len(body) > 2 else '?'}")
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
                words = [(body[3 + 2 * i] << 8) | body[4 + 2 * i] for i in range(count)]
                self._note(now, unit, fc, kind='response', detail=f"@{addr} x{count}")
                self._dispatch(unit, addr, words, now)
            else:
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
            self._dispatch(unit, addr, [value], now)
            return
        if fc == 16 and len(body) >= 7 and len(frame) == 9 + body[6]:  # write-multi request
            addr = (body[2] << 8) | body[3]
            count = (body[4] << 8) | body[5]
            if body[6] == 2 * count:
                words = [(body[7 + 2 * i] << 8) | body[8 + 2 * i] for i in range(count)]
                self.writes_seen += 1
                self._note(now, unit, fc, kind='write', detail=f"@{addr} x{count}")
                self._dispatch(unit, addr, words, now)
            return
        self._note(now, unit, fc, kind='other')

    def _note(self, now: float, unit: int, fc: int, kind: str, detail: str = "") -> None:
        self.recent.append({'t': time.time(), 'unit': unit, 'fc': fc,
                            'kind': kind, 'detail': detail})

    def _dispatch(self, unit: int, addr: int, words: List[int], now: float) -> None:
        client = self._clients.get(unit)
        if client:
            try:
                client._ingest_window(addr, words, now)
            except Exception as e:  # noqa: BLE001 — a decode bug must not kill the reader
                logger.error("rtu_tap %s unit %d: ingest failed — %s", self.port, unit, e)


# one reader per port, shared by every tap device on it
_READERS: Dict[str, TapReader] = {}
_READERS_LOCK = threading.Lock()


def reader_for(conn_cfg) -> TapReader:
    with _READERS_LOCK:
        r = _READERS.get(conn_cfg.serial_port)
        if r is None:
            r = _READERS[conn_cfg.serial_port] = TapReader(conn_cfg)
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
    def _ingest_window(self, addr: int, words: List[int], now: float) -> None:
        self.windows += 1
        self.last_rx_mono = now
        with self._lock:
            regs = list(self.registers)
        end = addr + len(words)
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
                cf = self._counter_filters.get(r.address)
                if cf is None:
                    from .counter_filter import CounterFilter
                    cf = self._counter_filters[r.address] = CounterFilter()
            val = apply_corrections(val, r, counter_filter=cf,
                                    siblings=self._sibling_raw)
            if val is None:
                continue
            data[r.address] = {'value': val, 'register': r, 'ts': ts, 'mono': now}
        if data and self.publish_callback:
            self.updates += len(data)
            try:
                self.publish_callback('tap', data)
            except Exception as e:  # noqa: BLE001 — see mqtt_input: never kill the reader thread
                logger.error("rtu_tap fan-out failed for %s: %s", self.device_id, e)

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
            },
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
