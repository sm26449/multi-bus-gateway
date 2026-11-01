# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The bus scan's routes (ADR-0031 §2 Address, "Many of the same";
bus_scan.py; FINDINGS L3-F1).

- ``POST /api/bus-scan`` {bus | connection, mode?: poll|listen, from?, to?,
  timeout?, fc43?, seconds?, confirm_open?} → 202 {job, mode, transport,
  where, bus, shared_with, total, …}: judged at once (409 on a listened bus
  for a scan, on a polled bus for a listen, on a foreign client of our
  bridge, while a preview or a scan of that bus runs; 422 on a range, a
  timeout or a connection a save would refuse; 429 past the global cap),
  then run in the background;
- ``GET /api/bus-scan/{job}`` → progress and the units found;
- ``DELETE /api/bus-scan/{job}`` → stops it at the next unit.

``POST /api/bridges/{id}/ports/{port}/scan`` and the RTU/TCP sweep of
``POST /api/discover/modbus/units`` run through :func:`launch` too.
Operator and up (a commissioning tool, like the preview); every finished
scan leaves a ``bus-scan`` audit line.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import bus_scan, preview
from .previews import env_of

logger = logging.getLogger(__name__)


def _http(e: preview.Refused) -> HTTPException:
    return HTTPException(status_code=e.status, detail=e.detail, headers=e.headers)


def launch(ctx, request: Optional[Request], body: Dict[str, Any], conn: Optional[Dict[str, Any]] = None,
           edited=None) -> bus_scan.ScanJob:
    """Judge and start one scan or listen (raises HTTPException). ``conn``:
    a connection the caller resolved (a bridge port); else the body's
    ``bus`` or ``connection``. Every refusal and every finished job leaves
    a ``bus-scan`` audit line."""
    user = (getattr(request.state, "user", "") or "-") if request is not None else "-"
    ip = request.client.host if (request is not None and request.client) else "-"
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail={"errors": ["a JSON object"]})
    env = env_of(ctx)
    mode = str(body.get("mode") or "poll").lower()

    def refused(e: HTTPException) -> HTTPException:
        d = e.detail if isinstance(e.detail, dict) else {"errors": [str(e.detail)]}
        ctx.audit_log.append(user=user, ip=ip, action="bus-scan", target=str(body.get("bus") or (conn or {}).get(
            "serial_port") or (conn or {}).get("host") or ""), status=f"refused ({e.status_code})",
            detail={"errors": d.get("errors"), "mode": mode, "confirm_open": body.get("confirm_open") is True,
                    "confirm_slow": body.get("confirm_slow") is True,
                    **({"rules": d.get("rules")} if d.get("rules") else {})})
        return e

    try:
        if conn is None:
            if body.get("bus"):
                conn = bus_scan.bus_connection(env, str(body["bus"]))
                edited = set()
            else:
                conn = body.get("connection")
                if conn is not None and not isinstance(conn, dict):
                    raise preview.refuse(422, "connection: an object")
                edited = set(conn or {})
        plan = bus_scan.prepare(env, conn or {}, mode=mode, body=body,
                                confirm_open=body.get("confirm_open") is True, edited=edited)
    except preview.Refused as e:
        raise refused(_http(e))
    except HTTPException as e:
        raise refused(e)

    def done(job: bus_scan.ScanJob) -> None:
        ctx.audit_log.append(user=user, ip=ip, action="bus-scan", target=job.plan.where or job.plan.key,
                             status=job.final or job.state, detail=bus_scan.audit_detail(job))
    try:
        return bus_scan.start(env, plan, on_done=done)
    except preview.Refused as e:
        raise refused(_http(e))


def started(job: bus_scan.ScanJob) -> Dict[str, Any]:
    v = job.view()
    return {k: v[k] for k in ("job", "state", "mode", "transport", "where", "bus", "shared_with", "total",
                              "warnings", "slow") if k in v} | \
        {k: v[k] for k in ("shared_line", "confirm_open", "from", "to", "timeout", "fc43", "seconds") if k in v}


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["bus-scan"])

    @r.post("/api/bus-scan")
    def start_scan(request: Request, payload: Dict = Body(...)):
        """Scan one bus for the unit addresses that answer, and what each one
        is (FC3/FC4, FC43/14 when ``fc43``, the SunSpec marker, the templates'
        fingerprints), through the bus's shared transport — or, with
        ``mode: listen``, list the addresses heard on it without sending
        anything. 202 {job, mode, transport, where, bus, shared_with, total}."""
        job = launch(ctx, request, payload)
        return JSONResponse(status_code=202, content=started(job))

    @r.get("/api/bus-scan/{job}")
    def bus_scan_status(job: str):
        """{state: running|cancelling|done|cancelled|failed, done, total,
        found: [{unit_id, answer, fc43, sunspec, matches, suggested,
        suggested_name, device}] (listen: [{unit_id, asked, answered,
        pattern, every_s, device}], heard), budget, warnings, error}."""
        s = bus_scan.get(job)
        if s is None:
            raise HTTPException(status_code=404, detail="no such scan (finished ones are kept 10 minutes)")
        return s

    @r.delete("/api/bus-scan/{job}")
    def bus_scan_cancel(job: str):
        """Stop a scan at its next unit (a listen at its next step)."""
        got = bus_scan.cancel(job)
        if got is None:
            raise HTTPException(status_code=404, detail="no such scan")
        return {"job": job, "cancelled": got}

    return r
