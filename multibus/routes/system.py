"""Event log + alerting — recent events, alert status/test-fire, alert config.

Moved verbatim from create_api().
"""
from __future__ import annotations

import time
from typing import Dict

from fastapi import APIRouter, Body, HTTPException, Request

_TEST_FIRE_COOLDOWN_S = 10.0
_HDR_MASK = "••••••"      # ●●●●●● — masked header value


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["system"])
    config, event_log, alert_mgr = ctx.config, ctx.event_log, ctx.alert_mgr
    auth_state, api_key = ctx.auth_state, ctx.api_key
    _test_fire_gate = {"ts": 0.0}

    @r.get("/api/audit")
    async def get_audit(request: Request, limit: int = 100, q: str = "", user: str = ""):
        """The audit trail (who changed what, when). With auth on this is
        ADMIN-only — it contains usernames, IPs and redacted change payloads,
        none of a viewer's business."""
        if auth_state.enabled and getattr(request.state, "role", None) != "admin":
            raise HTTPException(status_code=403,
                                detail="the audit trail requires the admin role")
        return {"entries": ctx.audit_log.recent(min(max(1, limit), 500),
                                                q=q[:100], user=user[:40])}

    @r.get("/api/audit/export.csv")
    async def export_audit_csv(request: Request):
        """Full audit as CSV (same admin gate as the JSON view)."""
        if auth_state.enabled and getattr(request.state, "role", None) != "admin":
            raise HTTPException(status_code=403,
                                detail="the audit trail requires the admin role")
        import csv
        import io as _io
        from datetime import datetime as _dt
        buf = _io.StringIO()
        w = csv.writer(buf)
        w.writerow(["time", "user", "ip", "action", "target", "status", "detail"])
        for e in reversed(ctx.audit_log.recent(10000)):
            w.writerow([_dt.fromtimestamp(e.get("ts", 0)).isoformat(sep=" ", timespec="seconds"),
                        e.get("user", ""), e.get("ip", ""), e.get("action", ""),
                        e.get("target", ""), e.get("status", ""), e.get("detail", "")])
        from fastapi.responses import Response as _Resp
        return _Resp(content=buf.getvalue(), media_type="text/csv",
                     headers={"Content-Disposition": 'attachment; filename="audit.csv"'})

    @r.get("/api/events")
    async def get_events(limit: int = 100):
        """Recent events (newest first), persisted across restarts."""
        return {"events": event_log.recent(min(max(1, limit), 300))}

    @r.get("/api/alerts")
    async def get_alerts(limit: int = 50):
        """Alerting config/status + recently fired alerts."""
        return {"status": alert_mgr.status(),
                "alerts": alert_mgr.recent(min(max(1, limit), 100))}

    @r.post("/api/alerts/test")
    async def test_alert(payload: Dict = Body(default={})):
        """Fire a synthetic alert over the configured channels (MQTT + webhook)
        to verify delivery. Bypasses the enabled/rate-limit gates so you can test
        the wiring before going live; webhook delivery is synchronous so the
        result (sent / failed) is reported back.

        A test-fire drives real outbound traffic (webhook → SMS/MQTT), so it is
        credentialed exactly like a Modbus write and throttled with a short
        cooldown — it is not an open, spammable endpoint on a trusted LAN."""
        if not (auth_state.enabled or api_key):
            raise HTTPException(status_code=403, detail={"errors": [
                "test-fire requires authentication — enable login (ui.auth) or set an API_KEY"]})
        now = time.monotonic()
        wait = _TEST_FIRE_COOLDOWN_S - (now - _test_fire_gate["ts"])
        if wait > 0:
            raise HTTPException(status_code=429, detail={"errors": [
                f"test-fire is throttled — retry in {wait:.0f}s"]})
        _test_fire_gate["ts"] = now
        msg = str(payload.get("message") or "Test alert from the Modbus gateway")
        return alert_mgr.test(msg)

    @r.get("/api/config/alerts")
    async def get_alerts_config():
        """Alert/webhook config for the Settings panel. Header VALUES are masked
        (they may hold an API key); the URL/body are returned as-is."""
        a = config.alerts or {}
        return {
            "enabled": bool(a.get("enabled", False)),
            "mqtt": bool(a.get("mqtt", True)),
            "webhook_url": a.get("webhook_url", "") or "",
            "webhook_headers": {k: _HDR_MASK for k in (a.get("webhook_headers") or {})},
            "webhook_body": a.get("webhook_body", None),
            "min_interval_s": a.get("min_interval_s", 300),
            "latency_ms": a.get("latency_ms", 1000),
            "buffer_points": a.get("buffer_points", 1000),
            "signals": a.get("signals") or {"device": True, "sink": True,
                                            "latency": True, "buffer": True},
        }

    @r.post("/api/config/alerts")
    async def update_alerts_config(payload: Dict = Body(...)):
        """Persist alert/webhook config and apply it live (no restart). Masked
        header values are preserved; a blank webhook_url disables that channel."""
        a = dict(config.alerts or {})
        errors = []
        url = str(payload.get("webhook_url", a.get("webhook_url", "")) or "").strip()
        if url and not (url.startswith("http://") or url.startswith("https://")):
            errors.append("webhook_url must start with http:// or https://")
        body = payload.get("webhook_body", a.get("webhook_body"))
        if body not in (None, "") and not isinstance(body, dict):
            errors.append("webhook_body must be a JSON object or empty")
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})
        a["enabled"] = bool(payload.get("enabled", a.get("enabled", False)))
        a["mqtt"] = bool(payload.get("mqtt", a.get("mqtt", True)))
        a["webhook_url"] = url
        a["webhook_body"] = (body or None)
        if "min_interval_s" in payload:
            a["min_interval_s"] = float(payload["min_interval_s"] or 300)
        if "latency_ms" in payload:
            a["latency_ms"] = float(payload["latency_ms"] or 1000)
        if "buffer_points" in payload:
            a["buffer_points"] = int(payload["buffer_points"] or 1000)
        if isinstance(payload.get("signals"), dict):
            a["signals"] = {k: bool(v) for k, v in payload["signals"].items()}
        if "webhook_headers" in payload:
            new = payload["webhook_headers"] or {}
            old = (config.alerts or {}).get("webhook_headers") or {}
            a["webhook_headers"] = {k: (old.get(k, v) if v == _HDR_MASK else v)
                                    for k, v in (new.items() if isinstance(new, dict) else [])}
        config.alerts = a
        config.save_yaml_config()
        alert_mgr.configure(a)                     # apply live — no restart
        return {"status": "ok", "channels": alert_mgr.status()["channels"]}

    return r
