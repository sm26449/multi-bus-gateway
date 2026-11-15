# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Raw device CRUD, the output/sink/publishing/opcua/kind/meter setters, and tombstones."""
import logging
import re

from typing import Any, Dict, List, Optional

from multibus.core import catalog_port, host_port
from multibus.core.config.connection import (
    _opcua_fields, primary_opcua_kept, primary_rest_push_kept,
)
from multibus.core.config.models import (
    PRIMARY_DEVICE_ID, DeviceConfig,
    MQTTConfig,
)

logger = logging.getLogger("multibus.core.config")


class RawDevicesMixin:
    def default_topic_prefix(self, device_id: str) -> str:
        """Resolve the default MQTT topic prefix for a NEW device from the
        configured pattern ({device} = id, {gateway} = this gateway's id)."""
        from multibus.core.identity import fill
        pat = getattr(self.mqtt, 'default_topic_pattern', '') or MQTTConfig.default_topic_pattern
        return fill(pat, device_id, self.gateway_id)

    def default_bucket(self, device_id: str) -> str:
        from multibus.core.identity import fill
        pat = getattr(self.influxdb, 'default_bucket_pattern', '{device}') or '{device}'
        return fill(pat, device_id, self.gateway_id)

    @property
    def primary_device(self) -> Optional[DeviceConfig]:
        """Device #1 (the legacy primary), or None — a fresh install has none
        (ADR-0024)."""
        d = self.devices[0] if self.devices else None
        return d if (d is not None and d.primary) else None

    @property
    def primary_id(self) -> str:
        """The id the legacy primary's live store answers to — '' when there
        is no primary, so no route offers a device that is not there."""
        return PRIMARY_DEVICE_ID if self.primary_device is not None else ''

    def get_device(self, device_id: str) -> Optional[DeviceConfig]:
        # O(1) via the index _build_devices maintains (DUP-15 / PERF-10).
        return self._device_index.get(device_id)

    def raw_device_refusal(self, did: str) -> Optional[str]:
        """Why a `devices[]` entry cannot be written at id ``did``, or None —
        asked by a save and by its dry run alike (ADR-0026 §5)."""
        if not did:
            return "device id is required"
        if did == PRIMARY_DEVICE_ID:
            # reserved even where there is no primary: its registers live in the
            # legacy root file, so the id must never mean two devices
            return (f"'{PRIMARY_DEVICE_ID}' is reserved for the legacy primary "
                    "device (edited via the Modbus settings) — choose another id")
        _ex = self.get_device(did)
        if _ex is not None and _ex.endpoint_id:
            return (f"device {did!r} is materialized from endpoint "
                    f"{_ex.endpoint_id!r} — edit the endpoint instead")
        if _ex is not None and _ex.component_of:
            return (f"device {did!r} is a component of {_ex.component_of!r} — "
                    "manage it from that device")
        return None

    def upsert_raw_device(self, raw: Dict) -> DeviceConfig:
        """Create or update a non-primary device from its raw yaml-shaped dict,
        rebuild the device list and persist config.yaml. Returns the built
        DeviceConfig. Raises ValueError on invalid/primary ids."""
        did = str(raw.get('id', '')).strip()
        why = self.raw_device_refusal(did)
        if why:
            raise ValueError(why)
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

    def upsert_raw_devices(self, raws: List[Dict]) -> List[DeviceConfig]:
        """Several ``upsert_raw_device`` at once: one rebuild, one config
        write, all or nothing (Edit bus line…, ADR-0031 §4)."""
        ids = [str(r.get('id', '')).strip() for r in raws]
        for did in ids:
            why = self.raw_device_refusal(did)
            if why:
                raise ValueError(why)
        _snapshot = [dict(d) for d in self._raw_devices]
        try:
            by = dict(zip(ids, raws))
            self._raw_devices = [by.pop(d.get('id'), d) for d in self._raw_devices] + list(by.values())
            self._build_devices()
            built = [self.get_device(did) for did in ids]
            missing = [did for did, b in zip(ids, built) if b is None]
            if missing:
                raise ValueError(f"device(s) {', '.join(missing)} could not be built from the given data")
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
            self._own_entry(device_id)['write_locked'] = bool(locked)
        self._build_devices()
        self.save_yaml_config()

    def force_lock_all_writes(self) -> int:
        """F20 (ADR-0041 L2): lock EVERY write path so a restore/import cannot write
        until the operator unlocks — the primary, every devices[] entry and its
        components, and every endpoint (its units lock together). Returns how many
        distinct paths it locked (an endpoint counts once). Idempotent: an
        already-locked path is skipped.

        All the flags are set in memory first, then the devices are rebuilt ONCE and
        the file is saved ONCE (R2D-F20-1): it used to save after every device, so a
        failure half-way left some paths locked on disk and the rest writable."""
        eps_done: set = set()
        locked = 0
        for d in list(self.devices):
            if d.write_locked:
                continue
            if d.primary:
                self.security.primary_write_locked = True
            elif d.endpoint_id:
                if d.endpoint_id in eps_done:
                    continue
                eps_done.add(d.endpoint_id)
                for p in self._raw_endpoints:
                    if p.get('id') == d.endpoint_id:
                        p['write_locked'] = True
                        break
                else:
                    raise ValueError(f"endpoint {d.endpoint_id!r} not found")
            else:
                self._own_entry(d.id)['write_locked'] = True
            locked += 1
        if locked:
            self._build_devices()
            self.save_yaml_config()
        return locked

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
            host_port.apply_http_output(self._own_entry(device_id), enabled)
        self._build_devices()
        self.save_yaml_config()

    def set_device_sinks(self, device_id: str, mqtt: Optional[bool] = None,
                         influxdb: Optional[bool] = None, ha_discovery: Optional[bool] = None) -> None:
        """Switch a device's MQTT / InfluxDB output or its Home Assistant
        discovery, and persist; None leaves a flag alone. Only a devices[]
        entry: the primary always routes, and an endpoint's units route as
        their installation says."""
        dev = self.get_device(device_id)
        if dev is None:
            raise ValueError(f"device {device_id!r} not found")
        if dev.primary:
            raise ValueError("the primary device always publishes to MQTT and InfluxDB")
        if dev.endpoint_id:
            raise ValueError(f"set on its installation ({dev.endpoint_id}): it applies to every unit")
        host_port.apply_sinks(self._own_entry(device_id), mqtt=mqtt, influxdb=influxdb, ha_discovery=ha_discovery)
        self._build_devices()
        self.save_yaml_config()

    def set_device_publishing(self, device_id: str, mqtt: Optional[Dict[str, Any]] = None,
                              influxdb: Optional[Dict[str, Any]] = None) -> None:
        """A device's own publishing: mqtt {publish_mode, heartbeat_interval},
        influxdb {publish_mode, write_interval}; a key set to None goes back
        to the gateway's (Settings), a key left out stays. Endpoint units →
        their GROUP's block (one kind of device, one rhythm), or the
        endpoint's. The primary is the gateway's own: it follows Settings."""
        dev = self.get_device(device_id)
        if dev is None:
            raise ValueError(f"device {device_id!r} not found")
        if dev.primary:
            raise ValueError("the primary device follows Settings → MQTT / InfluxDB")
        if dev.endpoint_id:
            ep = self._endpoint_entry_for(dev.endpoint_id)
            target = next((g for g in (ep.get('groups') or []) if isinstance(g, dict)
                           and str(g.get('id', '')) == dev.group_id), None) or ep
        else:
            target = self._own_entry(device_id)
        host_port.apply_publishing(target, mqtt=mqtt, influxdb=influxdb)
        self._build_devices()
        self.save_yaml_config()

    def set_opcua_output(self, device_id: str, enabled: bool, fields: Optional[List[str]] = None) -> None:
        """Serve a device on the OPC UA server or not, and which of its values
        ([] = every one), and persist. Primary → `opcua_output:`; devices[]
        entries → their own block; endpoint units → their GROUP's block (a
        group is one template, so one choice of values for all its units), or
        the endpoint's when it has no groups."""
        dev = self.get_device(device_id)
        if dev is None:
            raise ValueError(f"device {device_id!r} not found")
        block = {'enabled': bool(enabled)}
        names = _opcua_fields(fields)
        if names:
            block['fields'] = names
        if dev.primary:
            self.opcua_primary = primary_opcua_kept(block)
        elif dev.endpoint_id:
            ep = self._endpoint_entry_for(dev.endpoint_id)
            grp = next((g for g in (ep.get('groups') or []) if isinstance(g, dict)
                        and str(g.get('id', '')) == dev.group_id), None)
            (grp if grp is not None else ep)['opcua'] = block
        else:
            host_port.apply_opcua(self._own_entry(device_id), block)
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
            self._own_entry(device_id)['pq_recorder'] = cfg
        self._build_devices()
        self.save_yaml_config()

    def _own_entry(self, device_id: str) -> Dict:
        """A device's own raw entry (a component's included), for a setter."""
        raw = self._raw_device_entry(device_id)
        if raw is None:
            raise ValueError(f"device {device_id!r} is not a configurable device")
        return raw

    def _set_device_types(self, raw) -> None:
        """Hold the user device types as the file has them: the raw entries
        are kept for the next save (an entry that cannot be read too — it is
        the file's, and is only skipped). The catalog — process-wide — gets them
        from ``_publish_device_types``, not from here: a load parses into a
        staging copy (ADR-0047 §1) and must not empty the live catalog while it reads."""
        self._raw_device_types = [dict(x) if isinstance(x, dict) else x
                                  for x in (raw if isinstance(raw, list) else [])]

    def _publish_device_types(self, quiet: bool = False) -> None:
        """Hand the readable user device types to the catalog."""
        for where, why in catalog_port.set_user_types(self._raw_device_types).items():
            if not quiet:
                logger.warning(f"{where}: {why}: ignored")

    @property
    def device_types(self) -> List[Dict]:
        return [dict(x) for x in self._raw_device_types if isinstance(x, dict)]

    def save_device_types(self, types: List[Dict]) -> None:
        """Replace the user device types and save (the API checked them)."""
        before = self._raw_device_types
        self._raw_device_types = [dict(x) for x in types]
        catalog_port.set_user_types(self._raw_device_types)
        try:
            self.save_yaml_config()
        except Exception:
            self._raw_device_types = before
            catalog_port.set_user_types(before)
            raise

    def set_device_kind(self, device_id: str, kind: Optional[str]) -> None:
        """An admin's word on what kind of equipment a device is (None or ''
        = derive it again), persisted on its own entry. Never restarts it:
        the kind is not something it runs on."""
        dev = self.get_device(device_id)
        if dev is None:
            raise ValueError(f"device {device_id!r} not found")
        if dev.primary:
            raise ValueError("the primary device's kind comes from its template")
        if dev.endpoint_id:
            raise ValueError(f"a unit's kind is its group's role on its installation ({dev.endpoint_id})")
        raw = self._own_entry(device_id)

        def go():
            if kind:
                raw['kind'] = kind
            else:
                raw.pop('kind', None)
        self._mutate_raw_devices(go)

    def set_energy_meter(self, device_id: str, link: Dict) -> None:
        """The device that measures this one's electricity ({} = none)."""
        dev = self.get_device(device_id)
        if dev is None:
            raise ValueError(f"device {device_id!r} not found")
        if dev.endpoint_id:
            raise ValueError(f"'{device_id}' belongs to an installation — not supported for its units yet")
        raw = self._own_entry(device_id)

        def go():
            if link.get('device'):
                raw['energy_meter'] = dict(link)
            else:
                raw.pop('energy_meter', None)
        self._mutate_raw_devices(go)

    def set_meters(self, device_id: str, links: List[Dict], options: Optional[Dict] = None) -> None:
        """The meters that measure this device, each in a role ([] = none),
        already validated (meter_links.validate). A single consumption link
        is written as ``energy_meter``, as before ``meters`` existed.
        ``options`` (meter_links.check_options): None keeps what is stored,
        {} writes none (the defaults)."""
        dev = self.get_device(device_id)
        if dev is None:
            raise ValueError(f"device {device_id!r} not found")
        if dev.endpoint_id:
            raise ValueError(f"'{device_id}' belongs to an installation — not supported for its units yet")
        raw = self._own_entry(device_id)
        legacy, listed = host_port.meter_stored_form(links)

        def go():
            raw.pop('energy_meter', None)
            raw.pop('meters', None)
            if legacy:
                raw['energy_meter'] = legacy
            elif listed:
                raw['meters'] = listed
            if options is not None:
                raw.pop('meter_options', None)
                if options:
                    raw['meter_options'] = dict(options)
        self._mutate_raw_devices(go)

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
            self.rest_push_primary = primary_rest_push_kept(cfg)
        elif dev.endpoint_id:
            self._endpoint_entry_for(dev.endpoint_id)['rest_push'] = cfg
        else:
            host_port.apply_rest_push(self._own_entry(device_id), cfg)
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
        # what an Undo names (its deleted_ts), None when nothing was kept
        self._last_tombstone[device_id] = self._write_device_tombstone(device_id, raw) if raw is not None else None
        self._build_devices()
        self.save_yaml_config()
        return True

    def discard_raw_device(self, device_id: str) -> bool:
        """Take back a device just created whose creation failed: its entry
        goes, and nothing else — no tombstone is written, and an older
        device's kept dir at the same id is left as it was."""
        before = len(self._raw_devices)
        self._raw_devices = [d for d in self._raw_devices if d.get('id') != device_id]
        if len(self._raw_devices) == before:
            return False
        self._build_devices()
        self.save_yaml_config()
        return True

    def _write_device_tombstone(self, device_id: str, raw: Dict) -> Optional[float]:
        return self._tombstones.write(device_id, raw)

    def last_tombstone_ts(self, device_id: str) -> Optional[float]:
        """The ``deleted_ts`` the last delete of ``device_id`` kept, or None
        when its settings could not be kept (it cannot be restored)."""
        return self._last_tombstone.get(device_id)

    def deleted_device_ts(self, device_id: str) -> Optional[float]:
        """The kept device's ``deleted_ts`` (None: absent or unreadable)."""
        return self._tombstones.deleted_ts(device_id)

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

