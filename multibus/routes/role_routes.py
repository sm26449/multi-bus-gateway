# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The gateway's role (multibus/role.py) and alerts raised by a companion
service. A gateway is active unless an outside service sets it otherwise;
it never changes its role by itself."""
from __future__ import annotations

import base64
import hmac
import io
import json
import logging
import os
import signal
import threading
import time
import zipfile
from pathlib import Path
from typing import Dict

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

SEVERITIES = ('info', 'warning', 'error', 'critical')
logger = logging.getLogger(__name__)


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["role"])

    def _role():
        return ctx.app.state.role

    def _who(request: Request) -> str:
        return getattr(request.state, 'user', None) or (request.client.host if request.client else '-')

    def _authenticated_only():
        # with no login and no API key anyone on the LAN could make the
        # gateway stop working: changing the role needs credentials
        if not (ctx.auth_state.enabled or ctx.api_key):
            raise HTTPException(status_code=403, detail={"errors": [
                "changing the role needs authentication — enable login (ui.auth) or set an API_KEY"]})

    @r.get("/api/role")
    def get_role():
        """`{role, epoch, since, set_by, reason}` — 200 while active, 503 while
        standby: what a load balancer (HAProxy) asks, so it needs no login."""
        st = _role().status()
        return JSONResponse(st, status_code=200 if st['role'] == 'active' else 503)

    @r.put("/api/role")
    def put_role(request: Request, payload: Dict = Body(...)):
        """`{role: active|standby, epoch, reason}` — from a companion service
        (a service token with the `ha` role) or an admin. The epoch only grows:
        a change needs a higher one, an older one is refused."""
        _authenticated_only()
        role = _role()
        before = role.status()
        try:
            st = role.set(str(payload.get('role', '')), payload.get('epoch'), by=_who(request),
                          reason=str(payload.get('reason') or ''))
        except ValueError as e:
            ctx.audit_log.append(user=_who(request), ip=request.client.host if request.client else '-',
                                 action="role change refused", target=str(payload.get('role')),
                                 status="refused", detail={"error": str(e), "was": before})
            raise HTTPException(status_code=409, detail={"errors": [str(e)], "role": before})
        ctx.audit_log.append(user=_who(request), ip=request.client.host if request.client else '-',
                             action="role set", target=st['role'], status="ok",
                             detail={"epoch": st['epoch'], "was": before['role'], "reason": st['reason']})
        if st['role'] != before['role']:
            ctx.alert_mgr.fire('warning', 'role', 'gateway',
                               f"now {st['role']} (epoch {st['epoch']}, by {st['set_by']}"
                               f"{': ' + st['reason'] if st['reason'] else ''})", transition=True)
        return st

    @r.get("/api/role/bus")
    def bus_state():
        """The buses: those this gateway listens to (when a frame was last
        heard — on a standby, that is its partner talking) and those it polls
        (how many of their devices answer — whether this gateway still has its
        buses)."""
        from ..device_registry import client_is_live
        from ..device_runtime import TAPPABLE
        from ..rtu_tap import bus_activity
        polled: Dict[str, Dict[str, int]] = {}
        if _role().active:
            for dev_cfg, client in ctx.registry.pairs():
                for src in getattr(dev_cfg, 'sources', None) or []:
                    if (src.protocol or '').lower() not in TAPPABLE or not getattr(src, 'enabled', True):
                        continue
                    b = polled.setdefault(src.connection.bus, {'devices': 0, 'answering': 0})
                    b['devices'] += 1
                    b['answering'] += 1 if client is not None and client_is_live(client) else 0
        return {'role': _role().status()['role'], 'listened': bus_activity(),
                'polled': [{'bus': k, **v} for k, v in sorted(polled.items())]}

    # ── the mirror: what an active gateway hands its standby partner ──
    @r.get("/api/state/mirror")
    def get_mirror(since: float = None):
        """The polled devices' values (only those read after `since`, a wall
        time, when given), every rule's state and channel's protection, the
        write leases with their time left and the running sequences."""
        from ..state_mirror import export_values
        app = ctx.app
        rules = getattr(app.state, 'rules_runtime', None)
        seqs = getattr(app.state, 'sequences', None)
        leases = getattr(app.state, 'lease_mgr', None)
        return {'t': time.time(), 'role': _role().status(),
                'values': export_values(ctx.config, ctx.registry, since),
                'rules': rules.export_mirror() if rules is not None else {},
                'leases': leases.export_mirror() if leases is not None else [],
                'sequences': [seqs._public(run) for run in list((seqs.running or {}).values())]
                if seqs is not None else []}

    @r.post("/api/state/mirror")
    def put_mirror(request: Request, payload: Dict = Body(...)):
        """A standby takes its partner's state. Refused on an active gateway:
        it is the one the state comes from."""
        from ..state_mirror import import_values
        _authenticated_only()
        if _role().active:
            raise HTTPException(status_code=409, detail={"errors": [
                "this gateway is active: it gives its state, it does not take one"]})
        app = ctx.app
        got = import_values(ctx.config, ctx.registry, payload.get('values') or {})
        rules = getattr(app.state, 'rules_runtime', None)
        if rules is not None and payload.get('rules'):
            got.update(rules.import_mirror(payload['rules']))
        # what the partner held, for the moment this one takes over
        app.state.peer_mirror = {'at': time.time(), 'from_t': payload.get('t'),
                                 'leases': list(payload.get('leases') or []),
                                 'sequences': list(payload.get('sequences') or [])}
        got.update({'leases': len(app.state.peer_mirror['leases']),
                    'sequences': len(app.state.peer_mirror['sequences'])})
        return {'status': 'ok', 'took': got}

    # ── the configuration: the partner's, but not what is this machine's ──
    def _may_read_secrets(request: Request) -> None:
        """The bundle holds every secret: an admin or the ha service only."""
        if ctx.auth_state.enabled:
            if getattr(request.state, 'role', None) not in ('admin', 'ha'):
                raise HTTPException(status_code=403, detail="the configuration with its secrets: admin or the ha service")
        elif not (ctx.api_key and hmac.compare_digest(request.headers.get('X-API-Key', ''), ctx.api_key)):
            raise HTTPException(status_code=403, detail="the configuration with its secrets needs login or the API key")

    def _mirror_mark() -> Dict:
        p = ctx.config.config_path.parent / 'config_mirror.json'
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return {}

    @r.get("/api/state/config")
    def get_config_bundle(request: Request, hash_only: bool = False):
        """This gateway's configuration bundle (secrets included) and its hash —
        what the hash is, not when it was zipped, so an unchanged
        configuration hashes the same."""
        from ..config_mirror import bundle_hash, with_opcua_identity
        _may_read_secrets(request)
        data = ctx.app.state.snapshot_store.build_bundle_bytes()
        if ctx.config.opcua.public_url:
            # one OPC UA server behind a load balancer: one certificate
            data = with_opcua_identity(data, ctx.config.config_path.parent)
        out = {'hash': bundle_hash(data), 'applied_from_partner': _mirror_mark()}
        if not hash_only:
            out['bundle'] = base64.b64encode(data).decode()
        return out

    @r.post("/api/state/config")
    def put_config_bundle(request: Request, payload: Dict = Body(...)):
        """A standby takes its partner's configuration, keeping the paths that
        are its own — the defaults (its serial adapters, its address) and the
        `keep_local` ones added to them; a snapshot of its own comes first,
        and the gateway restarts to run it. The same configuration twice:
        nothing."""
        from ..config_mirror import (DEFAULT_KEEP_LOCAL, bundle_hash, check_paths, keep_local, replace_config_yaml,
                                     write_opcua_identity)
        from ..device_template import USER_DIR
        from ..snapshots import validate_bundle, write_bundle_files
        import yaml
        _authenticated_only()
        if _role().active:
            raise HTTPException(status_code=409, detail={"errors": [
                "this gateway is active: it gives its configuration, it does not take one"]})
        keep = payload.get('keep_local')
        keep = list(keep) if isinstance(keep, list) else []
        errs = check_paths(keep)
        try:
            data = base64.b64decode(str(payload.get('bundle') or ''), validate=True)
            zf = zipfile.ZipFile(io.BytesIO(data))
        except (ValueError, zipfile.BadZipFile):
            errs.append("bundle: a base64 ZIP from GET /api/state/config")
            zf = None
        if errs:
            raise HTTPException(status_code=422, detail={"errors": errs})
        their_hash = bundle_hash(data)
        if payload.get('hash') and payload['hash'] != their_hash:
            raise HTTPException(status_code=422, detail={"errors": ["bundle: its hash does not match (damaged?)"]})
        if _mirror_mark().get('hash') == their_hash:
            return {'status': 'same', 'hash': their_hash}
        verrs = validate_bundle(zf, allow_nonlan=ctx.config.security.allow_nonlan_http_devices)
        if verrs:
            raise HTTPException(status_code=422, detail={"errors": verrs})
        cfg_dir = ctx.config.config_path.parent
        theirs = yaml.safe_load(zf.read('config.yaml')) or {}
        mine = yaml.safe_load((cfg_dir / 'config.yaml').read_text()) or {}
        merged, kept = keep_local(theirs, mine, list(DEFAULT_KEEP_LOCAL) + [k for k in keep if k not in DEFAULT_KEEP_LOCAL])
        data = replace_config_yaml(data, yaml.safe_dump(merged, sort_keys=False, allow_unicode=True))
        try:
            ctx.app.state.snapshot_store.create("pre-partner-config", user=_who(request),
                                                note=f"before the partner's configuration {their_hash[:12]}")
        except Exception:  # noqa: BLE001 — the copy goes on; the log says it
            logger.exception("pre-partner-config snapshot failed")
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            summary = write_bundle_files(z, cfg_dir=cfg_dir,
                                         user_tpl_dir=Path(getattr(ctx.template_registry, 'user_dir', USER_DIR)),
                                         registers_path_for=ctx.config.device_registers_path, replace_config=True)
            summary['opcua_certificate'] = write_opcua_identity(z, cfg_dir)
        mark = {'hash': their_hash, 'at': time.time(), 'from': str(payload.get('from') or ''), 'kept': kept}
        (cfg_dir / 'config_mirror.json').write_text(json.dumps(mark))
        ctx.audit_log.append(user=_who(request), ip=request.client.host if request.client else '-',
                             action="partner configuration applied", target=their_hash[:12], status="ok",
                             detail={'kept': kept, 'files': summary})
        logger.warning("partner configuration %s applied (kept local: %s) — restarting to run it",
                       their_hash[:12], ', '.join(kept) or 'nothing')
        # a standby: nothing to interrupt; Docker's restart policy brings it back
        threading.Timer(1.5, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        return {'status': 'applied', 'hash': their_hash, 'kept': kept, 'restart': True}

    @r.post("/api/alerts/fire")
    def fire_alert(request: Request, payload: Dict = Body(...)):
        """`{severity, key, source, message}` — a companion service raises an
        alert through the gateway's channels (MQTT, webhook), rate-limited per
        key like the gateway's own."""
        _authenticated_only()
        sev = str(payload.get('severity', ''))
        key, source, message = (str(payload.get(k) or '').strip() for k in ('key', 'source', 'message'))
        errors = []
        if sev not in SEVERITIES:
            errors.append(f"severity: one of {', '.join(SEVERITIES)}")
        if not key or len(key) > 80:
            errors.append("key: 1..80 characters (alerts with one key are rate-limited together)")
        if not source or len(source) > 60:
            errors.append("source: 1..60 characters")
        if not message or len(message) > 500:
            errors.append("message: 1..500 characters")
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})
        ctx.alert_mgr.fire(sev, f"svc:{key}", source, message, transition=bool(payload.get('transition')))
        # said, not assumed: with alerting off in the gateway nothing went out
        return {"status": "ok", "sent": bool(ctx.alert_mgr.enabled)}

    return r
