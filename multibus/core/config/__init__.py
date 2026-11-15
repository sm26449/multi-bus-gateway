# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
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

import copy
import os
import yaml
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List

from multibus.core.tombstone_store import TombstoneStore

# The protocols a device speaks and the register-type machinery live in a true
# leaf (multibus/core/protocols.py); re-exported here so the ~20 importers of
# these names from multibus.config keep working unchanged.
from multibus.core.protocols import (  # noqa: F401,E402
    _INPUT_REGISTER_ALIASES, _COIL_ALIASES, _DISCRETE_ALIASES,
    SERIAL_PROTOCOLS, BUS_OVER_TCP_PROTOCOLS, NETWORK_PROTOCOLS, MODBUS_PROTOCOLS,
    VIRTUAL_MAP_SERIAL, VIRTUAL_MAP_OVER_TCP, VIRTUAL_MAP_PROTOCOLS, VIRTUAL_MAP_FAMILY,
    REGISTER_MAP_PROTOCOLS, CAN_PROTOCOLS, READ_ONLY_PROTOCOLS, WIRE_FRAMING,
    REGISTER_TYPE_KEY_OFFSET, _can_kwargs, protocol_family, store_key, normalize_register_type,
)


# The module-level config surface now lives in sibling leaves (S0a §4.2);
# re-exported here so every `from multibus.config import …` call site, and the
# Config methods below, keep resolving these names unchanged.
from multibus.core.config.util import (  # noqa: F401
    _TYPE_SPANS, validate_register_identity, _devices_in, _version_tuple, _copy_private,
)
from multibus.core.config.connection import (  # noqa: F401
    ModbusConfig, CONNECT_SETTLE_MAX_S, settle_seconds, _serial_from_conn, _MODULE_ID,
    _MODULE_ICON, _modules, PUBLISH_MODES, _publish_mode, _seconds, _block, _kind_override,
    _opcua_fields, primary_opcua_kept, primary_rest_push_kept, _number_or, PrimaryRebuildError,
    _influx_tags, _http_from_conn, _mqtt_in_from_conn, _can_from_conn,
)
from multibus.core.config.models import (  # noqa: F401
    PRIMARY_DEVICE_ID, LEGACY_TOPIC_PREFIX, LEGACY_HA_DEVICE_NAME, SourceConfig, DeviceConfig,
    MQTTConfig, INFLUX_HEARTBEAT_START_S, HEARTBEAT_MAX_S, PREVIEW_BUDGET, preview_budget_error,
    heartbeat_seconds, _device_heartbeat, _heartbeat_setting, InfluxDBConfig, SecurityConfig,
    OPCUA_ENCRYPTION, _on_off, _DATA_SECTIONS, _ACCESS_SECTIONS, _MAPPING_SECTIONS, _shape_sections,
    ConfigSaveBlocked, _REMOVED_397, _REMOVED_397_ENV, OpcUaConfig, UIConfig, PollGroup,
    parse_address_list, SelectedRegister,
)

# Config's methods are grouped into mixins, one module per responsibility
# (S0a §4.2); the class below keeps only construction, load, identity, the
# preview budget, the primary device and the update_* setters.
from multibus.core.config.build import BuildMixin
from multibus.core.config.components import ComponentsMixin
from multibus.core.config.endpoints import EndpointsMixin
from multibus.core.config.raw_devices import RawDevicesMixin
from multibus.core.config.raw_bridges_endpoints import RawBridgesEndpointsMixin
from multibus.core.config.register_files import RegisterFilesMixin
from multibus.core.config.io import IoMixin
from multibus.core.config.io_save import ConfigWriterMixin
from multibus.core.config.io_views import ConfigViewsMixin
from multibus.core.config.io_health import ConfigHealthMixin
from multibus.core.config.io_registers import RegisterIoMixin
from multibus.core.config.io_sections import ConfigSectionsMixin

logger = logging.getLogger(__name__)


