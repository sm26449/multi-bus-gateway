"""Modbus TCP Client for Janitza UMG 512-PRO."""

import time
import logging
import threading
from collections import deque
from typing import Dict, List, Optional, Callable, Any

from pymodbus.client import ModbusTcpClient, ModbusSerialClient
from pymodbus.exceptions import ModbusException

from . import bus_trace
from .config import ModbusConfig, SelectedRegister, PollGroup
from .register_parser import RegisterParser

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
    """Create a pymodbus client for the configured transport — TCP (host/port)
    or RTU (serial line). One factory so connect() and the per-read reconnect
    stay in sync."""
    if getattr(config, 'protocol', 'tcp') == 'rtu':
        return ModbusSerialClient(
            port=config.serial_port,
            baudrate=int(config.baudrate),
            parity=config.parity,
            stopbits=int(config.stopbits),
            bytesize=int(config.bytesize),
            timeout=config.timeout,
        )
    return ModbusTcpClient(host=config.host, port=config.port,
                           timeout=config.timeout)


def _endpoint(config: ModbusConfig) -> str:
    """Human label for logs/UI."""
    if getattr(config, 'protocol', 'tcp') == 'rtu':
        return f"{config.serial_port}@{config.baudrate} unit {config.unit_id}"
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
        # First-class dropout observability (mirrors VMeterStats.record_event):
        # a timestamped ring of read failures + last success/failure times, so a
        # Janitza comms loss leaves a record in the app, not just docker logs.
        self.events: deque = deque(maxlen=50)
        self.last_success_ts: Optional[float] = None
        self.last_failure_ts: Optional[float] = None
        self.last_latency_ms: Optional[float] = None
        # per-ATTEMPT error taxonomy (an error retried away still happened on
        # the wire): {"timeout": n, "exception_2": n, "connection": n, ...}
        self.error_counts: Dict[str, int] = {}
        self._ev_lock = threading.Lock()

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
        bus_trace.trace.instrument(client, label=self.trace_label,
                                   proto=getattr(self.config, 'protocol', 'tcp'))
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
                       register_type: str = "holding") -> Optional[List[int]]:
        """Read holding (FC3) or input (FC4) registers, thread-safe, with retry."""
        with self.lock:
            for attempt in range(self.config.retry_attempts):
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
                            time.sleep(0.1)
                            continue

                    # Janitza uses 0-based addressing in documentation
                    # but Modbus protocol is 0-indexed, so we use address directly
                    _t0 = time.perf_counter()
                    _read = (self.client.read_input_registers if register_type == "input"
                             else self.client.read_holding_registers)
                    try:
                        result = _read(address=address, count=count, slave=self.config.unit_id)
                    finally:
                        bus_trace.trace.commit(self.client)

                    if not result.isError() and result.registers:
                        self.successful_reads += 1
                        self.last_success_ts = time.time()
                        self.last_latency_ms = round((time.perf_counter() - _t0) * 1000, 1)
                        return result.registers
                    elif result.isError():
                        self._count_error(result)
                        if attempt < self.config.retry_attempts - 1:
                            time.sleep(self.config.retry_delay)

                except Exception as e:
                    logger.debug(f"Read error at address {address}: {e}")
                    self._count_error(e)
                    self.connected = False
                    if attempt < self.config.retry_attempts - 1:
                        time.sleep(self.config.retry_delay)

            self.failed_reads += 1
            self.last_failure_ts = time.time()
            self.record_event("warn", "read_fail",
                              f"addr {address} count {count} — no response after "
                              f"{self.config.retry_attempts} attempts")
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
                  register_type: str = "coil") -> Optional[List[bool]]:
        """Read coils (FC1) or discrete inputs (FC2). Returns a list of bools."""
        with self.lock:
            for attempt in range(self.config.retry_attempts):
                try:
                    if not self._ensure_connected():
                        self._count_error("connect refused")
                        time.sleep(0.1)
                        continue
                    _read = (self.client.read_discrete_inputs if register_type == "discrete"
                             else self.client.read_coils)
                    try:
                        result = _read(address=address, count=count, slave=self.config.unit_id)
                    finally:
                        bus_trace.trace.commit(self.client)
                    if not result.isError():
                        self.successful_reads += 1
                        self.last_success_ts = time.time()
                        return list(result.bits)[:count]
                    self._count_error(result)
                    if attempt < self.config.retry_attempts - 1:
                        time.sleep(self.config.retry_delay)
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"Read-bits error at address {address}: {e}")
                    self._count_error(e)
                    self.connected = False
                    if attempt < self.config.retry_attempts - 1:
                        time.sleep(self.config.retry_delay)
            self.failed_reads += 1
            self.last_failure_ts = time.time()
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
                                                             values=[bool(c) for c in coils], slave=slave)
                        else:
                            v = coils[0] if isinstance(coils, (list, tuple)) else coils
                            result = self.client.write_coil(address=address, value=bool(v), slave=slave)
                    elif register_type == "holding":
                        vals = list(values or [])
                        if len(vals) == 1 and prefer_fc6:
                            result = self.client.write_register(address=address, value=vals[0], slave=slave)
                        else:
                            result = self.client.write_registers(address=address, values=vals, slave=slave)
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
                 publish_callback: Callable, device_id: str = ""):
        # Multi-device: tag the thread + logs with the device so several devices'
        # identically-named poll groups (realtime/normal/slow) are distinguishable.
        super().__init__(daemon=True,
                         name=f"Poller-{device_id}-{name}" if device_id else f"Poller-{name}")
        self.device_id = device_id
        self._tag = f"[{device_id}] " if device_id else ""
        self.poll_group_name = name
        self.interval = interval
        self.registers = registers
        self.connection = connection
        self.parser = parser
        self.publish_callback = publish_callback

        self.running = False
        self._stop_event = threading.Event()

        # Poll rate tracking
        self.poll_count = 0
        self.last_poll_time = None

        # Optimize reads by grouping consecutive addresses
        self._read_groups = self._create_read_groups()

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

        for reg in sorted_regs:
            reg_type = getattr(reg, 'register_type', 'holding')
            # coils/discrete inputs are single bits, not 16-bit registers
            reg_count = 1 if reg_type in ('coil', 'discrete') else \
                self.parser.get_register_count(reg.data_type)

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
                       - current_group['start']) <= MAX_READ):
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
            gtype = group.get('register_type', 'holding')
            read_ts = time.time()   # measurement time — travels with the value

            # coils (FC1) / discrete inputs (FC2): bits, one per address
            if gtype in ('coil', 'discrete'):
                bits = self.connection.read_bits(group['start'], group['count'], gtype)
                if bits is None:
                    logger.warning(f"{self._tag}Failed to read {gtype}s {group['start']}-{group['end']}")
                    continue
                for reg in group['registers']:
                    off = reg.address - group['start']
                    if 0 <= off < len(bits):
                        results[reg.address] = {'value': 1 if bits[off] else 0,
                                                'register': reg, 'ts': read_ts}
                continue

            raw_data = self.connection.read_registers(
                group['start'], group['count'], gtype)

            if raw_data is None:
                logger.warning(f"{self._tag}Failed to read registers {group['start']}-{group['end']}")
                continue

            # Parse each register in this group
            for reg in group['registers']:
                offset = reg.address - group['start']
                reg_count = self.parser.get_register_count(reg.data_type)

                if offset + reg_count <= len(raw_data):
                    reg_values = raw_data[offset:offset + reg_count]
                    value = self.parser.parse_value(reg_values, reg.data_type)
                    if value is not None:
                        # engineering value = raw / scale (SunSpec int+SF meters,
                        # transformer ratios, …). scale defaults to 1.0 so the
                        # Janitza primary is byte-identical.
                        sc = getattr(reg, 'scale', 1.0) or 1.0
                        if sc != 1.0 and isinstance(value, (int, float)):
                            value = value / sc
                        results[reg.address] = {
                            'value': value,
                            'register': reg,
                            'ts': read_ts,
                        }

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

        self.running = True
        reg_addrs = [r.address for r in self.registers]
        logger.info(f"{self._tag}Poller {self.poll_group_name}: started with {len(self.registers)} registers, interval {self.interval}s")
        logger.debug(f"{self._tag}Poller {self.poll_group_name}: addresses {reg_addrs[:10]}...")

        try:
            while self.running:
                try:
                    data = self._poll_registers()

                    if data:
                        self.publish_callback(self.poll_group_name, data)
                        self.poll_count += 1
                        self.last_poll_time = time.time()
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

    def connect(self) -> bool:
        """Connect to Janitza device."""
        self.connected = self.connection.connect()
        return self.connected

    def disconnect(self):
        """Disconnect and stop all pollers."""
        for poller in self.pollers:
            poller.stop()
            poller.join(timeout=5)

        self.connection.disconnect()
        self.connected = False

    def start_polling(self):
        """Start polling threads for each poll group."""
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
                    value, scale: float = 1.0, prefer_fc6: bool = False):
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
            words = RegisterEncoder(self.byte_order).encode(value, data_type, scale)
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
                        value = self.parser.parse_value(reg_values, reg.get('data_type', 'float'))
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

        # Stop all pollers
        for poller in self.pollers:
            poller.stop()
        for poller in self.pollers:
            poller.join(timeout=5)
        self.pollers.clear()

        # Disconnect
        self.connection.disconnect()
        self.connected = False

        # Create new connection with current config
        self.connection = ModbusConnection(self.config, trace_label=self.device_id)

        # Reconnect
        self.connected = self.connection.connect()
        if self.connected:
            self.start_polling()
            logger.info("Modbus reconnected successfully")
        else:
            logger.warning("Modbus reconnection failed")

        return self.connected

    def reload_registers(self):
        """Reload registers and restart pollers without full reconnect."""
        logger.info("Reloading Modbus registers...")

        # Stop all pollers
        for poller in self.pollers:
            poller.stop()
        for poller in self.pollers:
            poller.join(timeout=5)
        self.pollers.clear()

        # Restart pollers with updated registers
        if self.connected:
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
        now = time.time()
        poll_groups_detail = [
            {'name': p.poll_group_name, 'interval': p.interval,
             'last_poll_ts': p.last_poll_time,
             'age_s': round(now - p.last_poll_time, 1) if p.last_poll_time else None,
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
            'staleness_age_s': round(now - last_success, 1) if last_success else None,
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
        now = time.time()
        last = self.connection.last_success_ts
        connected = self.connected
        if not self.registers or not self.pollers:
            return {"status": "ok", "stale": False, "staleness_age_s": None,
                    "last_success_ts": last, "connected": connected}
        fastest = min((p.interval for p in self.pollers if p.running),
                      default=stale_threshold_s)
        threshold = max(float(stale_threshold_s), fastest * 3 + 2)
        if last is None:
            status = "down" if self.connection.last_failure_ts else "ok"
            age = None
        else:
            age = now - last
            if age > threshold:
                status = "down"
            elif age > threshold / 2:
                status = "degraded"
            else:
                status = "ok"
        if not connected and status == "ok":
            status = "degraded"
        return {"status": status, "stale": status != "ok",
                "staleness_age_s": round(age, 1) if age is not None else None,
                "last_success_ts": last, "connected": connected,
                "threshold_s": round(threshold, 1)}
