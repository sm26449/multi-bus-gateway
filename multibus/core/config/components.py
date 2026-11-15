# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Component devices: their ids, entries, expansion and CRUD."""
import logging
import re

from typing import Dict, List, Optional

from multibus.core.config.models import (
    DeviceConfig,
)

logger = logging.getLogger("multibus.core.config")


class ComponentsMixin:
    @staticmethod
    def component_id(parent_id: str, ctype: str, instance: str) -> str:
        """The id a component gets when its entry names none."""
        return re.sub(r'[^a-z0-9_-]', '-', f"{parent_id}-{ctype}-{instance}".lower())

    def component_entries(self, p: Dict) -> List[Dict]:
        """A device's ``components`` list, normalized: {type, instance, id,
        name, enabled, entry}. A malformed or repeated entry is skipped with a
        warning; an instance written as a number is read as text."""
        pid = str(p.get('id', '')).strip()
        out: List[Dict] = []
        seen: set = set()
        for i, c in enumerate(p.get('components') or []):
            if not isinstance(c, dict):
                logger.warning(f"devices[{pid}].components[{i}]: not a mapping — skipped")
                continue
            ctype = str(c.get('type', '') or '').strip()
            inst = '' if c.get('instance') is None else str(c.get('instance')).strip()
            if not ctype or not self.COMPONENT_INSTANCE_RE.match(inst):
                logger.warning(f"devices[{pid}].components[{i}]: needs a type and an instance "
                               f"(letters, digits, _ . -) — skipped")
                continue
            if (ctype, inst) in seen:
                logger.warning(f"devices[{pid}].components: {ctype} {inst} twice — skipped")
                continue
            cid = str(c.get('id') or self.component_id(pid, ctype, inst)).strip()
            if not re.match(r'^[a-z0-9][a-z0-9_-]{1,63}$', cid):
                logger.warning(f"devices[{pid}].components[{i}]: id {cid!r} is not a device id — skipped")
                continue
            seen.add((ctype, inst))
            out.append({'type': ctype, 'instance': inst,
                        'id': cid,
                        'name': str(c.get('name') or ''),
                        'enabled': bool(c.get('enabled', True)), 'entry': c})
        return out

    @staticmethod
    def _parent_mqtt_connection(p: Dict) -> Dict:
        """The connection a component reads through: the parent's, or its
        first MQTT source's."""
        conn = dict(p.get('connection') or {})
        if conn:
            return conn
        for sx in p.get('sources') or []:
            if isinstance(sx, dict) and str(sx.get('protocol', '')).lower() == 'mqtt':
                return {k: v for k, v in sx.items()
                        if k not in ('id', 'template', 'enabled', 'poll_groups')}
        return {}

    def _expand_components(self) -> List[tuple]:
        """Every device's components as (parent id, component, raw device dict),
        shaped like a devices[] entry."""
        out: List[tuple] = []
        for p in self._raw_devices:
            if not p.get('components'):
                continue
            pid = str(p.get('id', '')).strip()
            pconn = self._parent_mqtt_connection(p)
            if str(pconn.get('protocol', 'tcp')).lower() != 'mqtt':
                # said once per parent: this runs on every ETag a device
                # write answers (ADR-0030), not only at a load
                warned = self.__dict__.setdefault('_comp_not_mqtt_warned', set())
                if pid not in warned:
                    warned.add(pid)
                    logger.warning(f"devices[{pid}]: components are read over MQTT only — "
                                   "its components are not started")
                continue
            pm, pi = dict(p.get('mqtt') or {}), dict(p.get('influxdb') or {})
            po = p.get('opcua') if isinstance(p.get('opcua'), dict) else {}
            for c in self.component_entries(p):
                e, cid = c['entry'], c['id']
                conn = dict(pconn)
                # the instance is the component's; nothing in an entry overrides it
                conn['vars'] = {**dict(conn.get('vars') or {}), 'instance': c['instance']}
                em, ei = dict(e.get('mqtt') or {}), dict(e.get('influxdb') or {})
                mqtt = {'ha_discovery': pm.get('ha_discovery', True),
                        'enabled': pm.get('enabled', True), **em,
                        'topic_prefix': em.get('topic_prefix') or f"mbg/devices/{cid}"}
                influx = {'enabled': pi.get('enabled', True),
                          **({'bucket': pi['bucket']} if pi.get('bucket') else {}),
                          **({'tags': dict(pi['tags'])} if isinstance(pi.get('tags'), dict) else {}),
                          **ei, 'device_tag': ei.get('device_tag') or cid}
                out.append((pid, c, {
                    'id': cid,
                    'name': c['name'] or f"{p.get('name') or pid} {c['type']} {c['instance']}",
                    'template': f"{p.get('template', '')}:{c['type']}",
                    'enabled': bool(p.get('enabled', True)) and c['enabled'],
                    'connection': conn, 'mqtt': mqtt, 'influxdb': influx,
                    'opcua': dict(e['opcua']) if isinstance(e.get('opcua'), dict)
                    else {'enabled': bool(po.get('enabled', True))},
                    # read-only: there is nothing to write through a component
                    'write_locked': bool(e.get('write_locked', True)),
                    **{k: e[k] for k in self._COMPONENT_OWN if k in e},
                }))
        return out

    def component_devices(self, parent_id: str) -> List["DeviceConfig"]:
        return [d for d in self.devices if d.component_of == parent_id]

    def _component_entry(self, parent_id: str, component_id: str) -> Optional[Dict]:
        p = next((d for d in self._raw_devices if d.get('id') == parent_id), None)
        if p is None:
            return None
        return next((c['entry'] for c in self.component_entries(p)
                     if c['id'] == component_id), None)

    def _raw_device_entry(self, device_id: str) -> Optional[Dict]:
        """The raw entry a device's own settings are written to: its
        devices[] entry, or — for a component — its entry in its parent's
        list. None for the primary and an installation's units."""
        raw = next((d for d in self._raw_devices if d.get('id') == device_id), None)
        if raw is not None:
            return raw
        dev = self.get_device(device_id)
        if dev is not None and dev.component_of:
            return self._component_entry(dev.component_of, device_id)
        return None

    def _mutate_raw_devices(self, fn) -> None:
        """Apply ``fn`` to the raw devices, rebuild and save — or put
        everything back as it was when any step fails."""
        import copy as _copy
        snap = _copy.deepcopy(self._raw_devices)
        try:
            fn()
            self._build_devices()
            self.save_yaml_config()
        except Exception:
            self._raw_devices = snap
            self._build_devices()
            raise

    def add_component(self, parent_id: str, entry: Dict) -> "DeviceConfig":
        """Add one component to a device and persist; the built device."""
        p = next((d for d in self._raw_devices if d.get('id') == parent_id), None)
        if p is None:
            raise ValueError(f"device {parent_id!r} is not a configurable device")
        cid = str(entry.get('id') or self.component_id(
            parent_id, str(entry.get('type', '')), str(entry.get('instance', ''))))
        self._mutate_raw_devices(lambda: p.setdefault('components', []).append(dict(entry, id=cid)))
        dev = self.get_device(cid)
        if dev is None or dev.component_of != parent_id:
            self._mutate_raw_devices(lambda: p.__setitem__(
                'components', [c for c in p['components'] if c.get('id') != cid]))
            raise ValueError(f"component {cid!r} could not be built (see the log)")
        return dev

    def update_component(self, parent_id: str, component_id: str, changes: Dict) -> "DeviceConfig":
        """Rename or switch a component; its type and instance never change."""
        e = self._component_entry(parent_id, component_id)
        if e is None:
            raise ValueError(f"no component {component_id!r} on {parent_id!r}")

        def go():
            for k in ('name', 'enabled'):
                if k in changes:
                    e[k] = changes[k]
        self._mutate_raw_devices(go)
        return self.get_device(component_id)

    def remove_component(self, parent_id: str, component_id: str) -> Dict:
        """Remove a component and persist; its entry. Its register file stays,
        so adding the same instance again resumes it."""
        p = next((d for d in self._raw_devices if d.get('id') == parent_id), None)
        e = self._component_entry(parent_id, component_id)
        if p is None or e is None:
            raise ValueError(f"no component {component_id!r} on {parent_id!r}")
        self._mutate_raw_devices(lambda: p.__setitem__(
            'components', [c for c in p['components'] if c is not e]))
        return dict(e)

