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
"""Modbus TCP Client for Janitza UMG 512-PRO."""

import time
import logging
import random
import threading
from collections import deque
from typing import Dict, List, Optional, Callable, Any

from pymodbus import FramerType
from pymodbus.client import ModbusTcpClient, ModbusSerialClient

from . import bus_trace
from .config import ModbusConfig, SelectedRegister, PollGroup
from .counter_filter import MonotonicFilter
from .register_parser import RegisterParser
from .value_decode import apply_corrections

# Suppress pymodbus exception logging
logging.getLogger("pymodbus").setLevel(logging.CRITICAL)

logger = logging.getLogger(__name__)


def coil_truthy(v) -> bool:
    """Coerce an API value to a coil bool. Strings '0'/'false'/'off'/'no'/'' and
    0/False/None are OFF; everything else is ON. Plain bool(v) is wrong here —
    it makes the string 'false' truthy, so a caller sending {"value":"false"}
    would switch the coil ON."""
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ('', '0', 'false', 'off', 'no'):
            return False
        try:
            return float(s) != 0.0          # '0.0', '0e0', '-0' … → OFF
        except ValueError:
            return True
    return bool(v)


def _build_client(config: ModbusConfig):
    """Create a pymodbus client for the configured transport:
      - tcp      → ModbusTcpClient (native Modbus/TCP)
      - rtu      → ModbusSerialClient (direct serial line)
      - rtu-tcp  → ModbusTcpClient with the RTU framer (RTU frames tunnelled over
                   a raw TCP socket — talks to a serial-over-TCP bridge like
                   ser2net; host/port point at the bridge endpoint)
    One factory so connect() and the per-read reconnect stay in sync."""
    proto = getattr(config, 'protocol', 'tcp')
    # retries=0: pymodbus' own per-call retry (default 3) MULTIPLIES with the
    # application-level retry_attempts loop in read_registers — on a wedged
    # link that compounded to (1+3)×timeout per call, ~38s per batch, ~76s
    # per 0.25s realtime cycle (audit DP-20). MBG owns the retry policy.
    if proto == 'rtu':
        return ModbusSerialClient(
            port=config.serial_port,
            baudrate=int(config.baudrate),
            parity=config.parity,
            stopbits=int(config.stopbits),
            bytesize=int(config.bytesize),
            timeout=config.timeout,
            retries=0,
        )
    if proto == 'rtu-tcp':
        return ModbusTcpClient(host=config.host, port=config.port,
                               framer=FramerType.RTU, timeout=config.timeout,
                               retries=0)
    return ModbusTcpClient(host=config.host, port=config.port,
                           timeout=config.timeout, retries=0)


def _endpoint(config: ModbusConfig) -> str:
    """Human label for logs/UI."""
    proto = getattr(config, 'protocol', 'tcp')
    if proto == 'rtu':
        return f"{config.serial_port}@{config.baudrate} unit {config.unit_id}"
    if proto == 'rtu-tcp':
        return f"rtu-tcp {config.host}:{config.port} unit {config.unit_id}"
    return f"{config.host}:{config.port}"


def _classify_error(obj) -> str:
    """Sort a failed attempt into the taxonomy an engineer diagnoses by:
    exception_<code> (the slave answered NO), timeout (silence — wiring,
    slave id, dead device), connection (TCP layer), other. pymodbus returns
    ExceptionResponse/ModbusIOException as result objects and raises
    ConnectionException/OSError, so both shapes land here."""
    exc_code = getattr(obj, "exception_code", None)
    if exc_code is not None:
        return f"exception_{exc_code}"
    s = str(obj).lower()
    if "timeout" in s or "no response" in s:
        return "timeout"
    if any(w in s for w in ("connect", "refused", "unreachable", "reset",
                            "broken pipe", "not open", "no route")):
        return "connection"
    return "other"


