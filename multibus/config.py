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

from . import __version__
from .tombstone_store import TombstoneStore


# register spans (in 16-bit words) per data type — for overlap validation
_TYPE_SPANS = {'int16': 1, 'uint16': 1, 'sm16': 1, 'bool': 1,
               'int32': 2, 'uint32': 2, 'sm32': 2, 'float': 2, 'float32': 2,
               'int64': 4, 'uint64': 4, 'double': 4, 'float64': 4}


def validate_register_identity(registers: List[Dict]) -> None:
    """Reject identity collisions in one device's selection (audit DP-9).

    Nothing used to stop: (a) two registers at the same (register_type,
    address) — distinct Modbus address spaces collapse onto one store key,
    last-write-wins and the other register silently vanishes; (b) span
    overlaps (a float@100 plus an int16@101 decoding the float's low word as
    its own reading); (c) duplicate names — nondeterministic vmeter binding
    plus a shared MQTT topic and Influx series (the class caught LIVE in the
    per-phase energy leaves at Migration B). Template validation covered
    templates only; CSV/YAML/manual edits reached the store unchecked.
    Raises ValueError naming the exact collision.

    Span OVERLAPS are only WARNED about: live production maps legitimately
    read overlapping windows (fronius_rtu serves an int32@10 alongside a
    uint16@11), and on HTTP/MQTT devices the address is a synthetic index
    where spans mean nothing — a hard reject would break healthy configs."""
    seen_addr: Dict[tuple, str] = {}
    seen_name: Dict[str, int] = {}
    spans: Dict[str, List[tuple]] = {}
    for r in registers:
        name = str(r.get('name') or '')
        addr = int(r.get('address', -1))
        rtype = str(r.get('register_type') or 'holding')
        key = (rtype, addr)
        if key in seen_addr:
            raise ValueError(f"duplicate address {addr} ({rtype}): "
                             f"{seen_addr[key]!r} and {name!r} would overwrite "
                             f"each other in the live store")
        seen_addr[key] = name
        if name:
            if name in seen_name:
                raise ValueError(
                    f"duplicate register name {name!r} (addresses "
                    f"{seen_name[name]} and {addr}): the name drives the "
                    f"vmeter binding, the MQTT topic and the Influx series — "
                    f"they must be unique per device")
            seen_name[name] = addr
        dt = str(r.get('data_type') or 'uint16').lower()
        span = (max(1, int(r.get('length') or 1)) if dt == 'string'
                else _TYPE_SPANS.get(dt, 2))
        spans.setdefault(rtype, []).append((addr, addr + span, name))
    for rtype, rows in spans.items():
        rows.sort()
        for (s1, e1, n1), (s2, e2, n2) in zip(rows, rows[1:]):
            if s2 < e1:
                logger.warning(
                    "register span overlap (%s): %r @%s (ends %s) overlaps "
                    "%r @%s — fine if intentional (overlapping reads are "
                    "legal); check it if %r decodes garbage",
                    rtype, n1, s1, e1, n2, s2, n2)
    # Canonical-name unit contract (WARNING only — loosening-only rule: a
    # mismatch is legal but almost always a scale mistake). A canonical name
    # promises the canonical unit to every consumer; a kWh value under an
    # energy_* name is a silent 1000x error the moment the register feeds a
    # vmeter, a fallback twin or a cross-device dashboard.
    from .canonical_fields import canonical_unit_for
    for r in registers:
        name = str(r.get('name') or '')
        cu = canonical_unit_for(name)
        unit = str(r.get('unit') or '').strip()
        if cu and unit and unit != cu:
            logger.warning(
                "register %r declares unit %r but the canonical unit for "
                "this name is %r — adjust the scale to deliver %s (e.g. a "
                "native kWh map needs scale/1000), or rename the register "
                "if it truly measures something else", name, unit, cu, cu)


def _devices_in(doc) -> int:
    if not isinstance(doc, dict):
        return 0
    d = doc.get('devices')
    return len(d) if isinstance(d, list) else 0


def _version_tuple(v: str) -> tuple:
    """Numeric-compare form of a version string; unparsable parts count as 0
    (fail-open: a malformed stamp must never block a config load)."""
    out = []
    for part in str(v).split("."):
        digits = re.match(r"\d+", part)
        out.append(int(digits.group()) if digits else 0)
    return tuple(out)

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
    # Startup jitter: each poll group waits a random delay in [0, min(interval,
    # startup_jitter_s)] before its FIRST read, so several devices/groups don't
    # fire in lock-step and hammer a shared transport (e.g. the RTU-over-TCP
    # serial bridge) at boot. 0 = off (all groups fire immediately, as before).
    startup_jitter_s: float = 0.0
    # Serialize every Modbus transaction that shares this TCP endpoint
    # (host:port) with other devices. Cheap gateways — a Fronius DataManager,
    # most RS-485-over-TCP bridges — serialize internally and serve a handful
    # of clients: N devices polling one of them at once do not go faster, they
    # go slower and start timing out. Queueing in the gateway turns a race back
    # into an orderly line. Off by default (a device with the endpoint to
    # itself gains nothing); endpoints turn it on, because an endpoint IS one endpoint.
    serialize_endpoint: bool = True
    # Share ONE socket with every other device behind the same access point.
    # A master device (DataManager, Modbus TCP/RTU gateway, RS-485 bridge)
    # fronts its units on one connection: giving each unit its own does not
    # make them independent — they still queue inside the master — and a master
    # that serves a handful of clients runs out of them. Per-unit counters,
    # health and reachability are unaffected: units share a wire, not an
    # identity. Set false to go back to a socket per unit.
    share_transport: bool = True
    # How many sockets that access point is worth. One is the safe answer and
    # the default: a master that serializes internally (a Fronius DataManager —
    # measured ~0.4 s per read alone, ~3 s with five callers racing) gains
    # nothing from a second, and loses its remaining client slots. A master with
    # per-unit engines behind it — an RS-485 bridge with several lines, a PLC
    # front end — overlaps happily, and there two or three lanes cut the sweep
    # proportionally. Units are distributed sticky across the lanes by unit id,
    # so a unit always rides the same socket. Which number yours wants is a
    # measurement, not a guess: scripts/calibrate_endpoint.py makes it.
    max_connections: int = 1
    # How long a read may wait for its turn before giving up on the cycle. A
    # missed turn is not a device failure: the value is simply skipped, exactly
    # as if that cycle had not come round yet.
    endpoint_wait_s: float = 10.0
    # Breathing room between consecutive transactions on this access point.
    # A master device is a small computer with its own job — a Fronius
    # DataManager has to poll its RS-485 side while it answers us — and hit
    # back-to-back it starves that side. The symptom is not a polite slowdown
    # but collapse: measured on a production one, demanding 32 transactions a
    # minute returned 21.6 with 6.5 % errors, while demanding 26 returned 26.
    # The long-lived collector this replaces never had that problem because it
    # waits a full second after every device and 200 ms between register
    # blocks, leaving the datalogger idle about a fifth of the time ON PURPOSE.
    # 0 (the default) keeps the old behaviour: nothing changes for a device
    # with its bus to itself, or a gateway that does not care.
    endpoint_min_gap_s: float = 0.0
    # Addresses this slave answers with ILLEGAL DATA ADDRESS (exception 02).
    # The batch builder never bridges a merged read across one of these (and
    # skips a selected register that sits on one), so a single unmapped address
    # can't poison a whole block. Use when max_gap=0 isn't enough (a hole INSIDE
    # a contiguous run). Per-device.
    illegal_registers: List[int] = field(default_factory=list)
    # Data-readiness gate: a sleepy device (an inverter at night) can answer a
    # read with an ALL-ZERO frame instead of an error; published as-is that reads
    # as real 0 V / 0 W data. With this on, a poll group whose numeric values are
    # ALL exactly zero (and there are >=2 of them) is DROPPED — the cache keeps
    # the last-good values until the device wakes. Off by default (a legitimately
    # all-zero group would be withheld); enable for devices that sleep.
    drop_all_zero: bool = False
    # transport: "tcp" (host/port) or "rtu" (serial line below)
    protocol: str = "tcp"
    serial_port: str = ""             # e.g. /dev/ttyUSB0
    baudrate: int = 9600
    parity: str = "N"                 # N | E | O
    stopbits: int = 1
    bytesize: int = 8


