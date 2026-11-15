# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The config.yaml section parsers — one method per top-level section, called in order by
_load_yaml_config (io.py). Split out of that method (ADR-0047 P1); a mixin on Config, so `self.*`
resolves across the other config mixins. Order matters and is the loader's: modbus, mqtt, influxdb,
ui, security, removed-3.97, opcua, polling, output sinks, then the raw entities (device_types
BEFORE devices; bridges BEFORE the build)."""
import logging

from multibus.core.config.connection import (
    ModbusConfig, settle_seconds, _modules, primary_opcua_kept, primary_rest_push_kept, _number_or,
)
from multibus.core.config.models import (
    MQTTConfig, INFLUX_HEARTBEAT_START_S, _heartbeat_setting, InfluxDBConfig, SecurityConfig,
    OPCUA_ENCRYPTION, _REMOVED_397, OpcUaConfig, UIConfig, PollGroup, parse_address_list,
)

logger = logging.getLogger("multibus.core.config")


class ConfigSectionsMixin:
    def _parse_modbus(self, data):
        if 'modbus' in data:
            m = data['modbus']
            self.modbus = ModbusConfig(
                host=m.get('host', self.modbus.host),
                port=m.get('port', self.modbus.port),
                unit_id=m.get('unit_id', self.modbus.unit_id),
                timeout=m.get('timeout', self.modbus.timeout),
                # whole / real numbers, whatever the file says (a stored
                # 4.0 broke range() in the meter's read loop, review #2)
                retry_attempts=_number_or(m.get('retry_attempts'), self.modbus.retry_attempts, int),
                retry_delay=_number_or(m.get('retry_delay'), self.modbus.retry_delay, float),
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
                connect_settle_s=settle_seconds(m.get('connect_settle_s')),
            )

    def _parse_mqtt(self, data):
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
                min_interval_s=float(m.get('min_interval_s', self.mqtt.min_interval_s) or 0),
                deadband_pct=float(m.get('deadband_pct', self.mqtt.deadband_pct) or 0),
                throttle_poll_groups=[str(g) for g in (m.get('throttle_poll_groups',
                                      self.mqtt.throttle_poll_groups) or [])],
            )

        # what the file says of MQTT, for the legacy primary's old
        # defaults (applied once device #1 is resolved, _resolve_primary)
        self._file_mqtt = data.get('mqtt') if isinstance(data.get('mqtt'), dict) else {}

    def _parse_influxdb(self, data):
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
                heartbeat_interval=(_heartbeat_setting(i['heartbeat_interval'])
                                    if 'heartbeat_interval' in i else INFLUX_HEARTBEAT_START_S),
                buffer_minutes=i.get('buffer_minutes', self.influxdb.buffer_minutes),
                buffer_max_points=i.get('buffer_max_points', self.influxdb.buffer_max_points),
                buffer_persist=i.get('buffer_persist', self.influxdb.buffer_persist),
                default_bucket_pattern=i.get('default_bucket_pattern', self.influxdb.default_bucket_pattern),
            )

    def _parse_ui(self, data):
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
                nodes_url=str(u.get('nodes_url', self.ui.nodes_url) or '').strip(),
                lockout_threshold=auth.get('lockout_threshold', self.ui.lockout_threshold),
                lockout_minutes=auth.get('lockout_minutes', self.ui.lockout_minutes),
                tls_enabled=tls.get('enabled', self.ui.tls_enabled),
                tls_cert=tls.get('cert', self.ui.tls_cert),
                tls_key=tls.get('key', self.ui.tls_key),
                trusted_proxies=list(u.get('trusted_proxies', self.ui.trusted_proxies) or []),
                timezone=u.get('timezone', self.ui.timezone),
                default_colors=dict(u.get('default_colors', self.ui.default_colors) or {}),
            )

    def _parse_security(self, data):
        if 'security' in data:
            s = data['security'] or {}
            self.security = SecurityConfig(
                allowlist=list(s.get('allowlist', []) or []),
                metrics_allowlist=list(s.get('metrics_allowlist', []) or []),
                allow_nonlan_http_devices=bool(
                    s.get('allow_nonlan_http_devices', False)),
                allow_writes=bool(s.get('allow_writes', False)),
                primary_write_locked=bool(s.get('primary_write_locked', True)),
                write_rate_limit_per_s=float(s.get('write_rate_limit_per_s', 10.0)),
            )

    def _parse_removed_397(self, data):
        # rules.recheck / commands.raw_lanes: removed in 3.97 — ignored,
        # said once, never written back by a save
        _gone = [f"{sec}.{key}" for sec, key in _REMOVED_397
                 if isinstance(data.get(sec), dict) and key in data[sec]]
        if _gone:
            logger.info("config: %s ignored — removed in 3.97 (always on); the next save drops it",
                        ', '.join(_gone))

    def _parse_opcua(self, data):
        if 'opcua' in data:
            o = data['opcua'] or {}
            enc = str(o.get('encryption', 'required') or 'required')
            self.opcua = OpcUaConfig(
                enabled=bool(o.get('enabled', False)),
                host=str(o.get('host', '0.0.0.0') or '0.0.0.0'),
                port=int(o.get('port', 4840) or 4840),
                encryption=enc if enc in OPCUA_ENCRYPTION else 'required',
                allow_anonymous=bool(o.get('allow_anonymous', False)),
                update_s=max(0.2, float(o.get('update_s', 1.0) or 1.0)),
                public_url=str(o.get('public_url', '') or ''),
            )

    def _parse_polling(self, data):
        if 'polling' in data and 'groups' in data['polling']:
            for name, group in data['polling']['groups'].items():
                self.poll_groups[name] = PollGroup(
                    interval=group.get('interval', 5),
                    description=group.get('description', ''),
                )
                self.default_poll_groups[name] = PollGroup(
                    interval=group.get('interval', 5),
                    description=group.get('description', ''),
                )

    def _parse_output_sinks(self, data):
        # HTTP/JSON output sink for the primary device (opt-in). Non-primary
        # devices carry their own flag inside the `devices[]` list below.
        if 'http_output' in data:
            self.http_output_primary_enabled = bool(
                (data['http_output'] or {}).get('enabled', False))
        if 'rest_push' in data:
            self.rest_push_primary = primary_rest_push_kept(data["rest_push"])
        if 'modules' in data:
            self.modules = _modules(data['modules'])
        if 'opcua_output' in data:
            self.opcua_primary = primary_opcua_kept(data["opcua_output"])
        if 'pq_recorder' in data:
            self.pq_recorder_primary = dict(data['pq_recorder'] or {})

    def _parse_raw_entities(self, data):
        # the user device types BEFORE the devices: a device's type may be one
        self._set_device_types(data.get('device_types'))

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
        # bridges BEFORE the device list is built: devices resolve through them
        self._raw_bridges = [b for b in (data.get('bridges') or []) if isinstance(b, dict)]
        if 'plants' in data and 'endpoints' not in data:
            logger.info("config: `plants:` read as `endpoints:` — the next "
                        "save writes the new key")

        # Optional alerting hooks (off unless enabled). Kept as a raw dict —
        # the AlertManager reads it. See multibus/alerts.py.
        self.alerts = data.get('alerts', {}) or {}

        # Optional Virtual BMS instances (ADR-0037). Off/disarmed unless
        # configured. Kept as a raw list — VbmsManager reads it. See
        # multibus/outputs/vbms_manager.py.
        self.virtual_bms = data.get('virtual_bms', []) or []
