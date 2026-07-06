"""Generic REST push (northbound sink): periodically POST a device's live values
as JSON to an arbitrary URL (a webhook or a cloud platform).

One background thread per enabled device; fire-and-forget every ``interval_s`` with
last-status tracking. Unlike the HTTP INPUT driver (which is SSRF-guarded to the
LAN), a push target is deliberately allowed to be EXTERNAL — pushing telemetry to
a cloud endpoint is the whole point — so configuration is admin-gated and redirects
are refused, but the host is not restricted to the LAN.
"""
import json
import logging
import ssl
import threading
import urllib.error
import urllib.request
from datetime import datetime

from .redact import redact_url

logger = logging.getLogger(__name__)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects — a configured push URL must be the literal target."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, f"redirect blocked → {newurl}", headers, fp)


def build_payload(device_id: str, name: str, values_by_addr: dict, fmt: str = "native") -> dict:
    """Build the push body from a device's live value store (address-keyed).
    ``native`` → {device, name, ts, values:{<name>:{value,unit,ts}}};
    ``flat``   → {device, name, ts, values:{<name>:<value>}}."""
    named, flat, newest = {}, {}, None
    for item in list(values_by_addr.values()):     # snapshot vs concurrent poller writes
        nm = item.get("name")
        if not nm:
            continue
        named[nm] = {"value": item.get("value"), "unit": item.get("unit", ""),
                     "ts": item.get("timestamp")}
        flat[nm] = item.get("value")
        ts = item.get("timestamp")
        if ts and (newest is None or ts > newest):
            newest = ts
    body = {"device": device_id, "name": name, "ts": newest}
    body["values"] = flat if fmt == "flat" else named
    return body


class RestPusher(threading.Thread):
    """Pushes one device's values on a fixed interval."""

    def __init__(self, device_id: str, cfg: dict, provider):
        super().__init__(daemon=True, name=f"RestPush-{device_id}")
        self.device_id = device_id
        self.url = str(cfg.get("url", "") or "").strip()
        self.interval = max(5, int(cfg.get("interval_s", 30) or 30))
        self.headers = dict(cfg.get("headers") or {})
        self.fmt = cfg.get("format", "native")
        self.timeout = max(1, int(cfg.get("timeout", 10) or 10))
        self.name_label = cfg.get("name", device_id)
        self.provider = provider                       # () -> address-keyed values
        self._stop = threading.Event()
        self.last = {"ok": None, "ts": None, "code": None, "error": None}
        handlers = [_NoRedirect()]
        if not cfg.get("verify_tls", True):            # allow self-signed targets
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self._opener = urllib.request.build_opener(*handlers)

    def run(self):
        if self._stop.wait(2):                         # let the first poll populate values
            return
        while not self._stop.is_set():
            self.push_once()
            if self._stop.wait(self.interval):
                break

    def push_once(self) -> dict:
        if self._stop.is_set():          # a replaced/stopped pusher must not POST
            return self.last
        try:
            payload = build_payload(self.device_id, self.name_label,
                                    self.provider() or {}, self.fmt)
            data = json.dumps(payload).encode("utf-8")
            headers = {"Content-Type": "application/json", **self.headers}
            req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
            with self._opener.open(req, timeout=self.timeout) as r:  # noqa: S310 (user-configured)
                self.last = {"ok": True, "ts": _now(), "code": getattr(r, "status", 200), "error": None}
        except urllib.error.HTTPError as e:
            self.last = {"ok": False, "ts": _now(), "code": e.code, "error": str(e)}
            logger.warning("REST push %s → %s failed: %s", self.device_id, redact_url(self.url), e)
        except Exception as e:  # noqa: BLE001
            self.last = {"ok": False, "ts": _now(), "code": None, "error": str(e)}
            logger.warning("REST push %s → %s failed: %s", self.device_id, redact_url(self.url), e)
        return self.last

    def stop(self):
        self._stop.set()


class RestPushManager:
    """Owns the per-device pusher threads; (re)configured as devices change."""

    def __init__(self):
        self._pushers = {}
        self._lock = threading.Lock()

    def apply(self, device_id: str, cfg: dict, provider):
        """Start/stop/replace a device's pusher. Enabled + a URL → run; else stop."""
        with self._lock:
            old = self._pushers.pop(device_id, None)
            if old:
                old.stop()
            if cfg and cfg.get("enabled") and str(cfg.get("url", "")).strip():
                p = RestPusher(device_id, cfg, provider)
                self._pushers[device_id] = p
                p.start()

    def status(self, device_id: str) -> dict:
        p = self._pushers.get(device_id)
        return dict(p.last) if p else {"ok": None, "ts": None, "code": None, "error": None}

    def push_now(self, device_id: str):
        """Force an immediate push (the Test button). Returns the status or None
        if the device has no running pusher (not enabled)."""
        p = self._pushers.get(device_id)
        return p.push_once() if p else None

    def running(self, device_id: str) -> bool:
        return device_id in self._pushers

    def stop_all(self):
        with self._lock:
            for p in self._pushers.values():
                p.stop()
            self._pushers.clear()


def _now() -> str:
    return datetime.now().isoformat()
