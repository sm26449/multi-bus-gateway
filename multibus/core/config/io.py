# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Load config.yaml and the register files, apply env overrides, and build to_dict / config_status.
The writer (save_yaml_config) lives in io_save.py (ADR-0047 P1)."""
import logging
import os

import yaml
from typing import Dict, List

from multibus import __version__
from multibus.core.config.util import (
    _version_tuple, _copy_private,
)
from multibus.core.config.models import (
    _on_off, _shape_sections,
    _REMOVED_397_ENV,
)

logger = logging.getLogger("multibus.core.config")

class IoMixin:
    def _load_yaml_config(self):
        """Load main YAML configuration."""
        self._primary_file, self._primary_legacy = None, False
        self._heartbeat_missing = False
        self._preview_missing = []
        self._file_mqtt = {}
        self._degraded_sections: List[str] = []
        self.identity_scope, self.gateway_id = 'legacy', ''
        self._topic_pattern_explicit = self._bucket_pattern_explicit = ''
        self._gateway_id_unsaved = False
        self._set_device_types([])
        if not self.config_path.exists():
            logger.warning(f"Config file not found: {self.config_path}, using defaults")
            # a first boot names its devices after this gateway (ADR-0026 §3);
            # the id is kept by the save that follows
            from multibus.core.identity import new_gateway_id
            self.identity_scope, self.gateway_id = 'gateway', new_gateway_id()
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
            # a section in the wrong shape costs that section, not the file
            self._degraded_sections = _shape_sections(data)

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

            self._load_identity(data)

            # The legacy primary (ADR-0024): the file's word, else an older
            # install has it exactly when its file holds a `modbus:` section
            if 'primary_device' in data:
                self._primary_file = _on_off(data.get('primary_device'), 'modbus' in data)
            else:
                self._primary_legacy = 'modbus' in data

            self._parse_modbus(data)
            self._parse_mqtt(data)
            self._parse_influxdb(data)
            self._parse_ui(data)
            self._parse_security(data)
            self._parse_removed_397(data)
            self._parse_opcua(data)
            self._parse_polling(data)
            self._parse_output_sinks(data)
            self._parse_raw_entities(data)

            # The Device Builder moved out of the gateway (MBG Pro): an
            # `esphome:` block left from an older version is
            # ignored (and dropped by the next save), never an error.
            if data.get('esphome'):
                logger.info("config: the `esphome:` block is ignored — ESP firmware "
                            "is available in MBG Pro")

            _ix = data.get('influxdb')
            self._heartbeat_missing = 'heartbeat_interval' not in (_ix if isinstance(_ix, dict) else {})
            self._load_preview_budget(data.get('preview'))
            logger.info(f"Loaded config from {self.config_path}")
            self._load_failed = False
            # The last-known-good snapshot is refreshed from this document AFTER the load is
            # published (_refresh_good, called by load()) — reading writes nothing. Not while
            # healing: that would only copy the snapshot onto itself.
            if not getattr(self, '_healing', False):
                self._good_doc = data

        except Exception as e:
            # A corrupt config.yaml must NOT silently become "defaults": the
            # primary would poll the wrong host and a later save would overwrite
            # the user's real file with defaults (permanent config loss). Keep a
            # copy of the broken file and block saves until it's fixed.
            self._load_failed = True
            # unreadable: nothing says the primary is not there — keep what an
            # older gateway did (device #1 with its old names); saves are
            # blocked until the file is repaired, so this is never persisted
            self._primary_file, self._primary_legacy = None, True
            self._file_mqtt = {}
            try:
                bad = self.config_path.with_suffix('.yaml.bad')
                _copy_private(self.config_path, bad)
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
                    # the rejected file may have assigned sections before it failed (security,
                    # ui, transport …): start from cold defaults again, or a `.good` that is
                    # silent on a section inherits it from the file that was REFUSED
                    # (R2D-HEAL-1: allow_writes: true survived the heal)
                    self._init_config_state()
                    self._load_yaml_config()          # re-parse the snapshot
                except Exception as e2:  # noqa: BLE001
                    logger.error(f"snapshot load also failed: {e2}")
                finally:
                    self.config_path = _orig
                    self._healing = False
                self._load_failed = True              # real file still broken → block saves
                self._healed_from_snapshot = True

    def _refresh_good(self) -> None:
        """Keep a last-known-good snapshot of the file a load just read, so a future corrupt
        edit can be self-healed instead of falling back to bare defaults. Runs after the load
        was published; a file that failed to load never gets here (``_good_doc`` is unset)."""
        data, self._good_doc = getattr(self, '_good_doc', None), None
        if data is None:
            return
        try:
            good = self.config_path.with_suffix('.yaml.good')
            if self._degraded_sections:
                # a section was read as its defaults: this file is
                # not a good copy — the real section lives on in .good
                logger.warning(
                    "config.yaml: %s read as defaults — NOT refreshing the "
                    "last-known-good snapshot %s", ', '.join(self._degraded_sections), good)
            elif self._good_would_regress(data, good):
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
                _copy_private(self.config_path, good)
        except Exception:  # noqa: BLE001
            pass

    def _load_identity(self, data: Dict) -> None:
        """`identity_scope` and `gateway.id` as the file says them. A file
        without the scope is an existing install: legacy. A value that is
        not one is reported and not used."""
        from multibus.core.identity import (BUILTIN_BUCKET_PATTERNS, BUILTIN_TOPIC_PATTERNS, SCOPES, gateway_id_error,
                               new_gateway_id)
        scope = data.get('identity_scope')
        if scope is None:
            scope = 'legacy'
        elif scope not in SCOPES:
            logger.warning("config.yaml: identity_scope %r is not one of %s — legacy is used",
                           scope, ', '.join(SCOPES))
            scope = 'legacy'
        gw = data.get('gateway')
        gid = gw.get('id') if isinstance(gw, dict) else None
        if gid is not None and gateway_id_error(gid):
            logger.warning("config.yaml: gateway.id %r is not a gateway id (%s) — ignored",
                           gid, gateway_id_error(gid))
            gid = None
        gid = gid or ''
        if scope == 'gateway' and not gid:
            gid = new_gateway_id()
            self._gateway_id_unsaved = True        # load() keeps it in the file at once
            logger.warning("config.yaml: identity_scope is gateway but no gateway.id — %s is generated", gid)
        self.identity_scope, self.gateway_id = scope, gid
        tp = (data.get('mqtt') or {}).get('default_topic_pattern') if isinstance(data.get('mqtt'), dict) else None
        bp = ((data.get('influxdb') or {}).get('default_bucket_pattern')
              if isinstance(data.get('influxdb'), dict) else None)
        # a scope's default written out (every save before the scope existed
        # did) is the default, not a choice
        self._topic_pattern_explicit = tp if tp and tp not in BUILTIN_TOPIC_PATTERNS else ''
        self._bucket_pattern_explicit = bp if bp and bp not in BUILTIN_BUCKET_PATTERNS else ''

    def _apply_env_overrides(self):
        """Apply environment variable overrides."""
        # the kill switches removed in 3.97: an environment still setting
        # them is said, and changes nothing
        _gone = [k for k in _REMOVED_397_ENV if os.getenv(k)]
        if _gone:
            logger.info("env %s ignored — removed in 3.97 (always on)", ', '.join(_gone))

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
