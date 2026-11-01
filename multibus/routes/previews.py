# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The read preview's routes (ADR-0031 §2 Check, §5; preview.py).

- ``POST /api/previews`` {connection, template, unit?, device_id?, seconds?}
  → 202 {job, mode, transport, …}: judged at once (409 on a listened bus, on
  a foreign client of our bridge, on a CAN interface that is not
  listen-only, on a bus a preview already reads; 422 as a save judges the
  connection), then read in the background;
- ``GET /api/previews/{job}`` → the answer (state running | done |
  cancelled | failed);
- ``DELETE /api/previews/{job}`` → stops it at the next request or listening
  step;
- ``POST /api/devices/{id}/preview`` {connection?, template?, unit?, seconds?}
  → the same for the device page: the edited connection merged over the
  stored one, before Save. A runtime action (ADR-0030: never 412, the tag
  said).

Operator and up (a live action, like Test); no snapshot; each finished
preview leaves an audit line (``preview``) with what it sent.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import preview

logger = logging.getLogger(__name__)


def env_of(ctx, device_id: str = "") -> preview.Env:
    """What a preview or a bus scan reads of the gateway: the configuration,
    the templates, the bridges' diagnostics, a save's connection check, each
    device's measured share of its bus and its running client."""
    config, registry = ctx.config, ctx.registry

    def _share(dev_id: str) -> Optional[float]:
        for d, c in registry:
            if d.id != dev_id or c is None or not d.enabled:
                continue
            try:
                return (c.get_stats() or {}).get("bus_occupancy")
            except Exception:  # noqa: BLE001
                return None
        return None

    def _validate(conn: Dict[str, Any], tid: str, device_id: str = "") -> None:
        check = getattr(ctx, "validate_connection", None)
        if check is not None:
            # a saved device's preview is judged as a save of that device:
            # its own address is not a clash with itself (L3.9)
            if device_id:
                check({"connection": dict(conn), "template": tid}, device_id)
            else:
                check({"connection": dict(conn), "template": tid})

    def _running(device_id: str):
        _i, _d, c = registry.find(device_id)
        return c

    app = getattr(ctx, "app", None)
    return preview.Env(config=config, templates=ctx.template_registry,
                       diag=getattr(getattr(app, "state", None), "bridge_diag", None),
                       validate=lambda c, t: _validate(c, t, device_id), share_of=_share, running=_running,
                       armed=lambda: _armed(app))


