"""Alerting hooks off the Status signals.

An ``AlertManager`` turns notable conditions (device down/recovered, sink
disconnect/reconnect, sustained high read latency, InfluxDB buffer backlog) into
alerts and delivers them over the configured channels:

* **MQTT** — publish a JSON alert to ``<topic_prefix>/alert`` on the same broker,
  so alertd / Home Assistant / Node-RED can subscribe and route to SMS/Telegram.
* **Webhook** — POST the JSON alert to a configurable URL (SMS gateway, ntfy,
  Telegram bridge, alertd HTTP ingest).

Off unless ``alerts.enabled`` is set. Every alert is rate-limited per key so a
flapping signal can't spam, and mirrored into the persisted event log.
"""

import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import List, Optional

from .redact import redact_url

logger = logging.getLogger(__name__)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse HTTP redirects on the webhook POST.

    The POST carries the operator's webhook credentials (e.g. X-API-Key); a 3xx
    to an attacker-controlled host would replay them off-site (a classic SSRF
    credential leak). A notifier endpoint has no legitimate reason to redirect,
    so we hard-fail instead of following."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, f"redirect blocked → {newurl}", headers, fp)


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect())


class AlertManager:
    def __init__(self, cfg: Optional[dict] = None, mqtt_publisher=None, event_log=None):
        self.mqtt = mqtt_publisher
        self.event_log = event_log
        self._last_fire = {}                 # key -> ts (rate-limit)
        self._recent: deque = deque(maxlen=100)
        self._lock = threading.Lock()
        self.configure(cfg)

    def configure(self, cfg: Optional[dict]) -> None:
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.use_mqtt = bool(cfg.get("mqtt", True))
        self.webhook_url = str(cfg.get("webhook_url", "") or "").strip()
        # Optional extra headers (e.g. X-API-Key) and a body template. When
        # webhook_body is a dict, each string value is rendered with the alert
        # fields ({severity} {source} {message} {key} {host} {ts}) and the dict
        # is sent as JSON — this is how a webhook maps onto an SMS gateway's
        # {"message": ...} contract. Without it, the raw alert JSON is posted.
        self.webhook_headers = dict(cfg.get("webhook_headers", {}) or {})
        self.webhook_body = cfg.get("webhook_body", None)
        self.min_interval_s = float(cfg.get("min_interval_s", 300))
        self.latency_ms = float(cfg.get("latency_ms", 1000))
        self.buffer_points = int(cfg.get("buffer_points", 1000))
        sig = cfg.get("signals", {}) or {}
        self.sig_device = bool(sig.get("device", True))
        self.sig_sink = bool(sig.get("sink", True))
        self.sig_latency = bool(sig.get("latency", True))
        self.sig_buffer = bool(sig.get("buffer", True))

    # ── introspection (Status page) ───────────────────────────────────────
    def status(self) -> dict:
        channels = []
        if self.use_mqtt:
            channels.append("mqtt")
        if self.webhook_url:
            channels.append("webhook")
        return {
            "enabled": self.enabled,
            "channels": channels,
            "min_interval_s": self.min_interval_s,
            "thresholds": {"latency_ms": self.latency_ms, "buffer_points": self.buffer_points},
            "signals": {"device": self.sig_device, "sink": self.sig_sink,
                        "latency": self.sig_latency, "buffer": self.sig_buffer},
        }

    def recent(self, n: int = 50) -> List[dict]:
        with self._lock:
            items = list(self._recent)
        items.sort(key=lambda a: a.get("ts", 0), reverse=True)
        return items[:n]

    # ── firing ────────────────────────────────────────────────────────────
    def fire(self, severity: str, key: str, source: str, message: str) -> None:
        """Emit an alert, unless disabled or rate-limited for this key."""
        if not self.enabled:
            return
        now = time.time()
        with self._lock:
            last = self._last_fire.get(key)
            if last is not None and (now - last) < self.min_interval_s:
                return
            self._last_fire[key] = now
        alert = {"ts": round(now, 1), "severity": severity, "source": source,
                 "message": message, "key": key, "host": socket.gethostname()}
        with self._lock:
            self._recent.append(alert)
        if self.event_log:
            try:
                self.event_log.add(severity, "ALERT " + source, message, "alert", now)
            except Exception:  # noqa: BLE001
                pass
        if self.use_mqtt and self.mqtt is not None:
            self._publish_mqtt(alert)
        if self.webhook_url:
            threading.Thread(target=self._post_webhook, args=(alert,), daemon=True).start()
        logger.info("ALERT [%s] %s: %s", severity, source, message)

    def _publish_mqtt(self, alert: dict) -> None:
        try:
            prefix = getattr(getattr(self.mqtt, "config", None), "topic_prefix", "") or "janitza"
            topic = prefix.rstrip("/") + "/alert"
            self.mqtt._publish(topic, json.dumps(alert), retain=False)
        except Exception as e:  # noqa: BLE001
            logger.warning("alert MQTT publish failed: %s", e)

    def _render_body(self, alert: dict) -> dict:
        if isinstance(self.webhook_body, dict):
            out = {}
            for k, v in self.webhook_body.items():
                try:
                    out[k] = v.format(**alert) if isinstance(v, str) else v
                except (KeyError, IndexError, ValueError):
                    out[k] = v
            return out
        return alert

    def _post_webhook_sync(self, alert: dict):
        """POST the (templated) alert; return (ok, error). Synchronous."""
        try:
            data = json.dumps(self._render_body(alert)).encode("utf-8")
            headers = {"Content-Type": "application/json", **self.webhook_headers}
            req = urllib.request.Request(self.webhook_url, data=data, headers=headers)
            with _NO_REDIRECT_OPENER.open(req, timeout=8) as r:  # noqa: S310 (user-configured)
                return True, f"HTTP {getattr(r, 'status', 200)}"
        except Exception as e:  # noqa: BLE001
            logger.warning("alert webhook POST failed (%s): %s", redact_url(self.webhook_url), e)
            return False, str(e)

    def _post_webhook(self, alert: dict) -> None:
        self._post_webhook_sync(alert)               # fire-and-forget wrapper

    def test(self, message: str = "Test alert from the Modbus gateway") -> dict:
        """Deliver a synthetic alert over the configured channels to verify wiring.
        Ignores ``enabled`` and rate-limiting so config can be checked before
        going live. Webhook delivery is synchronous so the result is reported."""
        alert = {"ts": round(time.time(), 1), "severity": "info", "source": "test",
                 "message": message, "key": "test", "host": socket.gethostname()}
        channels: dict = {}
        if self.use_mqtt and self.mqtt is not None:
            try:
                self._publish_mqtt(alert)
                channels["mqtt"] = "sent"
            except Exception as e:  # noqa: BLE001
                channels["mqtt"] = f"failed: {e}"
        if self.webhook_url:
            ok, info = self._post_webhook_sync(alert)
            channels["webhook"] = (f"sent ({info})" if ok else f"failed: {info}")
        with self._lock:
            self._recent.append(alert)
        return {"delivered": bool(channels), "channels": channels,
                "sent_body": self._render_body(alert)}
