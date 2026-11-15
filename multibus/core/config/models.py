# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The configuration dataclasses (SourceConfig, DeviceConfig, MQTTConfig,
InfluxDBConfig, SecurityConfig, OpcUaConfig, UIConfig, PollGroup,
SelectedRegister), their field parsers, the section shaper, and the primary
device's legacy identity."""
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from multibus.core.protocols import store_key
from multibus.core.config.connection import ModbusConfig

logger = logging.getLogger("multibus.core.config")


PRIMARY_DEVICE_ID = "umg512"

# What the legacy primary (device #1, the Janitza UMG 512-PRO synthesized from
# the flat sections) was called before a fresh install stopped creating it
# (ADR-0024). An install that HAS that device keeps these when its file does
# not name them; a save then writes them out, so they stop being implicit.
LEGACY_TOPIC_PREFIX = "multibus/umg512"
LEGACY_HA_DEVICE_NAME = "Janitza UMG 512-PRO"


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
    can: Dict[str, Any] = field(default_factory=dict)   # CAN input: carrier, channel, bitrate
    # Per-source intervals. A slow, complete source and a fast, partial one do
    # not share a polling rhythm, so the groups belong to the source and not to
    # the device.
    poll_groups: Dict[str, "PollGroup"] = field(default_factory=dict)
    # How long this source's value stays authoritative before a lower-ranked
    # source may fill the field. 0 = never yields (the right answer for a
    # counter, which must never alternate between sources or it goes
    # non-monotonic).
    stale_after_s: float = 0.0


def device_wire_identity(dev_cfg) -> tuple:
    """The physical target(s) a device's client writes to: an order-independent
    tuple of each source's ``(protocol, host, port, unit_id, serial_port,
    baudrate, parity, stopbits, bytesize, mode, adapter)`` plus, for an MQTT / CAN / HTTP
    source, its broker-topic / channel / URL (``_target_of``) — the fields that
    decide WHICH physical device on WHICH wire a write reaches (ADR-0042). A
    change here is a retarget; a kept client would still write the OLD wire until
    it is rebuilt. Non-wire fields (timeout, retries, settle) are excluded so
    tuning them never blocks writes. Lives in core so the control + api write
    heads can read it without importing the devices layer (sibling independence)."""
    out = []
    for s in getattr(dev_cfg, "sources", None) or []:
        c = getattr(s, "connection", None)
        if c is None:
            continue
        out.append((getattr(s, "protocol", "") or "",
                    getattr(c, "host", ""), getattr(c, "port", 0), getattr(c, "unit_id", 0),
                    getattr(c, "serial_port", ""), getattr(c, "baudrate", 0),
                    getattr(c, "parity", ""), getattr(c, "stopbits", 0),
                    getattr(c, "bytesize", 0), getattr(c, "mode", ""), getattr(c, "adapter", ""))
                   + _target_of(s))
    return tuple(sorted(map(str, out)))


# what aims a non-Modbus source at ITS physical target (R2D-AW-2): the Modbus fields above
# say nothing about a broker, a topic or a CAN channel, so a retargeted MQTT or CAN device
# used to compare equal and its kept client kept writing the old target unrefused
_TARGET_KEYS = {
    "mqtt_in": ("broker", "port", "tls", "topic", "vars"),
    "can": ("carrier", "channel", "bitrate"),
    "http": ("url",),
}
# a default written out is the same wire as the key left out (`tls: false`, `port: 1883`,
# `port: "1883"`) — the device editor's own "did anything it runs on change" check
# (`_built_sig`) reads them that way, and a false retarget refuses writes for nothing
_TARGET_DEFAULTS = {("mqtt_in", "port"): 1883, ("mqtt_in", "tls"): False}


def _target_value(block: str, key: str, value):
    if key in ("port", "bitrate"):
        try:
            value = int(value)
        except (TypeError, ValueError):
            pass
    elif key == "tls":
        value = bool(value)
    if value in (None, "", {}) or value == _TARGET_DEFAULTS.get((block, key), object()):
        return None
    return repr(sorted(value.items())) if isinstance(value, dict) else repr(value)