class Config(BuildMixin, ComponentsMixin, EndpointsMixin, RawDevicesMixin,
             RawBridgesEndpointsMixin, RegisterFilesMixin, ConfigWriterMixin,
             ConfigViewsMixin, ConfigHealthMixin, RegisterIoMixin,
             ConfigSectionsMixin, IoMixin):
    """Configuration manager for Multi-Bus Gateway."""

    def __init__(self, config_path: str = "config/config.yaml"):
        self.config_path = Path(config_path)
        self.registers_path = self.config_path.parent / "selected_registers.json"
        # parsed register selections and the .good snapshots, per file version
        self._regs_cache: Dict[Any, tuple] = {}
        self._good_sigs: Dict[str, tuple] = {}
        self.all_registers_path = Path("docs/modbus_data.json")
        # soft-delete lifecycle lives in a collaborator (see tombstone_store.py)
        self._tombstones = TombstoneStore(self.config_path.parent / 'devices',
                                          self._safe_device_id)
        self._last_tombstone: Dict[str, Optional[float]] = {}
        # serializes config-file writes so two concurrent saves (FastAPI runs handlers in a
        # threadpool) can't interleave their tmp/rename dance — set ONCE, never reset on reload
        self._file_lock = __import__("threading").Lock()
        self.load()

    def _init_config_state(self):
        """Every config-DATA field at its cold-boot default. __init__ and the top of load() both
        reach this (load() first), so a reload is TOTAL (ADR-0047 §6/H2): a section absent from the
        file falls back to its default, not the stale in-memory value. Paths, caches, the file lock
        and the tombstone store are set once in __init__ and are NOT reset here."""
        self.modbus = ModbusConfig()
        # the read preview's budget (PREVIEW_BUDGET), kept apart from the
        # primary's connection: no device runs on it
        self.preview_max_requests = PREVIEW_BUDGET['max_requests'][0]
        self.preview_listen_s = PREVIEW_BUDGET['listen_s'][0]
        self.mqtt = MQTTConfig()
        self.influxdb = InfluxDBConfig()
        self.ui = UIConfig()
        self.security = SecurityConfig()
        self.opcua = OpcUaConfig()
        self.poll_groups: Dict[str, PollGroup] = {
            "realtime": PollGroup(interval=1, description="Real-time values"),
            "normal": PollGroup(interval=5, description="Standard measurements"),
            "slow": PollGroup(interval=60, description="Energy counters"),
        }
        # the gateway's own groups (these + config.yaml polling.groups): what
        # a group removed from a device falls back to
        self.default_poll_groups: Dict[str, PollGroup] = {
            n: PollGroup(interval=g.interval, description=g.description) for n, g in self.poll_groups.items()}
        self.selected_registers: List[SelectedRegister] = []
        self.all_registers: Dict = {}
        self.devices: List[DeviceConfig] = []
        # id -> DeviceConfig, rebuilt whenever self.devices is (in _build_devices).
        # get_device() reads this instead of an O(N) scan per lookup (DUP-15 /
        # PERF-10). First-match semantics, matching the old next(...) scan.
        self._device_index: Dict[str, DeviceConfig] = {}
        self._raw_devices: List[Dict] = []
        # the user device types (`device_types:`, device_types.py, ADR-0027)
        self._raw_device_types: List[Dict] = []
        # `device.key` -> why a device's block was ignored at load (see _block)
        self.ignored_blocks: Dict[str, str] = {}
        # Endpoints (one template + one endpoint + N unit ids → N materialized
        # devices). Raw yaml-shaped list; expanded in _build_devices().
        self._raw_endpoints: List[Dict] = []
        self._raw_bridges: List[Dict] = []
        # HTTP/JSON output sink for the PRIMARY device (non-primary devices carry
        # their own flag in the raw `devices[]` list). Off by default — the
        # /api/meters/<id> endpoint is opt-in per device.
        self.http_output_primary_enabled: bool = False
        # the primary's OPC UA output (`opcua_output:`), like http_output
        self.opcua_primary: Dict[str, Any] = {}
        # companion services shown in the menu (`modules:`): [{id, name, icon, url}]
        self.modules: List[Dict[str, str]] = []
        # Generic REST push for the PRIMARY device (non-primary carry their own in
        # the devices[] list). {enabled,url,interval_s,headers,format,verify_tls,timeout}.
        self.rest_push_primary: Dict[str, Any] = {}
        # Jasic PQ event recorder for the PRIMARY device (non-primary carry their
        # own block). {enabled,poll_s,archive_waveforms,base_url}. See pq_recorder.py.
        self.pq_recorder_primary: Dict[str, Any] = {}
        # Original (pre-env) values of secrets overridden by env vars, so a save
        # writes the config value — never the env secret — to config.yaml.
        self._env_secret_shadow: Dict[str, str] = {}
        self.alerts: Dict = {}                # optional `alerts:` block (off by default)
        self.virtual_bms: list = []           # optional `virtual_bms:` instances (ADR-0037; off)
        # Whether the legacy primary (device #1, umg512) exists (ADR-0024).
        # A fresh install has none; `primary_device:` in the file says so
        # explicitly, an older file without the key has one exactly when it
        # holds a `modbus:` section. _primary_file = the file's own word (None:
        # not written there); resolved in load().
        self.primary_enabled: bool = False
        # How new devices are named (ADR-0026 §3): `legacy` (every file
        # written before the key existed) or `gateway` (a first boot), and
        # this gateway's own id. The default patterns as the FILE chose them
        # ('' = the scope's default; a built-in default written out by an
        # older save counts as not chosen).
        self.identity_scope: str = 'legacy'
        self.gateway_id: str = ''
        self._topic_pattern_explicit: str = ''
        self._bucket_pattern_explicit: str = ''
        self._primary_file: Optional[bool] = None
        self._primary_legacy: bool = False
        self._file_mqtt: Dict[str, Any] = {}

    def load(self) -> bool:
        """Load configuration from files — total, atomic, and pure while it reads (ADR-0047 §1,
        design §6; FINDINGS RELOAD-ATOMIC-1).

        * **Total:** state is reset first, so a section absent from the file returns to its
          cold-boot default, not the stale value.
        * **Atomic:** the files are read into a STAGING copy of this object and the result is
          published onto ``self`` in one step. Every component holds ``self``; none of them ever
          sees a half-loaded config (no devices, security at its defaults).
        * **A reload that cannot read its file is refused:** nothing live changes, ``_load_failed``
          is raised on the live object (it blocks saves — the broken file is the operator's — and
          shows in the status) and False is returned. A reload that finds NO file is refused too,
          without blocking saves: the next save writes the running config back. A cold boot has
          no running config to keep — it heals from the last-known-good copy, or runs on
          defaults, as before.
        * **Pure read of config.yaml:** the writes a load makes (``.good``, the starting values,
          a generated gateway id) happen after the publication, on the live object. While
          reading, only the ``.bad`` copy of a file that failed is written — and the register
          selections keep their own ``.good/.bad`` upkeep (``_load_json_with_heal``).

        Returns True when the file's config is now the live one."""
        import copy
        first = not getattr(self, '_loaded', False)
        staging = copy.copy(self)
        staging._staging = True
        staging._good_doc = None
        staging._healed_from_snapshot = False
        staging._load_failed = False          # this load's own verdict, not the last one's
        missing = not self.config_path.exists()
        try:
            staging._read_files()
        except BaseException:
            if not first:
                self._publish_device_types(quiet=True)   # the catalog is process-wide: undo
            raise
        if not first and (missing or staging._load_failed):
            # the catalog was handed the staging copy's device types for its build: give it
            # back the live ones (the bridge guard was never touched by the staging copy)
            self._publish_device_types(quiet=True)
            if missing:
                logger.error("config reload refused: %s is not there — the running "
                             "configuration is kept; the next save writes it back",
                             self.config_path)
            else:
                self._load_failed = True
                self._reload_refused = True
                logger.error("config reload refused: %s could not be loaded — the running "
                             "configuration is kept, saves are DISABLED until the file is "
                             "repaired", self.config_path)
            return False
        staging._reload_refused = False
        if not getattr(staging, '_load_failed', False) and not missing:
            # F20 (ADR-0041 L2, R2D-F20-1): an import/restore that must not unlock anything
            # leaves LOCK_PENDING next to config.yaml BEFORE it writes the bundle. The lock is
            # applied here, to the staging copy and to the file, so the unlocked bundle is
            # never published — not for an instant, and not after a crash: the marker
            # survives until a load has locked, so the next boot does it.
            try:
                staging._apply_pending_lock()
            except Exception:  # noqa: BLE001
                if first:
                    # a boot has to come up: come up with the master write switch OFF
                    logger.exception("the pending forced write-lock could not be applied — "
                                     "starting with security.allow_writes OFF")
                    staging.security.allow_writes = False
                else:
                    self._publish_device_types(quiet=True)
                    raise
        state = dict(staging.__dict__)
        state.pop('_staging', None)
        state['_loaded'] = True
        self.__dict__.update(state)           # the publication: one step, the same object
        self._publish_bridge_guard()          # its recorder is bound to the LIVE object
        self._refresh_good()
        self._write_starting_values()
        self._keep_generated_gateway_id()
        self._sync_tree()                     # the experimental config tree (off by default)
        return True

    LOCK_PENDING = ".lock-writes-pending"

    def lock_pending_path(self) -> Path:
        return self.config_path.parent / self.LOCK_PENDING

    def _apply_pending_lock(self) -> None:
        """Lock every write path of this (staging) config and save it, when the marker of an
        import/restore asks for it; the marker goes only once the locked file is on disk."""
        marker = self.lock_pending_path()
        if not marker.exists():
            return
        n = self.force_lock_all_writes()
        logger.warning("forced write-lock applied before the configuration was published "
                       "(%d path(s) locked) — unlock per device once reviewed", n)
        marker.unlink(missing_ok=True)

    def _read_files(self) -> None:
        """Read every config file into this object. Runs on the staging copy only: no file is
        written here except the ``.bad`` copy of one that failed."""
        self._init_config_state()
        self._load_yaml_config()
        self._resolve_identity()
        self._load_selected_registers()
        self._load_all_registers()
        self._apply_env_overrides()
        self._resolve_primary()
        # the catalog is process-wide and the build reads it (a device's kind may be a user
        # type): it gets this file's types just before the build. A refused reload gives it
        # back the live ones.
        self._publish_device_types()
        # AFTER env overrides, so device #1 inherits MODBUS_HOST etc.
        self._build_devices()
        if not self.primary_enabled and self.selected_registers:
            # a selection with no device to read it would still be announced
            # (HA sensors of a meter that is not there): kept on disk, unused
            logger.info("selected_registers.json ignored: this gateway has no "
                        "primary device (primary_device: false)")
            self.selected_registers = []

    def _keep_generated_gateway_id(self) -> None:
        """A file that says `identity_scope: gateway` with no id got one
        generated: written at once, so every restart names new devices after
        the same id (review M1). Unsaved, it is not used for anything stored."""
        if not getattr(self, '_gateway_id_unsaved', False):
            return
        self._gateway_id_unsaved = False
        try:
            self.save_yaml_config()
            logger.warning("config.yaml: gateway.id %s written", self.gateway_id)
        except Exception as e:  # noqa: BLE001 — a read-only file: no random id is stored anywhere
            logger.error("config.yaml: the generated gateway.id could not be written (%s) — "
                         "new devices get no gateway-scoped name until an id is set", e)
            self.gateway_id = ''
            self._resolve_identity()

    def _resolve_identity(self) -> None:
        """The default patterns new devices get, from the scope and the
        patterns the file chose, and what the publisher names HA entities
        with. Called after a load and after any change to either."""
        from multibus.core.identity import DEFAULT_PATTERNS
        d = DEFAULT_PATTERNS.get(self.identity_scope, DEFAULT_PATTERNS['legacy'])
        self.mqtt.default_topic_pattern = self._topic_pattern_explicit or d['topic']
        self.influxdb.default_bucket_pattern = self._bucket_pattern_explicit or d['bucket']

    def set_identity(self, *, scope: Optional[str] = None, gateway_id: Optional[str] = None) -> None:
        """Change the scope and / or the gateway id (validated by the
        caller), in memory; the caller saves."""
        if scope is not None:
            self.identity_scope = scope
        if gateway_id is not None:
            self.gateway_id = gateway_id
        self._resolve_identity()

    def _load_preview_budget(self, pv) -> None:
        """The read preview's budget from `preview:`; a value that fails its
        check runs as the starting value (said once), a key the file does not
        have is written out (_write_starting_values)."""
        pv = pv if isinstance(pv, dict) else {}
        self._preview_missing = [k for k in PREVIEW_BUDGET if k not in pv]
        for k, (start, _lo, _hi) in PREVIEW_BUDGET.items():
            v = pv.get(k, start)
            if preview_budget_error(k, v):
                logger.warning(f"config: {preview_budget_error(k, v)} — {v!r} ignored, {start} used")
                v = start
            setattr(self, f"preview_{k}", int(v))

    def preview_budget(self) -> Dict[str, int]:
        """The `preview:` section as stored and served."""
        return {'max_requests': int(self.preview_max_requests), 'listen_s': int(self.preview_listen_s)}

    def update_preview(self, **fields) -> None:
        """Set the read preview's budget (checked by the caller)."""
        for k in PREVIEW_BUDGET:
            if fields.get(k) is not None:
                setattr(self, f"preview_{k}", int(fields[k]))

    def _write_starting_values(self) -> None:
        """ADR-0029 (and ADR-0031 §5): a config.yaml that does not have
        influxdb.heartbeat_interval, or the preview budget under modbus:, gets
        the starting value written into it, once, so the value is the file's
        (and Settings') from then on — never a constant only the code knows.
        Only those keys are added: the rest of the file stays as it was (no
        env override is persisted). A file that failed to load is never
        touched. One rewrite, one INFO line per setting written."""
        missing_hb = bool(self._heartbeat_missing)
        missing_pv = list(getattr(self, '_preview_missing', []) or [])
        if ((not missing_hb and not missing_pv) or getattr(self, '_load_failed', False)
                or not self.config_path.exists()):
            return
        self._heartbeat_missing = False
        self._preview_missing = []
        wrote_hb, wrote_pv = False, []
        try:
            with self._file_lock:
                with open(self.config_path, 'r') as f:
                    data = yaml.safe_load(f) or {}
                if missing_hb:
                    ix = data.get('influxdb')
                    if not isinstance(ix, dict):
                        ix = data['influxdb'] = {}
                    if 'heartbeat_interval' not in ix:
                        ix['heartbeat_interval'] = INFLUX_HEARTBEAT_START_S
                        wrote_hb = True
                if missing_pv:
                    pv = data.get('preview')
                    if not isinstance(pv, dict):
                        pv = data['preview'] = {}
                    for k in missing_pv:
                        if k not in pv:
                            pv[k] = PREVIEW_BUDGET[k][0]
                            wrote_pv.append(k)
                if not wrote_hb and not wrote_pv:
                    return
                tmp = self.config_path.with_suffix(self.config_path.suffix + '.tmp')
                fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'w') as f:
                    yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.config_path)
        except Exception as e:  # noqa: BLE001 — it still runs on the value; the next save writes it
            logger.warning(f"config: could not write its starting values into "
                           f"{self.config_path}: {e}")
            return
        # the last-known-good snapshot is left as the load judged it (a
        # truncated file must never be promoted); the next save refreshes it
        if wrote_hb:
            logger.info(f"config: influxdb.heartbeat_interval was not set — wrote "
                        f"{INFLUX_HEARTBEAT_START_S} s into {self.config_path} (an unchanged value "
                        f"is rewritten to InfluxDB every {INFLUX_HEARTBEAT_START_S} s; "
                        f"Settings → InfluxDB, 0 = off). Like any save from the UI, this "
                        f"one-time rewrite drops the file's comments and YAML anchors")
        if wrote_pv:
            logger.info("config: the read preview's budget was not set — wrote "
                        + ", ".join(f"preview.{k}: {PREVIEW_BUDGET[k][0]}" for k in wrote_pv)
                        + f" into {self.config_path} (Settings → Modbus)")

    def _resolve_primary(self) -> None:
        """Does device #1 (the legacy Janitza primary) exist? The file's
        `primary_device:` decides; without it, an older install has one when
        its file holds a `modbus:` section (or MODBUS_HOST names its host),
        and a fresh install has none. ADR-0024."""
        if self._primary_file is not None:
            self.primary_enabled = self._primary_file
        else:
            self.primary_enabled = self._primary_legacy or bool(os.getenv('MODBUS_HOST'))
        if self.primary_enabled:
            # device #1 exists — however it was declared, its old names hold
            # wherever the file is silent (a save then writes them out)
            self._keep_legacy_primary_defaults(self._file_mqtt)

    def _keep_legacy_primary_defaults(self, mqtt_section) -> None:
        """An older install that has the legacy primary read the old defaults
        wherever its file was silent: keep them (a save writes them out)."""
        m = mqtt_section if isinstance(mqtt_section, dict) else {}
        if 'topic_prefix' not in m and not os.getenv('MQTT_PREFIX'):
            self.mqtt.topic_prefix = LEGACY_TOPIC_PREFIX
        if 'device_name' not in (m.get('ha_discovery') or {}):
            self.mqtt.ha_device_name = LEGACY_HA_DEVICE_NAME

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

    def apply_primary(self, *, modbus: Optional[Dict[str, Any]] = None, ha_discovery: Optional[bool] = None,
                      http_output: Optional[bool] = None, opcua: Optional[Dict[str, Any]] = None,
                      rest_push: Optional[Dict[str, Any]] = None) -> None:
        """An edit of the primary meter (PUT and PATCH /api/devices/{primary},
        ADR-0032 §6), already checked, onto its flat sections: ``modbus`` →
        ``modbus:`` (update_modbus, non-None keys), ``ha_discovery`` → the
        gateway's ``mqtt.ha_discovery_enabled``, ``http_output`` /
        ``opcua`` / ``rest_push`` → their primary sections, in the form the
        file keeps (primary_*_kept); None leaves one alone. ``ha_discovery``
        is PUT's legacy mapping only: PATCH refuses it (the gateway's switch).
        One save, then the devices rebuilt. A save that fails puts everything
        back in memory and raises; a rebuild that fails after the save raises
        PrimaryRebuildError (the file is new)."""
        import dataclasses
        snap = (copy.copy(self.modbus), self.mqtt.ha_discovery_enabled, self.http_output_primary_enabled,
                copy.deepcopy(self.opcua_primary), copy.deepcopy(self.rest_push_primary))
        try:
            if modbus:
                self.update_modbus(**modbus)
            if ha_discovery is not None:
                self.mqtt.ha_discovery_enabled = bool(ha_discovery)
            if http_output is not None:
                self.http_output_primary_enabled = bool(http_output)
            if opcua is not None:
                self.opcua_primary = primary_opcua_kept(opcua)
            if rest_push is not None:
                self.rest_push_primary = primary_rest_push_kept(rest_push)
            self.save_yaml_config()
        except Exception:
            # the same ModbusConfig object (the primary's connection, the
            # client's config): its fields put back in place
            for f in dataclasses.fields(snap[0]):
                setattr(self.modbus, f.name, getattr(snap[0], f.name))
            (self.mqtt.ha_discovery_enabled, self.http_output_primary_enabled,
             self.opcua_primary, self.rest_push_primary) = snap[1:]
            raise
        try:
            self._build_devices()
        except Exception as e:
            # the file is written: said as such, never as "nothing saved"
            raise PrimaryRebuildError(f"{type(e).__name__}: {e}") from e

    def update_mqtt(self, **fields):
        """Update MQTT configuration (non-None fields only). A default
        topic pattern equal to a scope's default (or empty) is the default:
        it follows the scope (ADR-0026 §3) — a form loaded before a scope
        switch cannot pin the other scope's."""
        from multibus.core.identity import BUILTIN_TOPIC_PATTERNS
        pat = fields.pop('default_topic_pattern', None)
        fields.pop('identity_scope', None)
        fields.pop('gateway_id', None)
        self._apply_updates(self.mqtt, fields)
        if pat is not None:
            pat = str(pat).strip()
            self._topic_pattern_explicit = '' if pat in ('',) + BUILTIN_TOPIC_PATTERNS else pat
        self._resolve_identity()

    def update_influxdb(self, **fields):
        """Update InfluxDB configuration (non-None fields only); the
        default bucket pattern as update_mqtt's topic pattern."""
        from multibus.core.identity import BUILTIN_BUCKET_PATTERNS
        pat = fields.pop('default_bucket_pattern', None)
        self._apply_updates(self.influxdb, fields)
        if pat is not None:
            pat = str(pat).strip()
            self._bucket_pattern_explicit = '' if pat in ('',) + BUILTIN_BUCKET_PATTERNS else pat
        self._resolve_identity()