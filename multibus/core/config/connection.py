# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The ModbusConfig dataclass and the per-device connection parsers that read a
raw `connection:` block into typed settings (serial, network, CAN, HTTP,
MQTT-in), plus the small publish/seconds/block/kind/opcua helpers."""
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from multibus.core.protocols import CAN_PROTOCOLS
from multibus.core import catalog_port

logger = logging.getLogger("multibus.core.config")


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
    # A pause after every successful connect, before the first request. Some
    # gateways ignore or refuse what arrives in the first seconds after a TCP
    # connect (a Huawei SDongle, some serial bridges), so the first batch after
    # every reconnect burned its whole retry budget. It belongs to the access
    # point: held under the transport lock (a sibling sharing the socket waits
    # too), and the largest value among the units sharing it wins (ADR-0015).
    # None = the connection says nothing: the template's protocol default
    # applies, else 0 — no pause, the old behaviour. Clamped to 0..30 s.
    connect_settle_s: Optional[float] = None
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
    # a virtual-map bus (seplos_v2): "poll" (the gateway asks) or "listen"
    # (it never transmits); "" = the driver's default (poll)
    mode: str = ""
    # which adapter of our serial bridge this bus is (its stable_id): the
    # bridge guard refuses to connect when the bridge serves another one on
    # this port (bridge_guard.py). "" = not recorded yet (backfilled).
    adapter: str = ""

    @property
    def bus(self) -> str:
        """Where the bus is: the serial port, or the bridge's host:port."""
        return self.serial_port or f"{self.host}:{self.port}"


CONNECT_SETTLE_MAX_S = 30.0


def settle_seconds(v) -> Optional[float]:
    """``connect_settle_s`` as given: None when unset (the template's default
    then applies), else clamped to 0..CONNECT_SETTLE_MAX_S."""
    if v is None or v == '':
        return None
    return min(CONNECT_SETTLE_MAX_S, max(0.0, float(v)))


def _serial_from_conn(c: Dict) -> Dict[str, Any]:
    return {k: c[k] for k in
            ('serial_port', 'baudrate', 'parity', 'stopbits', 'bytesize') if k in c}


_MODULE_ID = re.compile(r'^[a-z0-9][a-z0-9-]{0,39}$')
_MODULE_ICON = re.compile(r'^[a-z0-9-]{1,40}$')


def _modules(raw) -> List[Dict[str, str]]:
    """`modules:` — separate services beside the gateway, listed in Settings →
    Modules (the HA service, the ESP nodes service): {id, name, icon, url} and
    optionally a description (what it is, how it is installed) and a health
    path the page asks to show whether it answers. A wrong entry is skipped,
    said."""
    out: List[Dict[str, str]] = []
    for m in raw if isinstance(raw, list) else []:
        if not isinstance(m, dict):
            continue
        mid, name = str(m.get('id') or '').strip(), str(m.get('name') or '').strip()
        url, icon = str(m.get('url') or '').strip(), str(m.get('icon') or 'puzzle').strip()
        if not (_MODULE_ID.match(mid) and 0 < len(name) <= 40 and _MODULE_ICON.match(icon)
                and re.match(r'^(/(?!/)|https?://)', url) and mid not in {x['id'] for x in out}):
            logger.warning(f"modules: entry {m!r} skipped (id a-z0-9-, name 1..40, url /path or http(s)://)")
            continue
        entry = {'id': mid, 'name': name, 'icon': icon, 'url': url}
        desc = str(m.get('description') or '').strip()
        if desc:
            entry['description'] = desc[:400]
        health = str(m.get('health') or '').strip()
        if health and re.match(r'^/(?!/)[\w./-]*$', health):
            entry['health'] = health
        out.append(entry)
    return out


PUBLISH_MODES = ('changed', 'all')


def _publish_mode(raw, did: str) -> str:
    """A device's own publish mode; "" = the gateway's."""
    if raw in (None, ''):
        return ''
    if str(raw) in PUBLISH_MODES:
        return str(raw)
    logger.warning(f"devices[{did}]: publish_mode {raw!r} is not one of {PUBLISH_MODES}: the gateway's is used")
    return ''


def _seconds(raw, did: str, kind=float):
    """A device's own heartbeat / write interval in seconds; None = the gateway's."""
    if raw in (None, ''):
        return None
    try:
        v = kind(raw)
    except (TypeError, ValueError):
        v = -1
    if v < 0:
        logger.warning(f"devices[{did}]: {raw!r} is not a number of seconds: the gateway's is used")
        return None
    return v