class ModbusConnection:
    """Modbus connection (TCP or RTU serial) with thread-safe access."""

    def __init__(self, config: ModbusConfig, trace_label: str = ""):
        self.config = config
        self.trace_label = trace_label or _endpoint(config)
        self.client = None
        self.connected = False
        self.lock = threading.Lock()
        self.successful_reads = 0
        self.failed_reads = 0
        # Wedged-link backstop: a serial/PTY port (or a stuck ser2net bridge) can
        # stay "open" while every read errors — is_socket_open() never trips, so
        # the normal reconnect path never reopens it. After this many consecutive
        # failed polls, force a close+reopen regardless. (TCP already self-heals;
        # this is the belt-and-braces for RTU.)
        self._consecutive_fail = 0
        self._reopen_after_fails = 5
        self.forced_reopens = 0
        # Retired connections never touch the wire again (audit DP-5): after a
        # config reconnect swaps in a NEW ModbusConnection, an orphan poller
        # still finishing its cycle would otherwise REOPEN this one on demand —
        # two concurrent clients on one endpoint (catastrophic on RTU, where
        # responses carry no transaction id).
        self._retired = False
        # Failed batches (retry budget exhausted) — the per-batch loss that
        # data_health's connection-level timestamp can't see (audit DP-8).
        self.batch_failures = 0
        # First-class dropout observability (mirrors VMeterStats.record_event):
        # a timestamped ring of read failures + last success/failure times, so a
        # Janitza comms loss leaves a record in the app, not just docker logs.
        self.events: deque = deque(maxlen=50)
        self.last_success_ts: Optional[float] = None
        self.last_success_mono: Optional[float] = None   # step-immune staleness
        self.last_failure_ts: Optional[float] = None
        self.last_latency_ms: Optional[float] = None
        # per-ATTEMPT error taxonomy (an error retried away still happened on
        # the wire): {"timeout": n, "exception_2": n, "connection": n, ...}
        self.error_counts: Dict[str, int] = {}
        self._ev_lock = threading.Lock()
        # Edge-triggered reachability: log ONE warning when the device drops and
        # ONE info when it recovers, staying quiet (debug) while down — a flaky
        # link would otherwise spam a warning per failed batch × poll group.
        self._reachable = True

    def _note_reachable(self) -> None:
        """A successful read — announce recovery once if we were down. Hot path:
        the plain-bool read short-circuits without a lock; only the rare
        transition takes _ev_lock (double-checked, so peer pollers log once)."""
        if self._reachable:
            return
        with self._ev_lock:
            if self._reachable:
                return
            self._reachable = True
        logger.info("Modbus device recovered (%s)", _endpoint(self.config))
        self.record_event("info", "recovered", "device reachable again")

    def _note_unreachable(self, reason: str) -> None:
        """A read that exhausted its retries — announce the drop once, then stay
        quiet (debug) until recovery so an outage doesn't flood the log."""
        if not self._reachable:
            logger.debug("Modbus still unreachable (%s): %s", _endpoint(self.config), reason)
            return
        with self._ev_lock:
            if not self._reachable:
                return
            self._reachable = False
        logger.warning("Modbus device unreachable (%s): %s", _endpoint(self.config), reason)
        self.record_event("warn", "unreachable", reason)

    def _count_error(self, obj) -> None:
        """Tally one failed attempt into the taxonomy. Never raises."""
        try:
            kind = _classify_error(obj)
            with self._ev_lock:
                self.error_counts[kind] = self.error_counts.get(kind, 0) + 1
        except Exception:  # noqa: BLE001
            pass

    def snapshot_errors(self) -> Dict[str, int]:
        with self._ev_lock:
            return dict(self.error_counts)

    def record_event(self, level: str, kind: str, message: str, ts: Optional[float] = None) -> None:
        """Append an acquisition event (level: error|warn|info). Never raises."""
        try:
            with self._ev_lock:
                self.events.append({
                    "ts": round(ts if ts is not None else time.time(), 3),
                    "level": level, "kind": kind, "message": str(message)[:300],
                })
        except Exception:  # noqa: BLE001
            pass

    def snapshot_events(self) -> list:
        """A lock-guarded copy of the event ring. Iterating the deque without the
        lock can raise 'deque mutated during iteration' when a poller thread
        appends at maxlen mid-snapshot."""
        with self._ev_lock:
            return list(self.events)

    def _new_client(self):
        """Build a fresh pymodbus client, wired into the bus-trace monitor."""
        client = _build_client(self.config)
        # bus-trace decodes by WIRE framing, not transport: rtu-tcp puts RTU
        # frames (CRC, no MBAP) on the socket, so trace it as 'rtu'.
        _proto = getattr(self.config, 'protocol', 'tcp')
        _wire = 'rtu' if _proto in ('rtu', 'rtu-tcp') else 'tcp'
        bus_trace.trace.instrument(client, label=self.trace_label, proto=_wire)
        return client

    def connect(self) -> bool:
        """Establish the Modbus connection (TCP or RTU serial)."""
        try:
            if self.client:
                try:
                    self.client.close()
                except Exception:  # noqa: BLE001
                    pass
            self.client = self._new_client()
            self.connected = self.client.connect()
            if self.connected:
                logger.info(f"Modbus connected to {_endpoint(self.config)}")
            return self.connected
        except Exception as e:
            logger.error(f"Modbus connection error: {e}")
            return False

    def disconnect(self):
        """Close Modbus connection."""
        with self.lock:
            if self.client:
                self.client.close()
            self.connected = False
            logger.info("Modbus disconnected")

    def read_registers(self, address: int, count: int,
                       register_type: str = "holding",
                       stop_event=None) -> Optional[List[int]]:
        """Read holding (FC3) or input (FC4) registers, thread-safe, with retry.

        The lock is held only around each attempt's actual bus transaction and
        is RELEASED during the inter-retry backoff sleep — so a slow/failing read
        in one poll group (e.g. 'slow') can't hold the shared connection for its
        whole retry budget and stall a 'realtime' read waiting behind it. Modbus
        transactions still never overlap (each is serialized by the lock); they
        just interleave between retries. Re-checking the socket at the top of
        every attempt makes this safe against a peer thread reconnecting."""
        for attempt in range(self.config.retry_attempts):
            # a retired connection (config reconnect swapped it out) or a
            # stopping poller must not spend the remaining retry budget on the
            # wire (audit DP-5) — bail between attempts, never mid-transaction
            if self._retired or (stop_event is not None and stop_event.is_set()):
                return None
            retry_sleep: Optional[float] = None
            with self.lock:
                try:
                    # Reconnect if needed — close the dead client first so its
                    # socket FD is released now, not whenever GC gets to it.
                    if not self.connected or not self.client.is_socket_open():
                        if self.client:
                            try:
                                self.client.close()
                            except Exception:  # noqa: BLE001
                                pass
                        self.client = self._new_client()
                        self.connected = self.client.connect()
                        if not self.connected:
                            self._count_error("connect refused")
                            # config-controlled, like every other failure path
                            # (was a hardcoded 100 ms — hammered a down device
                            # with connects at 10/s per group)
                            retry_sleep = self.config.retry_delay

                    if retry_sleep is None:
                        # Janitza uses 0-based addressing in documentation
                        # but Modbus protocol is 0-indexed, so we use address directly
                        _t0 = time.perf_counter()
                        _read = (self.client.read_input_registers if register_type == "input"
                                 else self.client.read_holding_registers)
                        try:
                            result = _read(address=address, count=count, device_id=self.config.unit_id)
                        finally:
                            bus_trace.trace.commit(self.client)

                        if (not result.isError() and result.registers
                                and len(result.registers) >= count):
                            self.successful_reads += 1
                            self.last_success_ts = time.time()
                            self.last_success_mono = time.monotonic()
                            self.last_latency_ms = round((time.perf_counter() - _t0) * 1000, 1)
                            self._consecutive_fail = 0
                            self._note_reachable()
                            return result.registers
                        elif result.isError():
                            self._count_error(result)
                            if attempt < self.config.retry_attempts - 1:
                                retry_sleep = self.config.retry_delay
                        else:
                            # short or EMPTY non-error response (audit DP-7):
                            # counting it a success would silently drop the tail
                            # registers — and the old code retried the empty
                            # case with no backoff and no error count. Both now
                            # retry like any other bad read.
                            got = len(result.registers or [])
                            self._count_error(f"short response {got}/{count}")
                            if attempt < self.config.retry_attempts - 1:
                                retry_sleep = self.config.retry_delay

                except Exception as e:
                    logger.debug(f"Read error at address {address}: {e}")
                    self._count_error(e)
                    self.connected = False
                    if attempt < self.config.retry_attempts - 1:
                        retry_sleep = self.config.retry_delay

            if retry_sleep:
                time.sleep(retry_sleep)   # lock RELEASED → a realtime read can slip in

        with self.lock:                        # counter RMW shared across pollers
            self.failed_reads += 1
            self.batch_failures += 1
            self._consecutive_fail += 1
            # a wedged-but-"open" link never trips is_socket_open() → force a
            # close so the next read reopens a fresh client
            _link_down = self._consecutive_fail >= self._reopen_after_fails
            if _link_down and self.client:
                try:
                    self.client.close()
                except Exception:  # noqa: BLE001
                    pass
                self.connected = False
                self.forced_reopens += 1
                self._consecutive_fail = 0
                self.record_event('warn', 'forced_reopen',
                                  f'link wedged — forced reopen after '
                                  f'{self._reopen_after_fails} consecutive failures')
        self.last_failure_ts = time.time()
        # Reachability is a LINK verdict, not a batch verdict (audit DP-8): one
        # chronically-failing batch among healthy ones used to flap
        # unreachable/recovered every cycle, rotating the 50-event ring in ~25s
        # and burying real dropouts. Only a run of consecutive failures long
        # enough to trip the wedge backstop declares the link unreachable; the
        # per-batch loss stays visible via batch_failures + error_counts +
        # per-group staleness in data_health.
        if _link_down:
            self._note_unreachable(f"addr {address} count {count} — no response after "
                                   f"{self.config.retry_attempts} attempts")
        else:
            logger.debug("%sbatch failed addr %s count %s (%d attempts)",
                         f"[{self.trace_label}] ", address, count,
                         self.config.retry_attempts)
        return None

    def _ensure_connected(self) -> bool:
        """(Re)establish the socket; close a dead client first. Caller holds lock."""
        if not self.connected or not (self.client and self.client.is_socket_open()):
            if self.client:
                try:
                    self.client.close()
                except Exception:  # noqa: BLE001
                    pass
            self.client = self._new_client()
            self.connected = self.client.connect()
        return self.connected

    def read_bits(self, address: int, count: int,
                  register_type: str = "coil",
                  stop_event=None) -> Optional[List[bool]]:
        """Read coils (FC1) or discrete inputs (FC2). Returns a list of bools.

        Lock released during the retry backoff sleep — same fairness fix as
        ``read_registers`` so a slow bit-read can't stall another poll group."""
        for attempt in range(self.config.retry_attempts):
            if self._retired or (stop_event is not None and stop_event.is_set()):
                return None                       # audit DP-5, same as read_registers
            retry_sleep: Optional[float] = None
            with self.lock:
                try:
                    if not self._ensure_connected():
                        self._count_error("connect refused")
                        retry_sleep = self.config.retry_delay   # was hardcoded 0.1
                    else:
                        _read = (self.client.read_discrete_inputs if register_type == "discrete"
                                 else self.client.read_coils)
                        try:
                            result = _read(address=address, count=count, device_id=self.config.unit_id)
                        finally:
                            bus_trace.trace.commit(self.client)
                        if not result.isError():
                            self.successful_reads += 1
                            self.last_success_ts = time.time()
                            self.last_success_mono = time.monotonic()
                            self._note_reachable()
                            return list(result.bits)[:count]
                        self._count_error(result)
                        if attempt < self.config.retry_attempts - 1:
                            retry_sleep = self.config.retry_delay
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"Read-bits error at address {address}: {e}")
                    self._count_error(e)
                    self.connected = False
                    if attempt < self.config.retry_attempts - 1:
                        retry_sleep = self.config.retry_delay
            if retry_sleep:
                time.sleep(retry_sleep)   # lock RELEASED → another group can slip in
        # Same failure tail as read_registers (external audit: this path had
        # NO wedge backstop — a coil-only device on a wedged-but-open link
        # never forced a reopen — and declared unreachable on EVERY failed
        # batch, the exact flapping read_registers was cured of in DP-8).
        with self.lock:                    # counter RMW shared across pollers
            self.failed_reads += 1
            self.batch_failures += 1
            self._consecutive_fail += 1
            _link_down = self._consecutive_fail >= self._reopen_after_fails
            if _link_down and self.client:
                try:
                    self.client.close()
                except Exception:  # noqa: BLE001
                    pass
                self.connected = False
                self.forced_reopens += 1
                self._consecutive_fail = 0
                self.record_event('warn', 'forced_reopen',
                                  f'link wedged — forced reopen after '
                                  f'{self._reopen_after_fails} consecutive failures')
        self.last_failure_ts = time.time()
        if _link_down:
            self._note_unreachable(f"bits addr {address} — no response after "
                                   f"{self.config.retry_attempts} attempts")
        else:
            logger.debug("%sbits batch failed addr %s count %s (%d attempts)",
                         f"[{self.trace_label}] ", address, count,
                         self.config.retry_attempts)
        return None

    def write(self, address: int, *, register_type: str = "holding",
              values: Optional[List[int]] = None, coils=None,
              prefer_fc6: bool = False):
        """Write to the device. FC5/FC15 for coils, FC6/FC16 for holding registers.

        These function codes are idempotent (writing the same value twice yields
        the same state), so a single reconnect-and-retry is safe. Returns
        (ok: bool, error: Optional[str]). Never writes input/discrete (read-only).
        """
        with self.lock:
            try:
                if not self._ensure_connected():
                    return False, "not connected"
                slave = self.config.unit_id
                try:
                    if register_type == "coil":
                        if isinstance(coils, (list, tuple)) and len(coils) != 1:
                            result = self.client.write_coils(address=address,
                                                             values=[bool(c) for c in coils], device_id=slave)
                        else:
                            v = coils[0] if isinstance(coils, (list, tuple)) else coils
                            result = self.client.write_coil(address=address, value=bool(v), device_id=slave)
                    elif register_type == "holding":
                        vals = list(values or [])
                        if len(vals) == 1 and prefer_fc6:
                            result = self.client.write_register(address=address, value=vals[0], device_id=slave)
                        else:
                            result = self.client.write_registers(address=address, values=vals, device_id=slave)
                    else:
                        return False, f"{register_type!r} is read-only (only holding/coil are writable)"
                finally:
                    bus_trace.trace.commit(self.client)
                if hasattr(result, "isError") and result.isError():
                    self._count_error(result)
                    return False, str(result)
                return True, None
            except Exception as e:  # noqa: BLE001
                self._count_error(e)
                self.connected = False
                return False, str(e)


