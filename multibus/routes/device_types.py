# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The device-type catalog over the API (ADR-0027, device_types.py).

Reading is open to every role: every screen reads this one list (the find
facets, the icons, the type menu, the installation group roles, the template
filter). Changing it is configuration: an admin, or a service token with the
admin role. A type is never deleted while something uses it — a device, a
deleted device that can still be restored, a template that lists it, or an
installation group whose role it is (409, with them named).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Body, HTTPException, Request

from .. import device_types as dtypes

logger = logging.getLogger(__name__)


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["device types"])
    config = ctx.config

    def _template_rows():
        from ..device_kind import template_kind
        return [(template_kind(t, t.id)[0], list(getattr(t, 'types', None) or []))
                for t in ctx.template_registry.list()]

    def _writable() -> None:
        role = getattr(ctx.app.state, "role", None)
        if role is not None and not role.active:
            raise HTTPException(status_code=503, detail={"errors": [
                "this gateway is standby — change the device types on the active one"]})

    def _refuse(fields: Dict[str, str]) -> None:
        raise HTTPException(status_code=422, detail={
            "errors": [f"{k}: {v}" for k, v in fields.items()], "fields": fields})

    def _changed() -> None:
        """A device's meta may say another kind or other words now."""
        att = getattr(ctx.app.state, "attributes", None)
        if att is not None:
            att.invalidate()
        pub = getattr(ctx.app.state, "publish_attribute_meta", None)
        if pub is not None:
            try:
                pub()
            except Exception:  # noqa: BLE001 — publishing never fails a save
                logger.exception("device types: publishing meta failed")

    def _uses(type_id: str) -> Dict[str, List[str]]:
        """What holds a type: devices (a component included), deleted
        devices that can be restored, templates, installation groups."""
        devices = [d.id for d in config.devices if getattr(d, 'kind', '') == type_id]
        # what the file holds, a component's own entry included (an entry the
        # build skipped still names it)
        for raw in getattr(config, '_raw_devices', None) or []:
            if not isinstance(raw, dict):
                continue
            if raw.get('kind') == type_id:
                devices.append(str(raw.get('id')))
            devices += [str(c.get('id')) for c in (raw.get('components') or [])
                        if isinstance(c, dict) and c.get('kind') == type_id]
        for x in config.list_deleted_devices():
            try:
                raw = config.load_deleted_device(x['id']) or {}
            except ValueError:                    # damaged kept settings (L3.11c R1): no type in them
                continue
            comps = [c for c in (raw.get('components') if isinstance(raw.get('components'), list) else [])
                     if isinstance(c, dict)]
            if raw.get('kind') == type_id or any(c.get('kind') == type_id for c in comps):
                devices.append(x['id'])
        templates = sorted(t.id for t in ctx.template_registry.list() if type_id in (getattr(t, 'types', None) or ()))
        groups = []
        for p in config.endpoints:
            pid = str(p.get('id') or '')
            if p.get('role') == type_id:
                groups.append(pid)
            groups += [f"{pid}/{g.get('id')}" for g in (p.get('groups') or [])
                       if isinstance(g, dict) and g.get('role') == type_id]
        return {'devices': sorted(set(devices)), 'templates': templates, 'groups': groups}

    def _view(type_id: str) -> Dict[str, Any]:
        return next(t for t in dtypes.catalog_view(_template_rows())['types'] if t['id'] == type_id)

    @r.get("/api/device-types")
    def list_device_types():
        """Built-ins and user types: labels by language, the icon, the
        built-in a user type behaves like, how many templates suit it, the
        attributes worth asking for it."""
        return dtypes.catalog_view(_template_rows())

    @r.post("/api/device-types")
    def create_device_type(payload: Dict[str, Any] = Body(...)):
        """``{id, labels: {en, ro…}, icon?, behaves_like?}`` — admin."""
        _writable()
        with ctx.mutation_lock:
            held = {t['id']: t for t in dtypes.user_types()}
            if len(held) >= dtypes.MAX_TYPES:
                _refuse({'id': f"at most {dtypes.MAX_TYPES} user types"})
            clean, errs = dtypes.validate(payload, taken=held)
            if errs:
                _refuse(errs)
            # an id an installation already uses as a role (written by hand,
            # e.g. `grid`) would change the published kind of its units
            where = dtypes.roles_in_use(config.endpoints).get(clean['id'].lower())
            if where:
                _refuse({'id': f"{clean['id']!r} is already a role in {', '.join(where)} — "
                               "pick another id, or change that role first"})
            config.save_device_types(config.device_types + [clean])
        logger.info("device type %s added (behaves like %s)", clean['id'], clean['behaves_like'])
        _changed()
        return {"status": "created", "type": _view(clean['id'])}

    @r.put("/api/device-types/{type_id}")
    def update_device_type(type_id: str, payload: Dict[str, Any] = Body(...)):
        """New labels, icon or behaves_like for a user type (its id stays).
        A built-in is not changed here."""
        _writable()
        if type_id in dtypes.KINDS:
            _refuse({'id': f"{type_id!r} is a built-in type — it is not changed"})
        with ctx.mutation_lock:
            raw = config.device_types
            idx = next((i for i, t in enumerate(raw) if t.get('id') == type_id), None)
            if idx is None:
                raise HTTPException(status_code=404, detail="no such device type")
            body = {**{k: v for k, v in raw[idx].items() if k != 'id'}, **(payload or {})}
            clean, errs = dtypes.validate(body, existing_id=type_id)
            if errs:
                _refuse(errs)
            if clean['behaves_like'] == 'meter':
                # a device that IS its meters is never a meter itself
                held = sorted({d.id for d in config.devices
                               if getattr(d, 'kind', '') == type_id and getattr(d, 'protocol', '') == 'none'})
                if held:
                    raise HTTPException(status_code=409, detail={
                        "errors": [f"behaves_like: {', '.join(held)} measured only by their meters hold this type, "
                                   "and such a device is never a meter — give them another type first"],
                        "devices": held})
            raw[idx] = clean
            # a device of this type follows at once: its wire kind is derived
            # when it is read, from the catalog held now
            config.save_device_types(raw)
        logger.info("device type %s changed", type_id)
        _changed()
        return {"status": "updated", "type": _view(type_id)}

    @r.delete("/api/device-types/{type_id}")
    def delete_device_type(type_id: str, request: Request):
        """A user type nothing uses; else 409 naming what does."""
        _writable()
        if type_id in dtypes.KINDS:
            _refuse({'id': f"{type_id!r} is a built-in type — it cannot be deleted"})
        with ctx.mutation_lock:
            raw = config.device_types
            if not any(t.get('id') == type_id for t in raw):
                raise HTTPException(status_code=404, detail="no such device type")
            uses = _uses(type_id)
            if any(uses.values()):
                said = '; '.join(f"{k}: {', '.join(v)}" for k, v in uses.items() if v)
                raise HTTPException(status_code=409, detail={
                    "errors": [f"{type_id} is in use — {said}. Change their type first."], **uses})
            config.save_device_types([t for t in raw if t.get('id') != type_id])
        logger.info("device type %s deleted", type_id)
        return {"status": "deleted", "id": type_id}

    return r
