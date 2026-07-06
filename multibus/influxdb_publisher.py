"""InfluxDB Publisher for Janitza UMG 512-PRO with change detection and custom measurements."""

import json
import math
import os
import re
import time
import threading
from collections import deque
from typing import Dict, Any, Optional, List

from .config import InfluxDBConfig, SelectedRegister

import logging
logger = logging.getLogger(__name__)

# Retry configuration
RETRY_MAX_ATTEMPTS = 10
RETRY_INITIAL_DELAY = 2
RETRY_MAX_DELAY = 60
RETRY_BACKOFF_FACTOR = 2
RECONNECT_CHECK_INTERVAL = 30


class InfluxDBPublisher:
    """
    InfluxDB Publisher for Janitza data.

    Features:
    - Custom measurement name per register
    - Custom tags per register
    - Two-phase cache: check before write, confirm after success
    - NaN/Infinity guard to protect InfluxDB batches
    - Proactive health checks via ping()
    - Batched writes with error/retry callbacks
    - Automatic reconnection (background — never blocks application boot)
    - Points are stamped with the Modbus poll time, not the flush time
    - Store-and-forward: points that cannot be delivered (InfluxDB down, batch
      retries exhausted) go to a bounded RAM buffer and are replayed with their
      original timestamps on reconnect. InfluxDB dedupes on (measurement, tags,
      timestamp), so replay is idempotent — no duplicates by construction.
    """

    def __init__(self, config: InfluxDBConfig, registers: List[SelectedRegister],
                 publish_mode: str = 'changed'):
        self.config = config
        self.registers = registers
        self.publish_mode = publish_mode

        self.client = None
        self.write_api = None
        self._connected = threading.Event()
        self.last_values: Dict[int, Dict] = {}
        self.last_write_time: Dict[int, float] = {}
        # lock guards the client/write_api REFERENCES only — it must never be
        # held across network I/O, or the Modbus poller threads (which take it
        # per point in the hot path) stall behind a slow reconnect, the live
        # cache goes stale and the virtual meters drop their consumers.
        self.lock = threading.Lock()
        # the change-detection cache gets its own lock so the hot path never
        # contends with client lifecycle at all
        self._cache_lock = threading.Lock()

        # Build register lookup by address
        self._register_map: Dict[int, SelectedRegister] = {
            r.address: r for r in registers if r.influxdb_enabled
        }

        # Stats
        self.writes_total = 0
        self.last_write_ts = None
        self.writes_failed = 0
        self.writes_skipped = 0
        self.disconnection_count = 0

        # Store-and-forward replay buffer: (poll_epoch_s, bucket, line_protocol)
        # tuples, bounded by age (buffer_minutes) and count (buffer_max_points).
        self._buffer: deque = deque()
        self._buf_lock = threading.Lock()
        self.points_buffered = 0
        self.points_replayed = 0
        self.points_dropped = 0
        self.points_recovered = 0        # loaded from disk at boot

        # Optional on-disk persistence so the buffer survives a restart during
        # an outage. Snapshot file lives next to config.yaml.
        self._persist_path = None
        self._persist_dirty = False
        if getattr(config, 'buffer_persist', False):
            from pathlib import Path
            self._persist_path = Path(os.environ.get(
                'INFLUX_BUFFER_PATH', 'config/influx_buffer.jsonl'))
            self._load_persisted_buffer()

        # Reconnection thread
        self._stop_reconnect = threading.Event()
        self._reconnect_thread = None

        if config.enabled:
            # Non-blocking startup: the monitor thread performs the first
            # connect (and every reconnect) in the background, so a down
            # InfluxDB can never stall application boot. Points produced
            # before the first connect land in the replay buffer.
            self._start_reconnect_thread()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @connected.setter
    def connected(self, value: bool):
        if value:
            self._connected.set()
        else:
            if self._connected.is_set():
                self.disconnection_count += 1
            self._connected.clear()

    def _on_write_error(self, conf, data, exception):
        """Callback when an InfluxDB batch write fails permanently (all client
        retries exhausted). The batch is NOT lost: its line-protocol payload is
        recovered into the replay buffer (tagged with the batch's own bucket)
        and re-delivered on reconnect."""
        self.writes_failed += 1
        # conf is the client's batch key: (bucket, org, precision)
        bucket = None
        try:
            if isinstance(conf, (list, tuple)) and conf:
                bucket = conf[0]
        except Exception:  # noqa: BLE001
            pass
        recovered = self._rebuffer_batch(data, bucket=bucket)
        logger.error(f"InfluxDB batch failed permanently ({exception}) — "
                     f"recovered {recovered} points into the replay buffer")
        self._handle_write_error(exception)

    def _rebuffer_batch(self, data, bucket: Optional[str] = None) -> int:
        """Recover a failed batch (bytes/str/list of line protocol) into the
        replay buffer, preserving each point's own timestamp. Never raises."""
        try:
            if isinstance(data, bytes):
                data = data.decode('utf-8', 'replace')
            if isinstance(data, str):
                lines = data.splitlines()
            elif isinstance(data, (list, tuple)):
                lines = [str(l) for l in data]
            else:
                lines = [str(data)]
        except Exception:  # noqa: BLE001
            return 0
        now = time.time()
        recovered = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                # trailing token of a line-protocol record is the ns timestamp
                ts = int(line.rsplit(' ', 1)[1]) / 1e9
            except Exception:  # noqa: BLE001
                ts = now
            self._buffer_line(line, ts, bucket=bucket)
            recovered += 1
        return recovered

    def _on_write_retry(self, conf, data, exception):
        """Callback when InfluxDB batch write is being retried."""
        logger.warning(f"InfluxDB write retry: {exception}")

    def _setup_client(self):
        """Setup InfluxDB client with proper batching and error callbacks.

        All network I/O (ping, bucket check) happens WITHOUT holding self.lock;
        only the final reference swap is locked. Holding the lock across a slow
        connect (DNS timeouts while the server is down take seconds per try)
        would stall every Modbus poller behind it — observed live as the
        virtual meters dropping their consumers mid-reconnect."""
        try:
            from influxdb_client import InfluxDBClient, WriteOptions

            new_client = InfluxDBClient(
                url=self.config.url,
                token=self.config.token,
                org=self.config.org,
                timeout=10_000,
            )

            # Test connection with ping() (replaces deprecated health())
            if not new_client.ping():
                logger.warning("InfluxDB ping failed")
                try:
                    new_client.close()
                except Exception:  # noqa: BLE001
                    pass
                return

            new_write_api = new_client.write_api(
                write_options=WriteOptions(
                    batch_size=100,
                    flush_interval=10_000,
                    jitter_interval=2_000,
                    retry_interval=5_000,
                    max_retries=10,
                    max_retry_time=300_000,
                    exponential_base=2,
                ),
                error_callback=self._on_write_error,
                success_callback=None,
                retry_callback=self._on_write_retry,
            )

            with self.lock:                       # swap references only
                old_write_api, old_client = self.write_api, self.client
                self.client, self.write_api = new_client, new_write_api

            self.connected = True
            logger.info(f"InfluxDB connected to {self.config.url}")
            self._ensure_bucket()                 # auto-create if missing

            # Retire old objects after the swap, outside the lock: closing a
            # batching write_api can block while it flushes/abandons retries
            # (its dead batches come back through _on_write_error → buffer).
            for obj in (old_write_api, old_client):
                if obj:
                    try:
                        obj.close()
                    except Exception:  # noqa: BLE001
                        pass

        except ImportError:
            logger.warning("influxdb-client not installed. Install with: pip install influxdb-client")
            self.config.enabled = False
        except Exception as e:
            logger.warning(f"InfluxDB connection failed: {e}")
            self.connected = False

    def _ensure_bucket(self):
        """Auto-create the default bucket if it doesn't exist."""
        self.ensure_bucket(self.config.bucket)

    def ensure_bucket(self, name: str) -> bool:
        """Ensure an InfluxDB bucket exists (create with 90d retention if not).
        Used for per-device buckets (Tier 2) so a new device's history/energy
        works without manual setup. Non-fatal; returns True if present/created."""
        name = str(name or "").strip()
        if not name:
            return False
        if not (self.config.enabled and self.client):
            return False
        try:
            buckets_api = self.client.buckets_api()
            if buckets_api.find_bucket_by_name(name):
                return True
            from influxdb_client import BucketRetentionRules
            retention = BucketRetentionRules(type="expire", every_seconds=90 * 86400)
            org = self.client.organizations_api().find_organizations(org=self.config.org)[0]
            buckets_api.create_bucket(bucket_name=name, retention_rules=retention, org_id=org.id)
            logger.info(f"InfluxDB bucket '{name}' created (90d retention)")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Bucket auto-create failed for '{name}' (non-fatal): {e}")
            return False

    # ── on-disk persistence (survive a restart mid-outage) ─────────────────
    def _load_persisted_buffer(self) -> None:
        """Load a buffer snapshot written by a previous run, honoring age
        bounds. Never raises (persistence must not break boot)."""
        try:
            p = self._persist_path
            if not p or not p.exists():
                return
            cutoff = time.time() - getattr(self.config, 'buffer_minutes', 10) * 60
            loaded = 0
            with open(p, encoding='utf-8') as f:
                for row in f:
                    row = row.strip()
                    if not row:
                        continue
                    try:
                        ts, bucket, line = json.loads(row)
                    except Exception:  # noqa: BLE001
                        continue
                    if ts < cutoff:
                        continue
                    self._buffer.append((float(ts), bucket, line))
                    loaded += 1
            self._prune_buffer_locked()
            self.points_recovered = len(self._buffer)
            logger.info("InfluxDB replay buffer restored from disk: %d points "
                        "(of %d on file)", len(self._buffer), loaded)
        except Exception as e:  # noqa: BLE001
            logger.warning("InfluxDB buffer restore failed: %s", e)

    def _persist_buffer(self) -> None:
        """Atomically snapshot the buffer to disk (temp file + rename). Removes
        the file when the buffer is empty. Never raises."""
        if not self._persist_path:
            return
        try:
            with self._buf_lock:
                snapshot = list(self._buffer)
            p = self._persist_path
            if not snapshot:
                if p.exists():
                    p.unlink()
                self._persist_dirty = False
                return
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + '.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                for ts, bucket, line in snapshot:
                    f.write(json.dumps([ts, bucket, line]) + '\n')
            os.replace(tmp, p)
            self._persist_dirty = False
        except Exception as e:  # noqa: BLE001
            logger.warning("InfluxDB buffer persist failed: %s", e)

    # ── store-and-forward buffer ───────────────────────────────────────────
    def _buffer_line(self, line: str, ts: float, bucket: Optional[str] = None) -> None:
        """Queue one line-protocol record for replay. Bounded: drop-oldest by
        age (buffer_minutes) and count (buffer_max_points). Each entry carries
        its destination bucket (Tier 2 per-device routing); None = default."""
        with self._buf_lock:
            self._buffer.append((ts, bucket or self.config.bucket, line))
            self.points_buffered += 1
            self._persist_dirty = True
            self._prune_buffer_locked()

    def _prune_buffer_locked(self) -> None:
        """Enforce buffer bounds. Caller holds _buf_lock."""
        cutoff = time.time() - getattr(self.config, 'buffer_minutes', 10) * 60
        while self._buffer and self._buffer[0][0] < cutoff:
            self._buffer.popleft()
            self.points_dropped += 1
        overflow = len(self._buffer) - getattr(self.config, 'buffer_max_points', 50000)
        if overflow > 0:
            for _ in range(overflow):
                self._buffer.popleft()
            self.points_dropped += overflow

    def _drain_buffer(self) -> None:
        """Replay buffered points to InfluxDB in original order, in chunks,
        via a synchronous write (so success is known before points are let go
        of). A failed chunk goes back to the FRONT of the buffer and draining
        stops until the next monitor tick. Runs on the monitor thread."""
        with self._buf_lock:
            self._prune_buffer_locked()
            pending = len(self._buffer)
        if not pending:
            return
        with self.lock:
            client = self.client
        if client is None:
            return
        logger.info(f"InfluxDB replaying {pending} buffered points...")
        try:
            from influxdb_client.client.write_api import SYNCHRONOUS
            wapi = client.write_api(write_options=SYNCHRONOUS)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"InfluxDB replay unavailable: {e}")
            return
        try:
            while True:
                # take a run of consecutive same-bucket entries (order preserved)
                with self._buf_lock:
                    chunk = []
                    chunk_bucket = None
                    while self._buffer and len(chunk) < 5000:
                        ts, bucket, line = self._buffer[0]
                        if chunk_bucket is None:
                            chunk_bucket = bucket
                        elif bucket != chunk_bucket:
                            break
                        chunk.append(self._buffer.popleft())
                if not chunk:
                    break
                try:
                    wapi.write(bucket=chunk_bucket,
                               record="\n".join(line for _, _, line in chunk))
                    self.points_replayed += len(chunk)
                except Exception as e:  # noqa: BLE001
                    with self._buf_lock:
                        self._buffer.extendleft(reversed(chunk))
                    logger.warning(f"InfluxDB replay failed ({e}) — will retry")
                    self._handle_write_error(e)
                    return
            logger.info(f"InfluxDB replay complete: {pending} points delivered")
            self._persist_buffer()          # buffer drained → clear the snapshot
        finally:
            try:
                wapi.close()
            except Exception:  # noqa: BLE001
                pass

    def _start_reconnect_thread(self):
        """Start background thread for reconnection and health monitoring."""
        if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
            return

        self._stop_reconnect.clear()
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop,
            name="InfluxDB-Reconnect",
            daemon=True
        )
        self._reconnect_thread.start()
        logger.info("InfluxDB monitor thread started")

    def _reconnect_loop(self):
        """
        Persistent background loop that monitors and reconnects to InfluxDB.

        When connected: performs periodic ping() health checks to detect
        disconnections faster than waiting for batch retry exhaustion (up to 5 min).
        When disconnected: attempts reconnection every RECONNECT_CHECK_INTERVAL.
        """
        while not self._stop_reconnect.is_set():
            if self.connected:
                # Proactive health check
                try:
                    with self.lock:
                        client = self.client
                    if not client or not client.ping():
                        logger.warning("InfluxDB ping failed")
                        self.connected = False
                except Exception as e:
                    logger.warning(f"InfluxDB health check failed: {e}")
                    self.connected = False
            else:
                logger.debug("Attempting InfluxDB reconnection...")
                self._setup_client()

                if self.connected:
                    logger.info("InfluxDB reconnected successfully")

            # Deliver anything the outage left behind (no-op when empty).
            if self.connected:
                try:
                    self._drain_buffer()
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"InfluxDB buffer drain error: {e}")

            # Snapshot the buffer to disk while it's growing (outage in
            # progress) so a restart mid-outage doesn't lose it.
            if self._persist_path and self._persist_dirty:
                self._persist_buffer()

            self._stop_reconnect.wait(RECONNECT_CHECK_INTERVAL)

    def _handle_write_error(self, error: Exception):
        """Handle write errors and trigger reconnection if needed."""
        error_str = str(error).lower()
        connection_errors = [
            'connection refused', 'connection reset', 'connection closed',
            'no route to host', 'network is unreachable', 'timeout',
            'timed out', 'broken pipe', 'connection aborted',
        ]

        is_connection_error = any(err in error_str for err in connection_errors)

        if is_connection_error and self.connected:
            logger.warning("InfluxDB connection lost, monitor thread will reconnect")
            self.connected = False

    def is_enabled(self) -> bool:
        """Check if InfluxDB publishing is enabled and connected."""
        return self.config.enabled and self.connected

    def _safe_float(self, value) -> Optional[float]:
        """Convert to float, returning None for NaN/Infinity to protect InfluxDB batches."""
        try:
            val = float(value)
            if math.isfinite(val):
                return val
            logger.warning(f"Skipping non-finite value: {value}")
            return None
        except (ValueError, TypeError):
            return None

    def _should_write(self, address: int, value: Any, device_id: str = "") -> bool:
        """
        Check if value should be written based on mode and interval.
        Does NOT update cache — call _confirm_write() after successful write.
        Cache keys carry the device id (Tier 2) so two devices exposing the
        same register address never suppress each other.
        """
        current_time = time.time()
        key = (device_id, address)

        with self._cache_lock:
            # Rate limiting
            if key in self.last_write_time:
                elapsed = current_time - self.last_write_time[key]
                if elapsed < self.config.write_interval:
                    return False

            # Change detection
            if self.publish_mode == 'changed':
                if key in self.last_values:
                    if self.last_values[key] == value:
                        return False

            return True

    def _confirm_write(self, address: int, value: Any, device_id: str = ""):
        """Update the change-detection cache once a point is on a guaranteed
        path: either enqueued to the batching client (whose permanent failures
        are recovered into the replay buffer via _on_write_error) or placed in
        the replay buffer directly."""
        key = (device_id, address)
        with self._cache_lock:
            self.last_values[key] = value
            self.last_write_time[key] = time.time()

    def _get_measurement(self, register: SelectedRegister) -> str:
        """Get InfluxDB measurement name for a register."""
        if register.influxdb_measurement:
            return register.influxdb_measurement

        unit = register.unit.lower() if register.unit else ''
        if 'v' in unit and 'var' not in unit:
            return 'voltage'
        elif 'a' in unit and 'va' not in unit:
            return 'current'
        elif unit == 'w':
            return 'power_active'
        elif 'va' in unit and 'var' not in unit:
            return 'power_apparent'
        elif 'var' in unit:
            return 'power_reactive'
        elif 'wh' in unit:
            return 'energy_active'
        elif 'varh' in unit:
            return 'energy_reactive'
        elif 'hz' in unit:
            return 'frequency'
        elif '%' in unit:
            return 'percentage'
        else:
            return 'janitza'

    def _get_tags(self, register: SelectedRegister,
                  device_tag: Optional[str] = None) -> Dict[str, str]:
        """Get InfluxDB tags for a register. ``device_tag`` is the per-device
        tag value (Tier 2); None = the historical default, which device #1
        also passes explicitly — lines stay byte-identical."""
        tags = {
            'device': device_tag or 'janitza_umg512',
            'address': str(register.address),
            'name': register.name,
        }

        if register.influxdb_tags:
            tags.update(register.influxdb_tags)

        return tags

    def _build_point(self, register: SelectedRegister, safe_val: Any, ts: float,
                     poll_group: Optional[str] = None,
                     extra_tags: Dict[str, str] = None,
                     device_tag: Optional[str] = None):
        """Build a Point stamped with the Modbus poll time (not the flush time),
        so batching latency never skews the series and buffered replay lands the
        point exactly where it was measured."""
        from influxdb_client import Point, WritePrecision

        point = Point(self._get_measurement(register))
        for tag_key, tag_value in self._get_tags(register, device_tag).items():
            point = point.tag(tag_key, tag_value)
        if extra_tags:
            for tag_key, tag_value in extra_tags.items():
                point = point.tag(tag_key, tag_value)
        if poll_group:
            point = point.tag('poll_group', poll_group)

        field_name = register.name.lower().replace('[', '_').replace(']', '').replace('_g_', '')
        if isinstance(safe_val, (int, float)):
            point = point.field(field_name, float(safe_val))
            point = point.field('value', float(safe_val))
        else:
            point = point.field(field_name, str(safe_val))
        return point.time(int(ts * 1e9), WritePrecision.NS)

    def _deliver(self, point, ts: float, bucket: Optional[str] = None) -> None:
        """Route one point: enqueue to the batching client when connected,
        otherwise (or on enqueue failure) into the replay buffer. Either path
        guarantees eventual delivery within the buffer bounds."""
        bucket = bucket or self.config.bucket
        if self.connected and self.write_api:
            try:
                self.write_api.write(bucket=bucket, record=point)
                self.writes_total += 1
                self.last_write_ts = time.time()
                return
            except Exception as e:  # noqa: BLE001
                logger.warning(f"InfluxDB enqueue failed, buffering point: {e}")
                self._handle_write_error(e)
        self._buffer_line(point.to_line_protocol(), ts, bucket=bucket)

    def write_register_data(self, poll_group: str, data: Dict[int, Dict],
                            bucket: Optional[str] = None,
                            device_tag: Optional[str] = None,
                            device_id: str = ""):
        """Write register data from a poll group. Works whether or not InfluxDB
        is reachable — points produced during an outage go to the replay buffer.
        ``bucket``/``device_tag``/``device_id`` route one device's data
        (Tier 2); omitted = the legacy global routing (device #1)."""
        if not self.config.enabled:
            return

        try:
            for address, item in data.items():
                register = item.get('register')
                value = item.get('value')

                if register is None or not register.influxdb_enabled:
                    continue

                if not self._should_write(address, value, device_id):
                    self.writes_skipped += 1
                    continue

                # Validate value
                if isinstance(value, (int, float)):
                    safe_val = self._safe_float(value)
                    if safe_val is None:
                        continue
                else:
                    safe_val = value

                ts = item.get('ts') or time.time()
                point = self._build_point(register, safe_val, ts,
                                          poll_group=poll_group,
                                          device_tag=device_tag)
                self._deliver(point, ts, bucket=bucket)
                self._confirm_write(address, value, device_id)

        except Exception as e:
            self.writes_failed += 1
            logger.error(f"InfluxDB write error: {e}")
            self._handle_write_error(e)

    def write_single(self, register: SelectedRegister, value: Any,
                     extra_tags: Dict[str, str] = None, ts: float = None,
                     bucket: Optional[str] = None,
                     device_tag: Optional[str] = None,
                     device_id: str = ""):
        """Write a single register value."""
        if not self.config.enabled:
            return

        if not self._should_write(register.address, value, device_id):
            self.writes_skipped += 1
            return

        # Validate value
        if isinstance(value, (int, float)):
            safe_val = self._safe_float(value)
            if safe_val is None:
                return
        else:
            safe_val = value

        try:
            ts = ts or time.time()
            point = self._build_point(register, safe_val, ts,
                                      extra_tags=extra_tags,
                                      device_tag=device_tag)
            self._deliver(point, ts, bucket=bucket)
            self._confirm_write(register.address, value, device_id)

        except Exception as e:
            self.writes_failed += 1
            logger.error(f"InfluxDB write error: {e}")
            self._handle_write_error(e)

    def flush(self):
        """Flush pending writes."""
        if self.write_api:
            try:
                self.write_api.flush()
            except Exception as e:
                logger.error(f"InfluxDB flush error: {e}")

    def close(self):
        """Close InfluxDB connection. Best-effort final drain of the replay
        buffer; whatever cannot be delivered is snapshotted to disk (if
        persistence is on) so the next boot picks it up."""
        self._stop_reconnect.set()
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            self._reconnect_thread.join(timeout=2)

        if self.connected:
            try:
                self._drain_buffer()
            except Exception:  # noqa: BLE001
                pass

        # Persist whatever is left (undelivered) so a restart resumes it.
        if self._persist_path:
            self._persist_buffer()

        if self.write_api:
            try:
                self.write_api.close()
            except Exception:
                pass

        if self.client:
            try:
                self.client.close()
            except Exception:
                pass

        self.connected = False
        logger.info("InfluxDB connection closed")

    def update_config(self, new_config: InfluxDBConfig):
        """Update InfluxDB configuration."""
        self.config = new_config
        self.publish_mode = new_config.publish_mode
        logger.info(f"InfluxDB config updated: {new_config.url}")

    def update_registers(self, registers: List[SelectedRegister]):
        """Update register list."""
        self.registers = registers
        self._register_map = {r.address: r for r in registers if r.influxdb_enabled}
        logger.info(f"InfluxDB registers updated: {len(self._register_map)} enabled")

    def reconnect(self) -> bool:
        """Reconnect to InfluxDB with current config (one immediate attempt;
        the monitor thread keeps retrying in the background either way)."""
        logger.info("InfluxDB reconnecting...")
        self.close()

        if not self.config.enabled:
            logger.info("InfluxDB disabled, not reconnecting")
            return False

        self._setup_client()
        self._start_reconnect_thread()

        if self.connected:
            logger.info("InfluxDB reconnected successfully")
            return True
        logger.warning("InfluxDB reconnection failed, monitor thread will keep trying")
        return False

    def get_stats(self) -> Dict:
        """Return publisher statistics."""
        return {
            'enabled': self.config.enabled,
            'connected': self.connected,
            'url': self.config.url,
            'bucket': self.config.bucket,
            'writes_total': self.writes_total,
            'last_write_ts': self.last_write_ts,
            'last_contact_age_s': round(time.time() - self.last_write_ts, 1) if self.last_write_ts else None,
            'writes_failed': self.writes_failed,
            'writes_skipped': self.writes_skipped,
            'publish_mode': self.publish_mode,
            'registered_addresses': len(self._register_map),
            'disconnection_count': self.disconnection_count,
            'buffer_points': len(self._buffer),
            'buffered_total': self.points_buffered,
            'replayed_total': self.points_replayed,
            'dropped_total': self.points_dropped,
            'recovered_total': self.points_recovered,
            'buffer_minutes': getattr(self.config, 'buffer_minutes', 10),
            'buffer_persist': bool(self._persist_path),
        }

    # ── read-back for the UI history/trend view ───────────────────────────
    _EVERY_RE = re.compile(r"^\d+[smhd]$")
    _RANGE_RE = re.compile(r"^-\d+[smhdw]$")                     # relative: must be negative
    _RFC3339_RE = re.compile(r"^\d{4}-\d\d-\d\dT[0-9:.Z+-]*$")   # anchored, safe chars only
    _VALID_FN = {"mean", "min", "max", "last", "first"}

    def query_history(self, name: str, start: str = "-6h", stop: str = "now()",
                      every: str = "1m", fn: str = "mean",
                      measurement: Optional[str] = None,
                      bucket: Optional[str] = None,
                      device_tag: Optional[str] = None) -> Dict:
        """Read aggregated history for a register (matched by its ``name`` tag)
        back from InfluxDB. Returns ``{name, every, fn, series:[{t,v}]}`` (UTC
        ISO timestamps), or ``{series_mean/min/max}`` when ``fn=='all'`` (for a
        min/max band), or ``{"error": ...}``. Inputs are validated/escaped because
        the register name is a user-supplied tag value flowing into Flux."""
        if not self.config.enabled:
            return {"error": "influxdb disabled"}
        safe_name = str(name).replace("\\", "").replace('"', "")
        if not safe_name:
            return {"error": "name required"}
        if not self._EVERY_RE.match(str(every)):
            return {"error": "every must look like 30s / 5m / 1h / 1d"}
        fns = ["mean", "min", "max"] if fn == "all" else [fn]
        for f in fns:
            if f not in self._VALID_FN:
                return {"error": f"fn must be one of {sorted(self._VALID_FN)} or 'all'"}

        def _tok(t, allow_now=True):
            t = str(t)
            if (allow_now and t == "now()") or self._RANGE_RE.match(t) or self._RFC3339_RE.match(t):
                return t
            return None
        s = _tok(start, allow_now=False)
        if s is None:
            return {"error": "bad start (use -6h / -7d / RFC3339)"}
        e = _tok(stop) or "now()"
        meas_filter = ""
        if measurement:
            sm = re.sub(r"[^A-Za-z0-9_]", "", str(measurement))   # whitelist — no Flux injection
            if sm:
                meas_filter = f'  |> filter(fn: (r) => r["_measurement"] == "{sm}")\n'
        # Per-device (Tier 2): read from the device's bucket + filter its tag so a
        # shared bucket never mixes devices. Absent → the primary bucket, no tag
        # filter (byte-identical to the single-device query).
        q_bucket = re.sub(r'[^A-Za-z0-9_\-. ]', "", str(bucket)) if bucket else self.config.bucket
        dev_filter = ""
        if device_tag:
            sd = str(device_tag).replace("\\", "").replace('"', "")
            if sd:
                dev_filter = f'  |> filter(fn: (r) => r["device"] == "{sd}")\n'

        # Always use a short-lived client WITH a timeout: avoids racing the write
        # client (closed/replaced under lock by the reconnect thread) and bounds
        # the query so a hung InfluxDB can't stall the API event loop.
        try:
            from influxdb_client import InfluxDBClient
            client = InfluxDBClient(url=self.config.url, token=self.config.token,
                                    org=self.config.org, timeout=10_000)
        except Exception as ex:  # noqa: BLE001
            return {"error": f"influxdb client unavailable: {ex}"}

        def _run(f):
            flux = (f'from(bucket: "{q_bucket}")\n'
                    f'  |> range(start: {s}, stop: {e})\n'
                    f'  |> filter(fn: (r) => r["name"] == "{safe_name}")\n'
                    f'  |> filter(fn: (r) => r["_field"] == "value")\n'
                    f'{dev_filter}'
                    f'{meas_filter}'
                    f'  |> aggregateWindow(every: {every}, fn: {f}, createEmpty: false)\n'
                    f'  |> keep(columns: ["_time", "_value"])')
            out = []
            for table in client.query_api().query(flux, org=self.config.org):
                for rec in table.records:
                    v = rec.get_value()
                    out.append({"t": rec.get_time().isoformat().replace("+00:00", "Z"),
                                "v": round(v, 4) if isinstance(v, (int, float)) else v})
            return out

        try:
            if fn == "all":
                # run the three aggregate windows concurrently (~1x latency
                # instead of 3x) — they share the read-only client safely.
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=3) as ex:
                    mean_f = ex.submit(_run, "mean")
                    min_f = ex.submit(_run, "min")
                    max_f = ex.submit(_run, "max")
                    return {"name": safe_name, "every": every, "fn": "all",
                            "series_mean": mean_f.result(), "series_min": min_f.result(),
                            "series_max": max_f.result()}
            return {"name": safe_name, "every": every, "fn": fn, "series": _run(fn)}
        except Exception as ex:  # noqa: BLE001
            return {"error": f"query failed: {ex}"}
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    _NAME_RE = re.compile(r"[^A-Za-z0-9_\[\]]")   # register names may contain [ ]

    def energy_report(self, year: int, month: int, regs: list,
                      tz: str = "Europe/Bucharest",
                      bucket: Optional[str] = None,
                      device_tag: Optional[str] = None) -> Dict:
        """Energy used in a local calendar month: for each cumulative-counter
        register, the delta over the month (end-start) plus a per-day breakdown.
        ``regs`` = list of ``{name,label,unit,div}`` (div scales the raw unit, e.g.
        Wh→kWh with div=1000). Returns ``{year,month,totals:[...],daily:[...]}`` or
        ``{"error": ...}``."""
        if not self.config.enabled:
            return {"error": "influxdb disabled"}
        from datetime import datetime, timedelta
        try:
            from zoneinfo import ZoneInfo
            loc, utc = ZoneInfo(tz), ZoneInfo("UTC")
        except Exception as ex:  # noqa: BLE001
            return {"error": f"timezone unavailable: {ex}"}
        try:
            y, m = int(year), int(month)
            if not (1 <= m <= 12):
                return {"error": "month must be 1..12"}
            start_l = datetime(y, m, 1, tzinfo=loc)
            end_l = datetime(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1, tzinfo=loc)
            s = start_l.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            e = end_l.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception as ex:  # noqa: BLE001
            return {"error": f"bad month: {ex}"}
        try:
            from influxdb_client import InfluxDBClient
            client = InfluxDBClient(url=self.config.url, token=self.config.token,
                                    org=self.config.org, timeout=15_000)
        except Exception as ex:  # noqa: BLE001
            return {"error": f"influxdb client unavailable: {ex}"}

        def _q(flux):
            return [r for t in client.query_api().query(flux, org=self.config.org) for r in t.records]

        q_bucket = re.sub(r'[^A-Za-z0-9_\-. ]', "", str(bucket)) if bucket else self.config.bucket
        dev_pred = ""
        if device_tag:
            sd = str(device_tag).replace("\\", "").replace('"', "")
            if sd:
                dev_pred = f' and r["device"]=="{sd}"'
        totals, daily = [], []
        try:
            for spec in regs:
                nm = self._NAME_RE.sub("", str(spec.get("name", "")))
                if not nm:
                    continue
                div = float(spec.get("div", 1)) or 1.0
                label, unit = spec.get("label", nm), spec.get("unit", "")
                base = (f'b = from(bucket:"{q_bucket}") |> range(start:{s}, stop:{e}) '
                        f'|> filter(fn:(r)=> r["name"]=="{nm}" and r["_field"]=="value"{dev_pred})\n')
                # total = last - first over the month
                rows = _q(base + 'union(tables:[b|>first()|>set(key:"k",value:"f"), '
                                 'b|>last()|>set(key:"k",value:"l")]) |> keep(columns:["k","_value"])')
                vals = {r.values.get("k"): r.get_value() for r in rows}
                delta = None
                if vals.get("f") is not None and vals.get("l") is not None:
                    delta = round((vals["l"] - vals["f"]) / div, 3)
                totals.append({"name": spec.get("name"), "label": label, "unit": unit, "delta": delta})
                # per-day: cumulative value at each local day end, diffed in Python
                drows = _q('import "timezone"\noption location = timezone.location(name:"' + tz + '")\n'
                           + base + 'b |> aggregateWindow(every:1d, fn:last, createEmpty:false) '
                           '|> keep(columns:["_time","_value"])')
                pts = [(r.get_time().astimezone(loc), r.get_value()) for r in drows if r.get_value() is not None]
                days = []
                for i in range(1, len(pts)):
                    # value stamped at window stop (next local midnight) => belongs to the day before
                    day = (pts[i][0] - timedelta(seconds=1)).date().isoformat()
                    days.append({"date": day, "delta": round((pts[i][1] - pts[i - 1][1]) / div, 3)})
                daily.append({"name": spec.get("name"), "label": label, "unit": unit, "days": days})
        except Exception as ex:  # noqa: BLE001
            return {"error": f"query failed: {ex}"}
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        return {"year": y, "month": m, "start": s, "stop": e, "totals": totals, "daily": daily}