class RegisterPoller(threading.Thread):
    """
    Polling thread for a specific poll group.

    Each poll group (realtime, normal, slow) has its own interval and registers.
    """

    def __init__(self, name: str, interval: int, registers: List[SelectedRegister],
                 connection: ModbusConnection, parser: RegisterParser,
                 publish_callback: Callable, device_id: str = "",
                 startup_jitter_s: float = 0.0):
        # Multi-device: tag the thread + logs with the device so several devices'
        # identically-named poll groups (realtime/normal/slow) are distinguishable.
        super().__init__(daemon=True,
                         name=f"Poller-{device_id}-{name}" if device_id else f"Poller-{name}")
        self.device_id = device_id
        self._tag = f"[{device_id}] " if device_id else ""
        self.poll_group_name = name
        # A 0/negative interval would spin the loop with no pause, hammering the
        # bus (and the device) as fast as the transport allows. Clamp to a 50 ms
        # floor as a last-line guard — the API rejects it earlier with a clear
        # error, but any code path is safe here.
        try:
            interval = max(0.05, float(interval))
        except (TypeError, ValueError):
            interval = 5.0
        self.interval = interval
        # first-fire jitter is capped at the interval so it can never delay a
        # group by more than one of its own cycles (a slow-group energy row is
        # never held from the vmeters longer than its cadence already allows)
        try:
            self.startup_jitter_s = max(0.0, min(float(startup_jitter_s), interval))
        except (TypeError, ValueError):
            self.startup_jitter_s = 0.0
        self.registers = registers
        self.connection = connection
        self.parser = parser
        self.publish_callback = publish_callback

        self.running = False
        self._stop_event = threading.Event()

        # Poll rate tracking
        self.poll_count = 0
        self.last_poll_time = None       # wall time (display)
        self.last_poll_mono = None       # monotonic (step-immune age)

        # Optimize reads by grouping consecutive addresses
        self._read_groups = self._create_read_groups()

        # Per-register monotonic guard for cumulative energy counters (opt-in via
        # `monotonic: true`). Keyed by address; re-seeds on the first read after a
        # reload since a fresh poller is built each time. Empty unless a register
        # opts in, so the default poll path is untouched.
        self._counter_filters: Dict[int, MonotonicFilter] = {}
        # enum/bits registers whose decode failed — edge-triggered warn (DP-6)
        self._decode_failed: set = set()

        # Data-readiness gate: drop an all-zero frame (sleepy device) — opt-in.
        self._drop_all_zero = bool(getattr(
            getattr(connection, 'config', None), 'drop_all_zero', False))

    def _create_read_groups(self) -> List[Dict]:
        """
        Group consecutive register addresses for optimized batch reads.

        Returns list of groups with start address, count, and register configs.
        """
        if not self.registers:
            return []

        # Sort by address
        sorted_regs = sorted(self.registers, key=lambda r: r.address)

        groups = []
        current_group = None

        # Modbus caps one read at 125 registers (FC3/FC4). A merged span wider
        # than that would ALWAYS fail (exception from the slave) and silently
        # never populate any register inside it — split instead.
        MAX_READ = 120   # small safety margin under the 125 protocol limit
        # How far apart two registers may sit and still share one batch read.
        # Default 10 (optimize contiguous maps); set modbus.max_gap=0 for strict
        # slaves that reject a read spanning unmapped addresses (exception 02).
        _cfg = getattr(self.connection, 'config', None)
        max_gap = max(0, int(getattr(_cfg, 'max_gap', 10)))
        # addresses the slave rejects (exception 02) — never bridge a merged read
        # across one, and skip a register that sits on one.
        illegal = set(getattr(_cfg, 'illegal_registers', None) or [])

        def _spans_illegal(lo, hi):
            return bool(illegal) and any(a in illegal for a in range(lo, hi))

        for reg in sorted_regs:
            reg_type = getattr(reg, 'register_type', 'holding')
            # coils/discrete inputs are single bits, not 16-bit registers
            reg_count = 1 if reg_type in ('coil', 'discrete') else \
                self.parser.get_register_count(reg.data_type)

            # a selected register whose own span hits an illegal address can't be
            # read at all — drop it (loudly) rather than fail its whole batch
            if _spans_illegal(reg.address, reg.address + reg_count):
                logger.warning(f"{self._tag}skipping {reg.name}@{reg.address}: "
                               f"on the illegal-register skip-list")
                continue

            if current_group is None:
                # Start new group
                current_group = {
                    'start': reg.address,
                    'end': reg.address + reg_count,
                    'register_type': reg_type,
                    'registers': [reg]
                }
            elif (reg_type == current_group['register_type']
                  and reg.address <= current_group['end'] + max_gap
                  and (max(current_group['end'], reg.address + reg_count)
                       - current_group['start']) <= MAX_READ
                  # don't merge if the bridged gap covers an illegal address
                  and not _spans_illegal(current_group['end'], reg.address)):
                # Extend group (same FC type, small gap, within one legal read).
                # Holding (FC3) and input (FC4) share an address space but are
                # distinct blocks — never merge across types.
                current_group['end'] = max(current_group['end'], reg.address + reg_count)
                current_group['registers'].append(reg)
            else:
                # Different type, gap too large, or read would exceed the limit
                groups.append(current_group)
                current_group = {
                    'start': reg.address,
                    'end': reg.address + reg_count,
                    'register_type': reg_type,
                    'registers': [reg]
                }

        if current_group:
            groups.append(current_group)

        # Add count to each group
        for g in groups:
            g['count'] = g['end'] - g['start']

        return groups

    def _poll_registers(self) -> Dict[int, Any]:
        """
        Poll all registers in this group.

        Returns dict mapping address -> parsed value.
        """
        results = {}

        for group in self._read_groups:
            # a stop() must end the cycle HERE, not after n_groups × the retry
            # budget (audit DP-5 — this lingering window is what produced
            # orphan pollers at config reconnect)
            if self._stop_event.is_set():
                return results
            gtype = group.get('register_type', 'holding')
            read_ts = time.time()   # measurement time — travels with the value
            read_mono = time.monotonic()  # monotonic pair for step-immune freshness

            # coils (FC1) / discrete inputs (FC2): bits, one per address
            if gtype in ('coil', 'discrete'):
                bits = self.connection.read_bits(group['start'], group['count'], gtype,
                                                 stop_event=self._stop_event)
                if bits is None:
                    # per-group detail is DEBUG; the device-level edge warning
                    # (_note_unreachable) is the one-line "device down" signal
                    logger.debug(f"{self._tag}Failed to read {gtype}s {group['start']}-{group['end']}")
                    continue
                for reg in group['registers']:
                    off = reg.address - group['start']
                    if 0 <= off < len(bits):
                        results[reg.address] = {'value': 1 if bits[off] else 0,
                                                'register': reg, 'ts': read_ts,
                                                'mono': read_mono,
                                                'interval': self.interval}
                continue

            raw_data = self.connection.read_registers(
                group['start'], group['count'], gtype,
                stop_event=self._stop_event)

            if raw_data is None:
                logger.debug(f"{self._tag}Failed to read registers {group['start']}-{group['end']}")
                continue

            # Parse each register in this group
            for reg in group['registers']:
                offset = reg.address - group['start']
                reg_count = self.parser.get_register_count(reg.data_type)

                if offset + reg_count <= len(raw_data):
                    reg_values = raw_data[offset:offset + reg_count]
                    # sentinel/enum/scale/offset/monotonic all live in the
                    # SHARED pipeline now (apply_corrections — this exact
                    # sequence used to exist here inline and in five drifted
                    # copies elsewhere); the parser only parses.
                    value = self.parser.parse_value(reg_values, reg.data_type)
                    if value is not None:
                        f = None
                        if getattr(reg, 'monotonic', False):
                            f = self._counter_filters.get(reg.address)
                            if f is None:
                                f = self._counter_filters[reg.address] = MonotonicFilter()
                        _info: Dict[str, str] = {}
                        value = apply_corrections(value, reg, counter_filter=f,
                                                  info=_info)
                        if value is None:
                            _stage = _info.get('stage')
                            if _stage == 'decode_failed':
                                # corrupt enum/bits config (audit DP-6): hold
                                # last-good and warn once per register
                                if reg.address not in self._decode_failed:
                                    self._decode_failed.add(reg.address)
                                    logger.warning(
                                        "%s%s@%s: enum/bits decode failed — "
                                        "holding last-good (check mask/shift)",
                                        self._tag, reg.name, reg.address)
                            elif _stage == 'filter_drop':
                                logger.debug(f"{self._tag}{reg.name}@{reg.address}: "
                                             f"dropped counter glitch (held)")
                            continue          # sentinel/decode/filter → missing
                        if getattr(reg, 'enum', None) or getattr(reg, 'bits', None):
                            self._decode_failed.discard(reg.address)
                        if f is not None and f.just_reset:
                            # the one transition that used to leave no trace
                            # (audit DP-10) — a wrongly-adopted baseline
                            # poisons every downstream delta
                            logger.warning(
                                f"{self._tag}{reg.name}@{reg.address}: "
                                f"counter RESET adopted (new baseline "
                                f"{value}) after {f.reset_confirm} "
                                f"coherent low reads")
                        results[reg.address] = {
                            'value': value,
                            'register': reg,
                            'ts': read_ts,
                            'mono': read_mono,
                            # the poll cadence travels with the value — the
                            # vmeter derives a per-row freshness bound from it
                            'interval': self.interval,
                        }

        # Data-readiness gate: a device that answers a read while asleep (an
        # inverter at night) returns ALL zeros; published that reads as real
        # 0 V/0 W. Drop the frame (cache keeps last-good) when every numeric
        # value is exactly zero — requiring >=2 so a lone legitimate 0 isn't
        # withheld (voltage/frequency are never 0 on an awake device, so an
        # all-zero measurement frame is the asleep signature).
        if self._drop_all_zero and results:
            nums = [d['value'] for d in results.values()
                    if isinstance(d['value'], (int, float)) and not isinstance(d['value'], bool)]
            if len(nums) >= 2 and all(v == 0 for v in nums):
                logger.debug(f"{self._tag}{self.poll_group_name}: all-zero frame "
                             f"dropped (device not ready)")
                return {}

        return results

    def run(self):
        # Create event loop for this thread (required by pymodbus 3.x)
        import asyncio
        created_loop = False
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            created_loop = True

        # The STOP EVENT is the single source of truth (audit 2026-08-14 M1).
        # A stop() that lands between Thread.start() and this line used to be
        # overwritten by `running = True` — and with the event already set,
        # every `wait(interval)` returned instantly: an unkillable zombie
        # poller hammering the bus back-to-back through the OLD callback.
        if self._stop_event.is_set():
            logger.info(f"{self._tag}Poller {self.poll_group_name}: stopped before first poll")
            return
        self.running = True                    # observability mirror only
        reg_addrs = [r.address for r in self.registers]
        logger.info(f"{self._tag}Poller {self.poll_group_name}: started with {len(self.registers)} registers, interval {self.interval}s")
        logger.debug(f"{self._tag}Poller {self.poll_group_name}: addresses {reg_addrs[:10]}...")

        try:
            # Stagger the FIRST read by a random fraction of the interval so
            # several devices/groups don't fire in lock-step and collide on a
            # shared transport (RTU-over-TCP bridge) at boot. Interruptible: a
            # stop() during the wait sets the event, so the loop won't run.
            if self.startup_jitter_s > 0:
                delay = random.uniform(0, self.startup_jitter_s)
                logger.debug(f"{self._tag}Poller {self.poll_group_name}: startup jitter {delay:.2f}s")
                self._stop_event.wait(delay)
            while not self._stop_event.is_set():
                try:
                    data = self._poll_registers()

                    # a disconnect() during a slow read sets the event — don't
                    # publish a late batch (decoded against a possibly-old map)
                    # into the store the vmeters serve
                    if self._stop_event.is_set():
                        break
                    if data:
                        self.publish_callback(self.poll_group_name, data)
                        self.poll_count += 1
                        self.last_poll_time = time.time()
                        self.last_poll_mono = time.monotonic()
                        logger.debug(f"{self._tag}Poller {self.poll_group_name}: read {len(data)} values")

                except Exception as e:
                    import traceback
                    logger.error(f"{self._tag}Poller {self.poll_group_name} error: {e}\n{traceback.format_exc()}")

                # Sleep for interval
                self._stop_event.wait(self.interval)
        finally:
            # Close the loop we created so its FDs don't leak — every register
            # reload spawns fresh poller threads, each with a fresh event loop.
            if created_loop:
                try:
                    loop.close()
                except Exception:  # noqa: BLE001
                    pass
            self.running = False               # mirror follows the event
            logger.info(f"{self._tag}Poller {self.poll_group_name}: stopped")

    def stop(self):
        self.running = False
        self._stop_event.set()


