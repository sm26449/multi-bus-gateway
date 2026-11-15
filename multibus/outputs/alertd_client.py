# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Post MBG alerts to alertd's ``POST /v1/fire`` as a structured ``{type, context,
source}`` body (ADR-0043).

MBG is a first-class alertd **producer for EVENT-class signals** (a thing changed state:
device/sink up/down, auth/schema drops, template alarms, bridge health, gateway events).
**CONDITION-class signals** (sustained latency, buffer backlog, per-register threshold bands)
are **not** fired — MBG already publishes their values to MQTT and alertd's own
variables+rules evaluate them (the YAML pack, lot F). So this client owns three small things:

* the **key → type** catalog (and whether a key is *fired* or left to a *rule*),
* the MBG→alertd **severity map**,
* a single **non-blocking POST with no retry** — alertd's transactional outbox owns retry and
  dedups per ``<notif_id>:<channel>``; ``pending`` and any HTTP 2xx (alertd answers **202**)
  are success. See ADR-0043 D4/D5/D7 and the ``alertd-pending-outbox-verify-2026-11-08`` note.

The SSRF-safe opener (no redirects, so a 3xx can't replay the API key off-site) is shared from
``multibus.core.http_safe``.
"""

import json
import logging
import urllib.error
import urllib.request

from multibus.core.http_safe import NO_REDIRECT_OPENER
from multibus.core.redact import redact_url

logger = logging.getLogger(__name__)

# MBG severity vocabulary → alertd's (info | warning | critical). ADR-0043 D5.
_SEVERITY = {"info": "info", "warn": "warning", "warning": "warning",
             "error": "critical", "danger": "critical", "critical": "critical"}


def alertd_severity(sev) -> str:
    return _SEVERITY.get(str(sev or "").lower(), "warning")


# Extra structured context fields an emitter may carry (lot C fills these); copied
# through when present so alertd templates/rules can key off them.
_CTX_EXTRA = ("device_id", "field", "label", "value", "unit", "band", "code")


def type_for(key):
    """Map an alert ``key`` to ``(type_slug, mode)``. ``mode`` is ``"fire"`` (POST to
    ``/v1/fire``) or ``"rule"`` (a condition MBG leaves to an alertd rule). Ordered —
    first match wins; an unknown key is a fired ``mbg_event``. ADR-0043 D4."""
    k = str(key or "")

    def pre(*ps):
        return any(k.startswith(p) for p in ps)

    if k == "test":                              # the Settings "Test" button
        return ("mbg_test", "fire")
    # ── conditions → alertd rules (NOT fired by MBG) ──────────────────────
    if pre("lat:"):
        return ("mbg_latency", "rule")
    if k == "buffer":
        return ("mbg_influx_buffer", "rule")
    if pre("thr:"):
        return ("mbg_threshold", "rule")
    # ── events → fired ────────────────────────────────────────────────────
    if pre("dropped-"):
        return ("mbg_influx_dropped", "fire")
    if k == "influx-auth":                       # before the generic influx sink prefix
        return ("mbg_influx_auth", "fire")
    if pre("mqtt", "influx"):                    # mqtt:down/up, influx:down/up (sink)
        return ("mbg_sink", "fire")
    if pre("dev:") or k.endswith((":down", ":up")):
        return ("mbg_device", "fire")
    if pre("alarm:"):
        return ("mbg_device_alarm", "fire")
    if pre("bridge:"):
        return ("mbg_bridge", "fire")
    if pre("vmeter:"):                           # not serving / held / consumer lost
        return ("mbg_vmeter", "fire")
    if pre("config"):                            # config, config-downgrade
        return ("mbg_config", "fire")
    if pre("role", "svc:", "rule-fail:"):
        return ("mbg_gateway", "fire")
    return ("mbg_event", "fire")


class AlertdClient:
    """Thin ``/v1/fire`` producer. Off unless ``alerts.alertd.enabled`` + a valid URL."""

    DEFAULT_URL = "http://klopot:5090/v1/fire"

    def __init__(self, cfg=None):
        self.configure(cfg)

    def configure(self, cfg) -> None:
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.url = str(cfg.get("url", "") or "").strip()
        self.api_key = str(cfg.get("api_key", "") or "").strip()
        self.source = str(cfg.get("source", "") or "multi-bus-gateway").strip()
        self.timeout = float(cfg.get("timeout_s", 8) or 8)

    def active(self) -> bool:
        """Ready to fire: enabled and pointed at an http(s) endpoint."""
        return self.enabled and self.url.startswith(("http://", "https://"))

    def should_fire(self, key) -> bool:
        """True for EVENT-class keys; False for CONDITION-class (left to alertd rules)."""
        return type_for(key)[1] == "fire"

    def build_payload(self, alert: dict, test: bool = False) -> dict:
        """The ``{type, context, source[, test]}`` body for an alert dict. Pure."""
        typ, _mode = type_for(alert.get("key"))
        ctx = {k: alert[k] for k in ("source", "message", "key", "host", "ts")
               if alert.get(k) is not None}
        ctx["severity"] = alertd_severity(alert.get("severity"))
        for f in _CTX_EXTRA:
            if alert.get(f) is not None:
                ctx[f] = alert[f]
        body = {"type": typ, "context": ctx, "source": self.source}
        if test:
            body["test"] = True
        return body

    def send(self, alert: dict, test: bool = False) -> dict:
        """POST one alert. Returns ``{ok, status_code, id, status, error}``. Synchronous
        (the caller runs it off the hot path), never raises, never retries. A non-2xx or a
        transport error is a local failure; alertd's ``pending``/2xx is success."""
        payload = self.build_payload(alert, test=test)
        try:
            data = json.dumps(payload).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["X-API-Key"] = self.api_key
            req = urllib.request.Request(self.url, data=data, headers=headers)
            with NO_REDIRECT_OPENER.open(req, timeout=self.timeout) as r:  # noqa: S310
                raw = r.read(4096)
                code = getattr(r, "status", 200)
            resp = {}
            try:
                resp = json.loads(raw.decode("utf-8") or "{}")
            except Exception:  # noqa: BLE001 — a non-JSON 2xx is still a success
                pass
            return {"ok": True, "status_code": code, "id": resp.get("id"),
                    "status": resp.get("status"), "error": None}
        except urllib.error.HTTPError as e:
            logger.warning("Klopot fire rejected (%s): HTTP %s", redact_url(self.url), e.code)
            return {"ok": False, "status_code": e.code, "id": None,
                    "status": None, "error": f"HTTP {e.code}"}
        except Exception as e:  # noqa: BLE001 — delivery is best-effort; never raise upward
            logger.warning("Klopot fire failed (%s): %s", redact_url(self.url), e)
            return {"ok": False, "status_code": None, "id": None,
                    "status": None, "error": str(e)}
