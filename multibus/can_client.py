# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""CAN input driver: frames from a carrier, decoded by the template's
signal rows, LISTEN-ONLY by construction (no transmit path exists).

Event-driven like ``mqtt_input.py``: nothing is polled. A frame arrives, the
rows on its id are decoded (can_signal.decode_signal + the shared correction
pipeline) and published as the same ``{key: {value, register, ts, mono}}``
batch every other driver produces — the arrival time is the read time, the
connection's ``stale_after_s`` is the only notion of freshness, poll groups
are informational.

**No transmit path.** A carrier here only *receives*: the socket is opened,
bound and read; this module never sends, and nothing in a configuration can
make it (any transmission — the virtual BMS, a request frame — is a separate
ADR with its own safety review). A test reads this file and fails on any
send/write call.

Carriers (``connection.carrier``):
  * ``socketcan`` — ``socket(AF_CAN, SOCK_RAW, CAN_RAW)`` from the standard
    library on an interface this process can see (``can0`` on bare metal or
    the bench, ``vcan0`` in e2e). The interface's bitrate and listen-only
    mode belong to the host (``ip link set can0 type can bitrate 500000
    listen-only on``); ``connection.bitrate`` is what the device expects,
    shown, not applied.
  * the bridge carrier (the serial bridge serving CAN over TCP) comes with
    edge-fleet E7 — the gateway container never opens hardware in production.
