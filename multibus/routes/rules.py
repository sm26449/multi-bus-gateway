# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Rules — the declarative controller's API (docs/rules-design.md §5.1).
Everything goes through ``app.state.rules_runtime``; mutations are audited
by the runtime with who/via. Sync routes: the runtime holds a lock and may
run a command (seconds) when a rule is released."""
from __future__ import annotations

import re
from typing import Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Request, Response
from ..payload import flag


def payload_mode_armed(_raw: Dict, original) -> bool:
    """Whether the file had the rule armed (it is imported as shadow anyway —
    the preview says so, so nobody is surprised it is not running)."""
    return isinstance(original, dict) and str(original.get("mode", "")) == "armed"


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
                "Writes are disabled — set security.allow_writes=true before arming a rule"]})
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
            # additive: the refusals that name a field, keyed by it (ADR-0006)
            fields = {}
            for e in errs:
                head, sep, msg = str(e).partition(': ')
                if sep and re.fullmatch(r'[A-Za-z_][\w.\[\]]*', head):
                    fields.setdefault(head, msg)
            raise HTTPException(status_code=422, detail={"errors": errs, **({"fields": fields} if fields else {})})

    def _would(rt, raw: Dict, rid: Optional[str]) -> Dict:
        # what a save would do, nothing stored, nothing released (?dry_run=true)
        from ..rules import parse_rule_def, tuning_of
        if rid is None:
            return {"status": "valid", "would": "create"}
        prev = rt.raw.get(rid)
        same = prev is not None and rid not in rt.errors and \
            parse_rule_def(raw).to_dict() == parse_rule_def(prev).to_dict() and tuning_of(raw) == tuning_of(prev)
        return {"status": "valid", "would": "unchanged" if same else "update"}

    def _fresh(rt, rule_id: str) -> Dict:
        # what this request releases, and only that: a note left by another
        # path (MQTT, a reload) never rides on an unrelated answer
        rt.__dict__.setdefault('release_warnings', {}).pop(rule_id, None)
        released = rt.__dict__.setdefault('released', {})
        released.pop(rule_id, None)
        return released

    def _release_warning(rt, rule_id: str) -> Dict:
        # a rule that left armed but could send nothing back to safe says why
        w = rt.__dict__.setdefault('release_warnings', {}).pop(rule_id, None)
        return {"release_warning": w} if w else {}

    @r.get("/api/rule-presets")
    def rule_presets():
        """The rule examples the templates of the configured devices ship,
        made concrete for each device ({device} → its id), always shadow.
        The editor offers them as a starting point; nothing is created here."""
        from ..device_template import rule_preset_for
        out = []
        tr = getattr(ctx, 'template_registry', None)
        for dev_cfg, _c in (ctx.registry or []):
            tpl = tr.get(dev_cfg.template) if (tr and getattr(dev_cfg, 'template', '')) else None
            for p in (getattr(tpl, 'rules', None) or []):
                out.append({'device': dev_cfg.id, 'device_name': dev_cfg.name or dev_cfg.id,
                            'template': tpl.id, 'preset': p.get('id'), 'label': p.get('label') or p.get('id'),
                            'description': p.get('description', ''),
                            'rule': rule_preset_for(p, dev_cfg.id)})
        return {"presets": out}

    @r.get("/api/rule-fields")
    def rule_fields(device: str):
        """The names a formula may read on a device or an installation — the
        very set a rule's inputs are judged against — for the editors'
        `device.field` autocomplete. `fields: null` when they cannot be
        judged yet (nothing running, an MQTT input before its first
        message); units from what it has read."""
        fields_of = getattr(_rt(), '_fields_of', None)
        names = fields_of(device) if fields_of else None
        if names is False:
            raise HTTPException(status_code=404, detail=f"no device or installation {device!r}")
        # units: what the running drivers are told (a device not reading yet
        # has them too), then what it has read
        units = {}
        for dev_cfg, client in (ctx.registry or []):
            if dev_cfg.id != device:
                continue
            parts = getattr(client, 'parts', None)
            for drv in ([d for _s, d in parts] if parts else [client]):
                for reg in list(getattr(drv, 'registers', None) or []):
                    if getattr(reg, 'unit', '') and reg.name not in units:
                        units[reg.name] = reg.unit
        for it in list(((ctx.registry.store_for(device) if ctx.registry else None) or {}).values()):
            if it.get('name') and it.get('unit'):
                units.setdefault(it['name'], it['unit'])
        return {"device": device,
                "fields": None if names is None else
                [{"name": n, "unit": units.get(n, "")} for n in sorted(names)]}

    @r.get("/api/rules")
    def list_rules():
        """Every rule with its live picture: state in words, signal, per-unit
        want / commanded / clamp, the last decision and its reason."""
        return {"rules": _rt().list()}

    # ── moving rules between installations ─────────────────────────────────
    @r.get("/api/rules/export")
    def export_rules(ids: str = ""):
        """The rules as a YAML file (all, or ``?ids=a,b``): their definitions
        only — no live state, no history. ``mode`` is kept for the record, but
        an import always brings rules in as shadow."""
        import yaml
        rt = _rt()
        want = [x.strip() for x in ids.split(",") if x.strip()]
        raws = [dict(rt.raw[rid]) for rid in rt.raw if not want or rid in want]
        missing = [x for x in want if x not in rt.raw]
        if missing:
            raise HTTPException(status_code=404, detail={"errors": [f"no such rule: {', '.join(missing)}"]})
        body = ("# Multi-Bus Gateway rules — import with Rules → Import\n"
                "# Imported rules arrive in SHADOW: they decide and say so, they write\n"
                "# nothing until someone arms them on the installation that runs them.\n"
                + yaml.safe_dump({"rules": raws}, sort_keys=False, allow_unicode=True))
        name = (want[0] if len(want) == 1 else "rules") + ".yaml"
        return Response(content=body, media_type="application/x-yaml",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @r.post("/api/rules/import")
    def import_rules(request: Request, payload: Dict = Body(...)):
        """``{yaml, apply: false|true, replace: false|true}``. Without ``apply``
        nothing is written: every rule comes back with what would happen to it
        (``new`` / ``replace`` / ``exists`` / ``invalid``) and why. With it, the
        valid ones are saved. Imported rules ALWAYS arrive in shadow — arming
        is an explicit act on the installation that runs them — and a rule
        that is armed here is never replaced by an import (shadow it first)."""
        import yaml
        who = _who(request)
        text = str(payload.get("yaml") or "")
        if not text.strip():
            raise HTTPException(status_code=422, detail={"errors": ["yaml is empty"]})
        if len(text) > 1_000_000:
            raise HTTPException(status_code=413, detail={"errors": ["yaml too large (max 1 MB)"]})
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise HTTPException(status_code=422, detail={"errors": [f"invalid YAML: {e}"]})
        items = doc.get("rules") if isinstance(doc, dict) and "rules" in doc else doc
        if isinstance(items, dict):
            items = [items]                      # one rule on its own
        if not isinstance(items, list) or not items:
            raise HTTPException(status_code=422, detail={"errors": [
                "expected a list of rules (a `rules:` list, or one rule)"]})
        apply, replace = bool(payload.get("apply")), bool(payload.get("replace"))
        rt = _rt()
        out, seen = [], set()
        for i, raw in enumerate(items):
            if not isinstance(raw, dict):
                out.append({"id": f"#{i + 1}", "status": "invalid", "errors": ["not a rule (a mapping)"]})
                continue
            raw = {**raw, "mode": "shadow"}      # never arrives armed
            rid = str(raw.get("id") or "")
            entry = {"id": rid or f"#{i + 1}", "label": raw.get("label", ""),
                     "kind": raw.get("kind", ""), "was_armed": payload_mode_armed(raw, items[i])}
            if rid in seen:
                out.append({**entry, "status": "invalid", "errors": [f"id '{rid}' appears twice in the file"]})
                continue
            seen.add(rid)
            exists = rid in rt.rules
            if exists and not replace:
                out.append({**entry, "status": "exists", "errors": [
                    "a rule with this id is already here — tick “replace” to overwrite it"]})
                continue
            if exists and rt.rules[rid].mode == "armed":
                out.append({**entry, "status": "invalid", "errors": [
                    "the rule here is ARMED — put it in shadow before replacing it"]})
                continue
            errs = rt.validate(raw, existing=rid if exists else None)
            if errs:
                out.append({**entry, "status": "invalid", "errors": errs})
                continue
            status = "replace" if exists else "new"
            if apply:
                errs, _d = rt.upsert(raw, who=who, via="import")
                if errs:
                    out.append({**entry, "status": "invalid", "errors": errs})
                    continue
                status = "replaced" if exists else "created"
            # what it reads that is not here: said, not refused — it arrives
            # in shadow and is fixed on this installation before arming
            out.append({**entry, "status": status, "errors": [], "inputs": rt.input_problems(raw)})
        ok = sum(1 for x in out if x["status"] in ("new", "replace", "created", "replaced"))
        return {"applied": apply, "rules": out, "ok": ok, "total": len(out)}

    @r.get("/api/rules/{rule_id}")
    def get_rule(rule_id: str):
        d = _rt().get(rule_id)
        if d is None:
            raise HTTPException(status_code=404, detail="rule not found")
        return d

    @r.post("/api/rules")
    def create_rule(request: Request, payload: Dict = Body(...), dry_run: bool = False):
        who = _gate(request, payload)
        rt = _rt()
        if str(payload.get('id', '')) in rt.rules:
            raise HTTPException(status_code=409, detail={"errors": [f"rule '{payload.get('id')}' already exists"]})
        if dry_run:
            _errs(rt.validate(dict(payload)))
            return _would(rt, dict(payload), None)
        errs, d = rt.upsert(dict(payload), who=who, via='api')
        _errs(errs)
        return {"status": "created", "rule": d}

    @r.put("/api/rules/{rule_id}")
    def update_rule(rule_id: str, request: Request, payload: Dict = Body(...), dry_run: bool = False):
        who = _gate(request, payload)
        rt = _rt()
        if rule_id not in rt.rules:
            raise HTTPException(status_code=404, detail="rule not found")
        # arming, disarming, enabling and disabling are said, never implied: a
        # save that does not name `mode` or `enabled` keeps what the rule has
        # (before, a missing mode meant shadow — a disarm — and a missing
        # enabled meant enabled — which could put an armed rule back to work).
        # A dry run judges that same body, or it would answer for another save.
        body = {**payload, 'id': rule_id}
        stored = rt.rules[rule_id]
        for key in ('mode', 'enabled'):
            if key not in payload:
                body[key] = getattr(stored, key)
        if dry_run:
            _errs(rt.validate(body, existing=rule_id))
            return _would(rt, body, rule_id)
        released = _fresh(rt, rule_id)
        errs, d = rt.upsert(body, who=who, via='api')
        _errs(errs)
        # a save that leaves armed releases what the rule drove
        return {"status": "updated", "rule": d, "releasing": released.pop(rule_id, []),
                **_release_warning(rt, rule_id)}

    @r.delete("/api/rules/{rule_id}")
    def delete_rule(rule_id: str, request: Request):
        who = _gate(request)
        rt = _rt()
        released = _fresh(rt, rule_id)
        if not rt.delete(rule_id, who=who, via='api'):
            raise HTTPException(status_code=404, detail="rule not found")
        return {"status": "deleted", "releasing": released.pop(rule_id, []), **_release_warning(rt, rule_id)}

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
        rt = _rt()
        released = _fresh(rt, rule_id)
        _errs(rt.set_mode(rule_id, str(payload.get('mode', '')), who=who, via='api'))
        # the safe writes run in the background: say which units, the event log says how
        return {"status": "ok", "rule": rt.get(rule_id), "releasing": released.pop(rule_id, []),
                **_release_warning(rt, rule_id)}

    @r.post("/api/rules/{rule_id}/enable")
    def set_enabled(rule_id: str, request: Request, payload: Dict = Body(...)):
        rt = _rt()
        cur = rt.rules.get(rule_id)
        # true or false, said: "false" (a string) or nothing once ENABLED the
        # rule — and an armed one commands its target as soon as it is on
        on = flag(payload, 'enabled')
        who = _gate(request, {'mode': 'armed'} if (cur is not None and cur.mode == 'armed' and on) else None)
        released = _fresh(rt, rule_id)
        _errs(rt.set_enabled(rule_id, on, who=who, via='api'))
        return {"status": "ok", "rule": _rt().get(rule_id), "releasing": released.pop(rule_id, []),
                **_release_warning(rt, rule_id)}

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

    @r.post("/api/rules/{rule_id}/force")
    def force(rule_id: str, request: Request, payload: Dict = Body(...)):
        """`{value: 30, seconds: 3600}` (or `params: {...}`) — the rule asks
        for this for so long, whatever its signal says, then decides again;
        `seconds: 0` ends it. Armed, it writes; in shadow, it says it would."""
        who = _gate(request)
        params = payload.get('params') if isinstance(payload.get('params'), dict) else None
        _errs(_rt().force(rule_id, params, payload.get('seconds'), value=payload.get('value'), who=who, via='api'))
        return {"status": "ok", "rule": _rt().get(rule_id)}

    @r.delete("/api/rules/{rule_id}/force")
    def end_force(rule_id: str, request: Request):
        who = _gate(request)
        _errs(_rt().force(rule_id, None, 0, who=who, via='api'))
        return {"status": "ok", "rule": _rt().get(rule_id)}

    @r.get("/api/channels/{device_id}")
    def channels(device_id: str):
        """What a device's commands control (channels), the rules on each —
        winner first — and the motor's view (on, for how long, starts in the
        last hour). Rules phase 3."""
        rt = _rt()
        cfg = next((c for c, _cl in (ctx.registry or []) if c.id == device_id), None)
        if cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        return {"device": device_id, "channels": rt.channel_view(cfg)}

    @r.get("/api/rules/{rule_id}/decisions")
    def decisions(rule_id: str, limit: int = 100):
        rt = _rt()
        if rule_id not in rt.rules:
            raise HTTPException(status_code=404, detail="rule not found")
        return {"decisions": list(rt.decisions.get(rule_id, []))[:max(1, min(limit, 200))]}

    return r
