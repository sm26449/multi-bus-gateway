# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Raw bridge and endpoint CRUD."""
import logging

from typing import Dict, List, Optional

from multibus.core.config.models import (
    DeviceConfig,
)

logger = logging.getLogger("multibus.core.config")


class RawBridgesEndpointsMixin:
    # ── bridges ───────────────────────────────────────────────────────────────

    @property
    def bridges(self) -> List[Dict]:
        """The raw ``bridges:`` entries (copies; tokens included — callers
        that answer the API go through bridges.public_view)."""
        return [dict(b) for b in self._raw_bridges]

    def get_raw_bridge(self, bridge_id: str) -> Optional[Dict]:
        return next((dict(b) for b in self._raw_bridges if b.get('id') == bridge_id), None)

    def bridge_devices(self, bridge_id: str) -> List["DeviceConfig"]:
        """Devices (and installation units) reaching their bus through it."""
        return [d for d in self.devices if self._device_bridge(d.id) == bridge_id]

    def _device_bridge(self, device_id: str) -> str:
        """The bridge a device names (its own connection, a source, or its
        installation's connection)."""
        for raw in self._raw_devices:
            if raw.get('id') == device_id:
                srcs = [raw.get('connection') or {}] + list(raw.get('sources') or [])
                return next((s.get('bridge') for s in srcs if isinstance(s, dict) and s.get('bridge')), '')
        dev = next((d for d in self.devices if d.id == device_id), None)
        if dev is not None and dev.endpoint_id:
            ep = self.get_raw_endpoint(dev.endpoint_id) or {}
            conns = [ep.get('connection') or {}] + list(ep.get('sources') or [])
            for g in ep.get('groups') or []:
                conns += [g.get('connection') or {}] + list(g.get('sources') or [])
            return next((c.get('bridge') for c in conns if isinstance(c, dict) and c.get('bridge')), '')
        return ''

    def upsert_raw_bridge(self, raw: Dict, port_moves: Optional[Dict[int, int]] = None) -> None:
        """Create or replace a bridge and rebuild the device list (its devices
        follow a changed host). ``port_moves`` {old: new} renumbers a bus: the
        devices that named the old port name the new one. Transactional."""
        import copy as _copy
        bid = str(raw.get('id', '')).strip()
        if not bid:
            raise ValueError("bridge id is required")
        snap = [dict(b) for b in self._raw_bridges]
        snap_dev = _copy.deepcopy(self._raw_devices)
        snap_ep = _copy.deepcopy(self._raw_endpoints)
        try:
            if port_moves:
                self._move_bridge_ports(bid, port_moves)
            self._raw_bridges = [b for b in self._raw_bridges if b.get('id') != bid] + [raw]
            self._build_devices()
            self.save_yaml_config()
        except Exception:
            self._raw_bridges, self._raw_devices, self._raw_endpoints = snap, snap_dev, snap_ep
            self._build_devices()
            raise

    def _move_bridge_ports(self, bridge_id: str, moves: Dict[int, int]) -> None:
        """Every connection that names this bridge and a moved port."""
        def fix(c):
            if isinstance(c, dict) and c.get('bridge') == bridge_id:
                try:
                    old = int(c.get('bridge_port'))
                except (TypeError, ValueError):
                    return
                if old in moves:
                    c['bridge_port'] = moves[old]
        for d in self._raw_devices:
            fix(d.get('connection'))
            for sx in d.get('sources') or []:
                fix(sx)
        for ep in self._raw_endpoints:
            fix(ep.get('connection'))
            for sx in ep.get('sources') or []:
                fix(sx)
            for g in ep.get('groups') or []:
                fix(g.get('connection'))
                for sx in g.get('sources') or []:
                    fix(sx)

    def delete_bridge(self, bridge_id: str) -> None:
        if self.get_raw_bridge(bridge_id) is None:
            raise ValueError(f"bridge {bridge_id!r} not found")
        users = [d.id for d in self.bridge_devices(bridge_id)]
        if users:
            raise ValueError(f"bridge {bridge_id!r} is used by {', '.join(users)} — "
                             "move or delete those devices first")
        self._raw_bridges = [b for b in self._raw_bridges if b.get('id') != bridge_id]
        self._build_devices()
        self.save_yaml_config()

    # ── endpoints ─────────────────────────────────────────────────────────────

    @property
    def endpoints(self) -> List[Dict]:
        """The raw ``endpoints:`` entries (yaml-shaped, copies)."""
        return [dict(p) for p in self._raw_endpoints]

    def get_raw_endpoint(self, endpoint_id: str) -> Optional[Dict]:
        return next((dict(p) for p in self._raw_endpoints
                     if p.get('id') == endpoint_id), None)

    def endpoint_group_ids(self, endpoint_id: str) -> List[str]:
        """The groups of one endpoint, in declaration order. An endpoint that
        declares none reports its single implicit group, so callers never need a
        special case."""
        raw = self.get_raw_endpoint(endpoint_id)
        if raw is None:
            return []
        return [g['id'] for g in self._endpoint_groups(raw)]

    def endpoint_devices(self, endpoint_id: str) -> List[DeviceConfig]:
        """The materialized DeviceConfigs of one endpoint, in units[] order."""
        return [d for d in self.devices if d.endpoint_id == endpoint_id]

    def upsert_raw_endpoint(self, raw: Dict) -> List[DeviceConfig]:
        """Create or update an endpoint from its raw yaml-shaped dict, re-expand
        the device list and persist. Returns the materialized DeviceConfigs.
        Transactional — a build/save failure restores the previous state."""
        pid = str(raw.get('id', '')).strip()
        if not pid:
            raise ValueError("endpoint id is required")
        _snapshot = [dict(p) for p in self._raw_endpoints]
        try:
            self._raw_endpoints = ([p for p in self._raw_endpoints
                                 if p.get('id') != pid] + [raw])
            self._build_devices()
            made = self.endpoint_devices(pid)
            if not made:
                raise ValueError(f"endpoint {pid!r} produced no devices — check "
                                 "its units[] (valid unit ids 0..255, ids "
                                 "must not collide with existing devices)")
            self.save_yaml_config()
            return made
        except Exception:
            self._raw_endpoints = _snapshot
            self._build_devices()
            raise

    def delete_endpoint(self, endpoint_id: str) -> List[str]:
        """Remove an endpoint; its materialized devices vanish on rebuild. Returns
        the removed device ids (register files stay on disk for a re-add).
        Raises ValueError when the endpoint does not exist."""
        if self.get_raw_endpoint(endpoint_id) is None:
            raise ValueError(f"endpoint {endpoint_id!r} not found")
        removed = [d.id for d in self.endpoint_devices(endpoint_id)]
        self._raw_endpoints = [p for p in self._raw_endpoints
                            if p.get('id') != endpoint_id]
        self._build_devices()
        self.save_yaml_config()
        return removed

