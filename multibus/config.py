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
"""Configuration loader for Multi-Bus Gateway."""

import os
import re
import yaml
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field

from .tombstone_store import TombstoneStore

logger = logging.getLogger(__name__)


@dataclass
class ModbusConfig:
    host: str = "192.168.1.100"
    port: int = 502
    unit_id: int = 1
    timeout: int = 3
    retry_attempts: int = 3
    retry_delay: float = 1.0
    # No successful read within this many seconds => data-acquisition is stale
    # (surfaced in /health + /api/status; does NOT fail the container probe).
    stale_after_s: int = 30
    # Max address gap (in registers) the poller may bridge when merging two
    # registers into one batch read. 10 optimizes contiguous maps (default,
    # unchanged behavior). Strict/gapped slaves that answer a merged block with
    # ILLEGAL DATA ADDRESS (exception 02) should set this to 0 to read each
    # contiguous run separately.
    max_gap: int = 10
    # transport: "tcp" (host/port) or "rtu" (serial line below)
    protocol: str = "tcp"
    serial_port: str = ""             # e.g. /dev/ttyUSB0
    baudrate: int = 9600
    parity: str = "N"                 # N | E | O
    stopbits: int = 1
    bytesize: int = 8


PRIMARY_DEVICE_ID = "umg512"


@dataclass
class DeviceConfig:
    """One southbound Modbus device (Tier 2). The PRIMARY device is synthesized
    from the legacy flat sections (modbus/mqtt/influxdb) so existing installs
    migrate invisibly: same connection, same topic prefix, same bucket, same
    device tag — data collection does not change by one byte. Additional
    devices come from the optional ``devices:`` list in config.yaml."""
    id: str
    name: str = ""
    template: str = ""
    enabled: bool = True
    primary: bool = False
    protocol: str = "tcp"                    # tcp | rtu | http
    connection: "ModbusConfig" = field(default_factory=lambda: ModbusConfig())
    serial: Dict[str, Any] = field(default_factory=dict)   # rtu params (reserved)
    http: Dict[str, Any] = field(default_factory=dict)     # http input: url, timeout, headers, verify_tls
    mqtt_in: Dict[str, Any] = field(default_factory=dict)  # mqtt input: broker, port, username, password, tls, topic
    mqtt_topic_prefix: str = ""
    influxdb_bucket: str = ""
    influxdb_device_tag: str = ""
    ha_discovery_enabled: bool = True        # publish Home Assistant discovery for this device
    mqtt_enabled: bool = True                # route this device's values to MQTT
    influxdb_enabled: bool = True            # route this device's values to InfluxDB
    http_output_enabled: bool = False        # serve this device's live values as JSON (GET /api/meters/<id>)
    rest_push: Dict[str, Any] = field(default_factory=dict)   # push values to a URL: {enabled,url,interval_s,headers,format,verify_tls,timeout}

    def summary(self) -> Dict[str, Any]:
        return {
            'id': self.id, 'name': self.name, 'template': self.template,
            'enabled': self.enabled, 'primary': self.primary,
            'ha_discovery_enabled': self.ha_discovery_enabled,
            'mqtt_enabled': self.mqtt_enabled,
            'influxdb_enabled': self.influxdb_enabled,
            'http_output_enabled': self.http_output_enabled,
            'rest_push_enabled': bool(self.rest_push.get('enabled')),
            'protocol': self.protocol,
            'host': self.connection.host, 'port': self.connection.port,
            'unit_id': self.connection.unit_id,
            'http_url': self.http.get('url', '') if self.protocol == 'http' else '',
            'mqtt_in_topic': self.mqtt_in.get('topic', '') if self.protocol == 'mqtt' else '',
            'mqtt_in_broker': self.mqtt_in.get('broker', '') if self.protocol == 'mqtt' else '',
            'mqtt_topic_prefix': self.mqtt_topic_prefix,
            'influxdb_bucket': self.influxdb_bucket,
        }


@dataclass
class MQTTConfig:
    enabled: bool = True
    broker: str = "192.168.1.100"
    port: int = 1883
    username: str = ""
    password: str = ""
    topic_prefix: str = "multibus/umg512"
    retain: bool = True
    qos: int = 0
    publish_mode: str = "changed"  # "changed" or "all"
    # In "changed" mode, force a republish of an UNCHANGED value after this many
    # seconds (0 = off) so a steady reading keeps a fresh timestamp and HA/other
    # consumers don't grey the entity out during long steady states.
    heartbeat_interval: int = 0
    ha_discovery_enabled: bool = True
    ha_discovery_prefix: str = "homeassistant"
    ha_device_name: str = "Janitza UMG 512-PRO"
    # Default topic prefix pattern for NEW devices ({device} = the device id).
    # Device #1 keeps its migrated prefix; this only seeds new devices.
    default_topic_pattern: str = "meters/{device}"
    # TLS (8883): encrypt the broker link. ca_cert verifies the broker;
    # client_cert+client_key add mutual TLS. tls_insecure skips hostname/cert
    # checks (test only). All paths are inside the container (config dir).
    tls_enabled: bool = False
    tls_ca_cert: str = ""
    tls_client_cert: str = ""
    tls_client_key: str = ""
    tls_insecure: bool = False


@dataclass
class InfluxDBConfig:
    enabled: bool = False
    url: str = "http://localhost:8086"
    token: str = ""
    org: str = ""
    bucket: str = "multibus"
    write_interval: int = 5
    publish_mode: str = "changed"  # "changed" or "all"
    # Default bucket pattern for NEW devices ({device} = the device id).
    # Device #1 keeps its migrated bucket; this only seeds new devices.
    default_bucket_pattern: str = "{device}"
    # Store-and-forward buffer: points that cannot be delivered (InfluxDB down)
    # are kept in RAM and replayed with their original timestamps on reconnect.
    buffer_minutes: int = 10          # keep at most this much history
    buffer_max_points: int = 50000    # hard cap (drop-oldest beyond this)
    # Persist the buffer to disk so it survives a restart during an outage
    # (RAM-only otherwise). Snapshot file under the config dir.
    buffer_persist: bool = True


@dataclass
class SecurityConfig:
    # IP allowlist for the HTTP API/UI. Empty = open (trusted-LAN default).
    # Entries are IPs or CIDRs (e.g. 192.168.1.0/24). Loopback and the docker
    # gateway are always allowed so the container's own probes/UI keep working.
    allowlist: List[str] = field(default_factory=list)
    # SSRF guard: HTTP/JSON device URLs must point at a private LAN host. Set
    # true only if you knowingly poll a non-LAN endpoint (opens an SSRF path).
    allow_nonlan_http_devices: bool = False
    # Modbus WRITE gate: FC5/6/15/16 writes to devices are refused unless this is
    # true. Off by default — writing to real hardware is irreversible. The
    # primary device stays read-only regardless.
    allow_writes: bool = False
    # Per-client-IP write rate limit (writes/second) on the Modbus write API. A
    # flood of writes contends the shared Modbus lock and can starve the pollers;
    # excess writes get 429. 0 disables the limit. Generous by default so a normal
    # control loop is unaffected.
    write_rate_limit_per_s: float = 10.0


