# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Sequences — commands in order with waits and checks (rules phase 3 §4).
Defining one is configuration (admin); running or cancelling one is a live
action (operator), each command meeting every gate of the write path."""
from __future__ import annotations

import json
import logging
from typing import Dict

import yaml
from fastapi import APIRouter, Body, HTTPException, Request

from ..sequences import parse_sequence, validate_sequence

logger = logging.getLogger(__name__)


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["sequences"])

    def _store():
        return ctx.app.state.sequence_store

    def _runner():
        return ctx.app.state.sequences

    def _who(request: Request) -> str:
        return getattr(request.state, "user", None) or ("api-key" if ctx.api_key else "anon")

    def _offered(device_id: str):
        cmds_for = getattr(ctx.app.state, 'commands_for', None)
        cfg = next((c for c, _cl in (ctx.registry or []) if c.id == device_id), None)
        if cfg is None:
            return None
        if cmds_for is None:
            return []
        return [c['def'].name for c in cmds_for(cfg) if c['enabled']]

    def _body(payload: Dict) -> Dict:
        """A sequence as JSON, or as YAML text in `{yaml: "..."}` (the editor)."""
        if isinstance(payload, dict) and isinstance(payload.get('yaml'), str):
            try:
                data = yaml.safe_load(payload['yaml'])
            except yaml.YAMLError as e:
                raise HTTPException(status_code=422, detail={"errors": [f"YAML: {e}"]})
            if isinstance(data, dict) and isinstance(data.get('sequences'), list) and len(data['sequences']) == 1:
                data = data['sequences'][0]
            if not isinstance(data, dict):
                raise HTTPException(status_code=422, detail={"errors": ["YAML: one sequence {id, steps}"]})
            return data
        return payload

    def _validate(raw: Dict, existing: str = ''):
        from ..rules_runtime import _validate_expr
        errs = validate_sequence(raw, devices=_offered, validate_expr=_validate_expr)
        items = _store().load()
        if not errs and not existing and any(str(x.get('id')) == raw.get('id') for x in items):
            errs.append(f"id: '{raw.get('id')}' already exists")
        if errs:
            raise HTTPException(status_code=422, detail={"errors": errs})
        return items

    def _get(sid: str) -> Dict:
        raw = next((x for x in _store().load() if str(x.get('id')) == sid), None)
        if raw is None:
            raise HTTPException(status_code=404, detail="sequence not found")
        return raw

    def _one(raw: Dict) -> Dict:
        return {**raw, 'devices': parse_sequence(raw).devices(), **_runner().view(str(raw.get('id')))}

    def _audit(request, action, target, detail):
        try:
            ctx.app.state.audit_log.append(user=_who(request), ip='-', action=action, target=target,
                                           status='ok', detail=detail)
        except Exception:  # noqa: BLE001
            pass

    @r.get("/api/sequences")
    def list_sequences():
        return {"sequences": [_one(x) for x in _store().load()]}

    @r.get("/api/sequences/{sid}")
    def get_sequence(sid: str):
        return _one(_get(sid))

    @r.get("/api/sequences/{sid}/yaml")
    def get_sequence_yaml(sid: str):
        return {"yaml": yaml.safe_dump(_get(sid), sort_keys=False, allow_unicode=True)}

    @r.post("/api/sequences")
    def create_sequence(request: Request, payload: Dict = Body(...)):
        payload = _body(payload)
        items = _validate(payload)
        _store().save(items + [payload])
        _sync_mqtt()
        _audit(request, 'sequence created', payload.get('id'), {})
        return {"status": "created", "sequence": _one(payload)}

    @r.put("/api/sequences/{sid}")
    def update_sequence(sid: str, request: Request, payload: Dict = Body(...)):
        _get(sid)
        payload = _body(payload)
        if payload.get('id', sid) != sid:
            raise HTTPException(status_code=422, detail={"errors": ["id: cannot be changed"]})
        payload = {**payload, 'id': sid}
        items = _validate(payload, existing=sid)
        _store().save([payload if str(x.get('id')) == sid else x for x in items])
        _audit(request, 'sequence updated', sid, {})
        return {"status": "updated", "sequence": _one(payload)}

    @r.delete("/api/sequences/{sid}")
    def delete_sequence(sid: str, request: Request):
        _get(sid)
        if _runner().view(sid)['running']:
            raise HTTPException(status_code=409, detail={"errors": [f"'{sid}' is running — cancel it first"]})
        _store().save([x for x in _store().load() if str(x.get('id')) != sid])
        _sync_mqtt()
        _audit(request, 'sequence deleted', sid, {})
        return {"status": "deleted"}

    @r.post("/api/sequences/{sid}/run")
    def run_sequence(sid: str, request: Request, payload: Dict = Body(default={})):
        """`{params: {...}, dry_run: false}` — 202 with the run started (it
        goes on in the background; GET the sequence to follow it), 200 with
        the whole plan for a dry run."""
        seq = parse_sequence(_get(sid))
        code, res = _runner().start(seq, dict((payload or {}).get('params') or {}), who=_who(request),
                                    via='api', dry_run=bool((payload or {}).get('dry_run')))
        if code >= 400:
            raise HTTPException(status_code=code, detail={"errors": [res.get('reason')]})
        return res

    @r.post("/api/sequences/{sid}/cancel")
    def cancel_sequence(sid: str, request: Request):
        _get(sid)
        if not _runner().cancel(sid, who=_who(request)):
            raise HTTPException(status_code=409, detail={"errors": [f"'{sid}' is not running"]})
        return {"status": "cancelling"}

    # MQTT: mbg/sequences/<id>/run {params} — with the broker gate of every
    # command face (refused on an anonymous broker session)
    def _mqtt_run(sid: str, payload: str) -> None:
        refused = getattr(ctx.app.state, 'mqtt_writes_refused', lambda: None)()
        if refused:
            logger.warning("sequence %s: MQTT run ignored: %s", sid, refused)
            return
        try:
            body = json.loads(payload) if str(payload).strip() else {}
        except ValueError:
            body = {}
        raw = next((x for x in _store().load() if str(x.get('id')) == sid), None)
        if raw is None or not isinstance(body, dict):
            return
        if body.get('cancel'):
            _runner().cancel(sid, who=str(body.get('source') or 'mqtt'))
            return
        code, res = _runner().start(parse_sequence(raw), dict(body.get('params') or {}),
                                    who=str(body.get('source') or 'mqtt'), via='mqtt')
        if code >= 400:
            logger.warning("sequence %s over MQTT: %s", sid, res.get('reason'))

    def _sync_mqtt() -> None:
        m = getattr(ctx, 'mqtt_publisher', None) or getattr(ctx.app.state, 'mqtt_publisher', None)
        if not m or not hasattr(m, 'register_command'):
            return
        m.unregister_commands('mbg/sequences/')
        for x in _store().load():
            sid = str(x.get('id'))
            m.register_command(f"mbg/sequences/{sid}/run", lambda payload, s=sid: _mqtt_run(s, payload))

    try:
        _sync_mqtt()
    except Exception as e:  # noqa: BLE001
        logger.warning("sequences MQTT: %s", e)
    return r
