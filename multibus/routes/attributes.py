# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Asset attributes over the API (docs/attributes.md).

Reading is open to every role; coordinates are coarse for anyone who may not
see secrets (a viewer). Writing is configuration: an admin, or a service
token with the admin role (how an EMS writes), under the same middleware as
every other configuration write. A standby gateway answers reads and refuses
writes (its file is replaced by the active one's at the next mirror). A
write may carry ``If-Match: <rev>``; a document changed meanwhile → 412
Precondition Failed, with the current revision. A file that does not parse
answers reads from the last good document and refuses writes (503).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Body, HTTPException, Request

from .. import attribute_catalog as catalog
from ..attributes import Invalid, NotFound
from ..config_store import Broken, Conflict
from ._shared import secrets_visible

logger = logging.getLogger(__name__)

_LEVELS = {'installations': 'installation', 'devices': 'device'}     # the plant has its own routes


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["attributes"])

    def _svc():
        return ctx.app.state.attributes

    def _exact(request: Request) -> bool:
        return secrets_visible(request, ctx.auth_state, ctx.api_key)

    def _who(request: Request) -> str:
        return getattr(request.state, "user", None) or ("api-key" if ctx.api_key else "anon")

    def _ip(request: Request) -> str:
        return request.client.host if request.client else "-"

    def _writable(request: Request) -> None:
        role = getattr(ctx.app.state, "role", None)
        if role is not None and not role.active:
            raise HTTPException(status_code=503, detail={"errors": [
                "this gateway is standby — change the attributes on the active one "
                "(they reach this one through the mirror)"]})

    def _rev(request: Request):
        """If-Match as a revision; '*' (or none) means "whatever is there"."""
        v = (request.headers.get("If-Match") or '').strip()
        if v.startswith('W/'):
            v = v[2:]
        v = v.strip().strip('"')
        return None if v in ('', '*') else v

    def _level(kind: str) -> str:
        lv = _LEVELS.get(kind)
        if lv is None:
            raise HTTPException(status_code=404, detail="unknown level")
        return lv

    def _call(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except NotFound as e:
            raise HTTPException(status_code=404, detail={"errors": [str(e)]})
        except Invalid as e:
            raise HTTPException(status_code=422, detail={"errors": e.errors})
        except Conflict as e:
            raise HTTPException(status_code=412, detail={"errors": [
                "the attributes changed meanwhile — read them again"], "rev": e.current_rev})
        except Broken as e:
            raise HTTPException(status_code=503, detail={"errors": [
                f"{e} — fix or restore attributes.yaml; nothing was written"]})

    @r.get("/api/attributes/catalog")
    def attributes_catalog():
        """What can be said, by category: fields, types, units, limits,
        choices, the levels each category sits on, the kinds it fits."""
        return catalog.catalog_view()

    @r.get("/api/attributes")
    def attributes_all(request: Request):
        """Everything held, the orphans (held for an id that no longer
        exists) and the plant-wide warnings."""
        return _svc().read_all(exact=_exact(request))

    @r.get("/api/attributes/site")
    def attributes_site(request: Request):
        return _call(_svc().get, 'site', '', exact=_exact(request))

    @r.get("/api/attributes/{kind}/{target}")
    def attributes_of(kind: str, target: str, request: Request):
        return _call(_svc().get, _level(kind), target, exact=_exact(request))

    @r.put("/api/attributes/settings")
    def attributes_settings(request: Request, payload: Dict[str, Any] = Body(...)):
        _writable(request)
        rev = _call(_svc().set_publish_mode, str(payload.get('location_publish', '')),
                    who=_who(request), ip=_ip(request), expected_rev=_rev(request))
        return {"status": "saved", "rev": rev}

    @r.put("/api/attributes/site/{category}")
    def put_site(category: str, request: Request, value: Any = Body(...)):
        _writable(request)
        rev, warnings = _call(_svc().put, 'site', '', category, value,
                              who=_who(request), ip=_ip(request), expected_rev=_rev(request))
        return {"status": "saved", "rev": rev, "warnings": warnings}

    @r.delete("/api/attributes/site/{category}")
    def delete_site(category: str, request: Request):
        _writable(request)
        rev = _call(_svc().delete, 'site', '', category,
                    who=_who(request), ip=_ip(request), expected_rev=_rev(request))
        return {"status": "deleted", "rev": rev}

    @r.put("/api/attributes/{kind}/{target}/{category}")
    def put_category(kind: str, target: str, category: str, request: Request, value: Any = Body(...)):
        """Replace one whole category (every row of a repeatable one)."""
        _writable(request)
        rev, warnings = _call(_svc().put, _level(kind), target, category, value,
                              who=_who(request), ip=_ip(request), expected_rev=_rev(request))
        return {"status": "saved", "rev": rev, "warnings": warnings}

    @r.delete("/api/attributes/{kind}/{target}/{category}")
    def delete_category(kind: str, target: str, category: str, request: Request):
        _writable(request)
        rev = _call(_svc().delete, _level(kind), target, category,
                    who=_who(request), ip=_ip(request), expected_rev=_rev(request))
        return {"status": "deleted", "rev": rev}

    @r.delete("/api/attributes/{kind}/{target}")
    def delete_target(kind: str, target: str, request: Request):
        """Everything held for a device or an installation — how an orphan
        is dropped by hand."""
        _writable(request)
        rev = _call(_svc().delete, _level(kind), target, None,
                    who=_who(request), ip=_ip(request), expected_rev=_rev(request))
        return {"status": "deleted", "rev": rev}

    @r.post("/api/attributes/devices/{target}/{category}/copy")
    def copy_category(target: str, category: str, request: Request, payload: Dict[str, Any] = Body(...)):
        """Copy a device's category to other devices: ``{"to": [ids]}``.
        The values are copied, not linked."""
        _writable(request)
        raw_to = payload.get('to')
        to: List[str] = [x for x in raw_to if isinstance(x, str) and x.strip()] if isinstance(raw_to, list) else []
        if not to:
            raise HTTPException(status_code=422, detail={"errors": ["to: the devices to copy to"]})
        rev, warnings = _call(_svc().copy_to, target, category, to, who=_who(request), ip=_ip(request))
        return {"status": "copied", "rev": rev, "warnings": warnings}

    return r
