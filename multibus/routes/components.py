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
"""Device components — the things a device holds (GX batteries, Ruuvi tags, …)
and how a device is filed.

Seven endpoints moved VERBATIM out of create_api (P0.8b Lot G) into a
``build(ctx) -> APIRouter`` module: list_components, add_component,
organise_device (PATCH /organise — how a device is filed), update_component,
delete_component, discover_components, discover_components_unsaved. The
component-delete lock helper (_delete_component_locked, used only here) moves
too; the two helpers shared with device CRUD (_new_component, _kind_of_payload)
stay in create_api and are reached via ctx. Bodies are byte-identical;
@_device_write / @_device_runtime / @_serialized_mutation become the ctx form.
The logger keeps the name ``multibus.api``.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set, Tuple   # noqa: F401 — verbatim bodies/helpers

from fastapi import APIRouter, Body, HTTPException, Request

from multibus.catalog.device_kind import device_kind as _device_kind
from multibus.payload import flag

logger = logging.getLogger("multibus.api")


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["components"])

    config = ctx.config
    registry = ctx.registry
    auth_state = ctx.auth_state
    template_registry = ctx.template_registry
    attributes = ctx.attributes
    _cs = ctx.cs
    _mqtt_driver_of = _cs._mqtt_driver_of
    _sync_device_discovery = ctx.sync_device_discovery
    _new_component = ctx.new_component
    _kind_of_payload = ctx.kind_of_payload
    _parent_or_404 = ctx.parent_or_404
    _component_types = ctx.component_types
    _component_view = ctx.component_view
    _errors_detail = ctx.errors_detail
    _start_new_component = ctx.start_new_component
    _set_new_node = ctx.set_new_node
    _component_or_404 = ctx.component_or_404
    _refuse_if_used = ctx.refuse_if_used
    _drop_component_runtime = ctx.drop_component_runtime
    _reload_linked_to = ctx.reload_linked_to
    _device_type_of = ctx.device_type_of
    _publish_attribute_meta = ctx.publish_attribute_meta
    _start_device_client = ctx.start_device_client

    @r.get("/api/devices/{device_id}/components")
    def list_components(device_id: str):
        """The kinds of things this device can hold (its template's) and the
        ones it holds."""
        dev_cfg, _c = _parent_or_404(device_id)
        types = [{'type': k, 'name': str(c.get('name') or k), 'kind': str(c.get('kind', '') or ''),
                  'description': str(c.get('description', '') or ''),
                  'instance_label': str((c.get('instance') or {}).get('label', '') or ''),
                  'instance_hint': str((c.get('instance') or {}).get('hint', '') or ''),
                  'discover': bool(c.get('discover'))}
                 for k, c in _component_types(dev_cfg).items()]
        return {'device': device_id, 'types': types,
                'components': [_component_view(d) for d in config.component_devices(device_id)]}

    @r.post("/api/devices/{device_id}/components")
    @ctx.device_write
    @ctx.serialized_mutation
    def add_component(device_id: str, payload: Dict = Body(...)):
        """Add one component: {type, instance, name?, id?, enabled?}. It reads
        through this device's connection and starts at once."""
        dev_cfg, _c = _parent_or_404(device_id)
        types = _component_types(dev_cfg)
        errors = []
        if dev_cfg.primary or dev_cfg.endpoint_id or dev_cfg.component_of:
            errors.append(f"'{device_id}' cannot hold components (the primary device, an "
                          "installation's unit or a component itself)")
        elif dev_cfg.protocol != 'mqtt':
            errors.append("components are read over MQTT only, for now")
        held = {(d.component_type, d.component_instance) for d in config.component_devices(device_id)}
        entry, cid, errs = _new_component(device_id, types, payload, held, set())
        errors += errs
        if errors:
            raise HTTPException(status_code=422, detail=_errors_detail(errors))
        try:
            new = config.add_component(device_id, dict(entry, id=cid))
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        _start_new_component(new)
        _sync_device_discovery()
        logger.info(f"device {device_id}: component {cid} added ({entry['type']} {entry['instance']})")
        return {"status": "created", "component": _component_view(new)}

    @r.patch("/api/devices/{device_id}/organise")
    @ctx.device_write
    @ctx.serialized_mutation
    def organise_device(device_id: str, payload: Dict = Body(...), dry_run: bool = False):
        """How a device is filed, not how it is read: ``{type}`` (or ``{kind}``,
        its older word) — a built-in kind or a user type (device_types.py), or
        null to derive it again. Never restarts it. `?dry_run=true` checks and
        changes nothing."""
        dev = config.get_device(device_id)
        if dev is None:
            raise HTTPException(status_code=404, detail="device not found")
        _field = 'type' if isinstance(payload, dict) and 'type' in payload else 'kind'
        if not isinstance(payload, dict) or ('kind' not in payload and 'type' not in payload):
            _msg = "required: type (or kind) — null to derive it again"
            raise HTTPException(status_code=422, detail={"errors": [f"type: {_msg}"],
                                                         "fields": {"type": _msg, "kind": _msg}})
        kind, msg = _kind_of_payload(dev, payload)
        if msg:
            raise HTTPException(status_code=422, detail={"errors": [f"{_field}: {msg}"], "fields": {_field: msg}})
        why = ("the primary device's kind comes from its template" if dev.primary
               else f"a unit's kind is its group's role on its installation ({dev.endpoint_id})"
               if dev.endpoint_id else None)
        if why:
            raise HTTPException(status_code=422, detail={"errors": [f"kind: {why}"], "fields": {"kind": why}})
        # judged on what is STORED: a value load could not read (and dropped)
        # is still in the file, and null must be able to clear it
        try:
            stored = config._own_entry(device_id).get('kind')
        except ValueError:
            stored = None
        if stored == (kind or None) or (stored in (None, '') and not kind):
            k, src = _device_kind(dev, template_registry)
            return {"status": "unchanged", "device_id": device_id, "kind": k, "kind_source": src,
                    **dict(zip(("type", "type_label"), _device_type_of(dev)))}
        if dry_run:
            return {"status": "valid", "would": "update"}
        try:
            config.set_device_kind(device_id, kind or None)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)], "fields": {"kind": str(e)}})
        new_cfg = config.get_device(device_id)
        registry.replace(device_id, new_cfg)          # keep the running client
        k, src = _device_kind(new_cfg, template_registry)
        logger.info(f"device {device_id}: kind {'set to ' + kind if kind else 'derived again'} → {k} ({src})")
        try:
            attributes.invalidate()         # its meta says its new kind at once
            _publish_attribute_meta()       # and its type (ADR-0027)
        except Exception:  # noqa: BLE001 — publishing never fails a save
            logger.exception("attributes meta after a type change")
        return {"status": "updated", "device_id": device_id, "kind": k, "kind_source": src,
                **dict(zip(("type", "type_label"), _device_type_of(new_cfg)))}

    @r.patch("/api/devices/{device_id}/components/{component_id}")
    @ctx.device_write
    @ctx.serialized_mutation
    def update_component(device_id: str, component_id: str, payload: Dict = Body(...)):
        """Rename a component or switch it on or off: {name?, enabled?}."""
        dev, client = _component_or_404(device_id, component_id)
        changes: Dict[str, Any] = {}
        if 'name' in payload:
            name = str(payload.get('name') or '').strip()[:80]
            if not name:
                raise HTTPException(status_code=422, detail={"errors": ["name: required"]})
            changes['name'] = name
        if 'enabled' in payload:
            changes['enabled'] = flag(payload, 'enabled')
        try:
            new = config.update_component(device_id, component_id, changes)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        if client:
            client.disconnect()
        registry.replace(component_id, new, client=_start_device_client(new), add_if_missing=True)
        _sync_device_discovery()
        return {"status": "updated", "component": _component_view(new)}

    @r.delete("/api/devices/{device_id}/components/{component_id}")
    @ctx.device_write
    def delete_component(device_id: str, component_id: str, dry_run: bool = False):
        """Remove a component. Its measurements stay on disk: adding the same
        instance again resumes them. Refused (409) while anything uses it
        (asked outside the device lock, then again under it)."""
        _component_or_404(device_id, component_id)
        ans = _refuse_if_used('device', component_id, f"'{component_id}'", dry_run)
        if ans is not None:
            return ans
        return _delete_component_locked(device_id, component_id)

    @ctx.serialized_mutation
    def _delete_component_locked(device_id: str, component_id: str):
        dev, _client = _component_or_404(device_id, component_id)
        _refuse_if_used('device', component_id, f"'{component_id}'", False)
        _drop_component_runtime(dev)
        try:
            config.remove_component(device_id, component_id)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        _reload_linked_to(component_id)         # gone from the config now
        _sync_device_discovery()
        logger.info(f"device {device_id}: component {component_id} removed")
        return {"status": "deleted", "id": component_id}

    @r.post("/api/devices/{device_id}/components/discover")
    @ctx.device_runtime
    def discover_components(device_id: str):
        """What the device says it holds: listens through its connection for a
        few seconds after asking it to republish everything (a read; nothing
        is written). [{type, instance, product, name, added, device_id}]"""
        from multibus.commissioning import component_discover as _cd
        from multibus.transports.mqtt import mqtt_link
        dev_cfg, _c = _parent_or_404(device_id)
        types = _cd.discoverable(_component_types(dev_cfg))
        if not types:
            raise HTTPException(status_code=422, detail={"errors": [
                "the template does not say how to find its components"]})
        link = next((ln for ln in mqtt_link.links() if ln.name == device_id and ln.connected), None)
        if link is None:
            raise HTTPException(status_code=409, detail={"errors": [
                f"'{device_id}' is not connected — finding its components reads through its connection"]})
        tvars = {str(k): str(v) for k, v in ((dev_cfg.mqtt_in or {}).get('vars') or {}).items()}

        def keepalive():
            # the device's own keepalive, as its input keeps it, asked for
            # everything: what was asked at connect has gone by already
            drv = _mqtt_driver_of(_c)
            if drv is not None:
                drv.send_keepalive(first=True)
        found = _cd.listen(link, types, tvars, config.COMPONENT_INSTANCE_RE, 4.0, before=keepalive)
        have = {(d.component_type, d.component_instance): d.id for d in config.component_devices(device_id)}
        out = [dict(f, added=(f['type'], f['instance']) in have, device_id=have.get((f['type'], f['instance']), ''))
               for f in found]
        return {'device': device_id, 'found': out}

    @r.post("/api/devices/components/discover")
    def discover_components_unsaved(request: Request, payload: Dict = Body(...)):
        """What a device not saved yet holds (ADR-0031 §5): {template,
        connection (MQTT), seconds?} — a short-lived connection of its own to
        the broker, subscribe only, for ``seconds`` (1..10, default 4); it
        publishes nothing (a GX that nobody keeps alive says nothing: the
        answer is then empty). [{type, instance, product, name}]. Admin only
        (it makes the gateway connect where the body says); two at a time
        (429 beyond); a broker not reached answers the same 409, after the
        same wait, whatever the port did."""
        from multibus.commissioning import component_discover as _cd
        if auth_state.enabled and getattr(request.state, 'role', None) != 'admin':
            raise HTTPException(status_code=403, detail="finding components before saving is for the admin")
        errors: List[str] = []
        tid = str((payload or {}).get('template') or '').strip()
        tpl = template_registry.get(tid) if tid else None
        if tpl is None:
            errors.append(f"template: '{tid}' not found" if tid else "template: required")
        conn = (payload or {}).get('connection')
        if not isinstance(conn, dict) or str(conn.get('protocol', '')).lower() != 'mqtt':
            errors.append("connection.protocol: components are found over MQTT only, for now")
        elif not str(conn.get('broker', '')).strip():
            errors.append("connection.broker: required")
        else:
            try:
                if not (1 <= int(conn.get('port', 1883)) <= 65535):
                    raise ValueError
            except (TypeError, ValueError):
                errors.append("connection.port: must be 1..65535")
        types = _cd.discoverable(dict(getattr(tpl, 'components', None) or {})) if tpl is not None else {}
        if tpl is not None and not types:
            errors.append("template: it does not say how to find its components")
        if errors:
            raise HTTPException(status_code=422, detail=_errors_detail(errors))
        tvars = {str(k): str(v) for k, v in (conn.get('vars') or {}).items()} if isinstance(conn.get('vars'), dict) else {}
        if not _cd.SLOTS.acquire(blocking=False):
            raise HTTPException(status_code=429, headers={'Retry-After': str(int(_cd.CONNECT_S + _cd.MAX_S))},
                                detail={"errors": [f"busy: {_cd.MAX_AT_ONCE} searches are running — try again "
                                                   "in a few seconds"]})
        try:
            link = _cd.open_link(conn)
            if link is None:
                raise HTTPException(status_code=409, detail={"errors": [
                    f"connection.broker: {conn['broker']}:{conn.get('port', 1883)} could not be reached "
                    f"within {_cd.CONNECT_S:g} s"]})
            try:
                found = _cd.listen(link, types, tvars, config.COMPONENT_INSTANCE_RE,
                                   _cd.seconds_of((payload or {}).get('seconds')))
            finally:
                _cd.close(link)
        finally:
            _cd.SLOTS.release()
        return {'template': tid, 'found': found}

    return r