"""
from __future__ import annotations

import logging
import re
import socket
import struct
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .can_link import link_state, listen_only, parse_listen_only  # noqa: F401 — the driver reports both
from .can_signal import decode_signal, frame_key, signal_bits
from .value_decode import apply_corrections

logger = logging.getLogger(__name__)

# struct can_frame: u32 can_id, u8 len, 3 pad bytes, 8 data bytes
_CAN_FRAME = struct.Struct('=IB3x8s')
CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_EFF_MASK = 0x1FFFFFFF
CAN_SFF_MASK = 0x000007FF
CAN_ERR_MASK = 0x1FFFFFFF

CARRIERS = ('socketcan',)

class _ReceiveOnlySocket:
    """The CAN socket as the driver holds it: recv and close, nothing else —
    no send path reachable from outside, not even the descriptor."""
    __slots__ = ('__s',)

    def __init__(self, s):
        self.__s = s

    def recv(self, n: int) -> bytes:
        return self.__s.recv(n)

    def close(self) -> None:
        self.__s.close()


class SocketCanCarrier:
    """Receive-only view of one SocketCAN interface.

    The only methods are open / recv / close. Error frames (bus-off, the
    controller's error counters) are asked for, so a sick bus is counted
    rather than silent."""

    def __init__(self, channel: str):
        self.channel = channel
        self._sock: Optional[socket.socket] = None

    def open(self) -> None:
        if not hasattr(socket, 'AF_CAN'):
            raise OSError("this platform has no SocketCAN (AF_CAN)")
        s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        try:
            err_filter = getattr(socket, 'CAN_RAW_ERR_FILTER', 2)
            s.setsockopt(socket.SOL_CAN_RAW, err_filter, struct.pack('=I', CAN_ERR_MASK))
        except OSError:
            pass                               # a kernel without it: frames still arrive
        s.settimeout(1.0)                      # the reader checks its stop flag each second
        try:
            s.bind((self.channel,))
        except OSError:
            s.close()
            raise
        self._sock = _ReceiveOnlySocket(s)

    def recv(self) -> Optional[Tuple[int, bool, bool, bool, bytes]]:
        """One frame (id, extended, remote, error, data) or None after a quiet
        second. OSError when the interface is gone or down."""
        if self._sock is None:
            raise OSError("not open")
        try:
            raw = self._sock.recv(_CAN_FRAME.size)
        except socket.timeout:
            return None
        if len(raw) < _CAN_FRAME.size:
            return None
        cid, dlc, data = _CAN_FRAME.unpack(raw)
        ext = bool(cid & CAN_EFF_FLAG)
        return (cid & (CAN_EFF_MASK if ext else CAN_SFF_MASK), ext,
                bool(cid & CAN_RTR_FLAG), bool(cid & CAN_ERR_FLAG), data[:min(dlc, 8)])

    def close(self) -> None:
        s, self._sock = self._sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


def carrier_for(can_cfg: Dict[str, Any]):
    kind = str(can_cfg.get('carrier') or 'socketcan').lower()
    if kind == 'socketcan':
        return SocketCanCarrier(str(can_cfg.get('channel') or 'can0'))
    raise ValueError(f"CAN carrier {kind!r} is not available (one of {', '.join(CARRIERS)})")


_CHANNEL_RE = re.compile(r'^[A-Za-z0-9_.-]{1,15}$')
BITRATE_RANGE = (10_000, 1_000_000)          # classic CAN
STALE_RANGE = (1, 3600)


def connection_errors(conn: Dict[str, Any]) -> List[str]:
    """What a CAN connection may not be (a device's save and its Test)."""
    errors = []
    carrier = str(conn.get('carrier') or 'socketcan').lower()
    if carrier == 'bridge':
        errors.append("connection.carrier: 'bridge' (CAN over the serial bridge) is not available "
                      "yet — use socketcan")
    elif carrier not in CARRIERS:
        errors.append("connection.carrier: one of " + ", ".join(CARRIERS))
    if not _CHANNEL_RE.match(str(conn.get('channel') or '')):
        errors.append("connection.channel: the CAN interface's name (can0, vcan0)")
    for key, (lo, hi) in (('bitrate', BITRATE_RANGE), ('stale_after_s', STALE_RANGE)):
        if conn.get(key) is None:
            continue
        v = conn.get(key)
        try:
            ok = not isinstance(v, bool) and float(v) == int(float(v)) and lo <= int(float(v)) <= hi
        except (TypeError, ValueError):
            ok = False
        if not ok:
            errors.append(f"connection.{key}: a whole number {lo}..{hi}")
    return errors


def _id_key(can_id: int, extended: bool) -> str:
    return f"0x{can_id:X}" + ("x" if extended else "")


def hear(can_cfg: Dict[str, Any], seconds: float = 2.0, clock: Callable[[], float] = time.monotonic
         ) -> Dict[str, Any]:
    """A device's Test: does the interface exist, is it up, is it listen-only,
    and what does it carry for ``seconds`` — heard on the carrier's own
    receive-only socket (open / recv / close), nothing ever sent."""
    kind = str(can_cfg.get('carrier') or 'socketcan').lower()
    channel = str(can_cfg.get('channel') or 'can0')
    out: Dict[str, Any] = {'carrier': kind, 'channel': channel, 'bitrate': can_cfg.get('bitrate'),
                           'exists': None, 'up': None, 'listen_only': None, 'frames': 0,
                           'ids': {}, 'error_frames': 0, 'seconds': seconds, 'open_error': None}
    if kind == 'socketcan':
        out.update(link_state(channel))
        if out['exists'] is False:
            return out
        out['listen_only'] = listen_only(channel)
    car = carrier_for(can_cfg)
    try:
        car.open()
    except (OSError, ValueError) as e:
        out['open_error'] = str(e)
        return out
    try:
        end = clock() + seconds
        while clock() < end:
            try:
                f = car.recv()
            except OSError as e:
                out['open_error'] = str(e)
                break
            if f is None:
                continue
            cid, ext, _remote, err, _data = f
            if err:
                out['error_frames'] += 1
                continue
            out['frames'] += 1
            k = _id_key(cid, ext)
            if k in out['ids'] or len(out['ids']) < 64:
                out['ids'][k] = out['ids'].get(k, 0) + 1
    finally:
        car.close()
    return out


def hear_verdict(h: Dict[str, Any]) -> Tuple[Optional[bool], str]:
    """What a Test answers for ``hear()``'s report, in words."""
    ch, rate = h.get('channel'), h.get('bitrate') or 500000
    setup = f"ip link set {ch} type can bitrate {rate} listen-only on; ip link set {ch} up"
    if h.get('exists') is False:
        return False, (f"no CAN interface '{ch}' here — check its name (ip link); a gateway in a "
                       "container sees only its own network namespace, not the host's CAN "
                       "interfaces")
    if h.get('up') is False:
        return False, f"{ch} exists but is down — on the host: {setup}"
    if h.get('open_error'):
        return False, f"cannot listen on {ch}: {h['open_error']}"
    lo = h.get('listen_only')
    lo_words = ("listen-only on" if lo is True
                else f"NOT listen-only: the controller ACKs frames — set it on the host: {setup}"
                if lo is False else "listen-only: unknown (this interface has no such flag, e.g. vcan)")
    n, secs = h.get('frames') or 0, h.get('seconds')
    if not n:
        return False, (f"{ch} is up ({lo_words}) but silent for {secs:g} s — bus not wired, "
                       f"or not at {rate} bit/s?")
    ids = h.get('ids') or {}
    return True, (f"{ch} up · {lo_words} · heard {n} frames on {len(ids)} ids in {secs:g} s"
                  + (f" · {h['error_frames']} error frames" if h.get('error_frames') else ""))


# the counters a device list and the status page show for a CAN device
SUMMARY_KEYS = ('channel', 'bitrate', 'frames', 'unknown_frames', 'decode_errors',
                'error_frames', 'listen_only', 'open_error')


def stats_summary(stats: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A driver's ``get_stats()['can']`` cut to what a list row shows (the
    unknown ids counted, not listed), or None for a device that is not CAN."""
    c = (stats or {}).get('can')
    if not isinstance(c, dict):
        return None
    out = {k: c.get(k) for k in SUMMARY_KEYS}
    out['unknown_ids'] = len(c.get('unknown_ids') or {})
    return out


def _store_key(r) -> int:
    from .config import store_key
    return store_key(r.address, getattr(r, 'register_type', 'holding') or 'holding')


class CanClient:
    """ModbusClient/MqttInputClient-compatible surface for one CAN device."""

    REOPEN_S = 3.0

    def __init__(self, can_cfg: Dict[str, Any], registers: list, poll_groups: dict = None,
                 stale_after_s: float = 30.0, device_id: str = '', carrier=None):
        self.can_cfg = dict(can_cfg or {})
        self.carrier_name = str(self.can_cfg.get('carrier') or 'socketcan').lower()
        self.channel = str(self.can_cfg.get('channel') or 'can0')
        self.bitrate = self.can_cfg.get('bitrate')
        self.device_id = device_id
        try:
            self.stale_after_s = max(1.0, float(self.can_cfg.get('stale_after_s') or stale_after_s or 30))
        except (TypeError, ValueError):
            self.stale_after_s = 30.0
        self.poll_groups = poll_groups or {}
        self.publish_callback: Optional[Callable] = None
        self._carrier = carrier if carrier is not None else carrier_for(self.can_cfg)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.connected = False
        self.open_error = ''
        # counters, visible in the device's status (get_stats)
        self.frames = 0                # every data frame received
        self.matched_frames = 0        # frames whose id the template knows
        self.failed_frames = 0         # of those, frames a row could not be decoded from
        self.absent_signals = 0        # rows past a shorter frame's end (a BMS without the extension)
        self.listen_only: Optional[bool] = None   # the interface's own flag, read at each open
        self.unknown_frames = 0        # frames on ids the template does not describe
        self.unknown_ids: Dict[str, int] = {}
        self.decode_errors = 0         # a row the frame could not hold (short DLC, bad text)
        self.remote_frames = 0
        self.error_frames = 0          # bus-off / controller error reports
        self.reopens = 0
        self.updates = 0
        self.last_rx_ts: Optional[float] = None
        self.last_rx_mono: Optional[float] = None
        self._counter_filters: Dict[int, Any] = {}
        self._daily_filters: Dict[int, Any] = {}
        self.update_registers(registers, poll_groups)

    # ── the map ──────────────────────────────────────────────────────────────
    def update_registers(self, registers: list, poll_groups: dict = None) -> None:
        by_frame: Dict[Tuple[int, bool], List[Any]] = {}
        for r in registers or []:
            k = frame_key(r)
            if k is not None and getattr(r, 'start_bit', None) is not None \
                    and getattr(r, 'bit_length', None) is not None:
                # with the first byte it needs: a frame that ends before it
                # simply does not carry the signal
                first = min(signal_bits(int(r.start_bit), int(r.bit_length),
                                        str(getattr(r, 'byte_order', '') or 'little').lower())) // 8
                by_frame.setdefault(k, []).append((r, first))
        with self._lock:
            self.registers = list(registers or [])
            self._by_frame = by_frame
            self._counter_filters.clear()
            self._daily_filters.clear()
            if poll_groups is not None:
                self.poll_groups = poll_groups

    def reload_registers(self) -> None:
        """No pollers to bounce — update_registers already took effect."""

    # ── lifecycle ────────────────────────────────────────────────────────────
    def connect(self) -> bool:
        """Start the reader; it opens the interface, and opens it again
        whenever it goes away (down, unplugged, not there yet)."""
        if self._thread is not None:
            return self.connected
        self._stop = threading.Event()
        self._open()
        self._thread = threading.Thread(target=self._run, args=(self._stop,), daemon=True,
                                        name=f"can:{self.channel}:{self.device_id}")
        self._thread.start()
        return self.connected

    def start_polling(self) -> None:
        """Push-driven: the bus sets the rhythm, nothing to start."""

    def disconnect(self) -> None:
        self._stop.set()
        self._carrier.close()
        t, self._thread = self._thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        self.connected = False

    stop = disconnect

    def _open(self) -> bool:
        try:
            self._carrier.open()
        except OSError as e:
            if str(e) != self.open_error:
                logger.warning("CAN %s (%s): cannot listen on %s — %s (trying again every %g s)",
                               self.device_id, self.carrier_name, self.channel, e, self.REOPEN_S)
            self.open_error = str(e)
            self.connected = False
            return False
        if self.open_error:
            logger.info("CAN %s: listening on %s again", self.device_id, self.channel)
        else:
            logger.info("CAN %s: listening on %s (%s, receive only)", self.device_id,
                        self.channel, self.carrier_name)
        self.open_error = ''
        self.connected = True
        if self.carrier_name == 'socketcan':
            self.listen_only = listen_only(self.channel)
            if self.listen_only is False:
                logger.warning("CAN %s: %s is not in listen-only mode — the driver never "
                               "transmits, but the controller still ACKs frames; set it on the "
                               "host: ip link set %s type can bitrate %s listen-only on",
                               self.device_id, self.channel, self.channel, self.bitrate or 500000)
        return True

    def _run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            if not self.connected:
                if stop.wait(self.REOPEN_S):
                    break
                self.reopens += 1
                self._open()
                if stop.is_set() and self._stop is stop:
                    # disconnected while opening: close what was opened
                    self._carrier.close()
                    self.connected = False
                    break
                continue
            try:
                frame = self._carrier.recv()
            except OSError as e:
                if stop.is_set():
                    break
                # the interface went down or away: say it, close, open again
                logger.warning("CAN %s: %s lost — %s", self.device_id, self.channel, e)
                self.open_error = str(e)
                self.connected = False
                self._carrier.close()
                continue
            if frame is not None:
                self.on_frame(*frame)

    # ── the data path ────────────────────────────────────────────────────────
    def on_frame(self, can_id: int, extended: bool, remote: bool, error: bool, data: bytes,
                 now_mono: Optional[float] = None, now_ts: Optional[float] = None) -> None:
        if error:
            self.error_frames += 1
            return
        if remote:
            self.remote_frames += 1
            return
        mono = time.monotonic() if now_mono is None else now_mono
        ts = time.time() if now_ts is None else now_ts
        self.frames += 1
        self.last_rx_mono, self.last_rx_ts = mono, ts
        with self._lock:
            rows = self._by_frame.get((can_id, extended))
        if not rows:
            self.unknown_frames += 1
            k = _id_key(can_id, extended)
            if k in self.unknown_ids or len(self.unknown_ids) < 64:
                self.unknown_ids[k] = self.unknown_ids.get(k, 0) + 1
            return
        self.matched_frames += 1
        out: Dict[int, Dict[str, Any]] = {}
        failed = False
        for r, first in rows:
            if first >= len(data):
                # wholly past this frame's end: the BMS sends the shorter
                # frame (a genuine Pylontech's 7-byte 0x359) — absent, no error
                self.absent_signals += 1
                continue
            try:
                raw = decode_signal(data, r)
            except (ValueError, TypeError, OverflowError, struct.error) as e:
                self.decode_errors += 1
                failed = True
                if self.decode_errors in (1, 10, 100, 1000):
                    logger.warning("CAN %s: 0x%X %s not decoded — %s (%d so far)", self.device_id,
                                   can_id, getattr(r, 'name', '?'), e, self.decode_errors)
                continue
            if isinstance(raw, str):
                out[_store_key(r)] = {'value': raw, 'register': r, 'ts': ts, 'mono': mono}
                continue
            key = _store_key(r)
            cf = df = None
            if getattr(r, 'monotonic', False):
                from .counter_filter import MonotonicFilter
                cf = self._counter_filters.setdefault(key, MonotonicFilter())
            if getattr(r, 'daily', False):
                from .counter_filter import DailyCounterFilter
                df = self._daily_filters.setdefault(key, DailyCounterFilter())
            val = apply_corrections(raw, r, counter_filter=cf, daily_filter=df)
            if val is None:
                continue
            out[key] = {'value': val, 'register': r, 'ts': ts, 'mono': mono}
        if failed:
            self.failed_frames += 1
        if out and self.publish_callback:
            self.updates += len(out)
            try:
                # 'tap': the push group calculated fields evaluate on
                self.publish_callback('tap', out)
            except Exception as e:  # noqa: BLE001 — never kill the reader thread
                logger.error("CAN %s: fan-out failed — %s", self.device_id, e)

    # ── observability ────────────────────────────────────────────────────────
    def alive(self) -> bool:
        return (self.connected and self.last_rx_mono is not None
                and time.monotonic() - self.last_rx_mono <= self.stale_after_s)

    def get_stats(self) -> Dict[str, Any]:
        age = None if self.last_rx_mono is None else round(time.monotonic() - self.last_rx_mono, 1)
        return {
            'connected': self.connected,
            'mode': 'can (listen-only)',
            # both count FRAMES: a frame any row failed on is a failed read
            'successful_reads': self.matched_frames - self.failed_frames,
            'failed_reads': self.failed_frames,
            'updates': self.updates,
            'poll_rate': None,                 # push-driven: the bus's rhythm
            'staleness_age_s': age,
            'last_success_ts': self.last_rx_ts,
            'last_latency_ms': None,
            'error_counts': {'unknown_frames': self.unknown_frames,
                             'decode_errors': self.decode_errors,
                             'error_frames': self.error_frames},
            'can': {
                'carrier': self.carrier_name, 'channel': self.channel,
                'bitrate': self.bitrate, 'frames': self.frames,
                'matched_frames': self.matched_frames, 'unknown_frames': self.unknown_frames,
                'unknown_ids': dict(self.unknown_ids), 'decode_errors': self.decode_errors,
                'failed_frames': self.failed_frames, 'absent_signals': self.absent_signals,
                'listen_only': self.listen_only,
                'remote_frames': self.remote_frames, 'error_frames': self.error_frames,
                'reopens': self.reopens, 'open_error': self.open_error or None,
                'stale_after_s': self.stale_after_s,
            },
            'endpoint': f"{self.carrier_name}://{self.channel}",
        }

    def data_health(self, stale_threshold_s: Optional[float] = None) -> Dict[str, Any]:
        if not self.connected:
            return {'status': 'down', 'stale': True, 'staleness_age_s': None,
                    'reason': self.open_error or 'not listening'}
        if self.last_rx_mono is None:
            return {'status': 'stale', 'stale': True, 'staleness_age_s': None,
                    'reason': 'no frame heard yet'}
        bound = self.stale_after_s if stale_threshold_s is None else stale_threshold_s
        age = time.monotonic() - self.last_rx_mono
        if age > bound:
            return {'status': 'stale', 'stale': True, 'staleness_age_s': round(age, 1),
                    'reason': f'no frame for {int(age)}s'}
        return {'status': 'ok', 'stale': False, 'staleness_age_s': round(age, 1)}