def _target_of(source) -> tuple:
    """NOT covered: ``broker: "@gateway"`` follows the gateway's own broker, so moving the
    global MQTT broker moves the target without changing this identity (FINDINGS R2D-AW-2)."""
    out = []
    for block, keys in _TARGET_KEYS.items():
        d = getattr(source, block, None) or {}
        if isinstance(d, dict):
            for k in keys:
                v = _target_value(block, k, d.get(k))
                if v is not None:
                    out.append((f"{block}.{k}", v))
    return tuple(out)


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
    # Which group of its endpoint this unit belongs to, and what it IS. A plant
    # holds inverters and the meter at its grid connection; adding their powers
    # together would be meaningless, so the aggregator needs to tell them apart.
    group_id: str = ""
    role: str = ""
    # command bindings: [{name, from_template, enabled, faces, confirm, lease_s,
    # …or a full inline recipe}] — a template preset is offered, a binding enables it
    commands: List[Dict[str, Any]] = field(default_factory=list)
    # this device's overrides of its template's channel settings, keyed by a
    # declared channel name or a member command's name (ADR-0019):
    # {ess_setpoint: {min_interval_s: 5}} — the template's value is the default
    channels: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    serial: Dict[str, Any] = field(default_factory=dict)   # rtu params (reserved)
    http: Dict[str, Any] = field(default_factory=dict)     # http input: url, timeout, headers, verify_tls
    mqtt_in: Dict[str, Any] = field(default_factory=dict)  # mqtt input: broker, port, username, password, tls, topic
    mqtt_topic_prefix: str = ""
    # its Home Assistant node (ADR-0026 §3): stored with it at creation like
    # the prefix; none stored = `mbg_dev_<id>` (every device before the scope)
    ha_node: str = ""
    influxdb_bucket: str = ""
    influxdb_device_tag: str = ""
    # static tags on every point this device writes (`influxdb.tags`); an
    # endpoint's substitute ${unit_id} per unit — a series identity, like the
    # device tag (battery_id=N is what the Seplos history is keyed on)
    influxdb_tags: Dict[str, str] = field(default_factory=dict)
    ha_discovery_enabled: bool = True        # publish Home Assistant discovery for this device
    mqtt_enabled: bool = True                # route this device's values to MQTT
    influxdb_enabled: bool = True            # route this device's values to InfluxDB
    # the device's own publishing; ""/None = the gateway's (Settings → MQTT /
    # InfluxDB): mqtt.publish_mode + mqtt.heartbeat_interval, influxdb.publish_mode
    # + influxdb.write_interval in its block
    mqtt_publish_mode: str = ""
    mqtt_heartbeat_s: Optional[int] = None
    influxdb_publish_mode: str = ""
    influxdb_write_interval_s: Optional[float] = None
    influxdb_heartbeat_s: Optional[int] = None   # ADR-0029; None = the gateway's
    http_output_enabled: bool = False        # serve this device's live values as JSON (GET /api/meters/<id>)
    # served on the OPC UA server (`opcua:`); [] = every value, else these names.
    # A device without an `opcua:` block is on, as before the block existed;
    # devices made since get an explicit `enabled: false`
    opcua_enabled: bool = True
    opcua_fields: List[str] = field(default_factory=list)
    rest_push: Dict[str, Any] = field(default_factory=dict)   # push values to a URL: {enabled,url,interval_s,headers,format,verify_tls,timeout}
    pq_recorder: Dict[str, Any] = field(default_factory=dict)  # Jasic PQ event recorder: {enabled,poll_s,archive_waveforms,base_url}
    energy_meter: Dict[str, Any] = field(default_factory=dict)  # another device measures its electricity: {device, power, energy} (linked_meter.py)
    meters: List[Dict[str, Any]] = field(default_factory=list)  # the meters that measure it, each in a role (meter_links.py)
    meter_options: Dict[str, Any] = field(default_factory=dict)  # when its input/output values are honest: {min_load_w, tolerance_pct, max_skew_s} (meter_links.OPTION_DEFAULTS)
    endpoint_id: str = ""    # non-empty → materialized from a `endpoints:` entry
                          # (managed via the endpoint, not the device CRUD)
    # non-empty → a component of that device (its `components:` list): read
    # through the parent's connection, managed through the parent
    component_of: str = ""
    component_type: str = ""
    component_instance: str = ""
    # the kind of equipment an admin says it is (device_kind.py); '' = derived
    kind: str = ""
    write_locked: bool = False   # per-device write lock (F3a): refuses every
                                 # write regardless of guards. The primary
                                 # defaults to locked (security.primary_write_locked).

    def summary(self) -> Dict[str, Any]:
        return {
            'id': self.id, 'name': self.name, 'template': self.template,
            'enabled': self.enabled, 'primary': self.primary,
            'endpoint_id': self.endpoint_id, 'write_locked': self.write_locked,
            **({'component_of': self.component_of, 'component_type': self.component_type,
                'component_instance': self.component_instance} if self.component_of else {}),
            'ha_discovery_enabled': self.ha_discovery_enabled,
            'mqtt_enabled': self.mqtt_enabled,
            'influxdb_enabled': self.influxdb_enabled,
            'http_output_enabled': self.http_output_enabled,
            'opcua_enabled': self.opcua_enabled,
            'opcua_fields': list(self.opcua_fields),
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
    # the gateway's own root (<prefix>/status, <prefix>/site/meta, the virtual
    # meters' state) — and the legacy primary's topics on an install that has
    # one (ADR-0024: those keep LEGACY_TOPIC_PREFIX when their file is silent)
    topic_prefix: str = "mbg/gateway"
    retain: bool = True
    qos: int = 0
    publish_mode: str = "changed"  # "changed" or "all"
    # In "changed" mode, force a republish of an UNCHANGED value after this many
    # seconds (0 = off) so a steady reading keeps a fresh timestamp and HA/other
    # consumers don't grey the entity out during long steady states.
    heartbeat_interval: int = 0
    ha_discovery_enabled: bool = True
    ha_discovery_prefix: str = "homeassistant"
    # the legacy primary's name in Home Assistant; empty = no such device
    # (an install that has it keeps LEGACY_HA_DEVICE_NAME, ADR-0024)
    ha_device_name: str = ""
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
    # MQTT-RATE-1: optional throttling of device/calc VALUE topics (off = 0 =
    # exactly the old behaviour). min_interval_s: a changed value publishes at
    # most every N s per topic; a value held back is still sent once the
    # interval has elapsed (the latest wins, the final one is never lost).
    # deadband_pct: a numeric change smaller than this % of the last PUBLISHED
    # value is not published (the heartbeat still republishes). Both apply only
    # to the poll groups named in throttle_poll_groups (empty = every group).
    # In-process readers (virtual meters, rules) are not affected.
    min_interval_s: float = 0.0
    deadband_pct: float = 0.0
    throttle_poll_groups: List[str] = field(default_factory=list)


# ADR-0029: the starting value of influxdb.heartbeat_interval (10 min), written
# into a config.yaml that does not have the key yet; from then on the file's
# value (Settings → InfluxDB) is the only one that runs.
INFLUX_HEARTBEAT_START_S = 600
HEARTBEAT_MAX_S = 86400

# ADR-0031 §5: the read preview's budget (Add → Check, the device page's
# Preview), its own top-level `preview:` section: written into config.yaml on
# first load like the heartbeat, so the running value is the file's and
# Settings → Modbus shows it. key → (start, lo, hi)
PREVIEW_BUDGET = {
    'max_requests': (4, 1, 20),    # reads one preview may send
    'listen_s': (10, 1, 60),       # seconds a preview listens on a bus another master polls
}


def preview_budget_error(key: str, raw) -> Optional[str]:
    """The one check of a preview budget value (file, API): a whole number in
    its range; None when it is fine."""
    _start, lo, hi = PREVIEW_BUDGET[key]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw != raw \
            or not float(raw).is_integer() or not (lo <= raw <= hi):
        return f"preview.{key}: a whole number, {lo}..{hi}"
    return None


def heartbeat_seconds(raw) -> Optional[int]:
    """The one check of an InfluxDB heartbeat value — the gateway's, a
    device's, an installation's, and the API's: whole seconds 0..86400 (an
    integral float such as 30.0 counts). Anything else (a fraction, text,
    a boolean, NaN/inf, out of range) is None."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if not math.isfinite(raw) or raw != int(raw) or not 0 <= raw <= HEARTBEAT_MAX_S:
        return None
    return int(raw)


def _device_heartbeat(raw, did: str) -> Optional[int]:
    """A device's own influxdb.heartbeat_interval; None = the gateway's (a
    value that is not whole seconds 0..86400 is reported and ignored)."""
    if raw in (None, ''):
        return None
    v = heartbeat_seconds(raw)
    if v is None:
        logger.warning(f"devices[{did}]: influxdb.heartbeat_interval {raw!r} is not whole seconds "
                       f"0..{HEARTBEAT_MAX_S}: the gateway's is used")
    return v


def _heartbeat_setting(raw) -> int:
    """influxdb.heartbeat_interval as the file has it: whole seconds,
    0..86400. Anything else is reported and runs as the starting value."""
    v = heartbeat_seconds(raw)
    if v is not None:
        return v
    logger.warning(f"influxdb.heartbeat_interval {raw!r} is not whole seconds 0..{HEARTBEAT_MAX_S}: "
                   f"{INFLUX_HEARTBEAT_START_S} s runs until it is set in Settings → InfluxDB")
    return INFLUX_HEARTBEAT_START_S


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
    # In "changed" mode, rewrite an UNCHANGED value once it has not been
    # written for this many seconds (0 = off), so state timelines and "last
    # value" panels always have a recent point (ADR-0029). write_interval stays
    # the minimum gap. The value lives in config.yaml: a file without the key
    # gets INFLUX_HEARTBEAT_START_S written into it on its first load.
    heartbeat_interval: int = INFLUX_HEARTBEAT_START_S
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
    # Who may scrape GET /metrics besides loopback (OBS-PROM-1): IPs/CIDRs of
    # the Prometheus scraper. Opens /metrics ONLY — not the API/UI. Empty =
    # loopback only (or whatever `allowlist` admits, when that is set).
    metrics_allowlist: List[str] = field(default_factory=list)
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


OPCUA_ENCRYPTION = ('required', 'optional', 'off')


def _on_off(v: Any, default: bool) -> bool:
    """on / off as YAML or an environment says it (`off` reads as false in
    YAML, as a word in a quoted value or an env); anything else → default."""
    if isinstance(v, bool):
        return v
    w = str(v if v is not None else '').strip().lower()
    if w in ('on', 'true', 'yes', '1'):
        return True
    if w in ('off', 'false', 'no', '0'):
        return False
    return default


# The kill switches removed in 3.97 (ADR-0021 D9 `rules.recheck`, ADR-0022 D7
# `commands.raw_lanes`): their ON behaviour is the only one. A file that still
# holds them loads (one INFO line) and the next save drops them.
# Gateway-level sections that are a mapping (QA-D-11, review 2026-10-28).
# Three kinds, documented in docs/config-reference.md:
# - what reaches the plant and its data (broker, credentials, token): in
#   another shape the load is refused (the file is kept as .bad, the last
#   good copy runs, saves stay blocked) — their defaults would lose them for
#   good once saved; empty (null) is their defaults;
_DATA_SECTIONS = ('modbus', 'mqtt', 'influxdb')
# - where the defaults would open the gateway (no login, no TLS, no
#   allowlist): refused in another shape AND when explicitly null;
_ACCESS_SECTIONS = (('ui',), ('ui', 'auth'), ('ui', 'tls'), ('security',))
# - the rest: read as their defaults, said; such a load never refreshes the
#   last good copy, so the real section survives there
_MAPPING_SECTIONS = ('opcua', 'polling', 'http_output', 'rest_push', 'preview',
                     'opcua_output', 'pq_recorder', 'alerts', 'gateway')


def _shape_sections(data: Dict) -> List[str]:
    """Bring the gateway-level sections of a parsed config.yaml to the shape
    the loader reads, in place. Raises ValueError (the load is refused) for a
    data or an access section in another shape; returns the keys read as
    their defaults instead (empty: nothing was lost)."""
    def kind(v):
        return 'a list' if isinstance(v, list) else repr(v)
    for path in _ACCESS_SECTIONS:
        box, v, there = data, None, True
        for k in path:
            there = isinstance(box, dict) and k in box
            v = box.get(k) if there else None
            box = v
        if there and not isinstance(v, dict):
            raise ValueError(f"`{'.'.join(path)}` must be a section (key: value lines), found {kind(v)} — "
                             "its defaults would open the gateway, so this file is not used")
    for k in _DATA_SECTIONS:
        v = data.get(k)
        if k in data and v is None:
            data[k] = {}
        elif v is not None and not isinstance(v, dict):
            raise ValueError(f"`{k}` must be a section (key: value lines), found {kind(v)} — "
                             "its defaults would lose what reaches the plant (address, broker, "
                             "credentials, token), so this file is not used")
    degraded: List[str] = []
    for k in _MAPPING_SECTIONS:
        if k not in data:
            continue
        v = data[k]
        if v is None:
            data[k] = {}
        elif not isinstance(v, dict):
            logger.warning("config.yaml: `%s` must be a section (key: value lines), found %s — "
                           "its defaults are used; the rest of the file is read", k, kind(v))
            data[k] = {}
            degraded.append(k)
    m = data.get('mqtt') or {}
    ha = m.get('ha_discovery')
    if 'ha_discovery' in m and ha is None:
        m['ha_discovery'] = {}
    elif isinstance(ha, bool):
        # the per-device form at gateway level: nothing is lost
        logger.warning("config.yaml: `mqtt.ha_discovery: %s` read as `{enabled: %s}` "
                       "(the gateway's form is a section: enabled, prefix)",
                       str(ha).lower(), str(ha).lower())
        m['ha_discovery'] = {'enabled': ha}
    elif ha is not None and not isinstance(ha, dict):
        logger.warning("config.yaml: `mqtt.ha_discovery` must be a section (enabled, prefix), found %s — "
                       "its defaults are used", kind(ha))
        m['ha_discovery'] = {}
        degraded.append('mqtt.ha_discovery')
    pol = data.get('polling') or {}
    groups = pol.get('groups')
    if 'groups' in pol and groups is None:
        pol['groups'] = {}
    elif groups is not None and not isinstance(groups, dict):
        logger.warning("config.yaml: `polling.groups` must be a section, found %s — ignored", kind(groups))
        pol.pop('groups')
        degraded.append('polling.groups')
    elif isinstance(groups, dict):
        for name in [n for n, g in groups.items() if not isinstance(g, dict)]:
            logger.warning("config.yaml: `polling.groups.%s` must be a section (interval, description), "
                           "found %s — that group is skipped", name, kind(groups[name]))
            groups.pop(name)
            degraded.append(f'polling.groups.{name}')
    return degraded


class ConfigSaveBlocked(RuntimeError):
    """A save refused because config.yaml failed to load: writing would
    replace the user's file with defaults."""


_REMOVED_397 = (('rules', 'recheck'), ('commands', 'raw_lanes'))
_REMOVED_397_ENV = ('MBG_RULES_RECHECK', 'MBG_RAW_LANES')


@dataclass
class OpcUaConfig:
    """The OPC UA server — the northbound face for SCADA and HMI panels.
    Off by default: nothing is imported or opened until it is enabled."""
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 4840
    # required: only Basic256Sha256 Sign&Encrypt; optional: also no security
    # (some older HMIs need it — passwords then travel in clear on the LAN);
    # off: no security at all
    encryption: str = "required"
    # anonymous clients (read-only); otherwise the gateway's own accounts
    allow_anonymous: bool = False
    update_s: float = 1.0                 # how often values are pushed into the address space
    # behind a load balancer (a high-availability pair): the address clients
    # are told, opc.tcp://<balancer>:<port>/<path>; the server still listens
    # on host:port, and its certificate names the balancer's host
    public_url: str = ""


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
    # Link to a companion service on the same host (the ESP nodes service,
    # e.g. /nodes/). Empty = no link in the UI.
    nodes_url: str = ""
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

@dataclass
class SelectedRegister:
    address: int
    name: str
    label: str
    unit: str
    data_type: str
    poll_group: str
    description: str = ""  # Human-readable description from modbus_data.json
    category: str = ""     # the template's section (outdoor, battery…) — also the
                           # InfluxDB measurement of a field nothing else classifies
    json_path: str = ""   # HTTP/JSON + MQTT input: dot/bracket path into the JSON payload
    topic: str = ""       # MQTT input: the topic this register reads from (else device base topic)
    scale: float = 1.0    # Modbus input: engineering_value = raw / scale (SunSpec 10^-SF etc.)
    offset: float = 0.0   # engineering_value = raw / scale + offset (zero-point / unit shift)
    scale_from: str = ""  # dynamic SF: sibling register NAME whose raw value is the
                          # base-10 exponent → engineering = raw × 10^SF; a fixed
                          # `scale` may accompany it and divides AFTER the exponent
                          # (unit conversion, e.g. SunSpec PF percent → fraction)
    nan: Any = None       # not-available sentinel (True=std for type / value / list) → missing
    valid_min: Optional[float] = None   # plausibility range of the engineering value:
    valid_max: Optional[float] = None   # outside → missing, never published (TAP-PLAUS-1)
    monotonic: bool = False   # cumulative counter (energy): reject downward glitches → HA/Victron safe
    daily: bool = False       # day counter recomputed from whatever is awake: hold the day's max, adopt the midnight reset
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
    # endpoint fan-out: {output_name: sum|avg|min|max|spread} — how this field
    # combines across the units of an endpoint (declared by the template)
    aggregates: Optional[Dict[str, str]] = None
    # CAN signal (can_signal.py): the frame and the bits; None = not a CAN row
    can_id: Optional[int] = None
    can_id_extended: bool = False
    start_bit: Optional[int] = None
    bit_length: Optional[int] = None
    byte_order: str = ""

    @property
    def key(self) -> int:
        """Where its value lives in the store (address + its table's offset)."""
        return store_key(self.address, self.register_type)
