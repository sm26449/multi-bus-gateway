# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Build the configured DeviceConfig list from the raw devices, bridges and sources."""
import copy
import logging
import re

from typing import Dict, List, Optional

from multibus.core import host_port
from multibus.core.config.connection import (
    ModbusConfig, settle_seconds, _serial_from_conn, _publish_mode, _seconds, _block, _kind_override,
    _opcua_fields, _influx_tags, _http_from_conn, _mqtt_in_from_conn, _can_from_conn,
)
from multibus.core.config.models import (
    PRIMARY_DEVICE_ID, SourceConfig, DeviceConfig,
    _device_heartbeat, PollGroup,
    parse_address_list,
)

logger = logging.getLogger("multibus.core.config")


class BuildMixin:
    def _build_devices(self):
        """Materialize the device list. Device #1 = the legacy flat config
        (invisible migration — routing identical to today); extra devices come
        from the optional ``devices:`` yaml list."""
        self.ignored_blocks = {}               # judged afresh on every build
        devices: List[DeviceConfig] = [] if not self.primary_enabled else [DeviceConfig(
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
            opcua_enabled=bool(self.opcua_primary.get('enabled', True)),
            opcua_fields=_opcua_fields(self.opcua_primary.get('fields')),
            rest_push=dict(self.rest_push_primary or {}),
            pq_recorder=dict(self.pq_recorder_primary or {}),
            write_locked=self.security.primary_write_locked,
            # The primary is synthesized from the flat sections rather than
            # built from a raw dict, so it must be handed its one source
            # explicitly. Without it the device has no way to be reached at all
            # — every device, primary included, is started from its sources.
            sources=[SourceConfig(id='default', template='janitza_umg512_pro',
                                  protocol='tcp', connection=self.modbus)],
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
        # Components: the things a device holds (a GX's batteries and
        # sensors), each an ordinary device read through its parent's
        # connection
        for pid, c, d in self._expand_components():
            did = d['id']
            if did == PRIMARY_DEVICE_ID or any(x.id == did for x in devices):
                logger.warning(f"devices[{pid}].components: id {did!r} collides with "
                               f"an existing device — skipped")
                continue
            dev = self._device_from_raw(d)
            if dev is not None:
                dev.component_of, dev.component_type = pid, c['type']
                dev.component_instance = c['instance']
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
        # index for get_device() (DUP-15 / PERF-10); setdefault keeps the first
        # of any duplicate id, exactly as the old next(...) scan did.
        index: Dict[str, "DeviceConfig"] = {}
        for _d in devices:
            index.setdefault(_d.id, _d)
        self._device_index = index
        self._publish_bridge_guard()

    def _publish_bridge_guard(self) -> None:
        """Which hosts are our serial bridges, for the bus guard — process-wide, and its
        recorder is a bound method of THIS object. A staging copy (a load in progress,
        ADR-0047 §1) never publishes it: ``load()`` does, once it has published the copy."""
        if getattr(self, '_staging', False):
            return
        host_port.bridge_guard_configure(self._raw_bridges, host_port.bridge_types())
        host_port.bridge_guard_set_recorder(self.record_bus_adapter)

    def _raw_connections(self):
        """Every raw connection dict: devices, their sources, installations,
        their groups and sources."""
        for d in self._raw_devices:
            yield d.get('connection')
            yield from d.get('sources') or []
        for ep in self._raw_endpoints:
            yield ep.get('connection')
            yield from ep.get('sources') or []
            for g in ep.get('groups') or []:
                yield g.get('connection')
                yield from g.get('sources') or []

    def _guarded_bridge(self, host: str) -> Optional[Dict]:
        _bridge_types = host_port.bridge_types()
        return next((b for b in self._raw_bridges if str(b.get('host')) == host
                     and _bridge_types.get(str(b.get('type')), {}).get('discovery') == 'api'), None)

    def record_bus_adapter(self, host: str, port: int, adapter: str) -> bool:
        """The bridge guard's backfill: write which adapter serves a bus where
        the bus is declared — the bridge record's port entry, and every
        host:port connection to it. Never overwrites a recorded adapter."""
        changed = False
        rec = self._guarded_bridge(host)
        if rec is not None:
            ports = rec.setdefault('ports', [])
            entry = next((p for p in ports if str(p.get('port')) == str(port)), None)
            if entry is None:
                ports.append({'port': int(port), 'adapter': adapter})
                changed = True
            elif not entry.get('adapter'):
                entry['adapter'] = adapter
                changed = True
        for c in self._raw_connections():
            if (isinstance(c, dict) and not c.get('bridge') and str(c.get('host', '')) == host
                    and str(c.get('port', '')) == str(port) and not c.get('adapter')):
                c['adapter'] = adapter
                changed = True
        if changed:
            self.save_yaml_config()
            logger.info("bus %s:%s recorded as adapter %s", host, port, adapter)
        return changed

    def follow_bus_adapters(self, host: str, moves: Dict[int, int]) -> List[str]:
        """Re-bind every bus of this bridge whose adapter moved: {old port:
        new port}, applied together (two swapped buses swap). The bridge
        record's port entries and devices follow, and so do host:port
        connections. Returns the devices that moved."""
        moves = {int(k): int(v) for k, v in moves.items() if int(k) != int(v)}
        if not moves:
            return []
        rec = self._guarded_bridge(host)
        before = {d.id: (d.connection.host, d.connection.port) for d in self.devices}
        if rec is not None:
            raw = dict(rec)
            raw['ports'] = [{**p, 'port': moves.get(int(p.get('port', 0) or 0), p.get('port'))}
                            for p in rec.get('ports') or []]
            self._move_legacy_ports(host, moves)
            self.upsert_raw_bridge(raw, port_moves=moves)     # rebuilds and saves
        else:
            self._move_legacy_ports(host, moves)
            self._build_devices()
            self.save_yaml_config()
        return [d.id for d in self.devices
                if before.get(d.id) and before[d.id] != (d.connection.host, d.connection.port)]

    def _move_legacy_ports(self, host: str, moves: Dict[int, int]) -> None:
        for c in self._raw_connections():
            if isinstance(c, dict) and not c.get('bridge') and str(c.get('host', '')) == host:
                try:
                    old = int(c.get('port'))
                except (TypeError, ValueError):
                    continue
                if old in moves:
                    c['port'] = moves[old]

    def _resolve_bridge(self, conn: Dict) -> Dict:
        return host_port.bridge_resolve_connection(conn, self._raw_bridges)

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
            connect_settle_s=settle_seconds(conn.get('connect_settle_s')),
            protocol=str(conn.get('protocol', 'tcp')).lower(),
            serial_port=conn.get('serial_port', ''),
            baudrate=int(conn.get('baudrate', 9600)),
            parity=str(conn.get('parity', 'N')),
            stopbits=int(conn.get('stopbits', 1)),
            bytesize=int(conn.get('bytesize', 8)),
            mode=str(conn.get('mode', '') or '').lower(),
            adapter=str(conn.get('adapter', '') or ''),
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
            r = self._resolve_bridge(r)
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
                    can=_can_from_conn(r),
                    poll_groups=groups,
                    stale_after_s=max(0.0, float(r.get('stale_after_s', 0.0) or 0.0)),
                ))
            except (ValueError, TypeError) as e:
                logger.warning("sources[]: skipping %r — invalid config: %s", sid, e)
        return out

    def preview_devices(self, *, device: Optional[Dict] = None,
                        endpoint: Optional[Dict] = None) -> List["DeviceConfig"]:
        """The device(s) a raw definition would build — a standalone device or
        an installation's units — without storing anything. What an edit
        compares to decide whether anything a unit runs on changed."""
        if device is not None:
            built = self._device_from_raw(copy.deepcopy(device))
            return [built] if built is not None else []
        out = []
        for pid, d in self._expand_endpoints([copy.deepcopy(endpoint)]):
            built = self._device_from_raw(d, endpoint_id=pid)
            if built is not None:
                out.append(built)
        return out

    def _device_from_raw(self, d: Dict, endpoint_id: str = "") -> Optional[DeviceConfig]:
        """Build ONE DeviceConfig from its raw yaml dict (a devices[] entry or
        an endpoint-materialized dict). Returns None (with a warning) on invalid
        values — a single malformed entry must not crash the whole boot."""
        did = str(d.get('id', '')).strip()
        # a device on a bridge names it; host, port and protocol come from it
        conn = self._resolve_bridge(d.get('connection', {}) or {})
        mqtt_cfg = d.get('mqtt', {}) or {}
        influx_cfg = d.get('influxdb', {}) or {}
        http_out_cfg = d.get('http_output', {}) or {}
        opcua_cfg = d.get('opcua', {}) or {}
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
                group_id=str(d.get('group_id', '') or ''),
                role=str(d.get('role', '') or ''),
                commands=[dict(c) for c in (d.get('commands') or []) if isinstance(c, dict)],
                channels={str(k): dict(v) for k, v in (d.get('channels') or {}).items()
                          if isinstance(v, dict)} if isinstance(d.get('channels'), dict) else {},
                serial=_serial_from_conn(conn),
                http=_http_from_conn(conn),
                mqtt_in=_mqtt_in_from_conn(conn),
                mqtt_topic_prefix=prefix,
                ha_node=str(mqtt_cfg.get('ha_node') or '').replace('${device_id}', did).replace('${id}', did)
                or f"mbg_dev_{did}",
                influxdb_bucket=influx_cfg.get('bucket', self.influxdb.bucket),
                influxdb_device_tag=influx_cfg.get('device_tag', did),
                influxdb_tags=_influx_tags(influx_cfg.get('tags'), did),
                ha_discovery_enabled=bool(mqtt_cfg.get('ha_discovery', True)),
                mqtt_enabled=bool(mqtt_cfg.get('enabled', True)),
                influxdb_enabled=bool(influx_cfg.get('enabled', True)),
                mqtt_publish_mode=_publish_mode(mqtt_cfg.get('publish_mode'), did),
                mqtt_heartbeat_s=_seconds(mqtt_cfg.get('heartbeat_interval'), did, int),
                influxdb_publish_mode=_publish_mode(influx_cfg.get('publish_mode'), did),
                influxdb_write_interval_s=_seconds(influx_cfg.get('write_interval'), did, float),
                influxdb_heartbeat_s=_device_heartbeat(influx_cfg.get('heartbeat_interval'), did),
                http_output_enabled=bool(http_out_cfg.get('enabled', False)),
                opcua_enabled=bool(opcua_cfg.get('enabled', True)),
                opcua_fields=_opcua_fields(opcua_cfg.get('fields')),
                rest_push=_block(d.get('rest_push'), did, 'rest_push', self.__dict__.setdefault('ignored_blocks', {})),
                pq_recorder=_block(d.get('pq_recorder'), did, 'pq_recorder', self.__dict__.setdefault('ignored_blocks', {})),
                energy_meter=_block(d.get('energy_meter'), did, 'energy_meter', self.__dict__.setdefault('ignored_blocks', {})),
                kind=_kind_override(d.get('kind'), did, self.__dict__.setdefault('ignored_blocks', {})),
                meters=host_port.meter_parse_device(d, did, self.__dict__.setdefault('ignored_blocks', {})),
                meter_options=host_port.meter_parse_options(d.get('meter_options'), did,
                                                            self.__dict__.setdefault('ignored_blocks', {})),
            )
        except (ValueError, TypeError) as e:
            # A single malformed devices[] entry (e.g. port:"abc") must NOT
            # crash the whole boot — including the primary's polling. Skip it,
            # like the missing/duplicate-id skips above.
            logger.warning(f"devices[]: skipping {did!r} — invalid config: {e}")
            return None

    # ── components ──────────────────────
    COMPONENT_INSTANCE_RE = re.compile(r'^[A-Za-z0-9_.-]{1,32}$')
    # what a component's own entry may say about its outputs; everything
    # else is its parent's
    _COMPONENT_OWN = ('http_output', 'rest_push', 'energy_meter', 'meters', 'meter_options', 'pq_recorder', 'kind')