class ModbusClient:
    """
    Main Modbus client for Janitza UMG 512-PRO.

    Features:
    - Multiple poll groups with different intervals
    - Optimized batch reads for consecutive registers
    - Thread-safe connection sharing
    - Automatic reconnection
    """

    def __init__(self, config: ModbusConfig, registers: List[SelectedRegister],
                 poll_groups: Dict[str, PollGroup], publish_callback: Callable = None,
                 byte_order: str = "big", device_id: str = ""):
        """
        Initialize Modbus client.

        Args:
            config: Modbus connection configuration
            registers: List of registers to poll
            poll_groups: Dict of poll group configurations
            publish_callback: Callback for publishing data (poll_group, data)
            byte_order: word/byte order for decoding (big/little/badc/dcba). From
                the device template's protocol.byte_order; default big (Janitza).
            device_id: owning device id, tagged into poller thread names + logs
                (multi-device disambiguation). Empty for the legacy single-device.
        """
        self.config = config
        self.registers = registers
        self.poll_groups = poll_groups
        self.device_id = device_id
        self.publish_callback = publish_callback or (lambda *args: None)

        self.byte_order = byte_order
        self.parser = RegisterParser(byte_order)
        self.connection = ModbusConnection(config, trace_label=device_id)

        self.pollers: List[RegisterPoller] = []
        self.connected = False
        # Serializes start_polling / reconnect / reload_registers / disconnect
        # (external audit): two concurrent Applies used to interleave the
        # stop-clear-start sequences and leave a DOUBLED poller set (every
        # group polled twice — twice the bus load, interleaved timestamps).
        # RLock: reconnect() calls start_polling() while holding it.
        self._lifecycle_lock = threading.RLock()

    def connect(self) -> bool:
        """Connect to Janitza device."""
        self.connected = self.connection.connect()
        return self.connected

    def disconnect(self):
        """Disconnect and stop all pollers."""
        with self._lifecycle_lock:
            for poller in self.pollers:
                poller.stop()
            for poller in self.pollers:
                poller.join(timeout=5)
            self.pollers.clear()
            self.connection.disconnect()
            self.connected = False

    def start_polling(self):
        """Start polling threads for each poll group. Serialized + idempotent:
        a second call (boot racing an Apply) stops the existing set first —
        appending blindly would poll every group twice."""
        with self._lifecycle_lock:
            self._start_polling_locked()

    def _start_polling_locked(self):
        if self.pollers:
            logger.warning("start_polling called with %d pollers already running "
                           "— stopping them first (double-start guard)",
                           len(self.pollers))
            for poller in self.pollers:
                poller.stop()
            for poller in self.pollers:
                poller.join(timeout=5)
            self.pollers.clear()
        # Group registers by poll group
        registers_by_group: Dict[str, List[SelectedRegister]] = {}
        for reg in self.registers:
            group_name = reg.poll_group
            if group_name not in registers_by_group:
                registers_by_group[group_name] = []
            registers_by_group[group_name].append(reg)

        # Create poller for each group with registers
        for group_name, regs in registers_by_group.items():
            if group_name not in self.poll_groups:
                logger.warning(f"Unknown poll group: {group_name}, using 'normal'")
                group_name = 'normal'

            group_config = self.poll_groups.get(group_name)
            if not group_config:
                continue

            poller = RegisterPoller(
                name=group_name,
                interval=group_config.interval,
                registers=regs,
                connection=self.connection,
                parser=self.parser,
                publish_callback=self.publish_callback,
                device_id=self.device_id,
                startup_jitter_s=getattr(self.config, 'startup_jitter_s', 0.0),
            )
            poller.start()
            self.pollers.append(poller)

        logger.info(f"Started {len(self.pollers)} polling threads")

    def read_register(self, address: int, data_type: str = 'float',
                      register_type: str = 'holding') -> Optional[Any]:
        """
        Read a single register (for on-demand queries).

        Args:
            address: Register address
            data_type: Data type for parsing
            register_type: 'holding' (FC3) or 'input' (FC4)

        Returns:
            Parsed value or None
        """
        if register_type in ("coil", "discrete"):
            bits = self.connection.read_bits(address, 1, register_type)
            return bool(bits[0]) if bits else None
        count = self.parser.get_register_count(data_type)
        raw_data = self.connection.read_registers(address, count, register_type)

        if raw_data:
            return self.parser.parse_value(raw_data, data_type)
        return None

    def write_value(self, address: int, register_type: str, data_type: str,
                    value, scale: float = 1.0, offset: float = 0.0,
                    prefer_fc6: bool = False):
        """Encode `value` and write it. Holding → RegisterEncoder (FC6/FC16),
        respecting the device's byte order and scale; coil → a boolean (FC5).
        Returns (ok, error, written_words). Input/discrete are read-only.
        """
        if register_type == "coil":
            on = coil_truthy(value)
            ok, err = self.connection.write(address, register_type="coil", coils=on)
            return ok, err, [1 if on else 0]
        if register_type != "holding":
            return False, f"{register_type!r} registers are read-only", None
        from .encoder import RegisterEncoder
        try:
            words = RegisterEncoder(self.byte_order).encode(value, data_type, scale,
                                                            offset=offset)
        except Exception as e:  # noqa: BLE001
            return False, f"encode failed: {e}", None
        ok, err = self.connection.write(address, register_type="holding",
                                        values=words, prefer_fc6=prefer_fc6)
        return ok, err, words

    def read_registers_batch(self, registers: List[Dict]) -> Dict[int, Any]:
        """
        Read multiple registers in optimized batches.

        Args:
            registers: List of dicts with 'address' and 'data_type'

        Returns:
            Dict mapping address -> parsed value
        """
        if not registers:
            return {}

        # Sort by address
        sorted_regs = sorted(registers, key=lambda r: r['address'])

        results = {}
        current_batch_start = None
        current_batch_end = None
        current_batch_type = 'holding'
        current_batch_regs = []

        def flush_batch():
            nonlocal current_batch_start, current_batch_end, current_batch_regs
            if current_batch_start is None:
                return

            count = current_batch_end - current_batch_start
            raw_data = self.connection.read_registers(current_batch_start, count, current_batch_type)

            if raw_data:
                for reg in current_batch_regs:
                    offset = reg['address'] - current_batch_start
                    reg_count = self.parser.get_register_count(reg.get('data_type', 'float'))
                    if offset + reg_count <= len(raw_data):
                        reg_values = raw_data[offset:offset + reg_count]
                        value = self.parser.parse_value(reg_values, reg.get('data_type', 'float'),
                                                        nan=reg.get('nan'))
                        if value is not None:
                            results[reg['address']] = value

            current_batch_start = None
            current_batch_end = None
            current_batch_regs = []

        # Group into batches (never merge holding + input into one read)
        for reg in sorted_regs:
            reg_count = self.parser.get_register_count(reg.get('data_type', 'float'))
            reg_end = reg['address'] + reg_count
            reg_type = reg.get('register_type', 'holding')

            if current_batch_start is None:
                current_batch_start = reg['address']
                current_batch_end = reg_end
                current_batch_type = reg_type
                current_batch_regs = [reg]
            elif (reg_type == current_batch_type
                    and reg['address'] <= current_batch_end + 10
                    and (max(current_batch_end, reg_end) - current_batch_start) <= 125):
                # Extend batch — but never past the Modbus 125-register-per-read
                # limit (matches the poll-group grouping); over-long reads are
                # rejected wholesale by many devices.
                current_batch_end = max(current_batch_end, reg_end)
                current_batch_regs.append(reg)
            else:
                # Flush and start new
                flush_batch()
                current_batch_start = reg['address']
                current_batch_end = reg_end
                current_batch_type = reg_type
                current_batch_regs = [reg]

        flush_batch()
        return results

    def update_config(self, new_config: ModbusConfig):
        """Update Modbus configuration."""
        self.config = new_config
        self.connection.config = new_config
        logger.info(f"Modbus config updated: {new_config.host}:{new_config.port}")

    def update_registers(self, registers: List[SelectedRegister], poll_groups: Dict[str, PollGroup]):
        """Update register list and poll groups."""
        self.registers = registers
        self.poll_groups = poll_groups
        logger.info(f"Modbus registers updated: {len(registers)} registers")

    def reconnect(self) -> bool:
        """
        Reconnect to Modbus device with current config.
        Stops pollers, disconnects, reconnects, and restarts pollers.
        """
        logger.info("Modbus reconnecting...")
        self._lifecycle_lock.acquire()
        try:
            return self._reconnect_locked()
        finally:
            self._lifecycle_lock.release()

    def _reconnect_locked(self) -> bool:
        # Stop all pollers. With the stop event now checked between read-groups
        # AND between retry attempts (audit DP-5), a poller exits within one
        # in-flight transaction — but RETIRE the old connection regardless: a
        # join that still times out must never leave an orphan able to REOPEN
        # the old client and run a second concurrent Modbus master on the
        # same endpoint (no transaction ids on RTU — responses would match
        # the wrong request).
        for poller in self.pollers:
            poller.stop()
        for poller in self.pollers:
            poller.join(timeout=5)
            if poller.is_alive():
                logger.warning("%spoller %s still finishing after stop — old "
                               "connection retired, it cannot touch the wire",
                               f"[{self.device_id}] " if self.device_id else "",
                               poller.poll_group_name)
        self.pollers.clear()

        # Disconnect + retire: read/write paths refuse a retired connection.
        self.connection._retired = True
        self.connection.disconnect()
        self.connected = False

        # Create new connection with current config
        self.connection = ModbusConnection(self.config, trace_label=self.device_id)

        # Reconnect. Start pollers UNCONDITIONALLY: they reconnect on demand
        # inside read_registers, so gating start_polling on the immediate
        # connect would leave the device permanently unpolled after a momentary
        # blip at save/apply/restore time (the boot path self-heals the same
        # way). The return value is only the immediate-connect report.
        self.connected = self.connection.connect()
        self.start_polling()
        if self.connected:
            logger.info("Modbus reconnected successfully")
        else:
            logger.warning("Modbus immediate reconnect failed — pollers will retry")

        return self.connected

    def reload_registers(self):
        """Reload registers and restart pollers without full reconnect."""
        logger.info("Reloading Modbus registers...")
        with self._lifecycle_lock:
            for poller in self.pollers:
                poller.stop()
            for poller in self.pollers:
                poller.join(timeout=5)
            self.pollers.clear()
            # Restart pollers unconditionally (they reconnect on demand) — a
            # device briefly unreachable at reload time must not be left
            # unpolled forever.
            self.start_polling()
        logger.info("Modbus registers reloaded")

    def get_stats(self) -> Dict:
        """Return client statistics."""
        # Calculate poll rate (polls per second across all pollers)
        total_polls = sum(p.poll_count for p in self.pollers)

        # Calculate theoretical poll rate based on intervals
        # Each poller contributes 1/interval polls per second
        poll_rate = 0.0
        for poller in self.pollers:
            if poller.running and poller.interval > 0:
                poll_rate += 1.0 / poller.interval

        last_success = self.connection.last_success_ts
        last_success_mono = self.connection.last_success_mono
        now_mono = time.monotonic()      # ages are monotonic (step-immune)
        poll_groups_detail = [
            {'name': p.poll_group_name, 'interval': p.interval,
             'last_poll_ts': p.last_poll_time,
             'age_s': (round(now_mono - p.last_poll_mono, 1)
                       if getattr(p, 'last_poll_mono', None) else None),
             'poll_count': p.poll_count}
            for p in self.pollers]

        return {
            'connected': self.connected,
            'host': self.config.host,
            'port': self.config.port,
            'unit_id': self.config.unit_id,
            'successful_reads': self.connection.successful_reads,
            'failed_reads': self.connection.failed_reads,
            'errors': self.connection.failed_reads,
            'error_counts': self.connection.snapshot_errors(),
            'poll_groups': len(self.pollers),
            'total_registers': len(self.registers),
            'total_polls': total_polls,
            'poll_rate': round(poll_rate, 2),
            'last_success_ts': last_success,
            'last_failure_ts': self.connection.last_failure_ts,
            'last_latency_ms': self.connection.last_latency_ms,
            'staleness_age_s': (round(time.monotonic() - last_success_mono, 1)
                                if last_success_mono else None),
            'poll_groups_detail': poll_groups_detail,
            'events': self.connection.snapshot_events(),
        }

    def data_health(self, stale_threshold_s: float = 30) -> Dict:
        """Acquisition-pipeline health: ok | degraded | down.

        Staleness uses the connection's last successful read (driven by the
        FASTEST poll group). The effective threshold is raised to >= 3x the
        fastest poll interval so a slow-only config can't false-positive. Returns
        ``ok`` when nothing is configured to poll, and stays ``ok`` on cold start
        until a read has actually failed (avoids a false 'down' right after boot)."""
        last = self.connection.last_success_ts
        last_mono = self.connection.last_success_mono   # step-immune staleness
        connected = self.connected
        if not self.registers or not self.pollers:
            return {"status": "ok", "stale": False, "staleness_age_s": None,
                    "last_success_ts": last, "connected": connected}
        fastest = min((p.interval for p in self.pollers if p.running),
                      default=stale_threshold_s)
        threshold = max(float(stale_threshold_s), fastest * 3 + 2)
        if last_mono is None:
            status = "down" if self.connection.last_failure_ts else "ok"
            age = None
        else:
            age = time.monotonic() - last_mono          # NTP-step-proof
            if age > threshold:
                status = "down"
            elif age > threshold / 2:
                status = "degraded"
            else:
                status = "ok"
        if not connected and status == "ok":
            status = "degraded"
        # Per-group staleness (audit DP-8): the connection timestamp is driven
        # by the FASTEST group, so one chronically-failing batch/group was
        # invisible — health said ok while a whole group's registers were
        # frozen. A group that has polled before but not produced data for
        # 3× its own interval (+2s slack) degrades the verdict.
        stale_groups = []
        for p in self.pollers:
            _mono = getattr(p, 'last_poll_mono', None)
            if not p.running or _mono is None:
                continue
            g_age = time.monotonic() - _mono
            if g_age > p.interval * 3 + 2:
                stale_groups.append({"group": p.poll_group_name,
                                     "age_s": round(g_age, 1),
                                     "interval_s": p.interval})
        if stale_groups and status == "ok":
            status = "degraded"
        return {"status": status, "stale": status != "ok",
                "staleness_age_s": round(age, 1) if age is not None else None,
                "last_success_ts": last, "connected": connected,
                "threshold_s": round(threshold, 1),
                "stale_groups": stale_groups,
                "batch_failures": self.connection.batch_failures}
