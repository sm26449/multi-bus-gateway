# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The config writer: serialize the live Config to config.yaml atomically (0600, fsync, rename),
preserving env-shadowed secrets and refreshing the last-known-good snapshot. Split out of io.py
(ADR-0047 P1); a mixin on Config, so every `self.*` resolves across the other config mixins
(`_pattern_out`, `preview_budget`, the raw lists)."""
import logging
import os

import yaml

from multibus import __version__
from multibus.core.config.models import ConfigSaveBlocked
from multibus.core.config.util import _copy_private

logger = logging.getLogger("multibus.core.config")


class ConfigWriterMixin:
    def save_yaml_config(self):
        """Save current configuration to YAML file."""
        if getattr(self, '_load_failed', False):
            if getattr(self, '_reload_refused', False):
                raise ConfigSaveBlocked(
                    f"config.yaml could not be reloaded — the running configuration was kept "
                    f"and the file is not overwritten. Repair {self.config_path} (a copy of "
                    f"the broken file was kept as .yaml.bad) or restore a snapshot.")
            raise ConfigSaveBlocked(
                f"config.yaml failed to load at startup — refusing to overwrite it "
                f"with defaults. Repair {self.config_path} (a copy of the broken "
                f"file was kept as .yaml.bad) and restart.")
        data = {
            # First key on purpose: who wrote this file (see the downgrade
            # warning in _load_yaml_config).
            'config_version': __version__,
            # whether device #1 (the legacy primary) exists — always written,
            # so it is never again a default (ADR-0024)
            'primary_device': bool(self.primary_enabled),
            # how new devices are named — always written, so a file is never
            # again read as a default (ADR-0026 §3)
            'identity_scope': self.identity_scope,
            **({'gateway': {'id': self.gateway_id}} if self.gateway_id else {}),
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
            # the read preview's budget (ADR-0031 §5)
            'preview': self.preview_budget(),
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
                **self._pattern_out('default_topic_pattern', self._topic_pattern_explicit,
                                    self.mqtt.default_topic_pattern),
                'compat_aliases': self.mqtt.compat_aliases,
                # MQTT-RATE-1 — written only when set, so a file without them
                # stays byte-identical on save
                **({'min_interval_s': self.mqtt.min_interval_s}
                   if self.mqtt.min_interval_s else {}),
                **({'deadband_pct': self.mqtt.deadband_pct}
                   if self.mqtt.deadband_pct else {}),
                **({'throttle_poll_groups': list(self.mqtt.throttle_poll_groups)}
                   if self.mqtt.throttle_poll_groups else {}),
            },
            'influxdb': {
                'enabled': self.influxdb.enabled,
                'url': self.influxdb.url,
                'token': self._env_secret_shadow.get('influxdb.token', self.influxdb.token),
                'org': self.influxdb.org,
                'bucket': self.influxdb.bucket,
                'write_interval': self.influxdb.write_interval,
                'publish_mode': self.influxdb.publish_mode,
                'heartbeat_interval': self.influxdb.heartbeat_interval,
                'buffer_minutes': self.influxdb.buffer_minutes,
                'buffer_max_points': self.influxdb.buffer_max_points,
                'buffer_persist': self.influxdb.buffer_persist,
                **self._pattern_out('default_bucket_pattern', self._bucket_pattern_explicit,
                                    self.influxdb.default_bucket_pattern),
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
                'nodes_url': self.ui.nodes_url,
            },
            'security': {
                'allowlist': self.security.allowlist,
                'metrics_allowlist': self.security.metrics_allowlist,
                'allow_nonlan_http_devices': self.security.allow_nonlan_http_devices,
                'allow_writes': self.security.allow_writes,
                'primary_write_locked': self.security.primary_write_locked,
                'write_rate_limit_per_s': self.security.write_rate_limit_per_s,
            },
            'opcua': {
                'enabled': self.opcua.enabled, 'host': self.opcua.host, 'port': self.opcua.port,
                'encryption': self.opcua.encryption, 'allow_anonymous': self.opcua.allow_anonymous,
                'update_s': self.opcua.update_s, 'public_url': self.opcua.public_url,
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

        if self.modules:
            data['modules'] = [dict(m) for m in self.modules]

        # The primary's OPC UA output (write when not the default: on, every value).
        if self.opcua_primary and (self.opcua_primary.get('enabled', True) is not True
                                   or self.opcua_primary.get('fields')):
            data['opcua_output'] = self.opcua_primary

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
        if self._raw_bridges:
            data['bridges'] = self._raw_bridges
        if self._raw_device_types:
            data['device_types'] = self._raw_device_types

        # Preserve the optional alerts block across saves (device/config edits
        # rewrite this file; without this a save would silently drop alerting).
        if self.alerts:
            data['alerts'] = self.alerts

        # Preserve the optional virtual_bms block across saves (ADR-0037).
        if self.virtual_bms:
            data['virtual_bms'] = self.virtual_bms

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
            _copy_private(self.config_path, self.config_path.with_suffix('.yaml.good'))
        except Exception:  # noqa: BLE001
            pass

        logger.info(f"Saved config to {self.config_path}")
        self._write_tree()

    @staticmethod
    def tree_enabled() -> bool:
        """ADR-0047 P3c: also write the config as a tree (``base.yaml`` + ``config.d/``) next
        to config.yaml. Opt-in by environment (``MBG_CONFIG_TREE=1``) while it is experimental:
        the tree is NOT read — config.yaml stays the one authoritative file."""
        return os.getenv('MBG_CONFIG_TREE', '').strip().lower() in ('1', 'true', 'on', 'yes')

    def _write_tree(self) -> None:
        """Mirror the config.yaml just saved into the tree, as one transaction. It splits the
        FILE, not the in-memory dict: whatever order concurrent saves finish in, the tree is
        written from what is on disk and names the revision it came from. A failure here is
        said (log + status) and never fails the save — nothing reads the tree yet."""
        if not self.tree_enabled():
            return
        try:
            from multibus.core.config.tree_store import TreeStore, rev_of
            text = self.config_path.read_text(encoding='utf-8')
            doc = yaml.safe_load(text) or {}
            stats = TreeStore(self.config_path.parent).write(doc, rev_of(text))
            self._tree_status = {"state": "current", "error": None, **stats}
        except Exception as e:  # noqa: BLE001 — the tree is a mirror; config.yaml is saved
            self._tree_status = {"state": "failed", "error": f"{type(e).__name__}: {e}"}
            logger.error("config tree not written (%s) — config.yaml is saved and is what "
                         "the gateway runs on", self._tree_status["error"])

    def _sync_tree(self) -> None:
        """After a load: measure the tree against the config.yaml on disk and bring it up to
        date when it is not current — a write that was interrupted, a tree from before the
        flag was set, or a config.yaml rewritten without a save (the starting values). So the
        status reports what is on disk, not what the last save believed."""
        if not self.tree_enabled() or getattr(self, '_load_failed', False):
            return
        try:
            from multibus.core.config.tree_store import TreeStore, rev_of
            if not self.config_path.exists():
                return
            current = TreeStore(self.config_path.parent).state(
                rev_of(self.config_path.read_text(encoding='utf-8'))) == "current"
        except Exception:  # noqa: BLE001 — measured as not current: rewrite it
            current = False
        if current:
            self._tree_status = {"state": "current", "error": None}
        else:
            self._write_tree()
