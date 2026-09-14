# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Rules — the declarative controller's API (docs/rules-design.md §5.1).
Everything goes through ``app.state.rules_runtime``; mutations are audited
by the runtime with who/via. Sync routes: the runtime holds a lock and may
run a command (seconds) when a rule is released."""
from __future__ import annotations

from typing import Dict

from fastapi import APIRouter, Body, HTTPException, Request


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["rules"])

    def _rt():
        rt = getattr(ctx.app.state, "rules_runtime", None)
        if rt is None:
            raise HTTPException(status_code=503, detail="rules are not initialized")
        return rt

    def _who(request: Request) -> str:
        return getattr(request.state, "user", None) or ("api-key" if ctx.api_key else "anon")

    def _arm_gate(request: Request) -> str:
        """Arming is the act that lets a rule write: it needs the master switch
        and an authenticated caller. Shadow rules are configuration."""
        if not ctx.config.security.allow_writes:
            raise HTTPException(status_code=403, detail={"errors": [
                "Modbus writes are disabled — set security.allow_writes=true before arming a rule"]})
        if not (ctx.auth_state.enabled or ctx.api_key):
            raise HTTPException(status_code=403, detail={"errors": [
                "arming requires authentication — enable login (ui.auth) or set an API_KEY"]})
        return _who(request)

    def _gate(request: Request, payload: Dict = None) -> str:
        if payload is not None and payload.get('mode') == 'armed':
            return _arm_gate(request)
        return _who(request)

    def _errs(errs):
        if errs:
            raise HTTPException(status_code=422, detail={"errors": errs})

    @r.get("/api/rules")
    def list_rules():
        """Every rule with its live picture: state in words, signal, per-unit
        want / commanded / clamp, the last decision and its reason."""
        return {"rules": _rt().list()}

    @r.get("/api/rules/{rule_id}")
    def get_rule(rule_id: str):
        d = _rt().get(rule_id)
        if d is None:
            raise HTTPException(status_code=404, detail="rule not found")
        return d

    @r.post("/api/rules")
    def create_rule(request: Request, payload: Dict = Body(...)):
        who = _gate(request, payload)
        rt = _rt()
        if str(payload.get('id', '')) in rt.rules:
            raise HTTPException(status_code=409, detail={"errors": [f"rule '{payload.get('id')}' already exists"]})
        errs, d = rt.upsert(dict(payload), who=who, via='api')
        _errs(errs)
        return {"status": "created", "rule": d}

    @r.put("/api/rules/{rule_id}")
    def update_rule(rule_id: str, request: Request, payload: Dict = Body(...)):
        who = _gate(request, payload)
        rt = _rt()
        if rule_id not in rt.rules:
            raise HTTPException(status_code=404, detail="rule not found")
        errs, d = rt.upsert({**payload, 'id': rule_id}, who=who, via='api')
        _errs(errs)
        return {"status": "updated", "rule": d}

    @r.delete("/api/rules/{rule_id}")
    def delete_rule(rule_id: str, request: Request):
        who = _gate(request)
        if not _rt().delete(rule_id, who=who, via='api'):
            raise HTTPException(status_code=404, detail="rule not found")
        return {"status": "deleted"}

    @r.post("/api/rules/validate")
    def validate_rule(payload: Dict = Body(...)):
        """The rule as a body → what it would see and want right now, against
        the live store, with no state kept. The editor's live preview."""
        return _rt().evaluate_now(dict(payload))

    @r.post("/api/rules/{rule_id}/mode")
    def set_mode(rule_id: str, request: Request, payload: Dict = Body(...)):
        """`{mode: shadow|armed}` — arming is an explicit, audited act; leaving
        armed releases the target to its safe values (on_disable)."""
        who = _gate(request, payload)
        _errs(_rt().set_mode(rule_id, str(payload.get('mode', '')), who=who, via='api'))
        return {"status": "ok", "rule": _rt().get(rule_id)}

    @r.post("/api/rules/{rule_id}/enable")
    def set_enabled(rule_id: str, request: Request, payload: Dict = Body(...)):
        rt = _rt()
        cur = rt.rules.get(rule_id)
        who = _gate(request, {'mode': 'armed'} if (cur is not None and cur.mode == 'armed' and payload.get('enabled', True)) else None)
        _errs(rt.set_enabled(rule_id, bool(payload.get('enabled', True)), who=who, via='api'))
        return {"status": "ok", "rule": _rt().get(rule_id)}

    @r.post("/api/rules/{rule_id}/clamp")
    def set_clamp(rule_id: str, request: Request, payload: Dict = Body(...)):
        """`{max: 60, expires_s: 7200}` — a ceiling on the rule's numeric want.
        It never expires into a step: it holds until the signal is under the
        release threshold."""
        who = _gate(request)
        try:
            mx = float(payload['max'])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=422, detail={"errors": ["max: a number"]})
        _errs(_rt().set_clamp(rule_id, mx, payload.get('expires_s'), who=who, via='api'))
        return {"status": "ok", "rule": _rt().get(rule_id)}

    @r.delete("/api/rules/{rule_id}/clamp")
    def clear_clamp(rule_id: str, request: Request):
        who = _gate(request)
        _errs(_rt().clear_clamp(rule_id, who=who, via='api'))
        return {"status": "ok", "rule": _rt().get(rule_id)}

    @r.post("/api/rules/{rule_id}/override")
    def override(rule_id: str, request: Request, payload: Dict = Body(...)):
        """`{seconds: 900}` — pause the rule so another face may command its
        target; 0 lifts the pause."""
        who = _gate(request)
        try:
            secs = float(payload.get('seconds', 0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail={"errors": ["seconds: a number"]})
        _errs(_rt().override(rule_id, secs, who=who, via='api'))
        return {"status": "ok", "rule": _rt().get(rule_id)}

    @r.get("/api/rules/{rule_id}/decisions")
    def decisions(rule_id: str, limit: int = 100):
        rt = _rt()
        if rule_id not in rt.rules:
            raise HTTPException(status_code=404, detail="rule not found")
        return {"decisions": list(rt.decisions.get(rule_id, []))[:max(1, min(limit, 200))]}

    return r