def _armed(app) -> Dict[str, Any]:
    rt = getattr(getattr(app, "state", None), "rules_runtime", None)
    return rt.armed_devices() if rt is not None and hasattr(rt, "armed_devices") else {}


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["previews"])
    config, registry = ctx.config, ctx.registry

    def _env(device_id: str = "") -> preview.Env:
        return env_of(ctx, device_id)

    def _stored(device_id: str):
        """(stored raw connection, the device) — 404 when there is no such device."""
        _i, dev, _c = registry.find(device_id)
        if dev is None:
            raise HTTPException(status_code=404, detail="device not found")
        raw = config.get_raw_device(device_id) or {}
        conn = raw.get("connection")
        if not isinstance(conn, dict) or not conn:
            c = dev.connection
            conn = {k: getattr(c, k) for k in ("host", "port", "unit_id", "serial_port", "baudrate", "parity",
                                                "stopbits", "bytesize", "timeout", "mode")
                    if getattr(c, k, None) not in (None, "")}
            conn["protocol"] = dev.protocol
        return conn, dev

    def _refused(request: Request, payload, device_id: str, e: HTTPException) -> HTTPException:
        """Every refusal leaves a ``preview`` audit line (what was asked, why)."""
        conn = payload.get("connection") if isinstance(payload, dict) else None
        where = ""
        if isinstance(conn, dict):
            where = str(conn.get("serial_port") or conn.get("host") or conn.get("url") or conn.get("broker")
                        or conn.get("channel") or "")
        d = e.detail if isinstance(e.detail, dict) else {"errors": [str(e.detail)]}
        ctx.audit_log.append(user=getattr(request.state, "user", "") or "-",
                             ip=request.client.host if request.client else "-", action="preview",
                             target=where or device_id, status=f"refused ({e.status_code})", detail={
                                 "errors": d.get("errors"), "device": device_id,
                                 "template": (payload or {}).get("template") if isinstance(payload, dict) else None,
                                 "confirm_open": bool(isinstance(payload, dict) and payload.get("confirm_open") is True)})
        return e

    def _plan(request: Request, payload: Dict[str, Any], device_id: str = "") -> preview.Plan:
        try:
            return _plan_of(payload, device_id)
        except HTTPException as e:
            if e.status_code != 404:
                raise _refused(request, payload, device_id, e)
            raise

    def _plan_of(payload: Dict[str, Any], device_id: str = "") -> preview.Plan:
        from ..save_merge import MASKED, BridgeConflict, merge_connection
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail={"errors": ["a JSON object"]})
        conn = payload.get("connection") or {}
        if not isinstance(conn, dict):
            raise HTTPException(status_code=422, detail={"errors": ["connection: an object"]})
        edited = set(conn)                       # what the request itself says (m9)
        tid = str(payload.get("template") or "").strip()
        rows = groups = None
        if device_id:
            stored, dev = _stored(device_id)
            conn = dict(conn)
            if conn.get("password") in ("", MASKED):
                conn.pop("password")
            try:
                conn = merge_connection(conn, stored, config.bridges)
            except BridgeConflict as e:
                raise HTTPException(status_code=422, detail={"errors": [str(e)]})
            tid = tid or dev.template
            if tid == dev.template:
                # the device's own selection, as it reads now
                rows, groups = config.load_device_registers(dev)
                rows = rows or None
        try:
            return preview.prepare(_env(device_id), conn, tid, seconds=payload.get("seconds"),
                                   unit=payload.get("unit"), rows=rows, groups=groups,
                                   device_id=device_id, confirm_open=payload.get("confirm_open") is True,
                                   edited=edited)
        except preview.Refused as e:
            raise HTTPException(status_code=e.status, detail=e.detail, headers=e.headers)

    def _audit(request: Request):
        user = getattr(request.state, "user", "") or "-"
        ip = request.client.host if request.client else "-"

        def done(job: preview.Job) -> None:
            p = job.plan
            b = (job.answer or {}).get("budget") or {}
            ctx.audit_log.append(user=user, ip=ip, action="preview", target=p.where or p.key,
                                 status=job.final or job.state, detail={
                                     "mode": p.mode, "transport": p.transport,
                                     "template": getattr(p.template, "id", ""), "unit": p.unit,
                                     "device": p.device_id, "shared_with": p.shared_with,
                                     "confirm_open": p.confirm_open,
                                     "requests": b.get("requests", 0), "registers": b.get("registers", 0),
                                     **({"error": job.error} if job.error else {})})
        return done

    def _launch(request: Request, payload, plan: preview.Plan, device_id: str = "") -> JSONResponse:
        try:
            job = preview.start(_env(), plan, on_done=_audit(request))
        except preview.Refused as e:
            raise _refused(request, payload, device_id,
                           HTTPException(status_code=e.status, detail=e.detail, headers=e.headers))
        return JSONResponse(status_code=202, content={
            "job": job.id, "state": job.state, **plan.head(),
            "budget": {"max_requests": plan.max_requests, "listen_s": plan.listen_s},
            "warnings": list(plan.warnings)})

    @r.post("/api/previews")
    def start_preview(request: Request, payload: Dict = Body(...)):
        """Start a read preview (preview.py): reads only, through the shared
        transport, within the visible budget. 202 {job, mode, transport,
        where, template, unit, shared_with, shared_line?, confirm_open?,
        seconds, budget, warnings}. 429 (Retry-After) when too many run."""
        did = str((payload or {}).get("device_id") or "").strip() if isinstance(payload, dict) else ""
        return _launch(request, payload, _plan(request, payload, did), did)
    @r.get("/api/previews/{job}")
    def get_preview(job: str):
        """The preview's answer: {state, mode, transport, values[{name, label,
        value, unit, quality, address|path|can_id, group}], groups{total,
        asked, answered, heard}, identity, components, budget{requests,
        registers, ms, max_requests}, warnings, load{share_now, share_added}}."""
        ans = preview.get(job)
        if ans is None:
            raise HTTPException(status_code=404, detail="no such preview (finished ones are kept 10 minutes)")
        return ans

    @r.delete("/api/previews/{job}")
    def cancel_preview(job: str):
        """Stop a preview at its next request or listening step."""
        got = preview.cancel(job)
        if got is None:
            raise HTTPException(status_code=404, detail="no such preview")
        return {"job": job, "cancelled": got}

    @r.post("/api/devices/{device_id}/preview")
    @ctx.device_runtime
    def preview_device(device_id: str, request: Request, payload: Dict = Body(default={})):
        """The device page's preview: the edited connection (``connection``,
        merged over the stored one as a save merges it), the template (the
        stored one when not said: then the device's own selection is read),
        before Save. Nothing is stored. The same rules and answer as
        ``POST /api/previews``."""
        body = payload or {}
        return _launch(request, body, _plan(request, body, device_id), device_id)

    return r