def _serial_from_conn(c: Dict) -> Dict[str, Any]:
    return {k: c[k] for k in
            ('serial_port', 'baudrate', 'parity', 'stopbits', 'bytesize') if k in c}


def _http_from_conn(c: Dict) -> Dict[str, Any]:
    return {k: c[k] for k in ('url', 'timeout', 'headers', 'verify_tls') if k in c}


def _mqtt_in_from_conn(c: Dict) -> Dict[str, Any]:
    return {k: c[k] for k in
            ('broker', 'port', 'username', 'password', 'tls', 'topic') if k in c}


PRIMARY_DEVICE_ID = "umg512"


@dataclass
class SourceConfig:
    """One WAY OF REACHING a unit — not another unit.

    A master device offers the same slave over several protocols. A Fronius
    DataManager speaks Modbus TCP (complete, measured 1945-2376 ms a read) and
    a Solar API over HTTP (partial, 54 ms, and it does not disturb the Modbus
    side). Before this, using both meant declaring two endpoints, which meant
    two device identities, two topic trees and two sets of aggregates for one
    physical inverter. Identity belongs to the UNIT; a source is only how the
    value arrived.

    Sources are ordered, and the order IS the precedence: the first source that
    offers a field owns it. A later source fills that field only once the
    earlier one has gone stale past its ``stale_after_s`` — which is what makes
    failover automatic when a cached HTTP view freezes, without the flapping
    that "freshest wins" would produce.
    """
    id: str
    template: str = ""
    enabled: bool = True
    protocol: str = "tcp"                    # tcp | rtu | rtu-tcp | http | mqtt
    connection: "ModbusConfig" = field(default_factory=lambda: ModbusConfig())
    serial: Dict[str, Any] = field(default_factory=dict)
    http: Dict[str, Any] = field(default_factory=dict)
    mqtt_in: Dict[str, Any] = field(default_factory=dict)
    # Per-source intervals. A slow, complete source and a fast, partial one do
    # not share a polling rhythm, so the groups belong to the source and not to
    # the device.
    poll_groups: Dict[str, "PollGroup"] = field(default_factory=dict)
    # How long this source's value stays authoritative before a lower-ranked
    # source may fill the field. 0 = never yields (the right answer for a
    # counter, which must never alternate between sources or it goes
    # non-monotonic).
    stale_after_s: float = 0.0


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
    # The ordered ways of reaching this unit. Always at least one: a plain
    # `connection:` is the shorthand for a single source named `default`, so
    # `connection` above stays exactly what it has always been — the FIRST
    # source's transport. Order is precedence (see SourceConfig).
    sources: List["SourceConfig"] = field(default_factory=list)
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
    pq_recorder: Dict[str, Any] = field(default_factory=dict)  # Jasic PQ event recorder: {enabled,poll_s,archive_waveforms,base_url}
    endpoint_id: str = ""    # non-empty → materialized from a `endpoints:` entry
                          # (managed via the endpoint, not the device CRUD)
    write_locked: bool = False   # per-device write lock (F3a): refuses every
                                 # write regardless of guards. The primary
                                 # defaults to locked (security.primary_write_locked).

    def summary(self) -> Dict[str, Any]:
        return {
            'id': self.id, 'name': self.name, 'template': self.template,
            'enabled': self.enabled, 'primary': self.primary,
            'endpoint_id': self.endpoint_id, 'write_locked': self.write_locked,
            'ha_discovery_enabled': self.ha_discovery_enabled,
            'mqtt_enabled': self.mqtt_enabled,
            'influxdb_enabled': self.influxdb_enabled,
            'http_output_enabled': self.http_output_enabled,
            'rest_push_enabled': bool(self.rest_push.get('enabled')),
            'pq_recorder_enabled': bool(self.pq_recorder.get('enabled')),
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
    # default = the bundled compose broker, so a fresh deploy publishes from
    # the first boot with zero configuration; bare-metal/external setups
    # override from the UI (Config -> MQTT) or MQTT_BROKER
    broker: str = "mosquitto"
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
    # Expose writable registers as HA number/select entities AND subscribe to
    # their command topics to perform the write. OFF by default: this turns
    # broker-publish access into hardware-write access, so it is DOUBLE-gated —
    # a command is only executed when this AND security.allow_writes are true,
    # the register is declared writable, and the value is within its envelope.
    allow_write_entities: bool = False
    # Default topic prefix pattern for NEW devices ({device} = the device id).
    # Device #1 keeps its migrated prefix; this only seeds new devices.
    # Namespace convention (2026-09-11): device values live under
    # mbg/devices/<id>/… (entity-typed root — mbg/vmeter/<id>/… is reserved
    # for virtual-meter MQTT publishing when that lands). Pre-existing devices
    # keep their persisted prefixes (routing identity is fixed); this only
    # seeds NEW devices.
    default_topic_pattern: str = "mbg/devices/{device}"
    # Compatibility aliases (topic migrations, e.g. janitza/… → meters/…):
    # every publish whose topic starts with `from` is ALSO published under
    # `to`, with `leaves` renaming individual topic tails (old consumers keep
    # receiving byte-identical topics during a rename transition). Shape:
    #   [{from: meters/umg512, to: janitza/umg512,
    #     leaves: {energy/active/import: energy/active/consumed}}]
    compat_aliases: List[Dict] = field(default_factory=list)
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
    # default = the bundled compose InfluxDB, so enabling the sink in the UI
    # only needs the token pasted in (org/bucket defaults match the compose
    # first-boot setup)
    url: str = "http://influxdb:8086"
    token: str = ""
    org: str = "multibus"
    bucket: str = "multibus"
    write_interval: int = 5
    publish_mode: str = "changed"  # "changed" or "all"
    # Default bucket pattern for NEW devices ({device} = the device id).
    # Device #1 keeps its migrated bucket; this only seeds new devices.
    default_bucket_pattern: str = "{device}"
    # Store-and-forward buffer: points that cannot be delivered (InfluxDB down)
    # are kept in RAM and replayed with their original timestamps on reconnect.
    # Age window of the replay buffer, relative to its NEWEST point (a window
    # of data, not of wall time — audit DP-12/28: the old 10-min wall-relative
    # window silently capped real outage protection at ~10 minutes no matter
    # what the count limit said). 120 min of typical full-rate data (~13 pt/s)
    # is ~94k points; the count cap below is the hard RAM bound (~40 MB worst
    # case) and binds first on high-rate configs.
    buffer_minutes: int = 120
    buffer_max_points: int = 200000   # hard cap (drop-oldest beyond this)
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
    # true. Off by default — writing to real hardware is irreversible.
    allow_writes: bool = False
    # Write LOCK for the primary device (F3a): read-only used to be hardcoded;
    # it is now a per-device lock, and the primary DEFAULTS to locked so the
    # historical behavior survives every upgrade. Unlock deliberately here.
    primary_write_locked: bool = True
    # Per-client-IP write rate limit (writes/second) on the Modbus write API. A
    # flood of writes contends the shared Modbus lock and can starve the pollers;
    # excess writes get 429. 0 disables the limit. Generous by default so a normal
    # control loop is unaffected.
    write_rate_limit_per_s: float = 10.0


@dataclass
class UIConfig:
    # SAFE default: loopback only (external audit B5 — ~120 routes used to be
    # LAN-reachable, unauthenticated, out of the box). The container image
    # sets UI_HOST=0.0.0.0 explicitly — inside a container the namespace is
    # isolated and exposure is governed by the compose port mapping; on bare
    # metal, listening on every interface is an explicit opt-in
    # (ui.host: 0.0.0.0 / UI_HOST env / --host).
    host: str = "127.0.0.1"
    port: int = 8080
    auth_enabled: bool = False
    auth_username: str = "admin"
    # empty by default (external audit: the old "admin" literal + the
    # plaintext-compat fallback meant a hand-enabled auth accepted
    # admin/admin from the whole LAN; the shipped example already says "")
    auth_password: str = ""
    # read-only viewer account (optional) — GET only, no config changes
    viewer_username: str = ""
    viewer_password: str = ""
    # operator account (optional) — live actions (writes within template
    # bounds, diagnostics, discovery) but NO configuration changes
    operator_username: str = ""
    operator_password: str = ""
    # Canonical UI URL: when set (e.g. https://gateway.example.com), the browser is
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


def parse_address_list(raw) -> List[int]:
    """Coerce a config list of addresses (ints or ``0x…``/decimal strings) to a
    sorted, de-duplicated list of ints in the Modbus range. Bad entries are
    dropped, not raised — a typo must not down a device."""
    out = set()
    for a in (raw or []):
        try:
            n = int(a, 0) if isinstance(a, str) else int(a)
        except (TypeError, ValueError):
            continue
        if 0 <= n <= 0xFFFF:
            out.add(n)
    return sorted(out)


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
    offset: float = 0.0   # engineering_value = raw / scale + offset (zero-point / unit shift)
    scale_from: str = ""  # dynamic SF: sibling register NAME whose raw value is the
                          # base-10 exponent → engineering = raw × 10^SF; a fixed
                          # `scale` may accompany it and divides AFTER the exponent
                          # (unit conversion, e.g. SunSpec PF percent → fraction)
    nan: Any = None       # not-available sentinel (True=std for type / value / list) → missing
    monotonic: bool = False   # cumulative counter (energy): reject downward glitches → HA/Victron safe
    # status-register decode (mutually exclusive): raw int → text
    enum: Optional[Dict[Any, str]] = None   # {code: label}; unmapped → "unknown (n)"
    bits: Optional[Dict[Any, str]] = None   # {bit: name}; joined names of set bits
    mask: Optional[int] = None              # enum sub-field: (raw & mask) >> shift
    shift: Optional[int] = None
    # ── explicit Home Assistant entity typing (each overrides the unit heuristic;
    # unset ("" / None) falls back to inference; "none" suppresses that key) ──
    device_class: str = ""            # HA device_class (e.g. power, energy, voltage)
    state_class: str = ""             # HA state_class (measurement | total | total_increasing)
    entity_category: str = ""         # "diagnostic" | "config" → HA groups it out of the main view
    enabled_by_default: Optional[bool] = None   # False → HA hides the entity until enabled
    icon: str = ""                    # mdi icon, e.g. "mdi:flash"
    suggested_display_precision: Optional[int] = None   # HA decimal places
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
        # Endpoints (one template + one endpoint + N unit ids → N materialized
        # devices). Raw yaml-shaped list; expanded in _build_devices().
        self._raw_endpoints: List[Dict] = []
        # HTTP/JSON output sink for the PRIMARY device (non-primary devices carry
        # their own flag in the raw `devices[]` list). Off by default — the
        # /api/meters/<id> endpoint is opt-in per device.
        self.http_output_primary_enabled: bool = False
        # Generic REST push for the PRIMARY device (non-primary carry their own in
        # the devices[] list). {enabled,url,interval_s,headers,format,verify_tls,timeout}.
        self.rest_push_primary: Dict[str, Any] = {}
        # Jasic PQ event recorder for the PRIMARY device (non-primary carry their
        # own block). {enabled,poll_s,archive_waveforms,base_url}. See pq_recorder.py.
        self.pq_recorder_primary: Dict[str, Any] = {}
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
            pq_recorder=dict(self.pq_recorder_primary or {}),
            write_locked=self.security.primary_write_locked,
        )]
        for d in self._raw_devices:
            did = str(d.get('id', '')).strip()
            if not did:
                logger.warning("devices[]: entry without id skipped")
                continue
            if did == PRIMARY_DEVICE_ID or any(x.id == did for x in devices):
                logger.warning(f"devices[]: duplicate id {did!r} skipped")
                continue
            dev = self._device_from_raw(d)
            if dev is not None:
                devices.append(dev)
        # Endpoints: one template + one endpoint + N unit ids → N materialized
        # devices, each with its OWN socket. Deliberate: unit-switching on a
        # shared socket corrupts some gateway buffers (Fronius DataManager),
        # and independent sockets make units fail independently.
        for pid, d in self._expand_endpoints():
            did = str(d.get('id', '')).strip()
            if did == PRIMARY_DEVICE_ID or any(x.id == did for x in devices):
                logger.warning(f"endpoints[{pid}]: materialized id {did!r} "
                               f"collides with an existing device — skipped")
                continue
            dev = self._device_from_raw(d, endpoint_id=pid)
            if dev is not None:
                devices.append(dev)
        self.devices = devices

    def _modbus_from_conn(self, conn: Dict) -> "ModbusConfig":
        """Build the transport settings from one raw connection dict. Shared by
        the device and by every source, so a knob added here reaches both."""
        return ModbusConfig(
            host=conn.get('host', ''),
            port=int(conn.get('port', 502)),
            unit_id=int(conn.get('unit_id', 1)),
            timeout=conn.get('timeout', 3),
            retry_attempts=int(conn.get('retry_attempts', 3)),
            retry_delay=float(conn.get('retry_delay', 1.0)),
            stale_after_s=int(conn.get('stale_after_s', 30)),
            max_gap=int(conn.get('max_gap', 10)),
            startup_jitter_s=float(conn.get('startup_jitter_s',
                self.modbus.startup_jitter_s) or 0.0),
            illegal_registers=parse_address_list(conn.get('illegal_registers')),
            drop_all_zero=bool(conn.get('drop_all_zero', False)),
            serialize_endpoint=bool(conn.get('serialize_endpoint', True)),
            share_transport=bool(conn.get('share_transport', True)),
            max_connections=max(1, int(conn.get('max_connections', 1) or 1)),
            endpoint_wait_s=float(conn.get('endpoint_wait_s', 10.0)),
            endpoint_min_gap_s=max(0.0, float(conn.get('endpoint_min_gap_s', 0.0) or 0.0)),
            protocol=str(conn.get('protocol', 'tcp')).lower(),
            serial_port=conn.get('serial_port', ''),
            baudrate=int(conn.get('baudrate', 9600)),
            parity=str(conn.get('parity', 'N')),
            stopbits=int(conn.get('stopbits', 1)),
            bytesize=int(conn.get('bytesize', 8)),
        )

    def _sources_from_raw(self, d: Dict) -> List["SourceConfig"]:
        """The ordered ways of reaching this unit.

        ``sources:`` is the full form. A plain ``connection:`` is the shorthand
        for exactly one source named ``default`` — which is what every existing
        config is, so nothing has to be rewritten and ``dev.connection`` keeps
        meaning what it always meant (the first source's transport).
        """
        raw = d.get('sources')
        if not raw:
            raw = [{'id': 'default', 'template': d.get('template', ''),
                    **(d.get('connection', {}) or {})}]
        out: List[SourceConfig] = []
        seen = set()
        for i, r in enumerate(raw):
            if not isinstance(r, dict):
                logger.warning("sources[]: entry %d is not a mapping — skipped", i)
                continue
            sid = str(r.get('id', '') or f'source{i + 1}').strip()
            if sid in seen:
                # Two sources with one name would make provenance ambiguous,
                # which defeats the point of recording it at all.
                logger.warning("sources[]: duplicate id %r skipped", sid)
                continue
            seen.add(sid)
            groups = {k: PollGroup(interval=v.get('interval', 5),
                                   description=v.get('description', ''))
                      if isinstance(v, dict) else PollGroup(interval=float(v))
                      for k, v in (r.get('poll_groups') or {}).items()}
            try:
                out.append(SourceConfig(
                    id=sid,
                    template=str(r.get('template', '') or d.get('template', '')),
                    enabled=bool(r.get('enabled', True)),
                    protocol=str(r.get('protocol', 'tcp')).lower(),
                    connection=self._modbus_from_conn(r),
                    serial=_serial_from_conn(r),
                    http=_http_from_conn(r),
                    mqtt_in=_mqtt_in_from_conn(r),
                    poll_groups=groups,
                    stale_after_s=max(0.0, float(r.get('stale_after_s', 0.0) or 0.0)),
                ))
            except (ValueError, TypeError) as e:
                logger.warning("sources[]: skipping %r — invalid config: %s", sid, e)
        return out

    def _device_from_raw(self, d: Dict, endpoint_id: str = "") -> Optional[DeviceConfig]:
        """Build ONE DeviceConfig from its raw yaml dict (a devices[] entry or
        an endpoint-materialized dict). Returns None (with a warning) on invalid
        values — a single malformed entry must not crash the whole boot."""
        did = str(d.get('id', '')).strip()
        conn = d.get('connection', {}) or {}
        mqtt_cfg = d.get('mqtt', {}) or {}
        influx_cfg = d.get('influxdb', {}) or {}
        http_out_cfg = d.get('http_output', {}) or {}
        prefix = mqtt_cfg.get('topic_prefix', 'mbg/devices/${device_id}')
        prefix = prefix.replace('${device_id}', did).replace('${id}', did)
        sources = self._sources_from_raw(d)
        # `connection:`/`template:` stay the FIRST source's, so every caller
        # that predates sources keeps reading what it always read.
        if sources and not d.get('connection'):
            conn = {**(sources[0].http or {}), **(sources[0].mqtt_in or {}),
                    'protocol': sources[0].protocol,
                    'host': sources[0].connection.host,
                    'port': sources[0].connection.port,
                    'unit_id': sources[0].connection.unit_id}
        try:
            return DeviceConfig(
                id=did,
                endpoint_id=endpoint_id,
                write_locked=bool(d.get('write_locked', False)),
                name=d.get('name', did),
                template=d.get('template', ''),
                enabled=bool(d.get('enabled', True)),
                protocol=str(conn.get('protocol', 'tcp')).lower(),
                connection=self._modbus_from_conn(conn),
                sources=sources,
                serial=_serial_from_conn(conn),
                http=_http_from_conn(conn),
                mqtt_in=_mqtt_in_from_conn(conn),
                mqtt_topic_prefix=prefix,
                influxdb_bucket=influx_cfg.get('bucket', self.influxdb.bucket),
                influxdb_device_tag=influx_cfg.get('device_tag', did),
                ha_discovery_enabled=bool(mqtt_cfg.get('ha_discovery', True)),
                mqtt_enabled=bool(mqtt_cfg.get('enabled', True)),
                influxdb_enabled=bool(influx_cfg.get('enabled', True)),
                http_output_enabled=bool(http_out_cfg.get('enabled', False)),
                rest_push=dict(d.get('rest_push', {}) or {}),
                pq_recorder=dict(d.get('pq_recorder', {}) or {}),
            )
        except (ValueError, TypeError) as e:
            # A single malformed devices[] entry (e.g. port:"abc") must NOT
            # crash the whole boot — including the primary's polling. Skip it,
            # like the missing/duplicate-id skips above.
            logger.warning(f"devices[]: skipping {did!r} — invalid config: {e}")
            return None

    @staticmethod
    def _endpoint_units(p: Dict) -> List[Dict]:
        """Normalize an endpoint's ``units`` list. Accepts bare unit ids
        (``[1, 2, 3]``) or dicts (``{unit_id, id?, name?}``); returns
        ``{unit_id, id, name}`` with the id defaulting to ``<endpoint>-u<unit>``.
        Invalid entries are skipped with a warning."""
        pid = str(p.get('id', '')).strip()
        out: List[Dict] = []
        for u in (p.get('units') or []):
            raw_uid = u.get('unit_id') if isinstance(u, dict) else u
            try:
                uid = int(raw_uid)
                if not (0 <= uid <= 255):
                    raise ValueError
            except (TypeError, ValueError):
                logger.warning(f"endpoints[{pid}]: invalid unit {raw_uid!r} skipped")
                continue
            ov = u if isinstance(u, dict) else {}
            out.append({'unit_id': uid,
                        'id': str(ov.get('id') or f"{pid}-u{uid}").strip(),
                        'name': str(ov.get('name') or '')})
        return out

    def _expand_endpoints(self) -> List[tuple]:
        """Expand ``endpoints:`` into (endpoint_id, raw-device-dict) pairs, shaped
        exactly like devices[] entries so both flow through _device_from_raw.
        ``${unit_id}`` / ``${endpoint_id}`` / ``${device_id}`` substitute in the
        MQTT topic prefix, InfluxDB bucket, device tag and name."""
        out: List[tuple] = []
        for p in self._raw_endpoints:
            pid = str(p.get('id', '')).strip()
            if not pid:
                logger.warning("endpoints[]: entry without id skipped")
                continue
            # a disabled endpoint still materializes (units stay visible/managed);
            # enabled=False propagates so no clients/pollers are started
            conn = dict(p.get('connection', {}) or {})
            srcs = [dict(x) for x in (p.get('sources') or []) if isinstance(x, dict)]
            mqtt_cfg = dict(p.get('mqtt', {}) or {})
            influx_cfg = dict(p.get('influxdb', {}) or {})
            seen_units: set = set()
            for u in self._endpoint_units(p):
                uid, did = u['unit_id'], u['id']
                if uid in seen_units:
                    logger.warning(f"endpoints[{pid}]: duplicate unit {uid} skipped")
                    continue
                seen_units.add(uid)

                def sub(s: str) -> str:
                    return (str(s).replace('${unit_id}', str(uid))
                                  .replace('${endpoint_id}', pid)
                                  .replace('${device_id}', did))

                m = dict(mqtt_cfg)
                if m.get('topic_prefix'):
                    m['topic_prefix'] = sub(m['topic_prefix'])
                i = dict(influx_cfg)
                if i.get('bucket'):
                    i['bucket'] = sub(i['bucket'])
                if i.get('device_tag'):
                    i['device_tag'] = sub(i['device_tag'])
                # ${unit_id} / ${endpoint_id} / ${device_id} substitute in the
                # CONNECTION too, not only in the routing identity. An HTTP
                # master addresses its units by URL rather than by a unit id in
                # a frame — a Fronius Solar API endpoint is
                # `…?Scope=Device&DeviceId=${unit_id}` — so without this an
                # endpoint could not describe an HTTP master at all, and four
                # inverters would need four hand-written devices.
                def _addr(raw: dict) -> dict:
                    """One unit's view of a connection block. ${unit_id} and
                    friends substitute in the ADDRESS fields too, because an
                    HTTP master addresses its units by URL rather than by a unit
                    id inside a frame."""
                    out = dict(raw, unit_id=uid)
                    for _k in ('url', 'host', 'serial_port', 'topic'):
                        if isinstance(out.get(_k), str) and '${' in out[_k]:
                            out[_k] = sub(out[_k])
                    return out

                c = _addr(conn)
                unit_sources = [_addr(x) for x in srcs]
                out.append((pid, {
                    'id': did,
                    'name': u['name'] or f"{p.get('name') or pid} unit {uid}",
                    'template': p.get('template', ''),
                    'enabled': bool(p.get('enabled', True)),
                    'write_locked': bool(p.get('write_locked', False)),
                    'connection': c,
                    # declared once on the endpoint, resolved per unit
                    **({'sources': unit_sources} if unit_sources else {}),
                    'mqtt': m,
                    'influxdb': i,
                    # an endpoint is ONE endpoint: its sinks are declared once and
                    # apply to every unit, like the write lock above
                    **({'http_output': dict(p['http_output'])}
                       if p.get('http_output') else {}),
                    **({'rest_push': dict(p['rest_push'])}
                       if p.get('rest_push') else {}),
                }))
        return out

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
        _ex = self.get_device(did)
        if _ex is not None and _ex.endpoint_id:
            raise ValueError(f"device {did!r} is materialized from endpoint "
                             f"{_ex.endpoint_id!r} — edit the endpoint instead")
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

    def set_write_locked(self, device_id: str, locked: bool) -> None:
        """Set the per-device write lock and persist. Primary → the
        security.primary_write_locked flag; devices[] entries → their dict;
        endpoint units → the ENDPOINT's flag (applies to every unit — an endpoint is one
        physical endpoint, its units lock together)."""
        dev = self.get_device(device_id)
        if dev is None:
            raise ValueError(f"device {device_id!r} not found")
        if dev.primary:
            self.security.primary_write_locked = bool(locked)
        elif dev.endpoint_id:
            for p in self._raw_endpoints:
                if p.get('id') == dev.endpoint_id:
                    p['write_locked'] = bool(locked)
                    break
            else:
                raise ValueError(f"endpoint {dev.endpoint_id!r} not found")
        else:
            for d in self._raw_devices:
                if d.get('id') == device_id:
                    d['write_locked'] = bool(locked)
                    break
            else:
                raise ValueError(f"device {device_id!r} is not a configurable device")
        self._build_devices()
        self.save_yaml_config()

    def set_http_output(self, device_id: str, enabled: bool) -> None:
        """Enable/disable the HTTP/JSON output sink for a device and persist.
        Primary → the flat `http_output:` section; devices[] entries → their
        own block; endpoint units → the ENDPOINT's block, applying to every unit (a
        endpoint is one endpoint, so its sinks are declared once — the same rule
        the write lock follows)."""
        dev = self.get_device(device_id)
        if dev is None:
            raise ValueError(f"device {device_id!r} not found")
        if dev.primary:
            self.http_output_primary_enabled = bool(enabled)
        elif dev.endpoint_id:
            self._endpoint_entry_for(dev.endpoint_id).setdefault(
                'http_output', {})['enabled'] = bool(enabled)
        else:
            for d in self._raw_devices:
                if d.get('id') == device_id:
                    d.setdefault('http_output', {})['enabled'] = bool(enabled)
                    break
            else:
                raise ValueError(f"device {device_id!r} is not a configurable device")
        self._build_devices()
        self.save_yaml_config()

    def set_pq_recorder(self, device_id: str, cfg: Dict) -> None:
        """Set the PQ recorder config for a device and persist. Primary → flat
        `pq_recorder:` section; non-primary → their `pq_recorder` block."""
        dev = self.get_device(device_id)
        if dev is None:
            raise ValueError(f"device {device_id!r} not found")
        cfg = dict(cfg or {})
        if dev.primary:
            self.pq_recorder_primary = cfg
        else:
            for d in self._raw_devices:
                if d.get('id') == device_id:
                    d['pq_recorder'] = cfg
                    break
            else:
                raise ValueError(f"device {device_id!r} is not a configurable device")
        self._build_devices()
        self.save_yaml_config()

    def _endpoint_entry_for(self, endpoint_id: str) -> Dict:
        """The raw endpoints[] entry, for a setter that writes an endpoint-level flag."""
        for p in self._raw_endpoints:
            if p.get('id') == endpoint_id:
                return p
        raise ValueError(f"endpoint {endpoint_id!r} not found")

    def set_rest_push(self, device_id: str, cfg: Dict) -> None:
        """Set the REST push config for a device and persist. Primary → flat
        `rest_push:` section; devices[] entries → their own block; endpoint units
        → the ENDPOINT's block (one endpoint, one declaration)."""
        dev = self.get_device(device_id)
        if dev is None:
            raise ValueError(f"device {device_id!r} not found")
        cfg = dict(cfg or {})
        if dev.primary:
            self.rest_push_primary = cfg
        elif dev.endpoint_id:
            self._endpoint_entry_for(dev.endpoint_id)['rest_push'] = cfg
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

    # ── endpoints ─────────────────────────────────────────────────────────────

    @property
    def endpoints(self) -> List[Dict]:
        """The raw ``endpoints:`` entries (yaml-shaped, copies)."""
        return [dict(p) for p in self._raw_endpoints]

    def get_raw_endpoint(self, endpoint_id: str) -> Optional[Dict]:
        return next((dict(p) for p in self._raw_endpoints
                     if p.get('id') == endpoint_id), None)

    def endpoint_devices(self, endpoint_id: str) -> List[DeviceConfig]:
        """The materialized DeviceConfigs of one endpoint, in units[] order."""
        return [d for d in self.devices if d.endpoint_id == endpoint_id]

    def upsert_raw_endpoint(self, raw: Dict) -> List[DeviceConfig]:
        """Create or update an endpoint from its raw yaml-shaped dict, re-expand
        the device list and persist. Returns the materialized DeviceConfigs.
        Transactional — a build/save failure restores the previous state."""
        pid = str(raw.get('id', '')).strip()
        if not pid:
            raise ValueError("endpoint id is required")
        _snapshot = [dict(p) for p in self._raw_endpoints]
        try:
            self._raw_endpoints = ([p for p in self._raw_endpoints
                                 if p.get('id') != pid] + [raw])
            self._build_devices()
            made = self.endpoint_devices(pid)
            if not made:
                raise ValueError(f"endpoint {pid!r} produced no devices — check "
                                 "its units[] (valid unit ids 0..255, ids "
                                 "must not collide with existing devices)")
            self.save_yaml_config()
            return made
        except Exception:
            self._raw_endpoints = _snapshot
            self._build_devices()
            raise

    def delete_endpoint(self, endpoint_id: str) -> List[str]:
        """Remove an endpoint; its materialized devices vanish on rebuild. Returns
        the removed device ids (register files stay on disk for a re-add).
        Raises ValueError when the endpoint does not exist."""
        if self.get_raw_endpoint(endpoint_id) is None:
            raise ValueError(f"endpoint {endpoint_id!r} not found")
        removed = [d.id for d in self.endpoint_devices(endpoint_id)]
        self._raw_endpoints = [p for p in self._raw_endpoints
                            if p.get('id') != endpoint_id]
        self._build_devices()
        self.save_yaml_config()
        return removed

    def save_device_registers(self, device_id: str, registers: List[Dict],
                              poll_groups: Optional[Dict] = None) -> None:
        """Persist a non-primary device's register selection (same schema as
        the legacy file). Primary keeps using save_selected_registers()."""
        if device_id == PRIMARY_DEVICE_ID:
            self.save_selected_registers(registers)
            return
        validate_register_identity(registers)      # audit DP-9
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
        data = self._load_json_with_heal(path, required_key='registers',
                                         label=f"device {device.id} registers")
        if data is None:
            return [], dict(self.poll_groups)
        try:
            regs = self._parse_selected_payload(data)
            groups = dict(self.poll_groups)
            for name, group in (data.get('poll_groups') or {}).items():
                groups[name] = PollGroup(interval=group.get('interval', 5),
                                         description=group.get('description', ''))
            return regs, groups
        except Exception as e:  # noqa: BLE001
            logger.error(f"device {device.id}: error loading registers: {e}")
            return [], dict(self.poll_groups)

    def _good_would_regress(self, new_data: dict, good_path) -> bool:
        """True when promoting ``new_data`` over the existing .good snapshot
        would LOSE devices — the truncation signature (a cut file keeps the
        early modbus/mqtt sections and drops the devices tail). Comparison
        errors fail open (promote), so a weird snapshot never wedges loads."""
        try:
            if not good_path.exists():
                return False
            with open(good_path, 'r') as f:
                good = yaml.safe_load(f) or {}
            return _devices_in(new_data) < _devices_in(good)
        except Exception:  # noqa: BLE001
            return False

    def _load_yaml_config(self):
        """Load main YAML configuration."""
        if not self.config_path.exists():
            logger.warning(f"Config file not found: {self.config_path}, using defaults")
            return

        try:
            with open(self.config_path, 'r') as f:
                data = yaml.safe_load(f) or {}

            # Plausibility gate (external audit): YAML that PARSES can still be
            # a truncated/clobbered file — empty, a bare scalar, or cut before
            # the sections every MBG save writes unconditionally. Loading it
            # "successfully" is worse than a parse error, because the snapshot
            # below would then overwrite the last-known-GOOD copy with the
            # husk. Raise → the existing except path .bad-copies the file and
            # self-heals from .good. ('devices' is deliberately NOT required —
            # a save without extra devices legally omits the key.)
            if not isinstance(data, dict) or not ('modbus' in data or 'mqtt' in data):
                raise ValueError(
                    "config.yaml parsed but looks truncated/implausible "
                    f"(top-level keys: {sorted(data) if isinstance(data, dict) else type(data).__name__})")

            # Version stamp (audit MEDIUM-2): a file written by a NEWER gateway
            # may hold settings this version does not know — loading works
            # (unknown keys are simply not read), but a SAVE from here would
            # silently drop them. Warn loudly + surface in /api/status; the
            # operator decides (upgrade back, or accept the key loss).
            self.config_written_by = str(data.get('config_version') or '')
            self.config_written_by_newer = bool(
                self.config_written_by
                and _version_tuple(self.config_written_by) > _version_tuple(__version__))
            if self.config_written_by_newer:
                logger.warning(
                    "config.yaml was written by gateway %s but %s is running — "
                    "settings introduced after %s are preserved on load but "
                    "will be DROPPED by the next save from this version",
                    self.config_written_by, __version__, __version__)

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
                    startup_jitter_s=float(m.get('startup_jitter_s',
                        data.get('polling', {}).get('startup_jitter_s', 0.0)) or 0.0),
                    illegal_registers=parse_address_list(m.get('illegal_registers')),
                    drop_all_zero=bool(m.get('drop_all_zero', False)),
                    serialize_endpoint=bool(m.get('serialize_endpoint', True)),
                    share_transport=bool(m.get('share_transport', True)),
                    max_connections=max(1, int(m.get('max_connections', 1) or 1)),
                    endpoint_wait_s=float(m.get('endpoint_wait_s', 10.0)),
                    endpoint_min_gap_s=max(0.0, float(m.get('endpoint_min_gap_s', 0.0) or 0.0)),
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
                    allow_write_entities=bool(m.get('allow_write_entities',
                                                    self.mqtt.allow_write_entities)),
                    tls_enabled=m.get('tls_enabled', self.mqtt.tls_enabled),
                    tls_ca_cert=m.get('tls_ca_cert', self.mqtt.tls_ca_cert),
                    tls_client_cert=m.get('tls_client_cert', self.mqtt.tls_client_cert),
                    tls_client_key=m.get('tls_client_key', self.mqtt.tls_client_key),
                    tls_insecure=m.get('tls_insecure', self.mqtt.tls_insecure),
                    default_topic_pattern=m.get('default_topic_pattern', self.mqtt.default_topic_pattern),
                    compat_aliases=m.get('compat_aliases', self.mqtt.compat_aliases) or [],
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
                    primary_write_locked=bool(s.get('primary_write_locked', True)),
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
            if 'pq_recorder' in data:
                self.pq_recorder_primary = dict(data['pq_recorder'] or {})

            # Additional southbound devices (Tier 2) — materialized in
            # _build_devices() after env overrides.
            self._raw_devices = data.get('devices', []) or []

            # Endpoints — expanded into materialized devices in _build_devices().
            # `endpoints:` was called `plants:` until 3.51.0 — the concept was
            # never PV-specific (a master fronting units is just as likely to be
            # a meter bank or a sensor bus). A config written by an older
            # version keeps loading; the next save writes the new key.
            self._raw_endpoints = (data.get('endpoints')
                                   or data.get('plants') or [])
            if 'plants' in data and 'endpoints' not in data:
                logger.info("config: `plants:` read as `endpoints:` — the next "
                            "save writes the new key")

            # Optional alerting hooks (off unless enabled). Kept as a raw dict —
            # the AlertManager reads it. See multibus/alerts.py.
            self.alerts = data.get('alerts', {}) or {}

            # Optional ESPHome integration (Device Builder). Raw dict — the
            # builder routes read it at request time. See multibus/esphome_client.py.
            self.esphome = data.get('esphome', {}) or {}

            logger.info(f"Loaded config from {self.config_path}")
            self._load_failed = False
            # Keep a last-known-good snapshot so a future corrupt edit can be
            # self-healed instead of falling back to bare defaults. (Skipped
            # while healing — we'd only be copying the snapshot onto itself.)
            if not getattr(self, '_healing', False):
                try:
                    import shutil
                    good = self.config_path.with_suffix('.yaml.good')
                    if self._good_would_regress(data, good):
                        # Truncated-but-plausible file (external audit): a cut
                        # at a section boundary can still parse AND carry
                        # modbus/mqtt while having lost the devices tail.
                        # Promoting it would destroy the only recovery copy.
                        # Intentional shrinks refresh .good via save_config.
                        logger.warning(
                            "config.yaml lost devices relative to %s — NOT "
                            "refreshing the last-known-good snapshot (an "
                            "intentional device removal updates it on save)",
                            good)
                    else:
                        shutil.copyfile(self.config_path, good)
                except Exception:  # noqa: BLE001
                    pass

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
            # SELF-HEAL: run on the last known-good snapshot rather than defaults,
            # so the primary keeps polling the right host through a bad edit. The
            # broken file is preserved and saves stay disabled until it's fixed.
            good = self.config_path.with_suffix('.yaml.good')
            if good.exists() and not getattr(self, '_healing', False):
                logger.error(f"SELF-HEAL: loading last known-good snapshot {good}")
                self._healing = True
                _orig = self.config_path
                try:
                    self.config_path = good
                    self._load_yaml_config()          # re-parse the snapshot
                except Exception as e2:  # noqa: BLE001
                    logger.error(f"snapshot load also failed: {e2}")
                finally:
                    self.config_path = _orig
                    self._healing = False
                self._load_failed = True              # real file still broken → block saves
                self._healed_from_snapshot = True

    def _load_json_with_heal(self, path, *, required_key: str, label: str):
        """Load a JSON config file with the same .good/.bad self-heal contract
        as config.yaml (external audit: selected_registers.json had NONE — a
        truncated file silently emptied the whole selection, stopping every
        poller while the meter looked merely stale).

        - Parse error OR implausible shape (not a dict / ``required_key``
          missing — every MBG save writes that key, even when its list is
          empty) → the broken file is copied to ``<path>.bad`` and the last
          known-good snapshot is loaded instead.
        - A plausible load refreshes the ``<path>.good`` snapshot — but never
          with an implausible husk, so .good always holds a real selection.
        Returns the parsed dict, or None when nothing loadable exists."""
        import shutil
        good = path.with_suffix(path.suffix + '.good')

        def _plausible(d) -> bool:
            return isinstance(d, dict) and required_key in d

        try:
            with open(path, 'r') as f:
                data = json.load(f)
            if not _plausible(data):
                raise ValueError(f"parsed but looks truncated (missing {required_key!r} key)")
        except Exception as e:  # noqa: BLE001
            bad = path.with_suffix(path.suffix + '.bad')
            try:
                shutil.copyfile(path, bad)
                logger.error(f"{label}: {e} — broken file copied to {bad}")
            except Exception:  # noqa: BLE001
                logger.error(f"{label}: {e}")
            if good.exists():
                try:
                    with open(good, 'r') as f:
                        data = json.load(f)
                    if _plausible(data):
                        logger.error(f"SELF-HEAL: {label} loaded from last "
                                     f"known-good snapshot {good}")
                        return data
                    logger.error(f"{label}: snapshot {good} is implausible too")
                except Exception as e2:  # noqa: BLE001
                    logger.error(f"{label}: snapshot load also failed: {e2}")
            return None
        try:
            shutil.copyfile(path, good)
        except Exception:  # noqa: BLE001
            pass
        return data

    def _load_selected_registers(self):
        """Load selected registers configuration."""
        if not self.registers_path.exists():
            logger.warning(f"Selected registers file not found: {self.registers_path}")
            return

        data = self._load_json_with_heal(self.registers_path,
                                         required_key='registers',
                                         label='selected registers')
        if data is None:
            return
        try:
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
                offset=float(reg.get('offset', 0) or 0),
                scale_from=str(reg.get('scale_from', '') or ''),
                nan=reg.get('nan'),
                monotonic=bool(reg.get('monotonic', False)),
                enum=reg.get('enum'),
                bits=reg.get('bits'),
                mask=reg.get('mask'),
                shift=reg.get('shift'),
                device_class=str(reg.get('device_class', '') or ''),
                state_class=str(reg.get('state_class', '') or ''),
                entity_category=str(reg.get('entity_category', '') or ''),
                enabled_by_default=reg.get('enabled_by_default'),
                icon=str(reg.get('icon', '') or ''),
                suggested_display_precision=reg.get('suggested_display_precision'),
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
        if os.getenv('UI_HOST'):
            self.ui.host = os.getenv('UI_HOST')

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
        validate_register_identity(registers)      # audit DP-9
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
                "startup_jitter_s": self.modbus.startup_jitter_s,
                "illegal_registers": list(self.modbus.illegal_registers),
                "drop_all_zero": self.modbus.drop_all_zero,
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
                "allow_write_entities": self.mqtt.allow_write_entities,
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
            'UI_HOST': 'ui.host',
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

    def config_status(self) -> Dict:
        """Config-load health for /api/status — so a corrupt config that
        self-healed to the last-known-good snapshot is visible to an operator,
        not just buried in a boot log line."""
        failed = bool(getattr(self, '_load_failed', False))
        return {
            "healthy": not failed,
            "healed_from_snapshot": bool(getattr(self, '_healed_from_snapshot', False)),
            "saves_disabled": failed,
            "bad_file": (str(self.config_path.with_suffix('.yaml.bad'))
                         if failed else None),
            # version stamp: which gateway wrote the file, and whether that is
            # NEWER than the running one (downgrade — a save would drop keys)
            "written_by": getattr(self, 'config_written_by', '') or None,
            "written_by_newer": bool(getattr(self, 'config_written_by_newer', False)),
        }

    def save_yaml_config(self):
        """Save current configuration to YAML file."""
        if getattr(self, '_load_failed', False):
            raise RuntimeError(
                f"config.yaml failed to load at startup — refusing to overwrite it "
                f"with defaults. Repair {self.config_path} (a copy of the broken "
                f"file was kept as .yaml.bad) and restart.")
        data = {
            # First key on purpose: who wrote this file (see the downgrade
            # warning in _load_yaml_config).
            'config_version': __version__,
            'modbus': {
                'host': self.modbus.host,
                'port': self.modbus.port,
                'unit_id': self.modbus.unit_id,
                'timeout': self.modbus.timeout,
                'retry_attempts': self.modbus.retry_attempts,
                'retry_delay': self.modbus.retry_delay,
                'stale_after_s': self.modbus.stale_after_s,
                'max_gap': self.modbus.max_gap,
                'startup_jitter_s': self.modbus.startup_jitter_s,
                'illegal_registers': list(self.modbus.illegal_registers),
                'drop_all_zero': self.modbus.drop_all_zero,
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
                'allow_write_entities': self.mqtt.allow_write_entities,
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
                'compat_aliases': self.mqtt.compat_aliases,
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
                'primary_write_locked': self.security.primary_write_locked,
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

        # Jasic PQ event recorder for the primary (write when configured, so a
        # deliberate enabled:false survives a save).
        if self.pq_recorder_primary:
            data['pq_recorder'] = self.pq_recorder_primary

        # Additional southbound devices (Tier 2). The primary device is NOT
        # written here — it lives in the flat sections above (invisible
        # migration; rollback-safe).
        if self._raw_devices:
            data['devices'] = self._raw_devices

        # Endpoints persist as their raw entries — the materialized devices are
        # NEVER written to devices[] (they are derived, like the primary).
        if self._raw_endpoints:
            data['endpoints'] = self._raw_endpoints

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
        # A SAVE is intentional, validated state — refresh the last-known-good
        # snapshot here. This is what lets the load-time promotion below stay
        # conservative (refuse on device-count shrink) without .good going
        # stale after a legitimate device deletion.
        try:
            import shutil
            shutil.copyfile(self.config_path, self.config_path.with_suffix('.yaml.good'))
        except Exception:  # noqa: BLE001
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