def _block(raw, did: str, key: str, ignored: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """A device's own block (`energy_meter`, `rest_push`, …): a mapping, or
    nothing. A hand-edited value of another shape drops the BLOCK — never the
    device (a typo must not stop it polling) — and is noted in ``ignored``
    (`device.key` -> why), which the status page and the event log show."""
    if raw in (None, '', [], {}):
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    why = f"{key} {raw!r} is not a mapping: ignored"
    logger.warning(f"devices[{did}]: {why}")
    if ignored is not None:
        ignored[f"{did}.{key}"] = why
    return {}


def _kind_override(raw, did: str, ignored: Optional[Dict[str, str]] = None) -> str:
    """A device's `kind:` override — its type: one of the kinds, a user type
    (device_types.py), or nothing. Anything else is dropped — never the
    device — and noted in ``ignored``."""
    if raw in (None, ''):
        return ''
    k = catalog_port.kind_normalize(raw)
    if k in catalog_port.overridable_kinds():
        return k
    if catalog_port.is_user_type(raw):
        return raw
    why = f"kind {raw!r} is not one of {', '.join(catalog_port.type_ids(include_other=False))}: ignored"
    logger.warning(f"devices[{did}]: {why}")
    if ignored is not None:
        ignored[f"{did}.kind"] = why
    return ''


def _opcua_fields(raw) -> List[str]:
    """`opcua.fields`: the value names a device serves on OPC UA; empty = all."""
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for x in raw:
        n = str(x or '').strip()
        if n and n not in out:
            out.append(n)
    return out


def primary_opcua_kept(block: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The primary's `opcua_output:` as the file keeps it: on with every value
    (the default) is not written, so it is {} — memory, the diff and the
    ETag hold that form too (review 2026-11-01 #5)."""
    block = dict(block or {})
    if block.get('enabled', True) is True and not block.get('fields'):
        return {}
    return block


def primary_rest_push_kept(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The primary's `rest_push:` as the file keeps it: one neither enabled
    nor pointed at a URL is not written, so it is {}."""
    cfg = dict(cfg or {})
    return cfg if (cfg.get('enabled') or cfg.get('url')) else {}


def _number_or(v, default, kind):
    """A stored number read as ``kind`` (int or float); what cannot be read
    keeps ``default`` (a hand-edited file never stops the gateway)."""
    if v is None or isinstance(v, bool):
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        logger.warning("modbus: %r is not a number — %r kept", v, default)
        return default
    if kind is int:
        if not f.is_integer():
            logger.warning("modbus: %r is not a whole number — %r kept", v, default)
            return default
        return int(f)
    return f


class PrimaryRebuildError(RuntimeError):
    """The primary's edit was saved, but rebuilding the running devices from
    it failed: the file is new, the gateway runs on the old (restart it)."""


def _influx_tags(raw: Any, did: str) -> Dict[str, str]:
    """`influxdb.tags` as str->str; a malformed block is ignored, not fatal."""
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        logger.warning(f"devices[{did}]: influxdb.tags must be a mapping — ignored")
        return {}
    return {str(k).strip(): str(v) for k, v in raw.items()
            if str(k).strip() and v is not None and str(v) != ''}


def _http_from_conn(c: Dict) -> Dict[str, Any]:
    return {k: c[k] for k in ('url', 'timeout', 'headers', 'verify_tls') if k in c}


def _mqtt_in_from_conn(c: Dict) -> Dict[str, Any]:
    # accept_retained: the deliberate opt-in for sources that publish RETAINED
    # telemetry with an LWT (seplos-bms-mqtt does) — without it every value
    # the broker replays on subscribe is dropped and a freshly-restarted
    # gateway shows '--' until each field happens to change. The client-side
    # flag existed but no config path could reach it (audit 2026-10-03).
    # availability ({topic, online, offline}) and stale_after_s: how the
    # source's health is judged (mqtt_input.py); a template may declare
    # availability in protocol.availability, the connection overrides it.
    # keepalive ({topic, payload, first_payload, every_s} or false): asking a
    # source to keep publishing, the same way. tls_ca / tls_insecure: a
    # device with its own certificate (a Victron GX on 8883).
    return {k: c[k] for k in
            ('broker', 'port', 'username', 'password', 'tls', 'tls_ca', 'tls_insecure', 'topic',
             'accept_retained', 'availability', 'stale_after_s', 'vars', 'keepalive') if k in c}


def _can_from_conn(c: Dict) -> Dict[str, Any]:
    # a CAN source (can_client.py): the carrier (socketcan), its channel
    # (can0, vcan0), the bitrate the device expects (shown, set by the host)
    # and how long a quiet bus stays fresh
    if str(c.get('protocol', '')).lower() not in CAN_PROTOCOLS:
        return {}
    return {k: c[k] for k in ('carrier', 'channel', 'bitrate', 'stale_after_s') if k in c}