@dataclass
class UIConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    auth_enabled: bool = False
    auth_username: str = "admin"
    auth_password: str = "admin"
    # read-only viewer account (optional) — GET only, no config changes
    viewer_username: str = ""
    viewer_password: str = ""
    # operator account (optional) — live actions (writes within template
    # bounds, diagnostics, discovery) but NO configuration changes
    operator_username: str = ""
    operator_password: str = ""
    # Canonical UI URL: when set (e.g. https://mbus.diysolar.ro), the browser is
    # steered here by default (TLS + passkeys). Empty = no redirect. The local
    # IP stays reachable via ?local (client-side, so a down hostname can't lock
    # the operator out).
    canonical_url: str = ""
    # login-failure lockout (per client IP)
    lockout_threshold: int = 5      # failed attempts before lockout
    lockout_minutes: int = 5        # lockout duration
    # HTTPS for the UI (uvicorn TLS). Paths inside the container.
    tls_enabled: bool = False
    tls_cert: str = ""
    tls_key: str = ""
    # Reverse proxies whose X-Forwarded-* headers are TRUSTED (uvicorn
    # forwarded_allow_ips). Empty (default) = trust nobody: client IPs are the
    # socket peer, exactly the historical behaviour. Set to the proxy's IP
    # (e.g. the Traefik container) when TLS terminates in front of the app —
    # otherwise lockout buckets, the IP allowlist and the audit trail would
    # all see the proxy instead of the real client.
    trusted_proxies: List[str] = field(default_factory=list)
    # IANA timezone for calendar reports (monthly-energy month boundaries).
    # Empty/default keeps the historical Europe/Bucharest behaviour.
    timezone: str = "Europe/Bucharest"
    # Default widget colors: phase convention (distinct|iec|rst|custom) +
    # per-category hues, applied to NEW widgets only (existing choices kept).
    default_colors: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PollGroup:
    interval: int
    description: str = ""


_INPUT_REGISTER_ALIASES = {'input', 'inputregister', 'inputregisters', 'ir', 'fc4', '4'}
_COIL_ALIASES = {'coil', 'coils', 'fc1', 'fc5', '1'}
_DISCRETE_ALIASES = {'discrete', 'discreteinput', 'discreteinputs', 'di', 'fc2', '2'}


def normalize_register_type(v) -> str:
    """Normalize a register-type to one of holding (FC3) / input (FC4) /
    coil (FC1/FC5) / discrete (FC2). Unknown/empty -> 'holding' (the Modbus
    default and the Janitza's type). Coils are writable; input/discrete are not."""
    s = str(v or '').strip().lower().replace(' ', '').replace('_', '').replace('-', '')
    if s in _COIL_ALIASES:
        return 'coil'
    if s in _DISCRETE_ALIASES:
        return 'discrete'
    if s in _INPUT_REGISTER_ALIASES:
        return 'input'
    return 'holding'


@dataclass
class SelectedRegister:
    address: int
    name: str
    label: str
    unit: str
    data_type: str
    poll_group: str
    description: str = ""  # Human-readable description from modbus_data.json
    json_path: str = ""   # HTTP/JSON + MQTT input: dot/bracket path into the JSON payload
    topic: str = ""       # MQTT input: the topic this register reads from (else device base topic)
    scale: float = 1.0    # Modbus input: engineering_value = raw / scale (SunSpec 10^-SF etc.)
    nan: Any = None       # not-available sentinel (True=std for type / value / list) → missing
    register_type: str = "holding"   # Modbus: 'holding' (FC3) or 'input' (FC4)
    mqtt_enabled: bool = True
    mqtt_topic: str = ""
    influxdb_enabled: bool = True
    influxdb_measurement: str = ""
    influxdb_tags: Dict[str, str] = field(default_factory=dict)
    ui_show_on_dashboard: bool = True
    ui_widget: str = "value"
    ui_config: Dict[str, Any] = field(default_factory=dict)
    thresholds: Optional[Dict[str, Any]] = None  # Color coding thresholds


