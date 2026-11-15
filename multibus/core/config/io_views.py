# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Read-only config views: the API summary (to_dict — reduced, no secrets), the env-override
report (get_env_overrides — secrets shown only as env-pinned), the load-health surface
(config_status), and the default-pattern save helper (_pattern_out). Split out of io.py
(ADR-0047 P1); a mixin on Config, so `self.*` resolves across the other config mixins."""
import os
from typing import Dict


class ConfigViewsMixin:
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
            "preview": self.preview_budget(),
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
                "heartbeat_interval": self.influxdb.heartbeat_interval,
                "buffer_minutes": self.influxdb.buffer_minutes,
                "buffer_max_points": self.influxdb.buffer_max_points,
                "buffer_persist": self.influxdb.buffer_persist,
                "default_bucket_pattern": self.influxdb.default_bucket_pattern,
            },
            "poll_groups": {
                name: {"interval": g.interval, "description": g.description}
                for name, g in self.poll_groups.items()
            },
            "identity_scope": self.identity_scope,
            "gateway": {"id": self.gateway_id},
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
            'MODBUS_STALE_AFTER_S': 'modbus.stale_after_s',
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
                    from multibus.core.redact import redact_url
                    overrides[config_path] = redact_url(os.getenv(env_var))
                else:
                    overrides[config_path] = os.getenv(env_var)
        return overrides

    def config_status(self) -> Dict:
        """Config-load health for /api/status — so a corrupt config that
        self-healed to the last-known-good snapshot is visible to an operator,
        not just buried in a boot log line."""
        failed = bool(getattr(self, '_load_failed', False))
        out = {
            "healthy": not failed,
            "healed_from_snapshot": bool(getattr(self, '_healed_from_snapshot', False)),
            "saves_disabled": failed,
            "bad_file": (str(self.config_path.with_suffix('.yaml.bad'))
                         if failed else None),
            # version stamp: which gateway wrote the file, and whether that is
            # NEWER than the running one (downgrade — a save would drop keys)
            "written_by": getattr(self, 'config_written_by', '') or None,
            "written_by_newer": bool(getattr(self, 'config_written_by_newer', False)),
            # device blocks a hand edit left unreadable: ignored, the device kept
            "ignored_blocks": [{"where": k, "why": v}
                               for k, v in sorted(getattr(self, 'ignored_blocks', {}).items())],
        }
        if self.tree_enabled():
            # ADR-0047 P3c: the experimental config tree (a mirror — never read). What the
            # last save did with it; `unknown` until this process has saved once.
            out["tree"] = dict(getattr(self, '_tree_status', None) or {"state": "unknown", "error": None})
        return out

    def _pattern_out(self, key: str, explicit: str, effective: str) -> Dict[str, str]:
        """A default pattern as a save writes it: a legacy file keeps it
        written out, as every save before; under `gateway` only a chosen one
        is written, so the scope's default stays a default."""
        if explicit or self.identity_scope == 'legacy':
            return {key: explicit or effective}
        return {}
