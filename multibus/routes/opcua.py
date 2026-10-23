# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The OPC UA server's settings and state (docs/opcua.md)."""
from __future__ import annotations

import re
from typing import Dict

from fastapi import APIRouter, Body, HTTPException, Request, Response
from ..payload import flag

PUBLIC_URL = re.compile(r'^opc\.tcp://[A-Za-z0-9.\-]+(:\d{1,5})?(/[^\s]*)?$')


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["opcua"])

    def _ctl():
        c = getattr(ctx.app.state, 'opcua', None)
        if c is None:
            raise HTTPException(status_code=503, detail="OPC UA is not initialized")
        return c

    @r.get("/api/opcua")
    def opcua_status():
        """Whether it runs, its endpoint, how many devices and values it
        serves, and its settings."""
        return _ctl().status()

    @r.put("/api/opcua")
    def opcua_settings(request: Request, payload: Dict = Body(...)):
        """`{enabled, port, host, encryption: required|optional|off,
        allow_anonymous, update_s, public_url}` — saved, then the server restarts with
        them (OPC UA clients reconnect)."""
        from ..config import OPCUA_ENCRYPTION
        o = ctx.config.opcua
        errs = []
        port = payload.get('port', o.port)
        if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
            errs.append("port: 1..65535")
        enc = payload.get('encryption', o.encryption)
        if enc not in OPCUA_ENCRYPTION:
            errs.append(f"encryption: one of {', '.join(OPCUA_ENCRYPTION)}")
        upd = payload.get('update_s', o.update_s)
        if isinstance(upd, bool) or not isinstance(upd, (int, float)) or not (0.2 <= upd <= 60):
            errs.append("update_s: 0.2..60 seconds")
        host = str(payload.get('host', o.host) or '0.0.0.0')
        public_url = str(payload.get('public_url', o.public_url) or '').strip()
        if public_url and not PUBLIC_URL.match(public_url):
            errs.append("public_url: opc.tcp://<host>:<port>/<path> — the load balancer's address, or empty")
        if errs:
            raise HTTPException(status_code=422, detail={"errors": errs})
        o.enabled = flag(payload, 'enabled', o.enabled)
        o.port, o.encryption, o.update_s, o.host = int(port), enc, float(upd), host
        o.public_url = public_url
        o.allow_anonymous = flag(payload, 'allow_anonymous', o.allow_anonymous)
        ctx.config.save_yaml_config()
        try:
            ctx.app.state.audit_log.append(user=getattr(request.state, 'user', None) or 'anon', ip='-',
                                           action='opcua settings', target='opcua', status='ok',
                                           detail={'enabled': o.enabled, 'port': o.port, 'encryption': o.encryption,
                                                   'allow_anonymous': o.allow_anonymous, 'public_url': o.public_url})
        except Exception:  # noqa: BLE001
            pass
        _ctl().apply()
        return _ctl().status()

    @r.get("/api/opcua/certificate")
    def opcua_certificate():
        """The server's certificate (DER) — to trust it in a client that
        checks servers (UaExpert, Ignition, a Weintek panel)."""
        p = ctx.config.config_path.parent / 'opcua' / 'server_cert.der'
        if not p.exists():
            raise HTTPException(status_code=404, detail="no certificate yet — enable the server with encryption")
        return Response(p.read_bytes(), media_type='application/pkix-cert',
                        headers={'Content-Disposition': 'attachment; filename="mbg-opcua.der"'})

    return r