class Config:
    """Configuration manager for Multi-Bus Gateway."""

    def __init__(self, config_path: str = "config/config.yaml"):
        self.config_path = Path(config_path)
        self.registers_path = self.config_path.parent / "selected_registers.json"
        self.all_registers_path = Path("docs/modbus_data.json")
        # soft-delete lifecycle lives in a collaborator (see tombstone_store.py)
        self._tombstones = TombstoneStore(self.config_path.parent / 'devices',
                                          self._safe_device_id)

        self.modbus = ModbusConfig()
        self.mqtt = MQTTConfig()
        self.influxdb = InfluxDBConfig()
        self.ui = UIConfig()
        self.security = SecurityConfig()
        self.poll_groups: Dict[str, PollGroup] = {
            "realtime": PollGroup(interval=1, description="Real-time values"),
            "normal": PollGroup(interval=5, description="Standard measurements"),
            "slow": PollGroup(interval=60, description="Energy counters"),
        }
        self.selected_registers: List[SelectedRegister] = []
        self.all_registers: Dict = {}
        self.devices: List[DeviceConfig] = []
        self._raw_devices: List[Dict] = []
        # HTTP/JSON output sink for the PRIMARY device (non-primary devices carry
        # their own flag in the raw `devices[]` list). Off by default — the
        # /api/meters/<id> endpoint is opt-in per device.
        self.http_output_primary_enabled: bool = False
        # Generic REST push for the PRIMARY device (non-primary carry their own in
        # the devices[] list). {enabled,url,interval_s,headers,format,verify_tls,timeout}.
        self.rest_push_primary: Dict[str, Any] = {}
        # Original (pre-env) values of secrets overridden by env vars, so a save
        # writes the config value — never the env secret — to config.yaml.
        self._env_secret_shadow: Dict[str, str] = {}
        # serializes config-file writes so two concurrent saves (FastAPI runs
        # handlers in a threadpool) can't interleave their tmp/rename dance
        self._file_lock = __import__("threading").Lock()
        self.alerts: Dict = {}                # optional `alerts:` block (off by default)
        # Optional `esphome:` block (off by default) — the Device Builder
        # section talks to an external ESPHome dashboard over its HTTP/WS API.
        # Kept as a raw dict: {enabled, url, username, password, timeout_s}.
        self.esphome: Dict = {}

        self.load()

    def load(self):
        """Load configuration from files."""
        self._load_yaml_config()
        self._load_selected_registers()
        self._load_all_registers()
        self._apply_env_overrides()
        # AFTER env overrides, so device #1 inherits MODBUS_HOST etc.
        self._build_devices()

    def _build_devices(self):
        """Materialize the device list. Device #1 = the legacy flat config
        (invisible migration — routing identical to today); extra devices come
        from the optional ``devices:`` yaml list."""
        devices: List[DeviceConfig] = [DeviceConfig(
            id=PRIMARY_DEVICE_ID,
            name="Janitza UMG 512-PRO",
            template="janitza_umg512_pro",
            enabled=True,
            primary=True,
            protocol="tcp",
            connection=self.modbus,                      # same object: UI edits apply live
            mqtt_topic_prefix=self.mqtt.topic_prefix,
            influxdb_bucket=self.influxdb.bucket,
            influxdb_device_tag="janitza_umg512",        # today's hardcoded Influx tag
            ha_discovery_enabled=self.mqtt.ha_discovery_enabled,  # device #1 = global flag
            mqtt_enabled=True,                           # device #1 always routes (locked in UI)
            influxdb_enabled=True,
            http_output_enabled=self.http_output_primary_enabled,
            rest_push=dict(self.rest_push_primary or {}),
        )]
        for d in self._raw_devices:
            did = str(d.get('id', '')).strip()
            if not did:
                logger.warning("devices[]: entry without id skipped")
                continue
            if did == PRIMARY_DEVICE_ID or any(x.id == did for x in devices):
                logger.warning(f"devices[]: duplicate id {did!r} skipped")
                continue
            conn = d.get('connection', {}) or {}
            mqtt_cfg = d.get('mqtt', {}) or {}
            influx_cfg = d.get('influxdb', {}) or {}
            http_out_cfg = d.get('http_output', {}) or {}
            prefix = mqtt_cfg.get('topic_prefix', 'meters/${device_id}')
            prefix = prefix.replace('${device_id}', did).replace('${id}', did)
            try:
              devices.append(DeviceConfig(
                id=did,
                name=d.get('name', did),
                template=d.get('template', ''),
                enabled=bool(d.get('enabled', True)),
                protocol=str(conn.get('protocol', 'tcp')).lower(),
                connection=ModbusConfig(
                    host=conn.get('host', ''),
                    port=int(conn.get('port', 502)),
                    unit_id=int(conn.get('unit_id', 1)),
                    timeout=conn.get('timeout', 3),
                    retry_attempts=int(conn.get('retry_attempts', 3)),
                    retry_delay=float(conn.get('retry_delay', 1.0)),
                    stale_after_s=int(conn.get('stale_after_s', 30)),
                    max_gap=int(conn.get('max_gap', 10)),
                    protocol=str(conn.get('protocol', 'tcp')).lower(),
                    serial_port=conn.get('serial_port', ''),
                    baudrate=int(conn.get('baudrate', 9600)),
                    parity=str(conn.get('parity', 'N')),
                    stopbits=int(conn.get('stopbits', 1)),
                    bytesize=int(conn.get('bytesize', 8)),
                ),
                serial={k: conn[k] for k in
                        ('serial_port', 'baudrate', 'parity', 'stopbits', 'bytesize')
                        if k in conn},
                http={k: conn[k] for k in
                      ('url', 'timeout', 'headers', 'verify_tls') if k in conn},
                mqtt_in={k: conn[k] for k in
                         ('broker', 'port', 'username', 'password', 'tls', 'topic') if k in conn},
                mqtt_topic_prefix=prefix,
                influxdb_bucket=influx_cfg.get('bucket', self.influxdb.bucket),
                influxdb_device_tag=influx_cfg.get('device_tag', did),
                ha_discovery_enabled=bool(mqtt_cfg.get('ha_discovery', True)),
                mqtt_enabled=bool(mqtt_cfg.get('enabled', True)),
                influxdb_enabled=bool(influx_cfg.get('enabled', True)),
                http_output_enabled=bool(http_out_cfg.get('enabled', False)),
                rest_push=dict(d.get('rest_push', {}) or {}),
              ))
            except (ValueError, TypeError) as e:
                # A single malformed devices[] entry (e.g. port:"abc") must NOT
                # crash the whole boot — including the primary's polling. Skip it,
                # like the missing/duplicate-id skips above.
                logger.warning(f"devices[]: skipping {did!r} — invalid config: {e}")
        self.devices = devices

    def default_topic_prefix(self, device_id: str) -> str:
        """Resolve the default MQTT topic prefix for a NEW device from the
        configured pattern ({device} = id)."""
        pat = getattr(self.mqtt, 'default_topic_pattern', 'meters/{device}') or 'meters/{device}'
        return pat.replace('{device}', device_id).replace('{device_id}', device_id)

    def default_bucket(self, device_id: str) -> str:
        pat = getattr(self.influxdb, 'default_bucket_pattern', '{device}') or '{device}'
        return pat.replace('{device}', device_id).replace('{device_id}', device_id)

    @property
    def primary_device(self) -> DeviceConfig:
        return self.devices[0]

    def get_device(self, device_id: str) -> Optional[DeviceConfig]:
        return next((d for d in self.devices if d.id == device_id), None)

    def upsert_raw_device(self, raw: Dict) -> DeviceConfig:
        """Create or update a non-primary device from its raw yaml-shaped dict,
        rebuild the device list and persist config.yaml. Returns the built
        DeviceConfig. Raises ValueError on invalid/primary ids."""
        did = str(raw.get('id', '')).strip()
        if not did:
            raise ValueError("device id is required")
        if did == PRIMARY_DEVICE_ID:
            raise ValueError(f"'{PRIMARY_DEVICE_ID}' is the primary device — "
                             "edit it via the Modbus settings")
        # Transactional: snapshot the raw list so a build/save failure restores
        # the PREVIOUS state exactly, instead of leaving the old entry deleted
        # (which a later unrelated save would then persist — the device would
        # silently vanish from config.yaml).
        _snapshot = [dict(d) for d in self._raw_devices]
        try:
            self._raw_devices = [d for d in self._raw_devices if d.get('id') != did]
            self._raw_devices.append(raw)
            self._build_devices()
            built = self.get_device(did)
            if built is None:
                raise ValueError(f"device {did!r} could not be built from the given data")
            self.save_yaml_config()
            return built
        except Exception:
            self._raw_devices = _snapshot
            self._build_devices()
            raise

    def set_http_output(self, device_id: str, enabled: bool) -> None:
        """Enable/disable the HTTP/JSON output sink for a device and persist.
        Works for the primary (flat `http_output:` section) and non-primary
        devices (their `http_output` block in the raw list) alike."""
        dev = self.get_device(device_id)
        if dev is None:
            raise ValueError(f"device {device_id!r} not found")
        if dev.primary:
            self.http_output_primary_enabled = bool(enabled)
        else:
            for d in self._raw_devices:
                if d.get('id') == device_id:
                    d.setdefault('http_output', {})['enabled'] = bool(enabled)
                    break
            else:
                raise ValueError(f"device {device_id!r} is not a configurable device")
        self._build_devices()
        self.save_yaml_config()

    def set_rest_push(self, device_id: str, cfg: Dict) -> None:
        """Set the REST push config for a device and persist. Primary → flat
        `rest_push:` section; non-primary → their `rest_push` block."""
        dev = self.get_device(device_id)
        if dev is None:
            raise ValueError(f"device {device_id!r} not found")
        cfg = dict(cfg or {})
        if dev.primary:
            self.rest_push_primary = cfg
        else:
            for d in self._raw_devices:
                if d.get('id') == device_id:
                    d['rest_push'] = cfg
                    break
            else:
                raise ValueError(f"device {device_id!r} is not a configurable device")
        self._build_devices()
        self.save_yaml_config()

    @staticmethod
    def _safe_device_id(device_id: str) -> str:
        """A device id must be a single safe path segment — NEVER a traversal.
        Used by every method that maps an id to a config/devices/<id> path so a
        crafted id (``..``, ``a/b``, encoded slashes) can't escape the dir."""
        did = str(device_id or '')
        if (not did or did in ('.', '..') or '/' in did or '\\' in did
                or '\x00' in did or not re.match(r'^[A-Za-z0-9][A-Za-z0-9_.-]*$', did)):
            raise ValueError(f"invalid device id: {device_id!r}")
        return did

    def get_raw_device(self, device_id: str) -> Optional[Dict]:
        """The raw devices[] entry for a device id (None if absent). Used to
        snapshot the full definition before a delete so it can be restored."""
        for d in self._raw_devices:
            if d.get('id') == device_id:
                return dict(d)
        return None

    def remove_raw_device(self, device_id: str) -> bool:
        """Remove a non-primary device and persist. Before removing, the full
        device definition is snapshotted to config/devices/<id>/device.json
        (a "tombstone") so the UI can restore the exact device later; the
        selected-registers file is kept beside it."""
        if device_id == PRIMARY_DEVICE_ID:
            raise ValueError("the primary device cannot be removed")
        raw = self.get_raw_device(device_id)
        before = len(self._raw_devices)
        self._raw_devices = [d for d in self._raw_devices if d.get('id') != device_id]
        if len(self._raw_devices) == before:
            return False
        if raw is not None:
            self._write_device_tombstone(device_id, raw)
        self._build_devices()
        self.save_yaml_config()
        return True

    def _write_device_tombstone(self, device_id: str, raw: Dict) -> None:
        self._tombstones.write(device_id, raw)

    def list_deleted_devices(self) -> List[Dict]:
        """Restorable devices (a tombstone whose id is not currently active)."""
        return self._tombstones.list({d.id for d in self.devices})

    def load_deleted_device(self, device_id: str) -> Optional[Dict]:
        """The raw devices[] dict from a tombstone (None if absent)."""
        return self._tombstones.load(device_id)

    def forget_deleted_device(self, device_id: str) -> bool:
        """Permanently remove a deleted device's kept dir (tombstone + registers).
        Refuses to touch an ACTIVE device's dir."""
        did = self._safe_device_id(device_id)
        if did in {d.id for d in self.devices} or did == PRIMARY_DEVICE_ID:
            raise ValueError("device is active — delete it first")
        return self._tombstones.forget(device_id)

    def save_device_registers(self, device_id: str, registers: List[Dict],
                              poll_groups: Optional[Dict] = None) -> None:
        """Persist a non-primary device's register selection (same schema as
        the legacy file). Primary keeps using save_selected_registers()."""
        if device_id == PRIMARY_DEVICE_ID:
            self.save_selected_registers(registers)
            return
        path = self.device_registers_path(device_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": "1.0",
            "registers": registers,
            "poll_groups": poll_groups or {
                name: {"interval": g.interval, "description": g.description}
                for name, g in self.poll_groups.items()
            },
        }
        tmp = path.with_suffix(path.suffix + '.tmp')   # atomic: crash-safe write
        with self._file_lock:
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())           # durable before the rename
            os.replace(tmp, path)
        logger.info(f"device {device_id}: saved {len(registers)} selected registers")

    def device_registers_path(self, device_id: str) -> Path:
        """Where a device's selected-registers file lives. Device #1 keeps the
        legacy path (unchanged installs); others get config/devices/<id>/.

        The id is validated as a single safe path segment here — this is the
        chokepoint every register read/write and the energy endpoints flow
        through, so a crafted ``device`` param (``..``, ``../../tmp/x``) can
        never escape config/devices/ regardless of the caller's own checks."""
        if device_id == PRIMARY_DEVICE_ID:
            return self.registers_path
        return (self.config_path.parent / 'devices'
                / self._safe_device_id(device_id) / 'selected_registers.json')

    def save_device_poll_groups(self, device_id: str, groups: Dict[str, Dict]) -> None:
        """Update just the poll-group intervals in a device's registers file
        (keeps the register selection). Works for any device — the primary uses
        the legacy file and its in-memory groups are refreshed too."""
        # Reject a 0/negative interval up front with a clear error: it would
        # spin the poll loop with no pause and hammer the bus. Sub-second is
        # allowed (the ESS control loop needs 250 ms) but a real floor stands.
        for name, g in (groups or {}).items():
            try:
                iv = float(g.get("interval", 5))
            except (TypeError, ValueError):
                raise ValueError(f"poll group {name!r}: interval must be a number")
            if iv < 0.05:
                raise ValueError(f"poll group {name!r}: interval must be >= 0.05 s "
                                 f"(got {iv}) — 0 would flood the bus")
        path = self.device_registers_path(device_id)
        with self._file_lock:                  # serialize the read-modify-write
            if path.exists():
                with open(path) as f:
                    data = json.load(f)
            else:
                data = {"version": "1.0", "registers": [], "poll_groups": {}}
            data["poll_groups"] = groups
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())           # durable before the rename
            os.replace(tmp, path)
        if device_id == PRIMARY_DEVICE_ID:
            for name, g in groups.items():
                self.poll_groups[name] = PollGroup(interval=float(g.get("interval", 5)),
                                                   description=g.get("description", ""))
        logger.info(f"device {device_id}: poll groups updated {list(groups.keys())}")

    def load_energy_fields(self, device_id: str) -> List[Dict]:
        """The device's saved Energy-tab field selection (the cumulative counters
        to total per month), or [] if the user hasn't picked any yet."""
        path = self.device_registers_path(device_id)
        if not path.exists():
            return []
        try:
            with open(path) as f:
                return json.load(f).get("energy_fields", []) or []
        except Exception:  # noqa: BLE001
            return []

    def load_calculated(self, device_id: str) -> List[Dict]:
        """The device's saved calculated registers (formula-derived measurements),
        or [] if none. Each entry: {name, label, unit, expr, poll_group, decimals}."""
        path = self.device_registers_path(device_id)
        if not path.exists():
            return []
        try:
            with open(path) as f:
                return json.load(f).get("calculated", []) or []
        except Exception:  # noqa: BLE001
            return []

    def save_calculated(self, device_id: str, calculated: List[Dict]) -> None:
        """Persist the device's calculated registers alongside its register file
        (a top-level ``calculated`` list, next to registers/poll_groups)."""
        path = self.device_registers_path(device_id)
        with self._file_lock:                  # serialize the read-modify-write
            if path.exists():
                with open(path) as f:
                    data = json.load(f)
            else:
                data = {"version": "1.0", "registers": [], "poll_groups": {}}
            data["calculated"] = calculated
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + '.tmp')
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)

    def load_calculated_templates(self) -> List[Dict]:
        """User-saved reusable calculated presets (global, not per-device)."""
        p = self.config_path.parent / "calculated_templates.json"
        if not p.exists():
            return []
        try:
            with open(p) as f:
                return json.load(f).get("templates", []) or []
        except Exception:  # noqa: BLE001
            return []

    def save_calculated_templates(self, templates: List[Dict]) -> None:
        """Persist the user's reusable calculated presets (atomic)."""
        p = self.config_path.parent / "calculated_templates.json"
        with self._file_lock:                  # load()+save() cycles race otherwise
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + '.tmp')
            with open(tmp, 'w') as f:
                json.dump({"version": "1.0", "templates": templates}, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)

    def save_energy_fields(self, device_id: str, fields: List[Dict]) -> None:
        """Persist the Energy-tab field selection alongside the device's registers
        file (a top-level ``energy_fields`` list, next to registers/poll_groups)."""
        path = self.device_registers_path(device_id)
        with self._file_lock:                  # serialize the read-modify-write
            if path.exists():
                with open(path) as f:
                    data = json.load(f)
            else:
                data = {"version": "1.0", "registers": [], "poll_groups": {}}
            data["energy_fields"] = fields
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        logger.info(f"device {device_id}: energy fields set ({len(fields)})")

    def load_device_registers(self, device: DeviceConfig):
        """Return (selected_registers, poll_groups) for a device. Device #1
        returns the already-loaded legacy selection."""
        if device.primary:
            return self.selected_registers, self.poll_groups
        path = self.device_registers_path(device.id)
        if not path.exists():
            logger.info(f"device {device.id}: no selected registers yet ({path})")
            return [], dict(self.poll_groups)
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            regs = self._parse_selected_payload(data)
            groups = dict(self.poll_groups)
            for name, group in (data.get('poll_groups') or {}).items():
                groups[name] = PollGroup(interval=group.get('interval', 5),
                                         description=group.get('description', ''))
            return regs, groups
        except Exception as e:  # noqa: BLE001
            logger.error(f"device {device.id}: error loading registers: {e}")
            return [], dict(self.poll_groups)

    def _load_yaml_config(self):
        """Load main YAML configuration."""
        if not self.config_path.exists():
            logger.warning(f"Config file not found: {self.config_path}, using defaults")
            return

        try:
            with open(self.config_path, 'r') as f:
                data = yaml.safe_load(f) or {}

            # Modbus
            if 'modbus' in data:
                m = data['modbus']
                self.modbus = ModbusConfig(
                    host=m.get('host', self.modbus.host),
                    port=m.get('port', self.modbus.port),
                    unit_id=m.get('unit_id', self.modbus.unit_id),
                    timeout=m.get('timeout', self.modbus.timeout),
                    retry_attempts=m.get('retry_attempts', self.modbus.retry_attempts),
                    retry_delay=m.get('retry_delay', self.modbus.retry_delay),
                    stale_after_s=m.get('stale_after_s', self.modbus.stale_after_s),
                    max_gap=m.get('max_gap', self.modbus.max_gap),
                )

            # MQTT
            if 'mqtt' in data:
                m = data['mqtt']
                ha = m.get('ha_discovery', {})
                self.mqtt = MQTTConfig(
                    enabled=m.get('enabled', self.mqtt.enabled),
                    broker=m.get('broker', self.mqtt.broker),
                    port=m.get('port', self.mqtt.port),
                    username=m.get('username', self.mqtt.username),
                    password=m.get('password', self.mqtt.password),
                    topic_prefix=m.get('topic_prefix', self.mqtt.topic_prefix),
                    retain=m.get('retain', self.mqtt.retain),
                    qos=m.get('qos', self.mqtt.qos),
                    publish_mode=m.get('publish_mode', self.mqtt.publish_mode),
                    heartbeat_interval=m.get('heartbeat_interval', self.mqtt.heartbeat_interval),
                    ha_discovery_enabled=ha.get('enabled', self.mqtt.ha_discovery_enabled),
                    ha_discovery_prefix=ha.get('prefix', self.mqtt.ha_discovery_prefix),
                    ha_device_name=ha.get('device_name', self.mqtt.ha_device_name),
                    tls_enabled=m.get('tls_enabled', self.mqtt.tls_enabled),
                    tls_ca_cert=m.get('tls_ca_cert', self.mqtt.tls_ca_cert),
                    tls_client_cert=m.get('tls_client_cert', self.mqtt.tls_client_cert),
                    tls_client_key=m.get('tls_client_key', self.mqtt.tls_client_key),
                    tls_insecure=m.get('tls_insecure', self.mqtt.tls_insecure),
                    default_topic_pattern=m.get('default_topic_pattern', self.mqtt.default_topic_pattern),
                )

            # InfluxDB
            if 'influxdb' in data:
                i = data['influxdb']
                self.influxdb = InfluxDBConfig(
                    enabled=i.get('enabled', self.influxdb.enabled),
                    url=i.get('url', self.influxdb.url),
                    token=i.get('token', self.influxdb.token),
                    org=i.get('org', self.influxdb.org),
                    bucket=i.get('bucket', self.influxdb.bucket),
                    write_interval=i.get('write_interval', self.influxdb.write_interval),
                    publish_mode=i.get('publish_mode', self.influxdb.publish_mode),
                    buffer_minutes=i.get('buffer_minutes', self.influxdb.buffer_minutes),
                    buffer_max_points=i.get('buffer_max_points', self.influxdb.buffer_max_points),
                    buffer_persist=i.get('buffer_persist', self.influxdb.buffer_persist),
                    default_bucket_pattern=i.get('default_bucket_pattern', self.influxdb.default_bucket_pattern),
                )

            # UI
            if 'ui' in data:
                u = data['ui']
                auth = u.get('auth', {})
                tls = u.get('tls', {})
                self.ui = UIConfig(
                    host=u.get('host', self.ui.host),
                    port=u.get('port', self.ui.port),
                    auth_enabled=auth.get('enabled', self.ui.auth_enabled),
                    auth_username=auth.get('username', self.ui.auth_username),
                    auth_password=auth.get('password', self.ui.auth_password),
                    viewer_username=auth.get('viewer_username', self.ui.viewer_username),
                    viewer_password=auth.get('viewer_password', self.ui.viewer_password),
                    operator_username=auth.get('operator_username', self.ui.operator_username),
                    operator_password=auth.get('operator_password', self.ui.operator_password),
                    canonical_url=u.get('canonical_url', self.ui.canonical_url),
                    lockout_threshold=auth.get('lockout_threshold', self.ui.lockout_threshold),
                    lockout_minutes=auth.get('lockout_minutes', self.ui.lockout_minutes),
                    tls_enabled=tls.get('enabled', self.ui.tls_enabled),
                    tls_cert=tls.get('cert', self.ui.tls_cert),
                    tls_key=tls.get('key', self.ui.tls_key),
                    trusted_proxies=list(u.get('trusted_proxies', self.ui.trusted_proxies) or []),
                    timezone=u.get('timezone', self.ui.timezone),
                    default_colors=dict(u.get('default_colors', self.ui.default_colors) or {}),
                )

            # Security (IP allowlist)
            if 'security' in data:
                s = data['security'] or {}
                self.security = SecurityConfig(
                    allowlist=list(s.get('allowlist', []) or []),
                    allow_nonlan_http_devices=bool(
                        s.get('allow_nonlan_http_devices', False)),
                    allow_writes=bool(s.get('allow_writes', False)),
                    write_rate_limit_per_s=float(s.get('write_rate_limit_per_s', 10.0)),
                )

            # Poll groups
            if 'polling' in data and 'groups' in data['polling']:
                for name, group in data['polling']['groups'].items():
                    self.poll_groups[name] = PollGroup(
                        interval=group.get('interval', 5),
                        description=group.get('description', ''),
                    )

            # HTTP/JSON output sink for the primary device (opt-in). Non-primary
            # devices carry their own flag inside the `devices[]` list below.
            if 'http_output' in data:
                self.http_output_primary_enabled = bool(
                    (data['http_output'] or {}).get('enabled', False))
            if 'rest_push' in data:
                self.rest_push_primary = dict(data['rest_push'] or {})

            # Additional southbound devices (Tier 2) — materialized in
            # _build_devices() after env overrides.
            self._raw_devices = data.get('devices', []) or []

            # Optional alerting hooks (off unless enabled). Kept as a raw dict —
            # the AlertManager reads it. See multibus/alerts.py.
            self.alerts = data.get('alerts', {}) or {}

            # Optional ESPHome integration (Device Builder). Raw dict — the
            # builder routes read it at request time. See multibus/esphome_client.py.
            self.esphome = data.get('esphome', {}) or {}

            logger.info(f"Loaded config from {self.config_path}")
            self._load_failed = False

        except Exception as e:
            # A corrupt config.yaml must NOT silently become "defaults": the
            # primary would poll the wrong host and a later save would overwrite
            # the user's real file with defaults (permanent config loss). Keep a
            # copy of the broken file and block saves until it's fixed.
            self._load_failed = True
            try:
                bad = self.config_path.with_suffix('.yaml.bad')
                import shutil
                shutil.copyfile(self.config_path, bad)
                logger.error(f"Error loading config: {e} — broken file copied to {bad}; "
                             "config saves are DISABLED until it is repaired")
            except Exception:  # noqa: BLE001
                logger.error(f"Error loading config: {e} — config saves are DISABLED")

    def _load_selected_registers(self):
        """Load selected registers configuration."""
        if not self.registers_path.exists():
            logger.warning(f"Selected registers file not found: {self.registers_path}")
            return

        try:
            with open(self.registers_path, 'r') as f:
                data = json.load(f)

            # Poll groups from registers file
            if 'poll_groups' in data:
                for name, group in data['poll_groups'].items():
                    self.poll_groups[name] = PollGroup(
                        interval=group.get('interval', 5),
                        description=group.get('description', ''),
                    )

            # Registers
            self.selected_registers = self._parse_selected_payload(data)

            logger.info(f"Loaded {len(self.selected_registers)} selected registers")

        except Exception as e:
            logger.error(f"Error loading selected registers: {e}")

    @staticmethod
    def _parse_selected_payload(data: Dict) -> List[SelectedRegister]:
        """Parse a selected-registers payload (shared by the legacy file and
        the per-device files — identical schema)."""
        out: List[SelectedRegister] = []
        for reg in data.get('registers', []):
            mqtt = reg.get('mqtt', {})
            influx = reg.get('influxdb', {})
            ui = reg.get('ui', {})

            out.append(SelectedRegister(
                address=reg['address'],
                name=reg['name'],
                label=reg.get('label', reg['name']),
                unit=reg.get('unit', ''),
                data_type=reg.get('data_type', 'float'),
                poll_group=reg.get('poll_group', 'normal'),
                description=reg.get('description', ''),
                json_path=reg.get('json_path', ''),
                topic=reg.get('topic', ''),
                scale=float(reg.get('scale', 1) or 1),
                nan=reg.get('nan'),
                register_type=normalize_register_type(reg.get('register_type') or reg.get('fc')),
                mqtt_enabled=mqtt.get('enabled', True),
                mqtt_topic=mqtt.get('topic', ''),
                influxdb_enabled=influx.get('enabled', True),
                influxdb_measurement=influx.get('measurement', ''),
                influxdb_tags=influx.get('tags', {}),
                ui_show_on_dashboard=ui.get('show_on_dashboard', True),
                ui_widget=ui.get('widget', 'value'),
                ui_config=ui,
                thresholds=reg.get('thresholds'),
            ))
        return out

    def _load_all_registers(self):
        """Load all available registers from modbus_data.json."""
        if not self.all_registers_path.exists():
            logger.warning(f"All registers file not found: {self.all_registers_path}")
            return

        try:
            with open(self.all_registers_path, 'r') as f:
                self.all_registers = json.load(f)
            logger.info(f"Loaded all registers from {self.all_registers_path}")
        except Exception as e:
            logger.error(f"Error loading all registers: {e}")

    def _apply_env_overrides(self):
        """Apply environment variable overrides."""
        # Modbus
        if os.getenv('MODBUS_HOST'):
            self.modbus.host = os.getenv('MODBUS_HOST')
        if os.getenv('MODBUS_PORT'):
            self.modbus.port = int(os.getenv('MODBUS_PORT'))
        if os.getenv('MODBUS_UNIT_ID'):
            self.modbus.unit_id = int(os.getenv('MODBUS_UNIT_ID'))
        if os.getenv('MODBUS_STALE_AFTER_S'):
            self.modbus.stale_after_s = int(os.getenv('MODBUS_STALE_AFTER_S'))

        # MQTT
        if os.getenv('MQTT_ENABLED'):
            self.mqtt.enabled = os.getenv('MQTT_ENABLED').lower() == 'true'
        if os.getenv('MQTT_BROKER'):
            self.mqtt.broker = os.getenv('MQTT_BROKER')
        if os.getenv('MQTT_PORT'):
            self.mqtt.port = int(os.getenv('MQTT_PORT'))
        if os.getenv('MQTT_USERNAME'):
            self.mqtt.username = os.getenv('MQTT_USERNAME')
        if os.getenv('MQTT_PASSWORD'):
            self._env_secret_shadow['mqtt.password'] = self.mqtt.password
            self.mqtt.password = os.getenv('MQTT_PASSWORD')
        if os.getenv('MQTT_PREFIX'):
            self.mqtt.topic_prefix = os.getenv('MQTT_PREFIX')
        if os.getenv('MQTT_PUBLISH_MODE'):
            self.mqtt.publish_mode = os.getenv('MQTT_PUBLISH_MODE')

        # InfluxDB
        if os.getenv('INFLUXDB_ENABLED'):
            self.influxdb.enabled = os.getenv('INFLUXDB_ENABLED').lower() == 'true'
        if os.getenv('INFLUXDB_URL'):
            self.influxdb.url = os.getenv('INFLUXDB_URL')
        if os.getenv('INFLUXDB_TOKEN'):
            self._env_secret_shadow['influxdb.token'] = self.influxdb.token
            self.influxdb.token = os.getenv('INFLUXDB_TOKEN')
        if os.getenv('INFLUXDB_ORG'):
            self.influxdb.org = os.getenv('INFLUXDB_ORG')
        if os.getenv('INFLUXDB_BUCKET'):
            self.influxdb.bucket = os.getenv('INFLUXDB_BUCKET')
        if os.getenv('INFLUXDB_PUBLISH_MODE'):
            self.influxdb.publish_mode = os.getenv('INFLUXDB_PUBLISH_MODE')

        # UI
        if os.getenv('UI_PORT'):
            self.ui.port = int(os.getenv('UI_PORT'))

        # ESPHome / Device Builder. ESPHOME_URL doubles as the zero-config
        # seed: on a FRESH deploy (no esphome: block in config.yaml yet) its
        # presence enables the Builder outright — compose ships the esphome
        # service and points us at it, so everything works out of the box.
        # Once a block exists (any UI save), the user's enabled/disabled
        # choice wins unless ESPHOME_ENABLED explicitly overrides it.
        if any(os.getenv(k) for k in ('ESPHOME_URL', 'ESPHOME_ENABLED',
                                      'ESPHOME_USERNAME', 'ESPHOME_PASSWORD')):
            e = dict(self.esphome or {})
            seeded = not e
            if os.getenv('ESPHOME_URL'):
                e['url'] = os.getenv('ESPHOME_URL')
            if os.getenv('ESPHOME_USERNAME'):
                e['username'] = os.getenv('ESPHOME_USERNAME')
            if os.getenv('ESPHOME_PASSWORD'):
                self._env_secret_shadow['esphome.password'] = str(
                    e.get('password', '') or '')
                e['password'] = os.getenv('ESPHOME_PASSWORD')
            if os.getenv('ESPHOME_ENABLED'):
                e['enabled'] = os.getenv('ESPHOME_ENABLED').lower() == 'true'
            elif seeded and e.get('url'):
                e['enabled'] = True
            self.esphome = e

    def save_selected_registers(self, registers: List[Dict]):
        """Save selected registers to file."""
        data = {
            "version": "1.0",
            "registers": registers,
            "poll_groups": {
                name: {"interval": group.interval, "description": group.description}
                for name, group in self.poll_groups.items()
            }
        }

        # hold _file_lock like every other config writer: for the PRIMARY device
        # this file is shared with save_energy_fields/save_calculated/... which
        # take the lock; without it here a concurrent save could lose fields or
        # clobber the shared .tmp
        with self._file_lock:
            tmp = self.registers_path.with_suffix(self.registers_path.suffix + '.tmp')
            with open(tmp, 'w') as f:                   # atomic: crash-safe write
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())           # durable before the rename
            os.replace(tmp, self.registers_path)

        # Reload
        self._load_selected_registers()
        logger.info(f"Saved {len(registers)} selected registers")

    def get_registers_by_poll_group(self) -> Dict[str, List[SelectedRegister]]:
        """Group selected registers by poll group."""
        groups = {}
        for reg in self.selected_registers:
            if reg.poll_group not in groups:
                groups[reg.poll_group] = []
            groups[reg.poll_group].append(reg)
        return groups

    def to_dict(self) -> Dict:
        """Export config as dictionary."""
        return {
            "modbus": {
                "host": self.modbus.host,
                "port": self.modbus.port,
                "unit_id": self.modbus.unit_id,
                "timeout": self.modbus.timeout,
                "retry_attempts": self.modbus.retry_attempts,
                "retry_delay": self.modbus.retry_delay,
                "stale_after_s": self.modbus.stale_after_s,
                "max_gap": self.modbus.max_gap,
            },
            "mqtt": {
                "enabled": self.mqtt.enabled,
                "broker": self.mqtt.broker,
                "port": self.mqtt.port,
                "username": self.mqtt.username,
                "topic_prefix": self.mqtt.topic_prefix,
                "retain": self.mqtt.retain,
                "qos": self.mqtt.qos,
                "publish_mode": self.mqtt.publish_mode,
                "heartbeat_interval": self.mqtt.heartbeat_interval,
                "ha_discovery_enabled": self.mqtt.ha_discovery_enabled,
                "ha_discovery_prefix": self.mqtt.ha_discovery_prefix,
                "ha_device_name": self.mqtt.ha_device_name,
                "tls_enabled": self.mqtt.tls_enabled,
                "tls_ca_cert": self.mqtt.tls_ca_cert,
                "tls_client_cert": self.mqtt.tls_client_cert,
                "tls_client_key": self.mqtt.tls_client_key,
                "tls_insecure": self.mqtt.tls_insecure,
                "default_topic_pattern": self.mqtt.default_topic_pattern,
            },
            "influxdb": {
                "enabled": self.influxdb.enabled,
                "url": self.influxdb.url,
                "org": self.influxdb.org,
                "bucket": self.influxdb.bucket,
                "write_interval": self.influxdb.write_interval,
                "publish_mode": self.influxdb.publish_mode,
                "buffer_minutes": self.influxdb.buffer_minutes,
                "buffer_max_points": self.influxdb.buffer_max_points,
                "buffer_persist": self.influxdb.buffer_persist,
                "default_bucket_pattern": self.influxdb.default_bucket_pattern,
            },
            "poll_groups": {
                name: {"interval": g.interval, "description": g.description}
                for name, g in self.poll_groups.items()
            },
            "selected_registers_count": len(self.selected_registers),
            "devices": [d.summary() for d in self.devices],
        }

    def get_env_overrides(self) -> Dict[str, str]:
        """Return dict of environment variable overrides that are currently set."""
        overrides = {}
        env_mappings = {
            'MODBUS_HOST': 'modbus.host',
            'MODBUS_PORT': 'modbus.port',
            'MODBUS_UNIT_ID': 'modbus.unit_id',
            'MQTT_ENABLED': 'mqtt.enabled',
            'MQTT_BROKER': 'mqtt.broker',
            'MQTT_PORT': 'mqtt.port',
            'MQTT_USERNAME': 'mqtt.username',
            'MQTT_PASSWORD': 'mqtt.password',
            'MQTT_PREFIX': 'mqtt.topic_prefix',
            'MQTT_PUBLISH_MODE': 'mqtt.publish_mode',
            'INFLUXDB_ENABLED': 'influxdb.enabled',
            'INFLUXDB_URL': 'influxdb.url',
            'INFLUXDB_TOKEN': 'influxdb.token',
            'INFLUXDB_ORG': 'influxdb.org',
            'INFLUXDB_BUCKET': 'influxdb.bucket',
            'INFLUXDB_PUBLISH_MODE': 'influxdb.publish_mode',
            'UI_PORT': 'ui.port',
        }
        # Secret-bearing paths: report only that they are env-pinned, never the
        # value (this endpoint is readable without the API key). URL-valued
        # paths go through redact_url — an env URL can embed userinfo/tokens.
        secret_paths = {'mqtt.password', 'influxdb.token'}
        url_paths = {'influxdb.url'}
        for env_var, config_path in env_mappings.items():
            if os.getenv(env_var):
                if config_path in secret_paths:
                    overrides[config_path] = '***'
                elif config_path in url_paths:
                    from .redact import redact_url
                    overrides[config_path] = redact_url(os.getenv(env_var))
                else:
                    overrides[config_path] = os.getenv(env_var)
        return overrides

    def save_yaml_config(self):
        """Save current configuration to YAML file."""
        if getattr(self, '_load_failed', False):
            raise RuntimeError(
                f"config.yaml failed to load at startup — refusing to overwrite it "
                f"with defaults. Repair {self.config_path} (a copy of the broken "
                f"file was kept as .yaml.bad) and restart.")
        data = {
            'modbus': {
                'host': self.modbus.host,
                'port': self.modbus.port,
                'unit_id': self.modbus.unit_id,
                'timeout': self.modbus.timeout,
                'retry_attempts': self.modbus.retry_attempts,
                'retry_delay': self.modbus.retry_delay,
                'stale_after_s': self.modbus.stale_after_s,
                'max_gap': self.modbus.max_gap,
            },
            'mqtt': {
                'enabled': self.mqtt.enabled,
                'broker': self.mqtt.broker,
                'port': self.mqtt.port,
                'username': self.mqtt.username,
                'password': self._env_secret_shadow.get('mqtt.password', self.mqtt.password),
                'topic_prefix': self.mqtt.topic_prefix,
                'retain': self.mqtt.retain,
                'qos': self.mqtt.qos,
                'publish_mode': self.mqtt.publish_mode,
                'heartbeat_interval': self.mqtt.heartbeat_interval,
                'ha_discovery': {
                    'enabled': self.mqtt.ha_discovery_enabled,
                    'prefix': self.mqtt.ha_discovery_prefix,
                    'device_name': self.mqtt.ha_device_name,
                },
                'tls_enabled': self.mqtt.tls_enabled,
                'tls_ca_cert': self.mqtt.tls_ca_cert,
                'tls_client_cert': self.mqtt.tls_client_cert,
                'tls_client_key': self.mqtt.tls_client_key,
                'tls_insecure': self.mqtt.tls_insecure,
                'default_topic_pattern': self.mqtt.default_topic_pattern,
            },
            'influxdb': {
                'enabled': self.influxdb.enabled,
                'url': self.influxdb.url,
                'token': self._env_secret_shadow.get('influxdb.token', self.influxdb.token),
                'org': self.influxdb.org,
                'bucket': self.influxdb.bucket,
                'write_interval': self.influxdb.write_interval,
                'publish_mode': self.influxdb.publish_mode,
                'buffer_minutes': self.influxdb.buffer_minutes,
                'buffer_max_points': self.influxdb.buffer_max_points,
                'buffer_persist': self.influxdb.buffer_persist,
                'default_bucket_pattern': self.influxdb.default_bucket_pattern,
            },
            'ui': {
                'host': self.ui.host,
                'port': self.ui.port,
                'timezone': self.ui.timezone,
                'default_colors': self.ui.default_colors,
                'auth': {
                    'enabled': self.ui.auth_enabled,
                    'username': self.ui.auth_username,
                    'password': self.ui.auth_password,
                    'viewer_username': self.ui.viewer_username,
                    'viewer_password': self.ui.viewer_password,
                    'operator_username': self.ui.operator_username,
                    'operator_password': self.ui.operator_password,
                    'lockout_threshold': self.ui.lockout_threshold,
                    'lockout_minutes': self.ui.lockout_minutes,
                },
                'tls': {
                    'enabled': self.ui.tls_enabled,
                    'cert': self.ui.tls_cert,
                    'key': self.ui.tls_key,
                },
                'trusted_proxies': self.ui.trusted_proxies,
                'canonical_url': self.ui.canonical_url,
            },
            'security': {
                'allowlist': self.security.allowlist,
                'allow_nonlan_http_devices': self.security.allow_nonlan_http_devices,
                'allow_writes': self.security.allow_writes,
                'write_rate_limit_per_s': self.security.write_rate_limit_per_s,
            },
            'polling': {
                'groups': {
                    name: {'interval': g.interval, 'description': g.description}
                    for name, g in self.poll_groups.items()
                }
            }
        }

        # HTTP/JSON output sink for the primary device (opt-in; off = omit for a
        # clean file). Non-primary flags live inside their `devices[]` entry.
        if self.http_output_primary_enabled:
            data['http_output'] = {'enabled': True}

        # Generic REST push for the primary (write when configured).
        if self.rest_push_primary.get('enabled') or self.rest_push_primary.get('url'):
            data['rest_push'] = self.rest_push_primary

        # Additional southbound devices (Tier 2). The primary device is NOT
        # written here — it lives in the flat sections above (invisible
        # migration; rollback-safe).
        if self._raw_devices:
            data['devices'] = self._raw_devices

        # Preserve the optional alerts block across saves (device/config edits
        # rewrite this file; without this a save would silently drop alerting).
        if self.alerts:
            data['alerts'] = self.alerts

        # Preserve the optional esphome block for the same reason. The stored
        # password is the config one — never an ESPHOME_PASSWORD env secret.
        if self.esphome:
            e = dict(self.esphome)
            if 'esphome.password' in self._env_secret_shadow:
                e['password'] = self._env_secret_shadow['esphome.password']
                if not e['password']:
                    e.pop('password', None)
            data['esphome'] = e

        # Ensure config directory exists
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write: a crash mid-write must never truncate the live config
        # (same pattern the per-device register files use). The file holds secrets
        # (password hashes, MQTT/Influx tokens) so it is created 0600 from the
        # start — os.open with the mode avoids the open→chmod race a plain
        # open()+chmod would leave.
        # serialize with the same lock the register writers use: two concurrent
        # config saves (device CRUD vs a settings save, both on FastAPI's
        # threadpool) share config.yaml.tmp (O_TRUNC) — without the lock one
        # os.replace could publish a half-written mix, the other 500 on a
        # vanished tmp.
        tmp = self.config_path.with_suffix(self.config_path.suffix + '.tmp')
        with self._file_lock:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
                f.flush()
                os.fsync(f.fileno())           # durable before the rename
            os.replace(tmp, self.config_path)
        try:
            os.chmod(self.config_path, 0o600)   # tighten an already-existing file too
        except OSError:
            pass

        logger.info(f"Saved config to {self.config_path}")

    @staticmethod
    def _apply_updates(target, fields: dict) -> None:
        """Set each provided non-None field on ``target`` — the shared body of
        the update_* section setters. They used to hand-list every field; the
        Pydantic request model and the dataclass already enumerate them, so this
        stays field-agnostic (an unknown field is a programming error -> raise)."""
        for k, v in fields.items():
            if v is None:
                continue
            if not hasattr(target, k):
                raise AttributeError(f"unknown config field {k!r}")
            setattr(target, k, v)

    def update_modbus(self, **fields):
        """Update Modbus configuration (non-None fields only)."""
        self._apply_updates(self.modbus, fields)

    def update_mqtt(self, **fields):
        """Update MQTT configuration (non-None fields only)."""
        self._apply_updates(self.mqtt, fields)

    def update_influxdb(self, **fields):
        """Update InfluxDB configuration (non-None fields only)."""
        self._apply_updates(self.influxdb, fields)