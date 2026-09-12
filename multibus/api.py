# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""REST API and WebSocket server for Janitza Monitor."""

import asyncio
import hmac
import json
import logging
import os
import re
import threading
import time
from typing import Dict, List, Optional, Set
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Body, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .mqtt_publisher import MQTTPublisher
from .influxdb_publisher import InfluxDBPublisher

logger = logging.getLogger(__name__)

# Static assets referenced as /static/<path>?v=<token>. The token is rewritten
# at serve time from the file's mtime (see _render_index_html) so a changed
# app.js/css busts the browser cache automatically — no hand-edited ?v= tokens.
_STATIC_ASSET_RE = re.compile(r'(/static/([^"\'?\s]+))\?v=[^"\'\s]*')


_INDEX_CACHE = {"key": None, "html": None}


def _canonical_redirect_script(canonical_url: str) -> str:
    """A tiny <head> script that steers the browser to the canonical HTTPS host
    (so TLS + passkeys are the default) WITHOUT locking out the local IP: it is
    client-side (the page loads first, so a down hostname is recoverable), and
    two escape hatches keep the IP fallback usable — `?local` in the URL, or a
    sticky `mbg-stay-local` flag it sets. A server-side redirect would strand
    the operator if DNS/Traefik were down; this never can."""
    if not canonical_url:
        return ""
    # json.dumps alone does NOT neutralize "</script>" — the HTML tokenizer
    # ends the script element regardless of JS string context, so a crafted
    # canonical_url became stored XSS on every page incl. the login shell
    # (external audit). Escaping <, > and & inside the JSON string keeps the
    # value byte-identical to JS (\u003c parses back to '<') while making it
    # inert to the HTML parser.
    _safe = (json.dumps(canonical_url).replace('<', '\\u003c')
             .replace('>', '\\u003e').replace('&', '\\u0026'))
    return ("<script>(function(){var C=" + _safe + ";try{"
            "var h=new URL(C).host;var p=new URLSearchParams(location.search);"
            "if(p.has('local')){try{localStorage.setItem('mbg-stay-local','1')}catch(e){}return;}"
            "if(localStorage.getItem('mbg-stay-local')==='1')return;"
            "if(location.host===h)return;"
            "location.replace(C.replace(/\\/$/,'')+location.pathname+location.search+location.hash);"
            "}catch(e){}})();</script>")


def _render_index_html(path: str = "ui/templates/index.html",
                       canonical_url: str = "") -> str:
    """Return the SPA shell with each static asset's ?v= cache-bust token set to
    the asset's mtime (files are served from ui/ at /static/). The rendered HTML
    is cached and only re-read+re-stamped when the template file changes — the
    old path re-read the 138 KB file and ran the regex on EVERY request (page
    load, SPA nav, unauth redirect)."""
    try:
        mt = os.path.getmtime(path)
    except OSError:
        mt = None
    key = (mt, canonical_url)
    if mt is not None and _INDEX_CACHE["key"] == key and _INDEX_CACHE["html"] is not None:
        return _INDEX_CACHE["html"]
    with open(path, encoding="utf-8") as _f:
        html = _f.read()

    def _stamp(m):
        rel = m.group(2)
        try:
            ver = int(os.path.getmtime(os.path.join("ui", rel)))
        except OSError:
            ver = 0
        return f"{m.group(1)}?v={ver}"

    rendered = _STATIC_ASSET_RE.sub(_stamp, html)
    # inject the canonical redirect as the FIRST thing in <head> so it runs
    # before the heavy JS loads (no flash of the app on the wrong host)
    script = _canonical_redirect_script(canonical_url)
    if script:
        rendered = rendered.replace("<head>", "<head>" + script, 1)
    _INDEX_CACHE.update(key=key, html=rendered)
    return rendered


# Process start (approx = module import) for uptime, and last CPU sample for the
# %-delta. Read from /proc/self so no psutil dependency is needed (Linux/container).
_APP_START_TS = time.time()
_CPU_SAMPLE = {"wall": None, "cpu_s": None}


def _read_self_resources() -> dict:
    """Host/process resource footprint from /proc/self (no external deps).

    CPU% is the delta since the previous call, so the first call after start
    returns null and subsequent polls (the Status page refreshes) report a real
    figure. All fields are best-effort: missing /proc entries just omit a key."""
    out: dict = {"num_cpus": os.cpu_count(), "uptime_s": int(time.time() - _APP_START_TS)}
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    out["rss_mb"] = round(int(line.split()[1]) / 1024, 1)
                elif line.startswith("Threads:"):
                    out["threads"] = int(line.split()[1])
    except OSError:
        pass
    try:
        out["open_fds"] = len(os.listdir("/proc/self/fd"))
    except OSError:
        pass
    try:
        est = 0
        for fn in ("/proc/self/net/tcp", "/proc/self/net/tcp6"):
            try:
                with open(fn) as f:
                    next(f, None)  # header row
                    for line in f:
                        parts = line.split()
                        if len(parts) > 3 and parts[3] == "01":  # 01 = ESTABLISHED
                            est += 1
            except OSError:
                continue
        out["tcp_established"] = est
    except OSError:
        pass
    try:
        with open("/proc/self/stat") as f:
            fields = f.read().split()
        cpu_s = (int(fields[13]) + int(fields[14])) / os.sysconf("SC_CLK_TCK")
        now = time.time()
        prev_w, prev_c = _CPU_SAMPLE["wall"], _CPU_SAMPLE["cpu_s"]
        _CPU_SAMPLE["wall"], _CPU_SAMPLE["cpu_s"] = now, cpu_s
        out["cpu_pct"] = (round(100.0 * (cpu_s - prev_c) / (now - prev_w), 1)
                          if prev_w is not None and now > prev_w else None)
    except (OSError, IndexError, ValueError):
        pass
    return out


# Pydantic request models shared with the route modules — re-exported so
# janitza.api.<Model> references (tests, tooling) keep working.
from .routes._models import (  # noqa: F401,E402 — re-exported for external refs
    InfluxDBConfigUpdate, ModbusConfigUpdate, MQTTConfigUpdate,
    RegisterBatchQuery, RegisterQuery, SelectedRegisterUpdate, ThresholdConfig)


class WebSocketManager:
    """Manages WebSocket connections and broadcasts."""

    MAX_CONNECTIONS = 64        # bound FD/memory use; a monitor UI needs a handful

    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> bool:
        await websocket.accept()
        async with self.lock:
            n = len(self.active_connections)
            refused = n >= self.MAX_CONNECTIONS
            if not refused:
                self.active_connections.add(websocket)
        if refused:
            # refuse rather than accumulate to FD exhaustion. Close OUTSIDE the
            # lock — the close handshake against a wedged client must not hold
            # self.lock and stall connect/disconnect/broadcast (all share it).
            logger.warning("WebSocket refused: %d active (cap %d)", n, self.MAX_CONNECTIONS)
            await websocket.close(code=1013)   # try again later
            return False
        logger.info(f"WebSocket connected. Active: {n + 1}")
        return True

    async def disconnect(self, websocket: WebSocket):
        async with self.lock:
            self.active_connections.discard(websocket)
        logger.info(f"WebSocket disconnected. Active: {len(self.active_connections)}")

    async def broadcast(self, message: Dict):
        """Broadcast message to all connected clients."""
        # Snapshot the connections under the lock, then send OUTSIDE it: a slow
        # or wedged client's send_text must not hold the lock and stall
        # connect/disconnect and every other broadcast. Each send is bounded by
        # a timeout so one stuck socket can't block the whole fan-out.
        async with self.lock:
            conns = list(self.active_connections)
        if not conns:
            return
        data = json.dumps(message)

        # Fan out CONCURRENTLY: a sequential loop makes one slow client add its
        # full 5 s timeout to every client behind it (up to MAX_CONNECTIONS×5 s
        # per broadcast, backing up the event loop). gather bounds the whole
        # fan-out to the slowest single send instead.
        async def _send(conn):
            try:
                await asyncio.wait_for(conn.send_text(data), timeout=5)
                return None
            except Exception:  # noqa: BLE001 — timeout or send error → drop it
                return conn
        disconnected = [c for c in await asyncio.gather(*(_send(c) for c in conns)) if c]
        if disconnected:
            async with self.lock:
                for conn in disconnected:
                    self.active_connections.discard(conn)


def create_api(config, modbus_client, mqtt_publisher, influxdb_publisher,
               devices=None, template_registry=None) -> FastAPI:
    """
    Create FastAPI application.

    Args:
        config: Application configuration
        modbus_client: ModbusClient instance (device #1 — legacy back-compat)
        mqtt_publisher: MQTTPublisher instance
        influxdb_publisher: InfluxDBPublisher instance
        devices: optional list of (DeviceConfig, ModbusClient) pairs including
            device #1 first (Tier 2 multi-device). None => single legacy device.

    Returns:
        FastAPI application
    """
    # WebSocket manager
    ws_manager = WebSocketManager()

    # Store current values for dashboard
    current_values: Dict[int, Dict] = {}
    last_update = {"timestamp": None}

    # Store event loop reference for thread-safe async calls
    main_loop = {"loop": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup
        main_loop["loop"] = asyncio.get_running_loop()
        logger.info("API started, event loop captured")
        yield
        # Shutdown (cleanup if needed)
        logger.info("API shutting down")
        hs = getattr(app.state, 'harvester_stop', None)
        if hs is not None:
            hs.set()                               # let the event harvester exit its loop

    app = FastAPI(
        title="Multi-Bus Gateway",
        description="Multi-protocol acquisition gateway (Modbus/HTTP/MQTT in — MQTT/InfluxDB/virtual meters out)",
        version=__version__,
        lifespan=lifespan
    )

    # Expose the live value cache so the virtual-meter engine can read it.
    app.state.current_values = current_values

    # CORS — the UI is same-origin so it needs no CORS; the wildcard only eases
    # read-only third-party access. Credentials are OFF (wildcard + credentials is
    # spec-invalid and a CSRF liability). NOTE: the API is UNAUTHENTICATED, incl.
    # control endpoints — run on a trusted LAN / behind an auth proxy. See README.
    # The UI is served by this same app (same-origin), so cross-origin access
    # is not needed. A wildcard here would let any web page the operator visits
    # fire state-changing POSTs at the gateway (drive-by CSRF on a box that can
    # feed an ESS). Cross-origin API consumers can be added explicitly via
    # CORS_ALLOW_ORIGINS (comma-separated) if ever needed.
    _cors = [o.strip() for o in os.environ.get("CORS_ALLOW_ORIGINS", "").split(",") if o.strip()]
    if _cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=_cors,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Compress responses over the LAN: the SPA payload and the 4000+-register
    # catalog compress ~8-25x (988 KB catalog -> ~40 KB), saving bandwidth and
    # transfer time — the CPU cost is on the response path, not the poll loop.
    from fastapi.middleware.gzip import GZipMiddleware
    app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=5)

    # Optional login/auth (off by default). auth_state manages sessions,
    # password hashing and per-IP lockout; middleware enforces it when enabled.
    from . import auth as _auth
    # sessions persist next to the other secret-bearing state (0600) so a
    # container restart/upgrade no longer logs everyone out
    auth_state = _auth.AuthState(
        config.ui, store_path=str(config.config_path.parent / "sessions.json"))
    if auth_state.enabled and not getattr(config.ui, "tls_enabled", False):
        logger.warning("SECURITY: login is enabled but UI TLS is OFF — the session "
                       "cookie travels in cleartext; a LAN sniffer can hijack it. "
                       "Enable ui.tls or terminate TLS in front of the gateway.")
    if auth_state.enabled and not _auth.is_hashed(getattr(config.ui, "auth_password", "")):
        logger.warning("SECURITY: login is enabled but the admin password is stored "
                       "UNHASHED (likely a hand-edited default like 'admin') — change "
                       "it in the UI / hash it; a plaintext default is trivially guessed.")

    # Write-lease dead-man switch: a leased write auto-reverts to a safe value if
    # the controller stops renewing it.
    from .write_lease import WriteLeaseManager
    _lease_mgr = WriteLeaseManager(persist_path=config.config_path.parent / "write_leases.json")
    _lease_mgr.start()

    # IP allowlist (opt-in): when config.security.allowlist is non-empty, only
    # peers whose IP is in the list (or a listed CIDR) may reach the HTTP
    # API/UI. Loopback and the docker gateway are always allowed so the
    # container's own health probes and same-host access keep working. Empty
    # list => open (trusted-LAN default).
    import ipaddress as _ipaddr

    def _allow_networks():
        nets = []
        for entry in (getattr(config, 'security', None).allowlist
                      if getattr(config, 'security', None) else []):
            entry = str(entry).strip()
            if not entry:
                continue
            try:
                nets.append(_ipaddr.ip_network(entry, strict=False))
            except ValueError:
                logger.warning("security.allowlist: ignoring invalid entry %r", entry)
        return nets

    def _ip_allowed(peer: str) -> bool:
        nets = _allow_networks()
        if not nets:
            return True                       # allowlist empty => open
        try:
            ip = _ipaddr.ip_address(peer)
        except ValueError:
            return False
        # Dual-stack sockets report a v4 client as ::ffff:a.b.c.d — normalize so an
        # IPv4 allowlist entry (192.168.1.0/24) and loopback detection still match
        # (else legit admins/healthchecks get locked out on an IPv6-bound listener).
        if getattr(ip, "ipv4_mapped", None) is not None:
            ip = ip.ipv4_mapped
        if ip.is_loopback:
            return True
        for n in nets:
            if ip in n:
                return True
        return False

    # Security response headers. CSP keeps 'unsafe-inline' because the UI relies
    # on inline event handlers + inline styles (the two XSS-dangerous, data-
    # interpolating handlers were converted to delegation); it still blocks
    # external script/frame injection, clickjacking (frame-ancestors) and
    # base-uri hijack. HSTS is emitted only over HTTPS.
    # No external hosts: bootstrap-icons is vendored (ui/vendor/bootstrap-icons,
    # audit 2026-08-14) — the UI is fully self-contained again, works air-gapped
    # and stops beaconing every operator's browser to a CDN.
    _CSP = ("default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "font-src 'self' data:; "
            "img-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")

    def _apply_security_headers(resp, scheme: str = "http"):
        """Shared with the auth-guard's short-circuit shell return (external
        audit): a response produced by an OUTER middleware never flows through
        an inner one, so the LOGIN page — the one handling credentials — was
        served with no CSP, no frame protection and no nosniff."""
        resp.headers.setdefault("Content-Security-Policy", _CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if scheme == "https":
            resp.headers.setdefault("Strict-Transport-Security",
                                    "max-age=31536000; includeSubDomains")
        return resp

    @app.middleware("http")
    async def _security_headers(request, call_next):
        resp = await call_next(request)
        _apply_security_headers(resp, request.url.scheme)
        return resp

    @app.middleware("http")
    async def _allowlist_guard(request, call_next):
        nets = _allow_networks()
        if nets:
            peer = request.client.host if request.client else ""
            # docker gateway (172.16/12) reaches the container for healthchecks;
            # loopback is handled in _ip_allowed. Everything else must be listed.
            if not _ip_allowed(peer):
                return JSONResponse({"detail": "forbidden (IP not in allowlist)"},
                                    status_code=403)
        return await call_next(request)

    # Optional write protection (opt-in, defense-in-depth for a LAN appliance):
    # if API_KEY (or JANITZA_API_KEY) is set, every state-changing request
    # (POST/PUT/PATCH/DELETE) must carry a matching X-API-Key header. Read-only
    # telemetry (GET) and the on-demand query POSTs stay open so the UI works
    # without a key. Unset => fully open (default, backward-compatible).
    _api_key = os.getenv("API_KEY") or os.getenv("JANITZA_API_KEY") or ""
    _open_writes = {"/api/query/register", "/api/query/batch",
                    "/api/auth/logout"}  # POST but read-only (any role ends its own session)

    # OPERATOR: live actions yes, configuration no. Allowed mutations are the
    # commissioning tools (trace/probe/discovery/query), device tests, device
    # WRITES (bounded by the template's write_min/max — that is exactly the
    # operator's job) and logout. Everything that lands in a config file
    # (devices, registers, templates, vmeters, settings, snapshots) is admin's.
    _OPERATOR_WRITE_PREFIXES = ("/api/bus-trace", "/api/diagnostics",
                                "/api/discover", "/api/query",
                                "/api/auth/logout", "/api/alerts/test",
                                "/api/config/reload-registers",
                                # self-service passkey enrollment/removal
                                "/api/auth/passkey", "/api/auth/passkeys")

    def _operator_may_write(path: str) -> bool:
        # Segment-anchored, not raw prefix/suffix: a prefix must end at a path
        # boundary (so /api/bus-trace never matches /api/bus-trace-anything), and
        # a device live-action must be exactly /api/devices/<id>/<action> (so a
        # config sub-route or a device id literally named "write" can't slip
        # through an endswith check).
        for pfx in _OPERATOR_WRITE_PREFIXES:
            if path == pfx or path.startswith(pfx + "/"):
                return True
        # ad-hoc probe of a NOT-YET-SAVED device — commissioning, the
        # operator's job (the saved-device sibling below always was); the
        # docs promised it and the matcher missed the 4-segment shape
        if path == "/api/devices/test":
            return True
        parts = path.split("/")   # ['', 'api', 'devices', '<id>', '<action>']
        if (len(parts) == 5 and parts[1] == "api" and parts[2] == "devices"
                and parts[3] and parts[3] != "restorable"      # not the admin forget sub-tree
                and parts[4] in ("write", "test", "payload-sample")):
            return True
        # live TEST actions one level deeper — fire-once, change nothing:
        # /api/devices/<id>/rest-push/test and /api/devices/<id>/calculated/test
        if (len(parts) == 6 and parts[1] == "api" and parts[2] == "devices"
                and parts[3] and parts[3] != "restorable"
                and parts[4] in ("rest-push", "calculated") and parts[5] == "test"):
            return True
        return False

    @app.middleware("http")
    async def _write_guard(request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            # CSRF: reject a browser cross-site state-change (drive-by from any
            # page the operator visits). A non-browser client (curl/scripts)
            # sends neither header → allowed; the same-origin UI is 'same-origin'.
            sfs = request.headers.get("sec-fetch-site")
            if sfs in ("cross-site", "cross-origin"):
                return JSONResponse({"detail": "cross-site request blocked"}, status_code=403)
            origin = request.headers.get("origin")
            if origin:
                from urllib.parse import urlparse
                # Compare host:PORT, not just host — a different port is a different
                # origin (a co-hosted service on another port must not be trusted).
                o = urlparse(origin).netloc.lower()
                if o and o not in (request.headers.get("host", "").lower(),
                                   request.url.netloc.lower()):
                    return JSONResponse({"detail": "cross-origin request blocked"}, status_code=403)
            # Optional X-API-Key (opt-in), skipping the read-only query POSTs.
            if (_api_key and request.url.path not in _open_writes
                    and not hmac.compare_digest(request.headers.get("X-API-Key", ""), _api_key)):
                return JSONResponse({"detail": "missing or invalid API key"}, status_code=401)
        return await call_next(request)

    # Login/auth gate (opt-in). Open paths always work so the login flow and
    # static assets load; everything else needs a valid session when enabled.
    # A viewer session is read-only (GET/HEAD only).
    # /metrics joins /health: scrapers (Prometheus) can't log in; both stay
    # behind the IP allowlist and expose operational stats only, never config.
    _auth_open = {"/api/auth/login", "/api/auth/status", "/health", "/metrics", "/favicon.ico",
                  # passkey assertion happens BEFORE a session exists
                  "/api/auth/passkey/login/begin", "/api/auth/passkey/login/finish"}
    _auth_open_prefixes = ("/static/",)

    @app.middleware("http")
    async def _auth_guard(request, call_next):
        if not auth_state.enabled:
            return await call_next(request)
        path = request.url.path
        if path in _auth_open or path.startswith(_auth_open_prefixes):
            return await call_next(request)
        token = request.cookies.get(_auth.COOKIE_NAME, "")
        role = auth_state.role_for(token)
        if role is None:
            # unauthenticated: serve the SPA shell for navigations, 401 for API
            if path.startswith("/api/") or path == "/ws":
                return JSONResponse({"detail": "login required"}, status_code=401)
            # honor the IP allowlist here too — a non-allowlisted client must not
            # even get the UI shell (the allowlist middleware runs inside this
            # one, so without this check the shell would leak past it)
            _peer = request.client.host if request.client else ""
            if _allow_networks() and not _ip_allowed(_peer):
                return JSONResponse({"detail": "forbidden (IP not in allowlist)"},
                                    status_code=403)
            # this short-circuit bypasses the header middleware — apply the
            # security headers directly (this IS the login page)
            return _apply_security_headers(
                HTMLResponse(_render_index_html(canonical_url=config.ui.canonical_url),
                             headers={"Cache-Control": "no-cache"}),
                request.url.scheme)
        # identity lands on request.state BEFORE any deny, so the audit trail
        # records WHO was refused, not an anonymous dash
        request.state.role = role
        ident = auth_state.identity_for(token)
        request.state.user = ident[1] if ident else role
        if role == "viewer" and request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.url.path not in _open_writes:
                return JSONResponse({"detail": "read-only account"}, status_code=403)
        if role == "operator" and request.method not in ("GET", "HEAD", "OPTIONS"):
            if not _operator_may_write(path):
                return JSONResponse(
                    {"detail": "the operator account cannot change configuration"},
                    status_code=403)
        return await call_next(request)

    # Tier 2: the DeviceRegistry owns the (DeviceConfig, client) pairs and one
    # live-value store per device; device #1's store IS the legacy
    # current_values dict (alias), so the UI/vmeters/api keep reading the same
    # object. Mutations are atomic inside the registry (device CRUD runs in
    # FastAPI's threadpool); reads are lock-free snapshots, as before.
    from .device_registry import (DeviceRegistry, client_health,
                                  client_is_live)
    from .modbus_client import endpoint_bus_stats
    registry = DeviceRegistry(config.primary_device.id, current_values)
    for dev_cfg, _client in (devices or []):
        registry.register(dev_cfg, _client)
    app.state.registry = registry
    app.state.device_values = registry.values

    # ---- Calculated registers (formula-derived measurements) -------------------
    # Engine in multibus/calc_engine.py (synthetic 8M+ addressing, expression
    # eval, sink routing). store_for preserves the exact legacy store semantics;
    # publishers() resolves the (possibly nonlocal-rebound by /api/config/apply)
    # sink refs at call time.
    from .calc_engine import CalcEngine

    calc_engine = CalcEngine(config, registry.store_for,
                             lambda: (mqtt_publisher, influxdb_publisher))
    app.state.calc_engine = calc_engine

    for _dc, _c in registry:
        calc_engine.load(_dc.id)

    # ---- Generic REST push sink (northbound) -----------------------------------
    from .rest_push import RestPushManager, RestPusher
    rest_push_manager = RestPushManager()
    app.state.rest_push_manager = rest_push_manager
    _REST_HDR_MASK = "••••••"

    def _rest_provider(device_id, primary):
        def provider():
            return current_values if primary else (registry.store_for(device_id) or {})
        return provider

    def _rest_cfg(dev_cfg):
        cfg = dict(dev_cfg.rest_push or {})
        cfg.setdefault('name', dev_cfg.name)
        return cfg

    def _apply_rest_push(dev_cfg):
        rest_push_manager.apply(dev_cfg.id, _rest_cfg(dev_cfg),
                                _rest_provider(dev_cfg.id, dev_cfg.primary))

    def _rest_push_public(dev_cfg):
        """The device's REST push config for the API — header VALUES masked."""
        rp = dict(dev_cfg.rest_push or {})
        if rp.get('headers'):
            rp['headers'] = {k: _REST_HDR_MASK for k in rp['headers']}
        rp['last'] = rest_push_manager.status(dev_cfg.id)
        return rp

    for _dc, _c in registry:
        _apply_rest_push(_dc)

    def make_data_callback(device_cfg=None):
        """Build the poller callback for one device. device_cfg None (or the
        primary device) keeps the exact legacy behavior: publishers fall back
        to their own config for routing (topics/bucket/tags byte-identical and
        live-tracking config edits); WS broadcast stays primary-only until the
        UI grows a device dimension (Phase B)."""
        primary = device_cfg is None or device_cfg.primary
        topic_prefix = None if primary else device_cfg.mqtt_topic_prefix
        bucket = None if primary else device_cfg.influxdb_bucket
        device_tag = None if primary else device_cfg.influxdb_device_tag
        device_id = "" if primary else device_cfg.id
        values_store = (current_values if primary
                        else registry.ensure_store(device_cfg.id))
        # Per-device output sinks (Phase 2): a device can opt out of MQTT and/or
        # InfluxDB while still polling. The primary always routes to both.
        mqtt_on = True if device_cfg is None else device_cfg.mqtt_enabled
        influx_on = True if device_cfg is None else device_cfg.influxdb_enabled
        calc_key = config.primary_device.id if (device_cfg is None or device_cfg.primary) else device_cfg.id

        def data_callback(poll_group: str, data: Dict[int, Dict]):
            """Callback from a Modbus poller to update values and publish."""
            # Update current values. The timestamp is the MEASUREMENT time
            # (item['ts'], set by the driver when the value was actually read),
            # NOT callback time — the virtual-meter freshness watchdog reads
            # this to decide stale-vs-fresh, so under slow/retried reads callback
            # time would make stale data look newer than it is and defeat the
            # fail-safe. InfluxDB already records item['ts']; this keeps every
            # sink on the same clock.
            for address, item in data.items():
                _ts = item.get('ts')
                values_store[address] = {
                    'value': item.get('value'),
                    'name': item.get('register').name if item.get('register') else '',
                    'label': item.get('register').label if item.get('register') else '',
                    'unit': item.get('register').unit if item.get('register') else '',
                    'poll_group': poll_group,
                    # 'timestamp' is the ISO DISPLAY time (falls back to now for
                    # a rare untimestamped value); 'ts' is the numeric wall
                    # measurement time (public via /api/values). The vmeter
                    # freshness clock is 'mono' below — a MONOTONIC stamp, so it
                    # is immune to wall-clock steps; None when the driver gave no
                    # time, so a missing time fails CLOSED (stale).
                    'timestamp': (datetime.fromtimestamp(_ts).isoformat()
                                  if _ts else datetime.now().isoformat()),
                    'ts': _ts if _ts else None,
                    # monotonic stamp for STEP-IMMUNE freshness (the vmeter reads
                    # this, not the wall clock). None → the vmeter fails closed.
                    'mono': item.get('mono'),
                    # poll cadence of the group that produced this value; the
                    # vmeter derives a per-row bound from it (a 60s slow-group
                    # row must not be judged by a 15s instance bound). None for
                    # push sources → the instance bound applies.
                    'interval': item.get('interval'),
                }

            last_update['timestamp'] = datetime.now().isoformat()

            # Publish to each output sink independently — one sink failing (broker
            # down, bucket missing, network blip) must never skip the other sink or
            # the value store above. The poller thread itself is already isolated
            # per device, so a fault here stays local.
            if mqtt_publisher and mqtt_on:
                try:
                    mqtt_publisher.publish_register_data(poll_group, data,
                                                         topic_prefix=topic_prefix)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"MQTT publish failed for {device_id or 'primary'}: {e}")
            if influxdb_publisher and influx_on:
                try:
                    influxdb_publisher.write_register_data(
                        poll_group, data, bucket=bucket,
                        device_tag=device_tag, device_id=device_id)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"InfluxDB write failed for {device_id or 'primary'}: {e}")

            # Calculated registers: derive formula values from the freshly-updated
            # store and route them to this device's sinks (same routing as above).
            calc_batch = calc_engine.run(calc_key, poll_group, values_store,
                                         topic_prefix=topic_prefix, bucket=bucket,
                                         device_tag=device_tag, device_id=device_id,
                                         mqtt_on=mqtt_on, influx_on=influx_on)

            # Broadcast via WebSocket (thread-safe async call). Phase B: every
            # device broadcasts, tagged with its id — the client filters on the
            # active dashboard device (address spaces overlap across devices, so
            # an untagged merge would collide). Primary messages are unchanged
            # apart from the additive `device` field.
            if main_loop["loop"]:
                ws_values = {
                    str(addr): {
                        'value': item.get('value'),
                        'name': item.get('register').name if item.get('register') else '',
                    }
                    for addr, item in data.items()
                }
                # Include calc values so the live Monitor (which reads the WS-fed
                # store) can chart them; carry unit+label since they have no entry
                # in the register catalog the picker draws from.
                for addr, item in (calc_batch or {}).items():
                    reg = item.get('register')
                    ws_values[str(addr)] = {
                        'value': item.get('value'),
                        'name': reg.name if reg else '',
                        'unit': reg.unit if reg else '',
                        'label': reg.label if reg else '',
                        'calculated': True,
                    }
                asyncio.run_coroutine_threadsafe(
                    ws_manager.broadcast({
                        'type': 'data',
                        'device': calc_key,               # primary id for legacy/primary
                        'poll_group': poll_group,
                        'values': ws_values,
                        'timestamp': last_update['timestamp'],
                    }),
                    main_loop["loop"]
                )

        return data_callback

    # Wire a callback per device; legacy single-client mode keeps working.
    if registry:
        for dev_cfg, client in registry:
            if client:
                client.publish_callback = make_data_callback(dev_cfg)
    elif modbus_client:
        modbus_client.publish_callback = make_data_callback(None)

    # --- Routes ---

    @app.get("/")
    async def root():
        """Serve main UI (with mtime-derived asset cache-bust tokens).

        Cache-Control: no-cache is essential: without it browsers cache the
        SHELL heuristically, so after a deploy an operator keeps loading the
        OLD index (old script tags) and reports the old UI — the asset ?v=
        tokens can only bust caches if the shell itself is revalidated."""
        return HTMLResponse(_render_index_html(canonical_url=config.ui.canonical_url),
                            headers={"Cache-Control": "no-cache"})

    # /api/status(+resources) → routes/status_routes.py

    # ── Persisted cross-subsystem event log + alerting hooks (Status page) ──
    from .event_log import EventLog
    from .alerts import AlertManager
    event_log = EventLog(str(config.config_path.parent / "events.jsonl"))
    app.state.event_log = event_log
    alert_mgr = AlertManager(getattr(config, 'alerts', {}), mqtt_publisher, event_log)
    app.state.alert_manager = alert_mgr
    from .threshold_engine import ThresholdEngine
    threshold_engine = ThresholdEngine(
        deadband_pct=alert_mgr.threshold_deadband_pct,
        alert_on_start=alert_mgr.threshold_alert_on_start)
    app.state.threshold_engine = threshold_engine

    # /api/events, /api/alerts(+test), /api/config/alerts → routes/system.py

    def _harvest_events():
        """Fold new subsystem events into the persisted log (~every 5s) and fire
        alerts off them: source read failures, per-device up/down, MQTT/InfluxDB
        connect transitions, sustained high latency, InfluxDB buffer backlog and
        virtual-meter last errors. Deduped so an event/alert is emitted once."""
        seen: Set = set()
        prev: Dict[str, Optional[bool]] = {}
        drop_prev = {"n": None}
        last_err: Dict[str, str] = {"msg": ""}

        def note(src, evs):
            for e in (evs or []):
                k = (src, round(e.get('ts') or 0, 1), e.get('message') or e.get('kind') or '')
                if k in seen:
                    continue
                seen.add(k)
                event_log.add(e.get('level', 'warn'), src,
                              e.get('message') or e.get('kind') or 'event',
                              e.get('kind', ''), e.get('ts'))

        def transition(pk, src, connected, signal):
            if prev.get(pk) is not None and connected != prev[pk]:
                event_log.add('info' if connected else 'error', src,
                              'connected' if connected else 'disconnected', 'transition')
                fire = (signal == 'device' and alert_mgr.sig_device) or \
                       (signal == 'sink' and alert_mgr.sig_sink)
                if fire:
                    alert_mgr.fire('info' if connected else 'error',
                                   f'{pk}:{"up" if connected else "down"}', src,
                                   'recovered — connected' if connected else 'down — not responding')
            prev[pk] = connected

        while not harvester_stop.is_set():
            try:
                pairs = registry.pairs() or ([(None, modbus_client)] if modbus_client else [])
                # keep the threshold engine's tunables in sync with live config
                threshold_engine.deadband_pct = alert_mgr.threshold_deadband_pct
                threshold_engine.alert_on_start = alert_mgr.threshold_alert_on_start
                thr_seen: Set = set()
                for dev_cfg, client in pairs:
                    if not client or not hasattr(client, 'get_stats'):
                        continue
                    try:
                        st = client.get_stats()
                    except Exception:  # noqa: BLE001
                        continue
                    did = dev_cfg.id if dev_cfg else 'modbus'
                    name = (dev_cfg.name or dev_cfg.id) if dev_cfg else 'Modbus'
                    note(name, st.get('events'))
                    # ONE liveness verdict (see device_registry.client_is_live):
                    # data freshness, not socket state. The transport flag said
                    # "connected" for hours after an endpoint vanished, so a
                    # dark datalogger looked identical to a producing one.
                    live = client_is_live(client)
                    transition('dev:' + did, name, live, 'device')
                    # feed the per-device HA connectivity binary_sensor (publishes
                    # only on change). The PRIMARY publishes too (audit DP-1): it
                    # was the only device without an availability topic, so its
                    # retained data values had no liveness signal at all — a dead
                    # Janitza looked identical to a steady one on the broker.
                    if mqtt_publisher:
                        _avail_prefix = (mqtt_publisher.config.topic_prefix
                                         if (dev_cfg is None or dev_cfg.primary)
                                         else dev_cfg.mqtt_topic_prefix)
                        mqtt_publisher.publish_device_availability(
                            _avail_prefix, live)
                        _seen_ts = st.get('last_success_ts')
                        mqtt_publisher.publish_device_runtime(
                            _avail_prefix, live,
                            datetime.fromtimestamp(_seen_ts).isoformat()
                            if _seen_ts else None,
                            read_errors=st.get('failed_reads'))
                    lat = st.get('last_latency_ms')
                    if alert_mgr.sig_latency and lat and lat > alert_mgr.latency_ms:
                        alert_mgr.fire('warn', 'lat:' + did, name,
                                       f'read latency {lat} ms exceeds {int(alert_mgr.latency_ms)} ms')
                    # per-register threshold crossings → alert events (off by
                    # default). Read-only over the device's live value store, so
                    # this never touches the poll hot path.
                    if alert_mgr.sig_threshold:
                        store = registry.store_for(did) or {}
                        for reg in getattr(client, 'registers', None) or []:
                            th = getattr(reg, 'thresholds', None)
                            entry = store.get(reg.address) if th else None
                            if not entry:
                                continue
                            key = f'thr:{did}:{reg.address}'
                            thr_seen.add(key)
                            # suppress on stale data: don't alarm on a value the
                            # device stopped refreshing (down/frozen) — the band
                            # holds and resumes cleanly on reconnect
                            _mono = entry.get('mono')
                            _iv = entry.get('interval') or 5
                            if _mono is not None and (time.monotonic() - _mono) > max(2.5 * _iv, 15):
                                continue
                            ev = threshold_engine.evaluate(
                                key, entry.get('value'), th, source=name,
                                label=(reg.label or reg.name),
                                unit=getattr(reg, 'unit', ''))
                            if ev:
                                alert_mgr.fire(ev['severity'], ev['key'],
                                               ev['source'], ev['message'])
                if alert_mgr.sig_threshold:
                    # drop band state for registers/devices that went away, so a
                    # removed threshold can't leave a stuck alarm behind
                    threshold_engine.retain(thr_seen)
                # a config that failed to load (self-healed to snapshot/defaults)
                # is a loud, operator-actionable condition — surface it (rate-
                # limited per key so it doesn't spam)
                if getattr(config, '_load_failed', False):
                    alert_mgr.fire('error', 'config', 'Config',
                                   'config.yaml failed to load — running on '
                                   'last-known-good/defaults; saves disabled until repaired')
                if getattr(config, 'config_written_by_newer', False):
                    # downgrade detected: the file carries a newer gateway's
                    # stamp — a save from this version silently drops any
                    # settings that version introduced (audit MEDIUM-2)
                    alert_mgr.fire('warn', 'config-downgrade', 'Config',
                                   f'config.yaml written by '
                                   f'{config.config_written_by} but '
                                   f'{__version__} is running — saving from '
                                   f'this version drops newer settings')
                if mqtt_publisher:
                    transition('mqtt', 'MQTT', bool(mqtt_publisher.get_stats().get('connected')), 'sink')
                if influxdb_publisher:
                    ist = influxdb_publisher.get_stats()
                    transition('influx', 'InfluxDB', bool(ist.get('connected')), 'sink')
                    if ist.get('auth_failed'):
                        # rotated token / missing bucket: writes 401/403/404
                        # while /ping stays green (audit DP-3) — the operator
                        # must act; points are buffering meanwhile
                        alert_mgr.fire('error', 'influx-auth', 'InfluxDB',
                                       'InfluxDB rejects writes (auth/bucket) — '
                                       'token rotated or bucket missing; points '
                                       'are buffering until fixed')
                    if alert_mgr.sig_buffer:
                        bp = ist.get('buffer_points') or 0
                        if bp > alert_mgr.buffer_points:
                            alert_mgr.fire('warn', 'buffer', 'InfluxDB',
                                           f'store-and-forward buffer {bp} points exceeds {alert_mgr.buffer_points}')
                        dropped = ist.get('dropped_total') or 0
                        if drop_prev['n'] is not None and dropped > drop_prev['n']:
                            alert_mgr.fire('error', 'dropped', 'InfluxDB',
                                           f'{dropped - drop_prev["n"]} points dropped (buffer overflow)')
                        drop_prev['n'] = dropped
                mgr = getattr(app.state, 'vmeter_manager', None)
                if mgr:
                    try:
                        for i in mgr.overview():
                            le = i.get('last_error')
                            nm = i.get('name') or i.get('template') or ''
                            if le:
                                k = ('vmeter:' + nm, round(le.get('ts') or 0, 1), le.get('message') or le.get('kind') or '')
                                if k not in seen:
                                    seen.add(k)
                                    event_log.add(le.get('level', 'error'), 'vMeter ' + nm,
                                                  le.get('message') or le.get('kind') or 'error',
                                                  le.get('kind', ''), le.get('ts'))
                    except Exception:  # noqa: BLE001
                        pass
                if len(seen) > 3000:
                    seen.clear()
                last_err["msg"] = ""               # a clean pass resets the dedupe
            except Exception as e:  # noqa: BLE001
                # Inner operations already guard themselves, so a top-level error
                # here is a real bug — surface it (deduped, so no 5s spam).
                msg = f"{type(e).__name__}: {e}"
                if msg != last_err["msg"]:
                    logger.warning("event harvest error: %s", msg)
                    last_err["msg"] = msg
            harvester_stop.wait(5)

    harvester_stop = threading.Event()
    app.state.harvester_stop = harvester_stop
    threading.Thread(target=_harvest_events, daemon=True, name="event-harvester").start()

    # ── Tier 2: devices + device templates ─────────────────────────────────
    # Reuse the registry the boot path already built (main.py) instead of
    # loading every bundled template a SECOND time (~4000-register maps parsed
    # twice = wasted RSS + boot time). Tests that don't pass one get a fresh
    # registry, so behaviour is unchanged.
    from .device_template import TemplateRegistry
    if template_registry is None:
        template_registry = TemplateRegistry(
            user_dir=config.config_path.parent / "device_templates")
    app.state.template_registry = template_registry

    # ── Config snapshots (rollback + last-known-good) ───────────────────────
    from pathlib import Path as _PathSnap
    from .snapshots import SnapshotStore, write_bundle_files as _write_bundle_files
    snapshot_store = SnapshotStore(
        config.config_path.parent,
        _PathSnap(getattr(template_registry, "user_dir", "config/device_templates")),
        device_ids=lambda: [d.id for d in config.devices],
        registers_path_for=config.device_registers_path)
    app.state.snapshot_store = snapshot_store
    if not snapshot_store.list():
        # first boot with the feature: capture a baseline so there is always a
        # "before" to return to
        try:
            snapshot_store.create("baseline")
        except Exception:  # noqa: BLE001
            logger.exception("baseline snapshot failed")

    # Auto-snapshot: a successful mutation on any config-bearing route captures
    # the resulting state (bursts coalesce into one). The trigger list is an
    # allowlist of path prefixes; live actions (writes, probes, tests, trace)
    # deliberately do NOT snapshot — they change devices, not config files.
    _SNAP_PREFIXES = ("/api/devices", "/api/config/", "/api/registers/selected",
                      "/api/device-templates", "/api/virtual-meters",
                      "/api/calculated", "/api/energy/fields", "/api/poll-groups")
    _SNAP_EXCLUDE = ("/test", "/write", "/payload-sample", "/api/config/import",
                     "/api/config/reload-registers", "/api/config/snapshots")

    @app.middleware("http")
    async def _snapshot_trigger(request: Request, call_next):
        response = await call_next(request)
        try:
            if (request.method in ("POST", "PUT", "PATCH", "DELETE")
                    and response.status_code < 400):
                p = request.url.path
                if p.startswith(_SNAP_PREFIXES) and not any(x in p for x in _SNAP_EXCLUDE):
                    snapshot_store.schedule(f"{request.method} {p}",
                                            user=getattr(request.state, "user", "") or "")
        except Exception:  # noqa: BLE001 — never fail the request over a snapshot
            logger.exception("snapshot trigger failed")
        return response

    # ── Audit trail (who changed what, when, from where) ────────────────────
    from .audit import AuditLog
    audit_log = AuditLog(str(config.config_path.parent / "audit.jsonl"))
    app.state.audit_log = audit_log

    # every mutating /api/ call is recorded — including DENIED ones (401/403
    # are exactly what a security review wants to see). Live-data actions with
    # no config effect (queries, probes, discovery) are skipped; device writes
    # have their own richer entry on the write route.
    _AUDIT_SKIP = ("/api/query", "/api/diagnostics/probe", "/api/discover",
                   "/api/auth/", "/api/alerts/test", "/api/devices/test",
                   "/api/bus-trace",
                   # node YAML CRUD self-audits (key names only); skip the
                   # generic body capture so a pasted literal wifi_password:/OTA
                   # key inside the YAML string never lands in audit.jsonl
                   "/api/builder/nodes/", "/api/builder/settings",
                   "/api/builder/profiles")

    @app.middleware("http")
    async def _audit_mw(request: Request, call_next):
        body_summary = None
        p = request.url.path
        auditable = (request.method in ("POST", "PUT", "PATCH", "DELETE")
                     and p.startswith("/api/")
                     and not any(p.startswith(x) or x in p for x in _AUDIT_SKIP)
                     and "/test" not in p and "/payload-sample" not in p)
        if auditable:
            # Content-Length gate BEFORE reading: this middleware is outermost
            # (runs before the IP-allowlist/auth guards), so buffering the body
            # unconditionally would let an unauthenticated, non-allowlisted peer
            # OOM the process with a multi-GB chunked/oversized POST. Only small,
            # length-declared bodies are captured for the audit payload preview;
            # everything else is audited without the body (handlers that need it
            # read it themselves, with their own caps).
            _cl = request.headers.get("content-length", "")
            if _cl.isdigit() and int(_cl) <= 65536:
                try:
                    raw = await request.body()  # cached by Starlette; handlers reread freely
                    if raw and raw.lstrip()[:1] in (b"{", b"["):
                        body_summary = json.loads(raw)
                except Exception:  # noqa: BLE001
                    body_summary = None
        response = await call_next(request)
        if auditable:
            audit_log.append(
                user=getattr(request.state, "user", "") or "-",
                ip=request.client.host if request.client else "-",
                action=f"{request.method} {p}",
                status=("ok" if response.status_code < 400
                        else f"denied ({response.status_code})" if response.status_code in (401, 403)
                        else f"failed ({response.status_code})"),
                detail=body_summary)
        return response

    _DEVICE_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{1,63}$')

    def _find_device(device_id: str):
        return registry.find(device_id)

    def _make_lease_revert(dev, rt, addr, dt, sc, sv, off=0.0):
        """Build the dead-man revert: resolve the *current* device client at
        revert time (surviving device restarts) and write the safe value. Used by
        both the live write path and boot recovery, so they behave identically."""
        def _revert(is_current):
            # Re-check right before the (blocking) write: if the lease was renewed
            # while we waited on the Modbus lock, abort so we don't clobber the
            # fresh setpoint.
            if not is_current():
                return
            _i, _c, c = _find_device(dev)
            if c is None:
                # device gone/restarting — raise so the dead-man retries instead
                # of silently dropping the lease with a live setpoint
                raise RuntimeError(f"device {dev} not running; cannot revert to safe")
            rok, rerr, _w = c.write_value(addr, rt, dt, sv, scale=sc, offset=off)
            logger.warning("MODBUS WRITE (lease-revert) %s: device=%s addr=%s safe=%r%s",
                           "OK" if rok else "FAILED", dev, addr, sv, "" if rok else f" err={rerr}")
            if not rok:
                raise RuntimeError(f"lease-revert write failed: {rerr}")
        return _revert

    # Boot recovery: any write-lease left on disk means a previous run may have
    # crashed while holding a device at a non-safe setpoint. Re-arm each as
    # already-expired so the next sweep reverts it to safe (retrying until the
    # device is reachable). This closes the "crash strands a dangerous setpoint"
    # gap that the in-RAM-only lease store had.
    for _m in _lease_mgr.load_persisted():
        try:
            _rv = _make_lease_revert(_m['device'], _m['register_type'], int(_m['address']),
                                     _m['data_type'], float(_m['scale']), _m['safe_value'],
                                     off=float(_m.get('offset', 0.0) or 0.0))
            _lease_mgr.arm(_m['device'], _m['register_type'], int(_m['address']),
                           int(_m.get('lease_ms') or 0), _rv, meta=_m, fire_now=True)
            logger.warning("WRITE-LEASE recovered after restart → reverting to safe: "
                           "device=%s addr=%s safe=%r", _m['device'], _m['address'], _m['safe_value'])
        except Exception as _e:  # noqa: BLE001
            logger.error("write-lease recovery: bad persisted record %r: %s", _m, _e)

    def _write_rule(dev_cfg, address: int, rtype: str):
        """The TemplateRegister governing writes to (address, register_type) on
        this device, or None. Carries writable + bounds + safe value. A register
        absent here (or not writable) cannot be written — the safety allowlist."""
        tpl = template_registry.get(dev_cfg.template) if dev_cfg.template else None
        if tpl is None:
            return None
        for r in tpl.registers:
            if r.address == address and (r.register_type or 'holding') == rtype:
                return r
        return None

    def _start_device_client(dev_cfg):
        """Create + wire + background-start the client for a device.

        Goes through the SAME builder the boot path uses
        (device_runtime.build_device_client), so a device added at runtime and
        the same device after a restart are byte-identical. There used to be two
        copies of this logic and they drifted, invisibly, until a restart
        brought a device back decoding differently."""
        from .device_runtime import build_device_client
        client = build_device_client(
            config, template_registry, dev_cfg,
            allow_nonlan=config.security.allow_nonlan_http_devices)
        if client is None:
            return None
        client.publish_callback = make_data_callback(dev_cfg)

        def _bg():
            if client.connect():
                logger.info(f"device {dev_cfg.id}: connected")
            else:
                logger.warning(f"device {dev_cfg.id}: connect failed — pollers will retry")
            client.start_polling()

        threading.Thread(target=_bg, daemon=True,
                         name=f"Device-Init-{dev_cfg.id}").start()
        return client

    def _validate_device_payload(payload: Dict, *, existing_id: str = None) -> Dict:
        """Normalize + validate the raw device dict from the UI. Raises
        HTTPException(422) with a per-field error list."""
        errors = []
        did = str(payload.get('id', existing_id or '')).strip().lower()
        if not _DEVICE_ID_RE.match(did):
            errors.append("id: use a-z 0-9 - _ (2-64 chars, starts alphanumeric)")
        if existing_id and did != existing_id:
            errors.append("id: cannot be changed after creation")
        if not existing_id and registry.has(did):
            errors.append(f"id: '{did}' already exists")
        template_id = str(payload.get('template', '')).strip()
        conn = payload.get('connection', {}) or {}
        # Builder adopt: the generate response never carries the real broker
        # password — the sentinel is resolved here, server-side only.
        if conn.get('password') == '$GATEWAY_MQTT_PASSWORD':
            conn['password'] = config.mqtt.password
        protocol = str(conn.get('protocol', 'tcp')).lower()
        if protocol not in ('tcp', 'rtu', 'rtu-tcp', 'http', 'mqtt'):
            errors.append("connection.protocol: must be 'tcp', 'rtu', 'rtu-tcp', 'http' or 'mqtt'")
        # A template's register map is transport-specific (Modbus reads by address,
        # HTTP/MQTT by json_path), so the device protocol MUST match the template's
        # transport class — otherwise every read silently resolves to nothing.
        _tpl = template_registry.get(template_id) if template_id else None
        _classmap = {'http': 'http', 'mqtt': 'mqtt'}
        if template_id and _tpl is None:
            errors.append(f"template: '{template_id}' not found")
        elif _tpl is not None and protocol in ('tcp', 'rtu', 'rtu-tcp', 'http', 'mqtt'):
            from .device_template import template_transport
            dev_class = _classmap.get(protocol, 'modbus')
            tpl_class = template_transport(_tpl)
            if dev_class != tpl_class:
                errors.append(
                    f"template: '{template_id}' is a {tpl_class.upper()} map but this "
                    f"device is {protocol.upper()} — pick a {dev_class.upper()} template")
        if protocol == 'http':
            url = str(conn.get('url', '')).strip()
            if not (url.startswith('http://') or url.startswith('https://')):
                errors.append("connection.url: required (http:// or https://) for HTTP/JSON")
            elif not config.security.allow_nonlan_http_devices:
                from .http_client import lan_url_error
                _e = lan_url_error(url)                # SSRF guard
                if _e:
                    errors.append(f"connection.url: {_e} — set "
                                  "security.allow_nonlan_http_devices=true to allow it")
        elif protocol == 'mqtt':
            if not str(conn.get('broker', '')).strip():
                errors.append("connection.broker: required for MQTT input")
            if not str(conn.get('topic', '')).strip():
                errors.append("connection.topic: required for MQTT input")
            try:
                mp = int(conn.get('port', 1883))
                if not (1 <= mp <= 65535):
                    raise ValueError
            except (TypeError, ValueError):
                errors.append("connection.port: must be 1..65535")
        elif protocol == 'rtu':
            sp = str(conn.get('serial_port', '')).strip()
            if not sp:
                errors.append("connection.serial_port: required for Modbus RTU")
            else:
                # One physical serial line cannot be driven by two independent
                # masters (each ModbusClient owns its own lock) without bus
                # collisions. Until a shared-bus arbiter lands (Tier 3), refuse a
                # second RTU device on a serial port already in use.
                for d in config.devices:
                    if (getattr(d, 'id', None) != existing_id
                            and getattr(d, 'protocol', '') == 'rtu'
                            and str(getattr(d.connection, 'serial_port', '')).strip() == sp):
                        errors.append(f"connection.serial_port: '{sp}' is already used by "
                                      f"device '{d.id}' — one RTU master per serial line")
                        break
            for fld, lo, hi in (("baudrate", 300, 4_000_000), ("stopbits", 1, 2),
                                ("bytesize", 5, 8), ("unit_id", 0, 255)):
                if fld in conn or fld == "unit_id":
                    try:
                        v = int(conn.get(fld, {"stopbits": 1, "bytesize": 8, "unit_id": 1,
                                               "baudrate": 9600}[fld]))
                        if not (lo <= v <= hi):
                            raise ValueError
                    except (TypeError, ValueError):
                        errors.append(f"connection.{fld}: must be {lo}..{hi}")
            if str(conn.get('parity', 'N')).upper() not in ('N', 'E', 'O'):
                errors.append("connection.parity: must be N, E or O")
        else:
            if protocol in ('tcp', 'rtu-tcp') and not str(conn.get('host', '')).strip():
                errors.append(f"connection.host: required for Modbus {'RTU-over-TCP' if protocol == 'rtu-tcp' else 'TCP'}")
            try:
                port = int(conn.get('port', 502))
                if not (1 <= port <= 65535):
                    raise ValueError
            except (TypeError, ValueError):
                errors.append("connection.port: must be 1..65535")
            if protocol == 'rtu-tcp':
                # An rtu-tcp host:port IS a physical RS-485 line: the bridge
                # generates it with max-connections:1 + kickolduser, so two
                # devices on one endpoint evict each other forever, both
                # showing timeouts with no explanation (external audit E2 —
                # the same one-master-per-line rule the plain-RTU branch
                # already enforces on serial_port). Multi-drop belongs on ONE
                # device per line; distinct unit_ids do not change the rule.
                _host = str(conn.get('host', '')).strip().lower()
                try:
                    _port = int(conn.get('port', 502))
                except (TypeError, ValueError):
                    _port = None
                for d in config.devices:
                    if (getattr(d, 'id', None) != existing_id
                            and getattr(d, 'protocol', '') == 'rtu-tcp'
                            and str(getattr(d.connection, 'host', '')).strip().lower() == _host
                            and int(getattr(d.connection, 'port', 0) or 0) == _port):
                        errors.append(
                            f"connection: rtu-tcp endpoint {_host}:{_port} is "
                            f"already used by device '{d.id}' — one master per "
                            f"bridged serial line (the bridge kicks the older "
                            f"client, so two devices would evict each other "
                            f"forever)")
                        break
            try:
                unit = int(conn.get('unit_id', 1))
                if not (0 <= unit <= 255):
                    raise ValueError
            except (TypeError, ValueError):
                errors.append("connection.unit_id: must be 0..255")
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})
        raw = {
            'id': did,
            'name': str(payload.get('name', '') or did),
            'template': template_id,
            'enabled': bool(payload.get('enabled', True)),
            'connection': conn,
        }
        mqtt_block = dict(payload.get('mqtt') or {})
        # per-device HA discovery toggle lives under mqtt.ha_discovery
        if 'ha_discovery_enabled' in payload:
            mqtt_block['ha_discovery'] = bool(payload['ha_discovery_enabled'])
        if mqtt_block:
            raw['mqtt'] = mqtt_block
        if payload.get('influxdb'):
            raw['influxdb'] = payload['influxdb']
        return raw

    def _device_entry(dev_cfg, client, *, redact=False) -> Dict:
        entry = dev_cfg.summary()
        regs, _groups = config.load_device_registers(dev_cfg)
        entry['selected_registers'] = len(regs)
        entry['influxdb_device_tag'] = dev_cfg.influxdb_device_tag
        from .pq_recorder import template_supports_pq
        _tpl = (template_registry.get(dev_cfg.template)
                if template_registry and dev_cfg.template else None)
        entry['pq_supported'] = template_supports_pq(_tpl, dev_cfg.template)
        entry['pq_recorder'] = dict(dev_cfg.pq_recorder or {})
        # full connection block for the device detail editor
        c = dev_cfg.connection
        _url = dev_cfg.http.get('url', '') if dev_cfg.protocol == 'http' else ''
        if redact and _url:
            # a viewer must not see credentials embedded in the URL (userinfo or
            # a token query param); the admin sees the real URL to edit it
            from .redact import redact_url
            _url = redact_url(_url)
        entry['connection'] = {
            'protocol': dev_cfg.protocol,
            'host': c.host, 'port': c.port, 'unit_id': c.unit_id,
            'timeout': c.timeout, 'retry_attempts': c.retry_attempts,
            'retry_delay': c.retry_delay,
            'serial_port': c.serial_port, 'baudrate': c.baudrate,
            'parity': c.parity, 'stopbits': c.stopbits, 'bytesize': c.bytesize,
            'url': _url,
            'verify_tls': dev_cfg.http.get('verify_tls', True),
        }
        if dev_cfg.protocol == 'mqtt':
            m = dev_cfg.mqtt_in
            entry['connection'].update({
                'broker': m.get('broker', ''), 'port': m.get('port', 1883),
                'topic': m.get('topic', ''), 'username': m.get('username', ''),
                'tls': bool(m.get('tls', False)),
                'password': '******' if m.get('password') else '',   # never echo the secret
            })
        if dev_cfg.protocol == 'rtu':
            entry['serial'] = dev_cfg.serial
        if dev_cfg.protocol == 'http':
            # Never echo header VALUES back — they can carry Authorization / API
            # tokens and /api/devices is readable by the viewer role. Show the
            # header names only (masked); the UI does not round-trip headers.
            _http = dict(dev_cfg.http)
            if _http.get('headers'):
                _http['headers'] = {k: '******' for k in _http['headers']}
            entry['http'] = _http
        # Output-sink status (Phase 2): per-device enable + the shared broker/db
        # connection state, so the device detail can show each sink live.
        mqtt_conn = bool(getattr(mqtt_publisher, 'connected', False)) if mqtt_publisher else False
        influx_conn = bool(getattr(influxdb_publisher, 'connected', False)) if influxdb_publisher else False
        entry['sinks'] = {
            'mqtt': {
                'enabled': dev_cfg.mqtt_enabled,
                'available': mqtt_publisher is not None,
                'connected': mqtt_conn,
                'active': dev_cfg.mqtt_enabled and mqtt_conn,
                'topic_prefix': dev_cfg.mqtt_topic_prefix,
            },
            'influxdb': {
                'enabled': dev_cfg.influxdb_enabled,
                'available': influxdb_publisher is not None,
                'connected': influx_conn,
                'active': dev_cfg.influxdb_enabled and influx_conn,
                'bucket': dev_cfg.influxdb_bucket,
            },
            # HTTP/JSON output: serve this device's live values as JSON, Solar-API
            # style. Always "available" (it's just this app's own HTTP server);
            # active == enabled. Read-only, no external connection to fail.
            'http': {
                'enabled': dev_cfg.http_output_enabled,
                'available': True,
                'connected': True,
                'active': dev_cfg.http_output_enabled,
                'path': f"/api/meters/{dev_cfg.id}",
            },
            # Generic REST push: POST values to an external URL on an interval.
            'rest': {
                'enabled': bool(dev_cfg.rest_push.get('enabled')),
                'available': True,
                'connected': rest_push_manager.status(dev_cfg.id).get('ok') is not False,
                'active': bool(dev_cfg.rest_push.get('enabled')),
                'url': dev_cfg.rest_push.get('url', ''),
            },
        }
        entry['rest_push'] = _rest_push_public(dev_cfg)
        if redact:
            # The same credential can ride in more than one URL copy — the
            # summary's http_url, the rest sink url, and the rest_push url all
            # re-emit it. Redact every copy for a viewer (connection.url above
            # was only one of them).
            from .redact import redact_url
            if entry.get('http_url'):
                entry['http_url'] = redact_url(entry['http_url'])
            if entry['sinks']['rest'].get('url'):
                entry['sinks']['rest']['url'] = redact_url(entry['sinks']['rest']['url'])
            if isinstance(entry.get('rest_push'), dict) and entry['rest_push'].get('url'):
                entry['rest_push']['url'] = redact_url(entry['rest_push']['url'])
        if client:
            stats = client.get_stats()
            connected = stats.get('connected')
            health = client.data_health().get('status')
            # The status dot must never contradict the connection text: a device
            # that isn't connected can't be 'ok'. data_health() reports 'ok' on
            # cold start / when nothing has been polled yet, so gate it on the
            # actual connection — not connected but enabled => degraded (amber),
            # 'down' only once reads have actually been failing.
            if not connected and health == 'ok':
                health = 'degraded'
            entry.update({
                'connected': connected,
                'successful_reads': stats.get('successful_reads'),
                'failed_reads': stats.get('failed_reads'),
                'staleness_age_s': stats.get('staleness_age_s'),
                'poll_rate': stats.get('poll_rate'),
                'data_health': health,
            })
        else:
            # disabled or transport not run — idle (grey), not green
            entry.update({'connected': False, 'data_health': 'idle'})
        return entry

    @app.get("/api/devices")
    def list_devices(request: Request):
        """All southbound devices with live health (device #1 first)."""
        # only admin sees raw credentials; viewer AND operator get redacted URLs
        _redact = getattr(request.state, "role", None) in ("viewer", "operator")
        return {"devices": [_device_entry(d, c, redact=_redact) for d, c in registry]}

    # /api/serial-ports, /api/bridge/adapters → routes/commissioning.py

    def _sync_device_discovery():
        """Rebuild the MQTT discovery hooks from the current non-primary
        devices and publish them now (so HA sees a device the moment it is
        added/edited, not only on the next reconnect). Idempotent (retained)."""
        if not mqtt_publisher:
            return
        hooks = []
        for dev_cfg, _c in registry:
            if dev_cfg.primary or not dev_cfg.ha_discovery_enabled:
                continue

            def _hook(d=dev_cfg):
                regs, _g = config.load_device_registers(d)
                # write-entities (number/select) only when FULLY enabled; the
                # template is the write allowlist, so writability + bounds come
                # from _write_rule, never from the saved register.
                wrules = {}
                if (config.mqtt.allow_write_entities and config.security.allow_writes
                        and not d.primary and d.protocol != 'http'):
                    for r in regs:
                        if getattr(r, 'register_type', 'holding') != 'holding':
                            continue
                        rule = _write_rule(d, r.address, 'holding')
                        if rule is not None and rule.writable:
                            wrules[r.address] = rule
                mqtt_publisher.publish_device_discovery(
                    d.id, d.name, d.mqtt_topic_prefix, regs, model=d.template,
                    write_rules=wrules)
            hooks.append(_hook)
        mqtt_publisher.discovery_hooks = hooks
        if getattr(mqtt_publisher, "connected", False):
            for h in hooks:
                try:
                    h()
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"device discovery publish failed: {e}")

    def _push_readback_to_store(device_id, address, value):
        """Write-then-refresh: push a just-written register's read-back value into
        the live store so the vmeters / UI reflect the new value AT ONCE instead
        of at the next poll (a slow-group setpoint could otherwise lag 60 s)."""
        if value is None:
            return
        store = registry.store_for(device_id)
        if store is not None and address in store:
            # whole-dict swap, like the poller (audit DP-35): field-by-field
            # mutation was the one non-atomic store write — a concurrent
            # reader could see the new value with the old mono stamp
            e = dict(store[address])
            e['value'] = value
            e['ts'] = time.time()
            e['mono'] = time.monotonic()
            store[address] = e

    def _mqtt_write_command(device_id, register, payload):
        """Execute an HA number/select command as a Modbus write. The broker is
        NOT a trusted caller, so EVERYTHING is re-validated here regardless of
        what discovery advertised: both write gates, the template write envelope
        (writability + bounds), a rate limit, and an audit record. Runs on the
        MQTT command worker — never on paho's network thread, where the
        blocking Modbus I/O would stall every publish (audit M4); write_value
        takes the connection lock, so it is serialized with the poller."""
        import math as _math
        if not (config.security.allow_writes and config.mqtt.allow_write_entities):
            return
        _idx, dev_cfg, client = _find_device(device_id)
        if dev_cfg is None or client is None or dev_cfg.primary or dev_cfg.protocol == 'http':
            return
        rule = _write_rule(dev_cfg, register.address, 'holding')
        if rule is None or not rule.writable:
            logger.warning("MQTT write REJECTED (not writable): device=%s addr=%s", device_id, register.address)
            return
        enum_map = getattr(register, 'enum', None)
        if enum_map:                                   # select: label → code
            value = next((int(k) for k, v in enum_map.items() if str(v) == payload), None)
            if value is None:
                logger.warning("MQTT write REJECTED (unknown option %r): device=%s addr=%s",
                               payload, device_id, register.address)
                return
        else:                                          # number: parse
            try:
                value = float(payload)
            except (TypeError, ValueError):
                logger.warning("MQTT write REJECTED (non-numeric %r): device=%s addr=%s",
                               payload, device_id, register.address)
                return
        # bounds apply to BOTH a typed number and a mapped enum code
        if not _math.isfinite(value):
            return
        if rule.write_min is not None and value < rule.write_min:
            logger.warning("MQTT write REJECTED (%s < min %s): device=%s addr=%s",
                           value, rule.write_min, device_id, register.address)
            return
        if rule.write_max is not None and value > rule.write_max:
            logger.warning("MQTT write REJECTED (%s > max %s): device=%s addr=%s",
                           value, rule.write_max, device_id, register.address)
            return
        if not _write_rate_ok('mqtt:' + device_id):
            logger.warning("MQTT write RATE-LIMITED: device=%s", device_id)
            return
        data_type = (rule.data_type or 'uint16').lower()
        scale = float(rule.scale if rule.scale is not None else 1.0)
        offset = float(getattr(rule, 'offset', 0.0) or 0.0)
        ok, err, _words = client.write_value(register.address, 'holding', data_type,
                                             value, scale=scale, offset=offset)
        logger.warning("MODBUS WRITE %s (via HA): device=%s addr=%s dtype=%s value=%r%s",
                       "OK" if ok else "FAILED", device_id, register.address, data_type,
                       value, "" if ok else f" err={err}")
        if ok:                                          # write-then-refresh
            try:
                raw = client.read_register(register.address, data_type, 'holding')
                if raw is not None:
                    _push_readback_to_store(device_id, register.address,
                                            float(raw) / (scale or 1.0) + offset)
            except Exception:  # noqa: BLE001
                pass
        try:
            audit_log.append(user="ha-mqtt", ip="mqtt", action="modbus write",
                             status="ok" if ok else "fail",
                             detail={"device": device_id, "address": register.address,
                                     "value": value, "via": "ha-write-entity"})
        except Exception:  # noqa: BLE001
            pass

    if mqtt_publisher:
        mqtt_publisher.set_command_write_handler(_mqtt_write_command)

    def _apply_routing_defaults(raw: Dict) -> Dict:
        """Fill missing topic prefix / bucket from the configured {device}
        patterns so a new device always has sane routing."""
        did = raw['id']
        raw.setdefault('mqtt', {})
        if not raw['mqtt'].get('topic_prefix'):
            raw['mqtt']['topic_prefix'] = config.default_topic_prefix(did)
        raw.setdefault('influxdb', {})
        if not raw['influxdb'].get('bucket'):
            raw['influxdb']['bucket'] = config.default_bucket(did)
        return raw

    def _autoselect_template_registers(dev_cfg):
        """Seed a new device with its template's registers — extracted to
        device_seed.py so the boot path (endpoint units) uses the same logic."""
        from .device_seed import autoselect_template_registers
        autoselect_template_registers(config, template_registry, dev_cfg)

    def _ensure_device_bucket(dev_cfg):
        """Auto-create the device's InfluxDB bucket (off-thread) so its history/
        energy work without manual setup. Non-fatal."""
        if not (influxdb_publisher and dev_cfg.influxdb_enabled and dev_cfg.influxdb_bucket):
            return
        threading.Thread(target=influxdb_publisher.ensure_bucket,
                         args=(dev_cfg.influxdb_bucket,), daemon=True,
                         name=f"Bucket-{dev_cfg.id}").start()

    # Serialize device mutations: create/update/delete/restore each do a
    # check-then-act across validate → persist → client-swap → registry. Two
    # concurrent requests (a double-clicked Apply, two admins) would otherwise
    # orphan a client's live poller threads or leave 2× pollers publishing
    # every value twice. functools.wraps keeps the handler's real signature so
    # FastAPI still resolves path/body params (it follows __wrapped__).
    _device_mutation_lock = threading.Lock()

    def _serialized_mutation(fn):
        import functools

        @functools.wraps(fn)
        def _w(*a, **kw):
            with _device_mutation_lock:
                return fn(*a, **kw)
        return _w

    @app.post("/api/devices")
    @_serialized_mutation
    def create_device(payload: Dict = Body(...)):
        """Create a device: validate → persist → auto-select its template
        registers + ensure its InfluxDB bucket → hot-start its poller and
        publish HA discovery (no restart)."""
        raw = _apply_routing_defaults(_validate_device_payload(payload))
        try:
            dev_cfg = config.upsert_raw_device(raw)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        _autoselect_template_registers(dev_cfg)   # before start → poller loads them
        _ensure_device_bucket(dev_cfg)
        client = _start_device_client(dev_cfg)
        registry.add(dev_cfg, client)
        _sync_device_discovery()
        logger.info(f"device {dev_cfg.id}: created "
                    f"({dev_cfg.protocol}, template={dev_cfg.template or '—'})")
        return {"status": "created", "device": _device_entry(dev_cfg, client)}

    @app.get("/api/devices/restorable")
    def list_restorable_devices():
        """Deleted devices whose full definition was kept (id no longer active).
        Restoring one rebuilds the exact device — connection, template AND its
        register selection — instead of starting from scratch."""
        return {"devices": config.list_deleted_devices()}

    @app.post("/api/devices/{device_id}/restore")
    @_serialized_mutation
    def restore_device(device_id: str):
        """Re-create a previously deleted device from its kept tombstone. Its
        selected-registers file (kept on disk) is loaded by the poller, so the
        measurement selection returns intact."""
        if any(d.id == device_id for d in config.devices):
            raise HTTPException(status_code=409, detail={"errors": [
                f"device '{device_id}' already exists"]})
        try:
            raw = config.load_deleted_device(device_id)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        if raw is None:
            raise HTTPException(status_code=404, detail="no restorable device with that id")
        # re-validate the tombstone through the same SSRF/LAN + protocol/port
        # checks create_device enforces (raises 422 on failure) — a planted or
        # edited tombstone must not reactivate a definition that create would
        # reject. Returns the normalized raw dict.
        raw = _validate_device_payload(raw)
        try:
            dev_cfg = config.upsert_raw_device(raw)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        # registers file is already on disk → _autoselect skips (keeps the
        # user's exact selection); only seeds if the file was lost.
        _autoselect_template_registers(dev_cfg)
        _ensure_device_bucket(dev_cfg)
        client = _start_device_client(dev_cfg)
        registry.add(dev_cfg, client)
        _sync_device_discovery()
        logger.info(f"device {device_id}: restored from tombstone")
        return {"status": "restored", "device": _device_entry(dev_cfg, client)}

    @app.delete("/api/devices/restorable/{device_id}")
    @_serialized_mutation
    def forget_restorable_device(device_id: str):
        """Permanently drop a deleted device's kept settings (tombstone +
        registers). Irreversible; refuses to touch an active device."""
        try:
            ok = config.forget_deleted_device(device_id)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        if not ok:
            raise HTTPException(status_code=404, detail="nothing to forget for that id")
        return {"status": "forgotten", "id": device_id}

    def _update_primary_device(payload: Dict):
        """Edit device #1 in place: connection → config.modbus, HA flag →
        global mqtt, name/template updatable; routing identity (topic prefix /
        bucket / device tag) stays FIXED for byte-identical migration.
        Reconnects the primary client live."""
        conn = payload.get('connection', {}) or {}
        if str(conn.get('protocol', 'tcp')).lower() != 'tcp':
            raise HTTPException(status_code=422, detail={"errors": [
                "the primary device (UMG512) is Modbus TCP"]})
        # Validate like a secondary device — the primary was skipping this, so a
        # bad port/unit_id (e.g. "abc") was persisted and broke the primary client.
        errors = []
        host = str(conn.get('host', '')).strip()
        if not host:
            errors.append("connection.host: required for Modbus TCP")
        try:
            port = int(conn.get('port', 502))
            if not (1 <= port <= 65535):
                raise ValueError
        except (TypeError, ValueError):
            errors.append("connection.port: must be 1..65535")
        try:
            unit = int(conn.get('unit_id', 1))
            if not (0 <= unit <= 255):
                raise ValueError
        except (TypeError, ValueError):
            errors.append("connection.unit_id: must be 0..255")
        try:
            timeout = float(conn.get('timeout', 3))
        except (TypeError, ValueError):
            errors.append("connection.timeout: must be a number")
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})
        config.update_modbus(
            host=host, port=port, unit_id=unit, timeout=timeout,
            retry_attempts=conn.get('retry_attempts'),
            retry_delay=conn.get('retry_delay'))
        if 'ha_discovery_enabled' in payload:
            config.mqtt.ha_discovery_enabled = bool(payload['ha_discovery_enabled'])
        config.save_yaml_config()
        config._build_devices()
        prim = config.primary_device
        # _build_devices() rebuilt EVERY DeviceConfig — re-sync the whole list
        # (clients matched by id), so pollers and config.get_device() never
        # diverge on derived fields after a primary edit.
        registry.resync(config.devices, modbus_client)
        if modbus_client:
            modbus_client.update_config(config.modbus)
            modbus_client.reconnect()
        if mqtt_publisher and config.mqtt.ha_discovery_enabled:
            try:
                mqtt_publisher.publish_ha_discovery()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"primary HA re-publish failed: {e}")
        return {"status": "updated", "device": _device_entry(prim, modbus_client)}

    @app.put("/api/devices/{device_id}")
    @_serialized_mutation
    def update_device(device_id: str, payload: Dict = Body(...)):
        """Update a device: stop its poller, persist, restart. The primary
        (UMG512) is editable too — its connection maps to the flat Modbus
        config; its routing identity stays fixed."""
        idx, dev_cfg, client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        if dev_cfg.primary:
            return _update_primary_device(payload)
        raw = _apply_routing_defaults(_validate_device_payload(payload, existing_id=device_id))
        # Routing identity (topic prefix / bucket / Influx tag) is FIXED after
        # creation — changing it would re-route future data and orphan the
        # device's existing history + Home Assistant entities. Keep the stored
        # values on update (the MQTT/InfluxDB enable + HA-discovery flags stay
        # editable); this also stops an API caller bypassing the locked UI fields.
        raw.setdefault('mqtt', {})['topic_prefix'] = dev_cfg.mqtt_topic_prefix
        raw.setdefault('influxdb', {})['bucket'] = dev_cfg.influxdb_bucket
        raw['influxdb']['device_tag'] = dev_cfg.influxdb_device_tag
        # Preserve the HTTP-output opt-in across an edit (it is toggled from the
        # Outputs tab, not carried in the wizard payload — an omit must not wipe it).
        if dev_cfg.http_output_enabled:
            raw.setdefault('http_output', {})['enabled'] = True
        # Same for the REST push config (managed from the Outputs tab).
        if dev_cfg.rest_push:
            raw['rest_push'] = dev_cfg.rest_push
        # Same for the write lock (managed from its own endpoint — an edit
        # payload that omits it must not silently unlock the device).
        if dev_cfg.write_locked:
            raw['write_locked'] = True
        # Preserve real HTTP header secrets across an edit: the API echoes header
        # VALUES masked ("******"), and the UI does not manage headers, so an
        # update that omits or masks them must keep the stored values rather than
        # wiping/overwriting the tokens.
        if dev_cfg.protocol == 'http':
            _conn = raw.get('connection') or {}
            _old_h = dict(dev_cfg.http.get('headers') or {})
            _new_h = _conn.get('headers')
            if isinstance(_new_h, dict):
                _conn['headers'] = {k: (_old_h.get(k, v) if v == '******' else v)
                                    for k, v in _new_h.items()}
            elif _old_h:
                _conn['headers'] = _old_h
            raw['connection'] = _conn
        # Preserve the MQTT broker password across an edit (echoed masked).
        if dev_cfg.protocol == 'mqtt':
            _conn = raw.get('connection') or {}
            pw = _conn.get('password')
            if pw in ('', '******', None):
                _conn['password'] = dev_cfg.mqtt_in.get('password', '')
            raw['connection'] = _conn
        # Persist FIRST (upsert is transactional and rolls back on failure);
        # only tear down the old client once the new config is committed, so a
        # rejected edit leaves the running device untouched instead of
        # disconnected-and-not-restarted.
        try:
            new_cfg = config.upsert_raw_device(raw)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        if client:
            client.disconnect()
        _autoselect_template_registers(new_cfg)   # first-time template assignment
        _ensure_device_bucket(new_cfg)            # influx just enabled / bucket changed
        new_client = _start_device_client(new_cfg)
        # re-resolve by id: a concurrent delete may have shifted the index
        registry.replace(device_id, new_cfg, client=new_client, add_if_missing=True)
        _apply_rest_push(new_cfg)                  # restart pusher with new config
        _sync_device_discovery()
        logger.info(f"device {device_id}: updated")
        return {"status": "updated", "device": _device_entry(new_cfg, new_client)}

    @app.post("/api/devices/{device_id}/http-output")
    def set_device_http_output(device_id: str, payload: Dict = Body(...)):
        """Toggle the HTTP/JSON output sink for a device. Read-only feed, so no
        poller restart — just flip the flag, persist, and refresh the runtime
        cfg so /api/devices reflects it. Works for the primary too."""
        dev_cfg = config.get_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        enabled = bool(payload.get('enabled', False))
        try:
            config.set_http_output(device_id, enabled)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        new_cfg = config.get_device(device_id)
        registry.replace(device_id, new_cfg)       # keep the running client
        logger.info(f"device {device_id}: http output {'enabled' if enabled else 'disabled'}")
        return {"status": "ok", "device_id": device_id,
                "http_output_enabled": enabled,
                "path": f"/api/meters/{device_id}"}

    @app.post("/api/devices/{device_id}/rest-push")
    def set_device_rest_push(device_id: str, payload: Dict = Body(...)):
        """Configure the generic REST push sink for a device (POST its values to
        an external URL on an interval). Header VALUES are masked on read and
        preserved on save; the pusher thread is (re)started in place."""
        dev = config.get_device(device_id)
        if dev is None:
            raise HTTPException(status_code=404, detail="device not found")
        errors = []
        enabled = bool(payload.get('enabled', False))
        url = str(payload.get('url', '') or '').strip()
        if enabled and not (url.startswith('http://') or url.startswith('https://')):
            errors.append("url: required (http:// or https://) when enabled")
        try:
            interval = int(payload.get('interval_s', 30))
            if interval < 5:
                errors.append("interval_s: minimum 5 seconds")
        except (TypeError, ValueError):
            errors.append("interval_s: must be an integer")
            interval = 30
        fmt = str(payload.get('format', 'native'))
        if fmt not in ('native', 'flat'):
            errors.append("format: must be 'native' or 'flat'")
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})
        # Preserve masked header secrets: a value equal to the mask keeps the stored one.
        old_h = dict(dev.rest_push.get('headers') or {})
        new_h = payload.get('headers')
        headers = old_h
        if isinstance(new_h, dict):
            headers = {k: (old_h.get(k, v) if v == _REST_HDR_MASK else v)
                       for k, v in new_h.items() if k}
        cfg = {'enabled': enabled, 'url': url, 'interval_s': interval,
               'headers': headers, 'format': fmt,
               'verify_tls': bool(payload.get('verify_tls', True)),
               'timeout': int(payload.get('timeout', 10) or 10)}
        try:
            config.set_rest_push(device_id, cfg)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        new_cfg = config.get_device(device_id)
        registry.replace(device_id, new_cfg)       # keep the running client
        _apply_rest_push(new_cfg)
        logger.info(f"device {device_id}: REST push {'enabled' if enabled else 'disabled'}")
        return {"status": "ok", "rest_push": _rest_push_public(new_cfg)}

    @app.post("/api/devices/{device_id}/rest-push/test")
    def test_device_rest_push(device_id: str):
        """Push once right now and report the result — the card's Test button.
        Works even before enabling, as long as a URL is configured."""
        dev = config.get_device(device_id)
        if dev is None:
            raise HTTPException(status_code=404, detail="device not found")
        cfg = _rest_cfg(dev)
        if not str(cfg.get('url', '')).strip():
            raise HTTPException(status_code=422, detail={"errors": ["configure a URL first"]})
        status = (rest_push_manager.push_now(device_id)
                  if rest_push_manager.running(device_id)
                  else RestPusher(device_id, cfg,
                                  _rest_provider(device_id, dev.primary)).push_once())
        return {"status": "ok", **(status or {})}

    # ---- Modbus device auto-discovery -----------------------------------------
    # /api/discover/modbus/* → routes/discovery_routes.py

    # ---- Calculated registers API ---------------------------------------------
    # /api/calculated/* + /api/devices/{id}/calculated(+test) → routes/calculated.py

    # Per-client-IP token bucket for the write API. A write floods the shared
    # Modbus lock (blocking I/O), so a burst can starve the pollers; excess writes
    # get 429 instead of piling up. Generous default so a normal control loop is
    # unaffected; security.write_rate_limit_per_s=0 disables it.
    _write_rl_state: Dict[str, tuple] = {}
    _write_rl_lock = threading.Lock()

    def _write_rate_ok(ip: str) -> bool:
        rate = config.security.write_rate_limit_per_s
        if rate <= 0:
            return True
        cap = max(rate, 1.0)
        now = time.monotonic()
        with _write_rl_lock:
            tokens, last = _write_rl_state.get(ip, (cap, now))
            tokens = min(cap, tokens + (now - last) * rate)
            if tokens < 1.0:
                _write_rl_state[ip] = (tokens, now)
                return False
            _write_rl_state[ip] = (tokens - 1.0, now)
            return True

    @app.get("/api/devices/{device_id}/events")
    def device_events(device_id: str, limit: int = Query(200, ge=1, le=500),
                      level: str = Query("")):
        """What this device's acquisition has been doing, as a readable log.

        The connection has always kept a timestamped ring of its own troubles —
        unreachable, recovered, forced reopen, bus busy, failed batch — plus the
        poller's overrun episodes. Until now the only reader was the alert
        harvester, which fired on them and dropped them, so diagnosing a
        misbehaving endpoint meant grepping container logs for facts the process
        already had in memory and could not be asked for.

        Returned alongside the log is the live per-group state (interval, last
        sweep, reads per sweep, overruns), because "what happened" and "what it
        is doing now" are the same question when an endpoint is struggling.
        """
        _i, dev_cfg, client = _find_device(device_id)
        if client is None:
            raise HTTPException(status_code=404,
                                detail=f"device '{device_id}' is not running")
        try:
            st = client.get_stats() or {}
        except Exception:  # noqa: BLE001 — a stats blip must not 500 the log
            st = {}
        events = list(st.get('events') or [])
        if level:
            keep = {x.strip() for x in level.split(',') if x.strip()}
            events = [e for e in events if e.get('level') in keep]
        # newest first: an operator opening the page is asking "what just
        # happened", not "what happened when the process started"
        events = events[::-1][:limit]
        return {
            "device_id": device_id,
            "name": (dev_cfg.name or dev_cfg.id) if dev_cfg else device_id,
            # A unit reached several ways needs both halves of the story: how
            # each source is doing, and which source currently owns each field.
            # Flat JSON on purpose — a Node-RED http-request node reads this
            # without unwrapping anything.
            "sources": st.get('sources') or [],
            "provenance": st.get('provenance') or {},
            "field_handovers": st.get('field_handovers', 0),
            "events": events,
            "truncated": len(st.get('events') or []) >= 500,
            "poll_groups": st.get('poll_groups_detail') or [],
            "counters": {
                "successful_reads": st.get('successful_reads'),
                "failed_reads": st.get('failed_reads'),
                "batch_failures": getattr(getattr(client, 'connection', None),
                                           'batch_failures', None),
                "forced_reopens": getattr(getattr(client, 'connection', None),
                                          'forced_reopens', None),
                "error_counts": st.get('error_counts') or {},
                "last_latency_ms": st.get('last_latency_ms'),
                "last_success_ts": st.get('last_success_ts'),
                "last_failure_ts": st.get('last_failure_ts'),
            },
        }

    @app.post("/api/devices/{device_id}/write")
    def write_device_register(device_id: str, request: Request, payload: Dict = Body(...)):
        """Write a value to a device (Modbus FC5 coil / FC6+FC16 holding).

        F3a trust model — CAPABILITY IS THE BASE, GUARDS ARE OPT-IN:
        - armed by the security.allow_writes master switch (default off);
        - per-device `write_locked` refuses everything (the primary ships
          locked via security.primary_write_locked — configuration, not code);
        - a register DECLARED writable writes with its template encoding, and
          any guards the user declared (write_min/write_max/write_allowed)
          enforce — they are the user's own declaration, not a product limit;
        - an UNDECLARED register can still be written through the raw path by
          passing `unguarded: true` (+ its data_type/scale) — the deliberate
          statement that no envelope exists;
        - HTTP/JSON devices and input/discrete registers cannot be written;
        - authenticated + rate-limited + audited; the value is read back.
        """
        from .config import normalize_register_type
        from .modbus_client import coil_truthy as _coil_truthy
        if not config.security.allow_writes:
            raise HTTPException(status_code=403, detail={"errors": [
                "Modbus writes are disabled — set security.allow_writes=true to enable"]})
        # Writes must be authenticated so every write is attributable — refuse
        # unless login is enabled or an API key is configured. (Reads stay open
        # on a trusted LAN; writes touch hardware, so they always need a credential.)
        if not (auth_state.enabled or _api_key):
            raise HTTPException(status_code=403, detail={"errors": [
                "writes require authentication — enable login (ui.auth) or set an API_KEY"]})
        if not _write_rate_ok(request.client.host if request.client else "?"):
            raise HTTPException(status_code=429, detail={"errors": [
                f"write rate limit exceeded (> {config.security.write_rate_limit_per_s}/s) — slow down"]})
        _idx, dev_cfg, client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        if dev_cfg.write_locked:
            raise HTTPException(status_code=403, detail={"errors": [
                f"device '{device_id}' is write-locked — unlock it "
                + ("via security.primary_write_locked" if dev_cfg.primary
                   else "in the device's Outputs tab (write protection)")]})
        if dev_cfg.protocol == 'http':
            raise HTTPException(status_code=400, detail={"errors": [
                "HTTP/JSON devices cannot be written"]})
        rtype = normalize_register_type(payload.get('register_type') or payload.get('fc') or 'holding')
        if rtype in ('input', 'discrete'):
            raise HTTPException(status_code=400, detail={"errors": [
                f"{rtype} registers are read-only — write holding (FC16) or coil (FC5)"]})
        try:
            address = int(payload.get('address'))
            assert 0 <= address <= 65535
        except (TypeError, ValueError, AssertionError):
            raise HTTPException(status_code=422, detail={"errors": ["address must be an integer 0..65535"]})
        if 'value' not in payload:
            raise HTTPException(status_code=422, detail={"errors": ["value is required"]})
        # ── write envelope: declared path (guards enforce) or raw path (opt-in) ──
        rule = _write_rule(dev_cfg, address, rtype)
        unguarded = False
        if rule is not None and rule.writable:
            # DECLARED: encoding is a property of the register (its template
            # row), NOT caller-controlled — a declared register always writes
            # with its declared type and scale, so a caller can't corrupt it
            # (or an adjacent register) with a mismatched data_type/word-count.
            data_type = (rule.data_type or 'uint16').lower()
            scale = float(rule.scale if rule.scale is not None else 1.0)
            _w_offset = float(getattr(rule, 'offset', 0.0) or 0.0)
        elif bool(payload.get('unguarded')):
            # RAW (L0): the caller explicitly states no envelope exists. The
            # payload owns the encoding; nothing is clamped or checked beyond
            # type sanity. Master switch + device lock + auth + rate limit +
            # audit still apply — safety by configuration, not prohibition.
            unguarded = True
            rule = None
            data_type = str(payload.get('data_type') or 'uint16').lower()
            from .register_parser import RegisterParser as _RP
            if data_type not in _RP.REGISTER_COUNTS or data_type.startswith('string'):
                raise HTTPException(status_code=422, detail={"errors": [
                    f"data_type '{data_type}' is not writable — use one of: "
                    + ", ".join(sorted(k for k in _RP.REGISTER_COUNTS
                                       if not k.startswith('string')))]})
            try:
                scale = float(payload.get('scale', 1) or 1)
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail={"errors": ["scale must be numeric"]})
            _w_offset = 0.0
        else:
            raise HTTPException(status_code=403, detail={"errors": [
                f"address {address} ({rtype}) is not declared writable on this "
                f"device. Either declare it writable in the device template "
                f"(guards optional — each one you declare is enforced and "
                f"improves the UI), or pass unguarded:true for a raw write "
                f"with your own data_type/scale."]})
        prefer_fc6 = bool(payload.get('prefer_fc6', False))
        if rtype == 'holding':
            try:
                _fval = float(payload.get('value'))
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail={"errors": ["value must be numeric"]})
            # NaN/Inf survive the min/max guards below (every comparison against
            # a non-finite float is False) and struct.pack would ship the raw
            # bit pattern to a real device — reject up front. (The encoder's
            # deliberate NaN sentinel path is unaffected; that never comes from
            # a write request.)
            import math as _math
            if not _math.isfinite(_fval):
                raise HTTPException(status_code=422, detail={"errors": [
                    "value must be a finite number (NaN/Infinity rejected)"]})
            # declared guards enforce — each one is the user's own declaration
            if rule is not None and rule.write_min is not None and _fval < rule.write_min:
                raise HTTPException(status_code=422, detail={"errors": [
                    f"value {_fval} is below the register minimum {rule.write_min}"]})
            if rule is not None and rule.write_max is not None and _fval > rule.write_max:
                raise HTTPException(status_code=422, detail={"errors": [
                    f"value {_fval} is above the register maximum {rule.write_max}"]})
            if (rule is not None and getattr(rule, 'write_allowed', None)
                    and _fval not in [float(v) for v in rule.write_allowed]):
                raise HTTPException(status_code=422, detail={"errors": [
                    f"value {_fval} is not in the register's allowed set "
                    f"{rule.write_allowed}"]})
        _safe = rule.write_safe if rule is not None else None
        lease_ms = int(payload.get('lease_ms', 0) or 0)
        if lease_ms > 0 and _safe is None:
            raise HTTPException(status_code=422, detail={"errors": [
                "lease requested but the register has no write_safe value in "
                "the template (raw unguarded writes cannot lease)"]})
        if client is None:
            raise HTTPException(status_code=409, detail={"errors": ["device is not running"]})
        ok, err, words = client.write_value(address, rtype, data_type,
                                            payload.get('value'), scale=scale,
                                            offset=_w_offset, prefer_fc6=prefer_fc6)
        _who = getattr(request.state, "user", None) or ("api-key" if _api_key else "anon")
        _src = request.client.host if request.client else "?"
        logger.warning("MODBUS WRITE %s: by=%s@%s device=%s addr=%s type=%s dtype=%s value=%r words=%s%s",
                       "OK" if ok else "FAILED", _who, _src, device_id, address, rtype, data_type,
                       payload.get('value'), words, "" if ok else f" err={err}")
        audit_log.append(user=_who, ip=_src, action="modbus write",
                         target=f"{device_id} {rtype}@{address}",
                         status="ok" if ok else "failed",
                         detail={"value": payload.get('value'), "data_type": data_type,
                                 "lease_ms": lease_ms,
                                 **({"unguarded": True} if unguarded else {})})
        if not ok:
            raise HTTPException(status_code=502, detail={"errors": [f"write failed: {err}"]})
        # arm/renew (or cancel) the dead-man lease for this register
        if lease_ms > 0:
            _revert = _make_lease_revert(device_id, rtype, address, data_type, scale,
                                         _safe, off=_w_offset)
            meta = {'device': device_id, 'register_type': rtype, 'address': address,
                    'data_type': data_type, 'scale': scale, 'offset': _w_offset,
                    'safe_value': _safe,
                    'lease_ms': lease_ms}
            _lease_mgr.arm(device_id, rtype, address, lease_ms, _revert, meta=meta)
        else:
            _lease_mgr.clear(device_id, rtype, address)
        read_back, verified = None, None
        try:
            raw_back = client.read_register(address, data_type, rtype)
            if raw_back is not None:
                want = payload.get('value')
                if rtype == 'coil':
                    read_back = bool(raw_back)
                    verified = read_back == _coil_truthy(want)
                else:
                    # read_register returns the RAW value; the write applied *scale,
                    # so divide back to engineering units before comparing to `want`
                    # (otherwise `verified` is always false for any scale != 1).
                    read_back = float(raw_back) / (scale or 1.0) + _w_offset
                    verified = abs(read_back - float(want)) <= max(1e-6, abs(float(want)) * 1e-4)
                # write-then-refresh: reflect the new value in the live store now
                _push_readback_to_store(device_id, address, read_back)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "device": device_id, "address": address, "register_type": rtype,
                "data_type": data_type, "written": payload.get('value'),
                "words": words, "read_back": read_back, "verified": verified,
                "lease_ms": lease_ms or None,
                "reverts_to": _safe if lease_ms > 0 else None,
                "unguarded": unguarded or None}

    @app.post("/api/devices/{device_id}/write-lock")
    @_serialized_mutation
    def set_device_write_lock(device_id: str, request: Request, payload: Dict = Body(...)):
        """Set/clear the per-device write lock (F3a). A locked device refuses
        every write regardless of guards. Endpoint units lock at the ENDPOINT level
        (one physical endpoint — its units lock together); the primary maps to
        security.primary_write_locked. Audited."""
        if 'locked' not in payload:
            raise HTTPException(status_code=422, detail={"errors": ["'locked' (bool) is required"]})
        locked = bool(payload.get('locked'))
        try:
            config.set_write_locked(device_id, locked)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        # refresh the registry snapshot so /api/devices reflects the new lock
        registry.resync(config.devices, modbus_client)
        _who = getattr(request.state, "user", None) or ("api-key" if _api_key else "anon")
        _src = request.client.host if request.client else "?"
        audit_log.append(user=_who, ip=_src, action="write-lock",
                         target=device_id, status="ok",
                         detail={"locked": locked})
        logger.warning("WRITE-LOCK %s: device=%s by=%s@%s",
                       "SET" if locked else "CLEARED", device_id, _who, _src)
        return {"ok": True, "device": device_id, "write_locked": locked}

    @app.get("/api/devices/{device_id}/write-info/{register_type}/{address}")
    def get_write_info(device_id: str, register_type: str, address: int):
        """What a write to (register_type, address) would look like: declared?
        guards? lock state? — drives the write dialog's affordances (bounded
        input / dropdown / raw-with-confirmation)."""
        from .config import normalize_register_type
        _i, dev_cfg, _c = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        rtype = normalize_register_type(register_type)
        rule = _write_rule(dev_cfg, address, rtype)
        declared = bool(rule is not None and rule.writable)
        return {
            "device": device_id, "address": address, "register_type": rtype,
            "writes_enabled": bool(config.security.allow_writes),
            "write_locked": bool(dev_cfg.write_locked),
            "declared": declared,
            "data_type": (rule.data_type if declared else None),
            "scale": (rule.scale if declared else None),
            "write_min": (rule.write_min if declared else None),
            "write_max": (rule.write_max if declared else None),
            "write_allowed": (getattr(rule, 'write_allowed', None) if declared else None),
            "write_safe": (rule.write_safe if declared else None),
        }

    @app.get("/api/writes/leases")
    def list_write_leases():
        """Active write-leases (dead-man switches) with time remaining."""
        return {"leases": _lease_mgr.snapshot()}

    @app.get("/api/devices/{device_id}/poll-groups")
    def get_device_poll_groups(device_id: str):
        """Current poll-group intervals for a device (from its registers file).
        Only groups the device's registers actually USE are returned — the
        merged global groups (e.g. the primary's 0.25 s realtime) used to show
        on every device and read as if the device polled at that rate."""
        _i, dev_cfg, _c = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        regs, groups = config.load_device_registers(dev_cfg)
        used = {r.poll_group or 'normal' for r in regs}
        if used:                       # no registers yet → show all (seed view)
            groups = {n: g for n, g in groups.items() if n in used}
        return {"device": device_id, "poll_groups": {
            n: {"interval": g.interval, "description": g.description} for n, g in groups.items()}}

    @app.post("/api/devices/{device_id}/poll-groups")
    def set_device_poll_groups(device_id: str, payload: Dict = Body(...)):
        """Update a device's poll-group intervals and live-restart its pollers so
        each group re-samples at its own rate (e.g. slow down a fragile HTTP/
        gateway source)."""
        idx, dev_cfg, client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        groups_in = payload.get("poll_groups", payload) or {}
        clean = {}
        for name, g in groups_in.items():
            try:
                iv = float(g.get("interval") if isinstance(g, dict) else g)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"{name}: interval must be a number")
            if not (0.05 <= iv <= 86400):
                raise HTTPException(status_code=400, detail=f"{name}: interval must be 0.05..86400 s")
            desc = g.get("description", "") if isinstance(g, dict) else ""
            clean[str(name)] = {"interval": iv, "description": desc}
        if not clean:
            raise HTTPException(status_code=400, detail="no poll groups given")
        config.save_device_poll_groups(device_id, clean)
        # live-restart this device's pollers with the new intervals
        target = modbus_client if dev_cfg.primary else client
        if target:
            regs, groups = config.load_device_registers(dev_cfg)
            target.update_registers(regs, groups)
            if hasattr(target, 'reload_registers'):
                target.reload_registers()
        return {"status": "ok", "device": device_id, "poll_groups": clean}

    def _vmeter_users_of(device_id: str) -> set:
        """Virtual-meter templates that source from a device — deleting the
        device would leave them permanently stale. Covers the instance-level
        source device AND composite templates with `<device_id>.<register>`
        rows."""
        mgr = getattr(app.state, "vmeter_manager", None)
        users: set = set()
        if mgr is None:
            return users
        for i in mgr._load_cfg().get("instances", []):
            tid = i.get("template")
            if i.get("device") == device_id:
                users.add(tid)
                continue
            try:
                from .virtual_meter import load_template as _lt
                t = _lt(str(mgr.templates_dir / f"{tid}.yaml"))
                pref = device_id + "."
                for r in t.registers:
                    srcs = (r.source if isinstance(r.source, list) else [r.source])
                    if r.source_kind in ("live", "sum") and any(
                            isinstance(s, str) and s.startswith(pref) for s in srcs):
                        users.add(tid)
                        break
            except Exception:  # noqa: BLE001 — a broken template must not block deletes
                pass
        return users

    def _teardown_device(device_id: str, dev_cfg, client) -> None:
        """Stop a device's runtime side effects before it is removed: client,
        REST pusher, dead-man leases, retained HA discovery."""
        if client:
            client.disconnect()
        rest_push_manager.apply(device_id, {'enabled': False}, lambda: {})  # stop pusher
        _lease_mgr.clear_device(device_id)        # drop any dead-man leases
        # clear the device's retained HA discovery so HA drops its entities
        if mqtt_publisher and getattr(mqtt_publisher, "connected", False):
            try:
                regs, _g = config.load_device_registers(dev_cfg)
                pref = mqtt_publisher.config.ha_discovery_prefix
                for r in regs:
                    if not r.mqtt_enabled:
                        continue
                    sn = r.name.lower().replace('[', '_').replace(']', '').replace('_g_', '')
                    mqtt_publisher._publish(
                        f"{pref}/sensor/mbg_dev_{device_id}/{r.address}_{sn}/config",
                        "", retain=True)          # empty retained payload = delete
            except Exception as e:  # noqa: BLE001
                logger.warning(f"clearing discovery for {device_id} failed: {e}")

    @app.delete("/api/devices/{device_id}")
    @_serialized_mutation
    def delete_device(device_id: str):
        """Delete a non-primary device (its selected-registers file is kept
        on disk for safety)."""
        idx, dev_cfg, client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        if dev_cfg.primary:
            raise HTTPException(status_code=422, detail={"errors": [
                "the primary device cannot be deleted"]})
        if dev_cfg.endpoint_id:
            raise HTTPException(status_code=422, detail={"errors": [
                f"device '{device_id}' is materialized from endpoint "
                f"'{dev_cfg.endpoint_id}' — edit or delete the endpoint instead"]})
        # A virtual meter sourcing from this device would go permanently stale
        # (fail-safe, but confusing) — make the dependency explicit instead.
        users = _vmeter_users_of(device_id)
        if users:
            raise HTTPException(status_code=422, detail={"errors": [
                f"device is the source of virtual meter(s): {', '.join(sorted(users))} — "
                "delete or re-point them first"]})
        _teardown_device(device_id, dev_cfg, client)
        config.remove_raw_device(device_id)
        # re-resolve by id (idx may be stale after a concurrent mutation);
        # drops the pair AND its value store in one atomic step
        registry.remove(device_id)
        _sync_device_discovery()
        logger.info(f"device {device_id}: deleted")
        return {"status": "deleted"}

    # ── endpoints: one template × N unit ids behind one endpoint ──────────────
    # An endpoint materializes into N ordinary devices (own socket each — unit-
    # switching on a shared socket corrupts some gateway buffers, and units
    # must fail independently). The devices are managed THROUGH the endpoint:
    # device CRUD refuses them; edit/delete the endpoint instead.

    def _endpoint_bus(p: Dict, units: List[Dict]) -> Dict:
        """What this access point costs, measured on the live wire.

        Every interval an operator can ask for is bounded by one number: how
        long a transaction actually takes here, times how many the sweep needs,
        divided by the lanes serving it. That number is a property of the master
        device and nobody can look it up — so it is measured, and reported next
        to the interval it constrains. ``floor_s`` is the fastest honest cadence
        at the current shape; an interval below it is a promise the wire cannot
        keep.
        """
        conn = p.get('connection', {}) or {}
        host, port = conn.get('host', ''), int(conn.get('port', 502) or 502)
        if not host:
            return {}
        try:
            st = endpoint_bus_stats(host, port)
        except Exception:  # noqa: BLE001 — telemetry must never break the list
            return {}
        st['max_connections'] = max(1, int(conn.get('max_connections', 1) or 1))
        # Transactions per sweep: every unit's batches, as the pollers report
        # them. Missing means nothing has swept yet, and no floor is claimed.
        reads = [u.get('reads_per_cycle') for u in units if u.get('reads_per_cycle')]
        st['reads_per_sweep'] = sum(reads) if reads else None
        if st.get('tx_p95_s') and st['reads_per_sweep']:
            st['floor_s'] = round(
                st['tx_p95_s'] * st['reads_per_sweep'] / st['lanes'], 1)
        else:
            st['floor_s'] = None
        return st

    def _endpoint_entry(p: Dict) -> Dict:
        pid = p.get('id')
        units = []
        for dev in config.endpoint_devices(pid):
            _i, _cfg, client = registry.find(dev.id)
            st = {}
            if client is not None:
                try:
                    st = client.get_stats() or {}
                except Exception:  # noqa: BLE001 — a stats blip must not 500 the list
                    st = {}
            _seen = st.get('last_success_ts')
            _groups = st.get('poll_groups_detail') or []
            units.append({
                'unit_id': dev.connection.unit_id,
                'device_id': dev.id,
                'name': dev.name,
                'enabled': dev.enabled,
                'running': client is not None,
                # the same liveness verdict the MQTT/alert paths use, so the
                # endpoint census can never disagree with the unit's own topics
                'connected': client_is_live(client),
                'health': client_health(client),
                'last_seen': (datetime.fromtimestamp(_seen).isoformat()
                              if _seen else None),
                'staleness_age_s': st.get('staleness_age_s'),
                'poll_rate': st.get('poll_rate'),
                'failed_reads': st.get('failed_reads'),
                # what this unit asks of the wire each sweep, and what its
                # slowest group actually took — the two halves of the budget
                'reads_per_cycle': sum(int(g.get('reads') or 0) for g in _groups),
                'cycle_s': (max((g.get('cycle_s') or 0) for g in _groups)
                            if _groups else None),
                'overruns': sum(int(g.get('overruns') or 0) for g in _groups),
            })
        from .canonical_fields import field_meta
        from .endpoint_aggregator import compute_endpoint_aggregates
        try:
            agg = compute_endpoint_aggregates(config, registry, pid)
        except Exception:  # noqa: BLE001 — aggregates must never break the list
            agg = {}
        bus = _endpoint_bus(p, units)
        return {'id': pid, 'name': p.get('name') or pid,
                'template': p.get('template', ''),
                'enabled': bool(p.get('enabled', True)),
                'connection': dict(p.get('connection', {}) or {}),
                'mqtt': dict(p.get('mqtt', {}) or {}),
                'influxdb': dict(p.get('influxdb', {}) or {}),
                'units': units,
                'aggregates': agg,
                # canonical label/unit/topic per aggregate name, so a view can
                # render "Total active power · W" without re-deriving the
                # vocabulary client-side
                'aggregate_fields': {k: m for k, m in
                                     ((k, field_meta(k)) for k in agg) if m},
                # the endpoint's own settings, so the page can render + edit them
                'aggregates_enabled': bool(p.get('aggregates', True)),
                'write_locked': bool(p.get('write_locked', False)),
                'http_output_enabled': bool((p.get('http_output') or {}).get('enabled')),
                'rest_push': dict(p.get('rest_push', {}) or {}),
                'bus': bus,
                'status': agg.get('status', 'offline' if units else ''),
                'online_units': sum(1 for u in units if u['connected']),
                'total_units': len(units)}

    def _merge_unit_overrides(pid: str, units, prev: Dict = None):
        """Keep a unit's hand-written id/name when the caller sends a bare id.

        ``units: [1, 2, 3]`` is the common shape, and a client that re-sends it
        after an edit used to ERASE `{unit_id: 3, id: inv3, name: East roof}`
        from the config. An explicit dict from the caller always wins."""
        if not prev:
            return units
        overrides = {u['unit_id']: u for u in config._endpoint_units(prev)}
        out = []
        for u in (units or []):
            if isinstance(u, dict):
                out.append(u)                      # explicit wins
                continue
            po = overrides.get(u if isinstance(u, int) else None)
            if po and (po['id'] != f"{pid}-u{po['unit_id']}" or po['name']):
                out.append({'unit_id': po['unit_id'], 'id': po['id'],
                            **({'name': po['name']} if po['name'] else {})})
            else:
                out.append(u)                      # no override → stays bare
        return out

    def _validate_endpoint_payload(payload: Dict, *, existing_id: str = None) -> Dict:
        """Normalize + validate a raw endpoint dict. Raises HTTPException(422)
        with a per-field error list."""
        errors = []
        pid = str(payload.get('id', existing_id or '')).strip().lower()
        if not _DEVICE_ID_RE.match(pid):
            errors.append("id: use a-z 0-9 - _ (2-64 chars, starts alphanumeric)")
        if existing_id and pid != existing_id:
            errors.append("id: cannot be changed after creation")
        if not existing_id and (config.get_raw_endpoint(pid) is not None
                                or registry.has(pid)):
            errors.append(f"id: '{pid}' already exists")
        conn = payload.get('connection', {}) or {}
        protocol = str(conn.get('protocol', 'tcp')).lower()
        if protocol not in ('tcp', 'rtu-tcp'):
            # plain RTU shares one serial line across masters — the same
            # one-master-per-line rule the device CRUD enforces; multi-drop
            # RTU endpoints need the shared-bus arbiter (Tier 3) first.
            errors.append("connection.protocol: must be 'tcp' or 'rtu-tcp'")
        if not str(conn.get('host', '')).strip():
            errors.append("connection.host: required")
        try:
            port = int(conn.get('port', 502))
            if not (1 <= port <= 65535):
                raise ValueError
        except (TypeError, ValueError):
            errors.append("connection.port: must be 1..65535")
        if conn.get('max_connections') is not None:
            # Sockets to a master device are a scarce, shared resource — the
            # datalogger this replaces serves ten and starts refusing near five.
            # A typo asking for thirty would take the access point down for
            # everything else on it, including our own other units.
            try:
                lanes = int(conn['max_connections'])
                if not (1 <= lanes <= 8):
                    raise ValueError
                conn['max_connections'] = lanes
            except (TypeError, ValueError):
                errors.append("connection.max_connections: must be 1..8 "
                              "(measure it: scripts/calibrate_endpoint.py)")
        template_id = str(payload.get('template', '')).strip()
        if template_id and template_registry.get(template_id) is None:
            errors.append(f"template: '{template_id}' not found")
        probe = {'id': pid, 'units': payload.get('units')}
        units = config._endpoint_units(probe)
        if not units:
            errors.append("units: at least one valid unit id (0..255) is required")
        seen_uids, seen_ids = set(), set()
        for u in units:
            if u['unit_id'] in seen_uids:
                errors.append(f"units: duplicate unit_id {u['unit_id']}")
            seen_uids.add(u['unit_id'])
            if not _DEVICE_ID_RE.match(u['id']):
                errors.append(f"units: id '{u['id']}' is invalid")
            if u['id'] in seen_ids:
                errors.append(f"units: duplicate device id '{u['id']}'")
            seen_ids.add(u['id'])
            ex = config.get_device(u['id'])
            if ex is not None and ex.endpoint_id != pid:
                errors.append(f"units: id '{u['id']}' collides with an "
                              f"existing device")
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})
        prev = config.get_raw_endpoint(existing_id) if existing_id else None
        raw = {
            'id': pid,
            'name': str(payload.get('name', '') or pid),
            'template': template_id,
            'enabled': bool(payload.get('enabled', True)),
            'connection': conn,
            'units': _merge_unit_overrides(pid, payload.get('units'), prev),
        }
        if payload.get('aggregates') is not None:
            raw['aggregates'] = bool(payload['aggregates'])
        for opt in ('mqtt', 'influxdb'):
            if payload.get(opt):
                raw[opt] = dict(payload[opt])
        if prev is not None:
            # An edit REPLACES the stored entry, so anything the endpoint owns but
            # the form does not send would vanish: a locked endpoint silently
            # unlocked itself and `aggregates: false` came back on, one save
            # after the operator set them.
            for key in ('write_locked', 'aggregates', 'http_output', 'rest_push'):
                if key not in raw and key in prev:
                    raw[key] = prev[key]
            # a form that omits a whole sink block keeps the stored one — the
            # pin below would otherwise rebuild it from the routing keys ALONE
            # and drop ha_discovery/enabled with it
            for sect in ('mqtt', 'influxdb'):
                if sect not in raw and prev.get(sect):
                    raw[sect] = dict(prev[sect])
            # Routing identity is FIXED after creation, exactly as for a device
            # (see update_device): changing it re-routes every unit's future
            # data and orphans their history + Home Assistant entities.
            for sect, keys in (('mqtt', ('topic_prefix',)),
                               ('influxdb', ('bucket', 'device_tag'))):
                for k in keys:
                    old = (prev.get(sect) or {}).get(k)
                    if old is not None:
                        raw.setdefault(sect, {})[k] = old
        return raw

    def _stop_endpoint_devices(pid: str) -> None:
        """Disconnect + deregister every materialized device of an endpoint."""
        for dev in config.endpoint_devices(pid):
            _i, _cfg, client = registry.find(dev.id)
            _teardown_device(dev.id, dev, client)
            registry.remove(dev.id)

    def _start_endpoint_devices(made) -> List[Dict]:
        """Seed + bucket + start + register each materialized device."""
        out = []
        for dev_cfg in made:
            _autoselect_template_registers(dev_cfg)
            _ensure_device_bucket(dev_cfg)
            client = _start_device_client(dev_cfg)
            registry.add(dev_cfg, client)
            out.append(_device_entry(dev_cfg, client))
        return out

    @app.get("/api/endpoints")
    def list_endpoints():
        """All endpoints with their materialized units and live status."""
        return {"endpoints": [_endpoint_entry(p) for p in config.endpoints]}

    @app.get("/api/endpoints/{endpoint_id}")
    def get_endpoint(endpoint_id: str):
        p = config.get_raw_endpoint(endpoint_id)
        if p is None:
            raise HTTPException(status_code=404, detail="endpoint not found")
        return _endpoint_entry(p)

    @app.post("/api/endpoints/{endpoint_id}/test")
    def test_endpoint(endpoint_id: str):
        """Probe every unit of an endpoint on its shared endpoint.

        One endpoint is one physical endpoint, so a per-unit answer is the only
        way to tell "the datalogger is deaf" from "unit 3 is not configured on
        it". CAUTION: a probe opens ANOTHER Modbus client on that endpoint, and
        dataloggers serve only a few at once — this is an operator-triggered
        check, never a background poll."""
        p = config.get_raw_endpoint(endpoint_id)
        if p is None:
            raise HTTPException(status_code=404, detail="endpoint not found")
        conn = dict(p.get('connection', {}) or {})
        timeout = float(conn.get('timeout', 3) or 3)
        units = []
        for dev in config.endpoint_devices(endpoint_id):
            regs, _g = config.load_device_registers(dev)
            # a real address beats address 0 — some gateways answer 0 blindly
            address = regs[0].address if regs else 0
            res = _modbus_probe(conn, dev.connection.unit_id, timeout, address)
            units.append({'unit_id': dev.connection.unit_id,
                          'device_id': dev.id, **res})
        return {'endpoint': endpoint_id,
                'ok': bool(units) and all(u.get('ok') for u in units),
                'units': units}

    @app.post("/api/endpoints")
    @_serialized_mutation
    def create_endpoint(payload: Dict = Body(...)):
        """Create an endpoint: validate → persist → materialize N devices, seed
        each from the template and hot-start their pollers (no restart)."""
        raw = _validate_endpoint_payload(payload)
        try:
            made = config.upsert_raw_endpoint(raw)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        devices_out = _start_endpoint_devices(made)
        _sync_device_discovery()
        logger.info(f"endpoint {raw['id']}: created with "
                    f"{len(made)} unit(s) (template={raw['template'] or '—'})")
        return {"status": "created", "endpoint": _endpoint_entry(raw),
                "devices": devices_out}

    def _endpoint_runtime_sig(raw: Dict) -> str:
        """Everything a materialized unit is BUILT from. Two definitions with
        the same signature produce identical clients, so an edit that leaves it
        untouched — a rename, the endpoint-totals toggle — must not tear the
        pollers down: a cosmetic save should never punch a hole in acquisition.
        Unit NAMES are deliberately absent (cosmetic; refreshed in place)."""
        return json.dumps({
            'connection': raw.get('connection') or {},
            'template': raw.get('template', ''),
            'enabled': bool(raw.get('enabled', True)),
            'units': [(u['unit_id'], u['id']) for u in config._endpoint_units(raw)],
            'mqtt': raw.get('mqtt') or {},
            'influxdb': raw.get('influxdb') or {},
            'write_locked': bool(raw.get('write_locked', False)),
            'http_output': raw.get('http_output') or {},
            'rest_push': raw.get('rest_push') or {},
        }, sort_keys=True, default=str)

    # exposed for tests: the restart-or-not contract is worth pinning
    app.state.endpoint_runtime_sig = _endpoint_runtime_sig

    @app.put("/api/endpoints/{endpoint_id}")
    @_serialized_mutation
    def update_endpoint(endpoint_id: str, payload: Dict = Body(...)):
        """Update an endpoint. An edit that changes what the units are built from
        re-materializes them (stop → rebuild → start); an edit that only changes
        endpoint-level settings keeps every poller running and just refreshes the
        config behind it."""
        prev_raw = config.get_raw_endpoint(endpoint_id)
        if prev_raw is None:
            raise HTTPException(status_code=404, detail="endpoint not found")
        raw = _validate_endpoint_payload(payload, existing_id=endpoint_id)
        settings_only = _endpoint_runtime_sig(prev_raw) == _endpoint_runtime_sig(raw)
        if not settings_only:
            _stop_endpoint_devices(endpoint_id)
        try:
            made = config.upsert_raw_endpoint(raw)
        except ValueError as e:
            # config restored the previous definition — restart its units so
            # a failed edit doesn't leave the endpoint stopped
            if not settings_only:
                _start_endpoint_devices(config.endpoint_devices(endpoint_id))
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        if settings_only:
            # the DeviceConfig objects were rebuilt by upsert; swap them in
            # behind the RUNNING clients so a rename shows up without a restart
            for dev_cfg in made:
                registry.replace(dev_cfg.id, dev_cfg)
            devices_out = [_device_entry(d, registry.find(d.id)[2]) for d in made]
        else:
            devices_out = _start_endpoint_devices(made)
        _sync_device_discovery()
        logger.info("endpoint %s: updated (%d unit(s))%s", endpoint_id, len(made),
                    " — settings only, pollers kept running" if settings_only else "")
        return {"status": "updated", "endpoint": _endpoint_entry(raw),
                "devices": devices_out}

    @app.delete("/api/endpoints/{endpoint_id}")
    @_serialized_mutation
    def delete_endpoint(endpoint_id: str):
        """Delete an endpoint and stop all its units (their register files are
        kept on disk, so re-adding the endpoint restores the selection)."""
        if config.get_raw_endpoint(endpoint_id) is None:
            raise HTTPException(status_code=404, detail="endpoint not found")
        used = {u for d in config.endpoint_devices(endpoint_id)
                for u in _vmeter_users_of(d.id)}
        if used:
            raise HTTPException(status_code=422, detail={"errors": [
                f"endpoint units feed virtual meter(s): {', '.join(sorted(used))} — "
                "delete or re-point them first"]})
        _stop_endpoint_devices(endpoint_id)
        try:
            removed = config.delete_endpoint(endpoint_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        _sync_device_discovery()
        logger.info(f"endpoint {endpoint_id}: deleted ({len(removed)} unit(s))")
        return {"status": "deleted", "removed_devices": removed}

    def _modbus_probe(conn: Dict, unit_id: int, timeout: float,
                      address: int = 0) -> Dict:
        """One ad-hoc Modbus probe (TCP or RTU): connect + FC3 read. ANY
        protocol-level answer (even a Modbus exception) proves a live device;
        only silence/timeouts fail. Used by the wizard's Test connection button."""
        from pymodbus import FramerType
        from pymodbus.client import ModbusTcpClient, ModbusSerialClient
        from pymodbus.pdu import ExceptionResponse
        proto = str(conn.get('protocol', 'tcp')).lower()
        rtu = proto == 'rtu'
        if not rtu:
            # same LAN-egress policy as every /api/discover/* route — without
            # it the probe doubles as an internal TCP port scanner (audit L1)
            from .discovery import lan_host_error
            _e = lan_host_error(conn.get('host', ''),
                                config.security.allow_nonlan_http_devices)
            if _e:
                return {"ok": False, "message": f"blocked: {_e}"}
        t0 = time.perf_counter()
        if rtu:
            where = f"{conn.get('serial_port','')}@{conn.get('baudrate',9600)}"
            c = ModbusSerialClient(port=conn.get('serial_port', ''),
                                   baudrate=int(conn.get('baudrate', 9600)),
                                   parity=str(conn.get('parity', 'N')),
                                   stopbits=int(conn.get('stopbits', 1)),
                                   bytesize=int(conn.get('bytesize', 8)),
                                   timeout=timeout)
        else:
            where = f"{conn.get('host','')}:{conn.get('port',502)}"
            # rtu-tcp: RTU frames over a raw TCP socket (serial-over-TCP bridge)
            _framer = {'framer': FramerType.RTU} if proto == 'rtu-tcp' else {}
            if proto == 'rtu-tcp':
                # audit DP-19: the bridge kicks the OLDER client — a test
                # against an endpoint a running device is polling would boot
                # the live poller mid-transaction and push it into the slow
                # timeout path. Refuse; the operator can disable the device
                # first if a raw probe is really needed.
                _th = str(conn.get('host', '')).strip().lower()
                _tp = int(conn.get('port', 502) or 502)
                for _d in config.devices:
                    if (getattr(_d, 'enabled', False)
                            and getattr(_d, 'protocol', '') == 'rtu-tcp'
                            and str(getattr(_d.connection, 'host', '')).strip().lower() == _th
                            and int(getattr(_d.connection, 'port', 0) or 0) == _tp):
                        return {"ok": False, "error":
                                f"endpoint {where} is being live-polled by "
                                f"device '{_d.id}' — a test would kick its "
                                f"connection (bridge is single-client). "
                                f"Disable the device first."}
            c = ModbusTcpClient(host=conn.get('host', ''),
                                port=int(conn.get('port', 502)), timeout=timeout,
                                **_framer)
        try:
            if not c.connect():
                return {"ok": False,
                        "message": (f"Serial open of {where} failed — check the port/permissions"
                                    if rtu else
                                    f"TCP connect to {where} failed — check IP/port/firewall")}
            rr = c.read_holding_registers(address=address, count=2, device_id=unit_id)
            lat = round((time.perf_counter() - t0) * 1000, 1)
            if not rr.isError():
                return {"ok": True, "latency_ms": lat,
                        "message": f"Device answered in {lat} ms (unit {unit_id}, FC3 @ {address})"}
            if isinstance(rr, ExceptionResponse):
                return {"ok": True, "latency_ms": lat,
                        "message": f"Device is alive (answered with Modbus exception "
                                   f"code {getattr(rr, 'exception_code', '?')} @ {address} "
                                   f"— try another register address)"}
            return {"ok": False,
                    "message": f"TCP connected but no Modbus response from unit {unit_id} "
                               f"(timeout) — check unit ID"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": f"probe failed: {e}"}
        finally:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass

    @app.post("/api/devices/test")
    def test_device_adhoc(payload: Dict = Body(...)):
        """Wizard step-1 probe for a NOT-yet-saved device (TCP or RTU)."""
        conn = payload.get('connection', payload) or {}
        protocol = str(conn.get('protocol', 'tcp')).lower()
        if protocol == 'http':
            url = str(conn.get('url', '')).strip()
            if not (url.startswith('http://') or url.startswith('https://')):
                raise HTTPException(status_code=422, detail={"errors": ["connection.url required (http:// or https://)"]})
            from .http_client import HttpClient
            # If a template is named, resolve ITS json_paths against the live
            # response so the test reports "N/M paths resolved" (proves the map
            # fits the endpoint), not just that the URL is reachable.
            tpl_id = str(payload.get('template') or conn.get('template') or '').strip()
            tpl = template_registry.get(tpl_id) if tpl_id else None
            regs = list(tpl.registers) if tpl else []
            return HttpClient({'url': url, 'timeout': conn.get('timeout', 8)}, regs, {},
                              allow_nonlan=config.security.allow_nonlan_http_devices).test_read()
        if protocol == 'mqtt':
            broker = str(conn.get('broker', '')).strip()
            topic = str(conn.get('topic', '')).strip()
            if not broker or not topic:
                raise HTTPException(status_code=422, detail={"errors": ["connection.broker and connection.topic required"]})
            import paho.mqtt.client as mqtt
            got = {"connected": False, "msg": None, "topic": None}
            ev = threading.Event()
            cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            if conn.get('username'):
                cli.username_pw_set(str(conn.get('username')), str(conn.get('password', '')))
            if conn.get('tls'):
                try:
                    cli.tls_set()
                except Exception as e:  # noqa: BLE001
                    # fail CLOSED (external audit): falling through would send
                    # the operator's broker credentials over cleartext and
                    # report the TLS config as working
                    return {"ok": False, "error": f"TLS setup failed: {e}"}

            def _oc(c, u, f, rc, props=None):
                got["connected"] = True
                c.subscribe(topic)

            def _om(c, u, m):
                got["msg"] = m.payload.decode('utf-8', 'replace')[:400]
                got["topic"] = m.topic
                ev.set()
            cli.on_connect = _oc
            cli.on_message = _om
            try:
                cli.connect(broker, int(conn.get('port', 1883)), keepalive=10)
                cli.loop_start()
                ev.wait(timeout=3.0)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"connect failed: {e}"}
            finally:
                # both halves, on EVERY path — a connect that half-succeeded
                # must not leak its socket/network thread
                try:
                    cli.loop_stop()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    cli.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            if not got["connected"]:
                return {"ok": False, "message": "could not connect to the broker"}
            if got["msg"] is not None:
                return {"ok": True, "connected": True, "message_received": True,
                        "topic": got["topic"], "sample": got["msg"],
                        "message": f"connected · message on {got['topic']}: {got['msg'][:80]}"}
            return {"ok": True, "connected": True, "message_received": False,
                    "message": "connected — no message on the topic within 3s (it may be idle)"}
        if protocol == 'rtu' and not str(conn.get('serial_port', '')).strip():
            raise HTTPException(status_code=422, detail={"errors": ["connection.serial_port required"]})
        if protocol in ('tcp', 'rtu-tcp') and not str(conn.get('host', '')).strip():
            raise HTTPException(status_code=422, detail={"errors": ["connection.host required"]})
        return _modbus_probe(conn, int(conn.get('unit_id', 1)),
                             float(conn.get('timeout', 3)),
                             int(payload.get('address', 0)))

    @app.post("/api/devices/{device_id}/test")
    def test_device(device_id: str):
        """Probe a saved device (TCP or RTU). Uses its first selected register
        (a real address beats address 0) when one exists."""
        _idx, dev_cfg, _client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        regs, _g = config.load_device_registers(dev_cfg)
        address = regs[0].address if regs else 0
        c = dev_cfg.connection
        conn = {"protocol": dev_cfg.protocol, "host": c.host, "port": c.port,
                "serial_port": c.serial_port, "baudrate": c.baudrate,
                "parity": c.parity, "stopbits": c.stopbits, "bytesize": c.bytesize}
        return _modbus_probe(conn, c.unit_id, float(c.timeout), address)

    # /api/device-templates/* → routes/device_templates.py

    # /health → routes/status_routes.py

    # /api/languages(+{code}) → routes/languages.py

    # /api/config (+modbus/mqtt/influxdb/ui-security/security/env-overrides) → routes/config.py

    # /api/registers/* → routes/registers_routes.py

    # /api/values*, /api/meters*, /api/history* → routes/values_routes.py

    # /api/energy/* → routes/energy.py

    # /api/fronius/discover → routes/discovery_routes.py

    # /api/virtual-meters/* → routes/vmeters.py

    # /api/query/*, /api/search, /api/poll-groups → routes/registers_routes.py

    # --- Config Management ---

    # /api/config, /api/config/{env-overrides,modbus,mqtt,ui-security,security,influxdb}
    #   → routes/config.py  (persist-only). The rest below rebind publishers /
    #   restart clients / touch the data path, so they stay in create_api.

    @app.post("/api/config/apply")
    def apply_config():
        """Apply configuration changes by reconnecting all services."""
        nonlocal mqtt_publisher, influxdb_publisher

        results = {"modbus": False, "mqtt": False, "influxdb": False}

        try:
            # Reconnect Modbus
            if modbus_client:
                modbus_client.update_config(config.modbus)
                modbus_client.update_registers(config.selected_registers, config.poll_groups)
                results["modbus"] = modbus_client.reconnect()
                # drop store ghosts at deselected addresses (audit M2) — an
                # apply after a config import/restore can change the register set
                from .device_registry import purge_deselected
                purge_deselected(current_values, config.selected_registers)

            # Handle MQTT - create if needed
            if config.mqtt.enabled:
                if mqtt_publisher:
                    mqtt_publisher.update_config(config.mqtt)
                    mqtt_publisher.update_registers(config.selected_registers)
                    results["mqtt"] = mqtt_publisher.reconnect()
                else:
                    # Create new MQTT publisher (MQTT was off at boot / just
                    # enabled) and re-wire it into the virtual-meter manager so
                    # its state + HA discovery publish through the LIVE ref
                    # instead of the old None — otherwise vmeter state/discovery
                    # stays dark until a restart.
                    mqtt_publisher = MQTTPublisher(
                        config=config.mqtt,
                        registers=config.selected_registers,
                        publish_mode=config.mqtt.publish_mode,
                        heartbeat_interval=getattr(config.mqtt, 'heartbeat_interval', 0),
                    )
                    ctx.mqtt_publisher = mqtt_publisher   # mirror for route modules
                    alert_mgr.mqtt = mqtt_publisher       # else alerts publish to the dead ref
                    vmgr = getattr(app.state, "vmeter_manager", None)
                    if vmgr is not None:
                        vmgr.mqtt_publisher = mqtt_publisher
                    # Connect in background
                    def connect_mqtt():
                        if mqtt_publisher.connect():
                            logger.info("MQTT connected after enable")
                            if config.mqtt.ha_discovery_enabled:
                                mqtt_publisher.publish_ha_discovery()
                            # (re)start the vmeter state publisher on the new ref
                            if vmgr is not None:
                                try:
                                    vmgr.start_state_publisher()
                                    vmgr.publish_ha_discovery()
                                except Exception as e:  # noqa: BLE001
                                    logger.warning("vmeter re-wire after MQTT enable failed: %s", e)
                    threading.Thread(target=connect_mqtt, daemon=True).start()
                    results["mqtt"] = True
            elif mqtt_publisher:
                # Disable MQTT
                mqtt_publisher.disconnect()
                results["mqtt"] = True

            # Handle InfluxDB - create if needed
            if config.influxdb.enabled:
                if influxdb_publisher:
                    influxdb_publisher.update_config(config.influxdb)
                    influxdb_publisher.update_registers(config.selected_registers)
                    results["influxdb"] = influxdb_publisher.reconnect()
                else:
                    # Create new InfluxDB publisher
                    influxdb_publisher = InfluxDBPublisher(
                        config=config.influxdb,
                        registers=config.selected_registers,
                        publish_mode=config.influxdb.publish_mode,
                        buffer_dir=config.config_path.parent
                    )
                    ctx.influxdb_publisher = influxdb_publisher   # mirror for route modules
                    results["influxdb"] = influxdb_publisher.connected
                    logger.info(f"InfluxDB publisher created, connected: {influxdb_publisher.connected}")
            elif influxdb_publisher:
                # Disable InfluxDB
                influxdb_publisher.close()
                results["influxdb"] = True

            return {
                "status": "ok",
                "results": results,
                "message": "Configuration applied"
            }
        except Exception as e:
            logger.exception("Error applying config")
            raise HTTPException(status_code=500,
                                detail=f"internal error ({type(e).__name__}) — see the server log")

    @app.post("/api/config/reload-registers")
    def reload_registers():
        """Reload registers without full reconnect."""
        try:
            # Reload config
            config._load_selected_registers()

            # Update clients
            if modbus_client:
                modbus_client.update_registers(config.selected_registers, config.poll_groups)
                modbus_client.reload_registers()
            # drop store ghosts at deselected addresses (audit M2)
            from .device_registry import purge_deselected
            purge_deselected(current_values, config.selected_registers)

            if mqtt_publisher:
                mqtt_publisher.update_registers(config.selected_registers)

            if influxdb_publisher:
                influxdb_publisher.update_registers(config.selected_registers)

            return {
                "status": "ok",
                "count": len(config.selected_registers),
                "message": "Registers reloaded"
            }
        except Exception as e:
            logger.exception("Error reloading registers")
            raise HTTPException(status_code=500,
                                detail=f"internal error ({type(e).__name__}) — see the server log")

    # --- Config backup / restore (ZIP) ---

    import io
    import zipfile
    import yaml as _yaml
    from pathlib import Path as _Path

    BACKUP_VERSION = 1
    # secret keys stripped from config.yaml on export unless include_secrets=true
    _SECRET_PATHS = [("mqtt", "password"), ("influxdb", "token"),
                     ("esphome", "password"),
                     ("ui", "auth", "password"), ("ui", "auth", "viewer_password"),
                     ("ui", "auth", "operator_password"),
                     ("alerts", "webhook_headers"),   # holds the webhook X-API-Key/bearer
                     ("rest_push", "headers")]        # primary device REST-push auth headers

    def _strip_device_secrets(data: dict):
        """Redact per-device secrets that live inside the devices[] list: the MQTT
        broker / HTTP-input password + headers, REST-push auth headers, and any
        URL that may embed credentials. These are NOT top-level so _strip_paths
        misses them. The import side (_merge_devices) recognizes the redacted
        URL forms and keeps the live originals on a merge-import."""
        from .redact import redact_url
        for dev in (data.get("devices") or []):
            if not isinstance(dev, dict):
                continue
            conn = dev.get("connection")
            if isinstance(conn, dict):
                conn.pop("password", None)           # MQTT-input broker password
                conn.pop("headers", None)            # HTTP-input auth headers
                if conn.get("url"):                  # HTTP URL may hold userinfo/token
                    conn["url"] = redact_url(conn["url"])
            rp = dev.get("rest_push")
            if isinstance(rp, dict):
                rp.pop("headers", None)              # REST-push auth headers
                if rp.get("url"):                    # push target may hold a token
                    rp["url"] = redact_url(rp["url"])
    # network identity kept out of a portable backup (clone-to-another-host safe)
    _IDENTITY_PATHS = [("ui", "host"), ("ui", "port")]

    def _strip_paths(d: dict, paths):
        for p in paths:
            node = d
            for k in p[:-1]:
                node = node.get(k) if isinstance(node, dict) else None
                if node is None:
                    break
            if isinstance(node, dict):
                node.pop(p[-1], None)

    @app.get("/api/config/export")
    def export_config(request: Request,
                      include_secrets: bool = Query(default=False),
                      include_identity: bool = Query(default=False)):
        """Download a ZIP backup: config.yaml (secrets/identity stripped by
        default), every device's selected_registers.json, user device
        templates, and virtual_meters.yaml. Restores via /api/config/import."""
        # A backup WITH secrets carries the MQTT/InfluxDB credentials and the
        # admin/viewer password hashes, so it always needs a credential — a
        # read-only viewer must not walk away with the admin hash. Export is a GET,
        # and the API-key middleware only guards state-changing methods, so we
        # must check the key HERE too (else an api-key-protected, auth-off box
        # would still leak secrets over a plain GET).
        if include_secrets or include_identity:
            _is_admin = auth_state.enabled and getattr(request.state, "role", None) == "admin"
            _key_ok = bool(_api_key) and hmac.compare_digest(
                request.headers.get("X-API-Key", ""), _api_key)
            if not (_is_admin or _key_ok):
                raise HTTPException(status_code=403, detail={"errors": [
                    "exporting secrets/identity requires the admin role or a valid API key"]})
        if include_secrets or include_identity:
            audit_log.append(user=getattr(request.state, "user", "") or "-",
                             ip=request.client.host if request.client else "-",
                             action="config export", status="ok",
                             detail={"include_secrets": include_secrets,
                                     "include_identity": include_identity})
        cfg_dir = config.config_path.parent
        buf = io.BytesIO()
        manifest = {"backup_version": BACKUP_VERSION,
                    "app_version": __import__("multibus").__version__,
                    "include_secrets": include_secrets,
                    "include_identity": include_identity,
                    "devices": [d.id for d in config.devices]}
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            # config.yaml (sanitized copy — never mutate the live file)
            if config.config_path.exists():
                data = _yaml.safe_load(config.config_path.read_text()) or {}
                if not include_secrets:
                    _strip_paths(data, _SECRET_PATHS)
                    _strip_device_secrets(data)
                    # top-level URLs can embed credentials too (userinfo /
                    # ?token=…) — same redaction, same import re-injection
                    from .redact import redact_url as _redact_url
                    for _sect in ("rest_push", "influxdb"):
                        _node = data.get(_sect)
                        if isinstance(_node, dict) and _node.get("url"):
                            _node["url"] = _redact_url(_node["url"])
                    _al = data.get("alerts")
                    if isinstance(_al, dict) and _al.get("webhook_url"):
                        from urllib.parse import urlsplit, urlunsplit
                        u = urlsplit(str(_al["webhook_url"]))
                        if u.query or "@" in u.netloc:      # token in query/userinfo
                            host = u.netloc.rsplit("@", 1)[-1]
                            _al["webhook_url"] = urlunsplit((u.scheme, host, u.path, "", ""))
                if not include_identity:
                    _strip_paths(data, _IDENTITY_PATHS)
                z.writestr("config.yaml", _yaml.dump(data, default_flow_style=False,
                                                     allow_unicode=True, sort_keys=False))
            # per-device selected registers
            for d in config.devices:
                p = config.device_registers_path(d.id)
                if p.exists():
                    z.writestr(f"devices/{d.id}/selected_registers.json", p.read_text())
            # user device templates
            from .device_template import USER_DIR as _UDIR
            udir = _Path(getattr(template_registry, "user_dir", _UDIR))
            if udir.is_dir():
                for f in udir.iterdir():
                    if f.suffix.lower() in (".json", ".yaml", ".yml"):
                        z.writestr(f"device_templates/{f.name}", f.read_text())
            # virtual meters (instances + user templates)
            vm = cfg_dir / "virtual_meters.yaml"
            if vm.exists():
                z.writestr("virtual_meters.yaml", vm.read_text())
            _vtpl = cfg_dir / "templates"
            if _vtpl.is_dir():
                for f in sorted(_vtpl.iterdir()):
                    if f.suffix.lower() in (".yaml", ".yml"):
                        z.writestr(f"templates/{f.name}", f.read_text())
            # calculated presets + builder hardware profiles travel with every
            # backup; the passkey registry (identity) only when secrets do
            for name in ("calculated_templates.json", "builder_profiles.json"):
                p = cfg_dir / name
                if p.exists():
                    z.writestr(name, p.read_text())
            if include_secrets:
                p = cfg_dir / "passkeys.json"
                if p.exists():
                    z.writestr("passkeys.json", p.read_text())
            z.writestr("manifest.json", json.dumps(manifest, indent=1))
        buf.seek(0)
        return Response(
            content=buf.getvalue(), media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="multibus-config-backup.zip"'})

    @app.post("/api/config/import")
    async def import_config(request: Request, apply: bool = Query(default=True)):
        """Restore a ZIP backup produced by /api/config/export. The raw ZIP is
        sent as the request body (application/zip) — no multipart dependency.
        Writes the files back under the config dir (paths validated — no
        traversal), reloads config and hot-applies. Secrets/identity absent
        from the backup keep their current values (the live config.yaml is
        merged, not blindly overwritten)."""
        # Bound the input: a config backup is tiny, so cap the body and the total
        # uncompressed size to refuse an oversized upload / ZIP bomb (OOM guard).
        _MAX_IMPORT = 25 * 1024 * 1024        # 25 MB compressed
        _cl = request.headers.get('content-length', '')
        if _cl.isdigit() and int(_cl) > _MAX_IMPORT:
            raise HTTPException(status_code=413, detail={"errors": ["backup too large (max 25 MB)"]})
        raw = await request.body()
        if len(raw) > _MAX_IMPORT:
            raise HTTPException(status_code=413, detail={"errors": ["backup too large (max 25 MB)"]})
        cfg_dir = config.config_path.parent
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except Exception:
            raise HTTPException(status_code=422, detail={"errors": ["not a valid ZIP file"]})
        if sum(getattr(zi, 'file_size', 0) for zi in zf.infolist()) > 50 * 1024 * 1024:
            raise HTTPException(status_code=422, detail={"errors": ["archive expands too large (ZIP bomb?)"]})
        from .device_template import USER_DIR as _UDIR
        # safety net: the pre-import state is one click away if the backup is bad
        try:
            snapshot_store.create("pre-import",
                                  user=getattr(request.state, "user", "") or "")
        except Exception:  # noqa: BLE001
            logger.exception("pre-import snapshot failed")
        try:
            # sanitized backups merge config.yaml over the live file so stripped
            # secrets/identity survive; per-device register paths are mapped
            # through device_registers_path (the primary keeps its legacy root
            # file — writing devices/<primary>/ would be a dead copy).
            summary = _write_bundle_files(
                zf, cfg_dir=cfg_dir,
                user_tpl_dir=_Path(getattr(template_registry, "user_dir", _UDIR)),
                registers_path_for=config.device_registers_path,
                replace_config=False)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        _reload_from_disk(apply)
        summary["note"] = ("imported; a restart is recommended so newly-added "
                           "devices start polling") if len(config.devices) > 1 else "imported"
        logger.info(f"config import: {summary}")
        return {"status": "ok", **summary}

    def _reload_from_disk(apply: bool = True) -> None:
        """Reload config + registries from disk and hot-apply — the shared tail
        of backup import and snapshot restore (restart-lite)."""
        config.load()
        # hot-apply imported login settings so a backup that enables/disables auth
        # or rotates credentials takes effect immediately (the middleware reads
        # auth_state live) — matches update_ui_security(), not a restart-only change
        auth_state.reload(config.ui)
        template_registry.reload()
        if apply:
            # rebuild device runtime like a restart-lite: reconnect primary +
            # reload its registers; other devices are picked up on next restart
            if modbus_client:
                modbus_client.update_config(config.modbus)
                modbus_client.update_registers(config.selected_registers, config.poll_groups)
                modbus_client.reconnect()
            if mqtt_publisher:
                mqtt_publisher.update_config(config.mqtt)
                mqtt_publisher.update_registers(config.selected_registers)
            if influxdb_publisher:
                influxdb_publisher.update_config(config.influxdb)
                influxdb_publisher.update_registers(config.selected_registers)

    # --- Config snapshots: list / create / download / delete / restore ---

    @app.get("/api/config/snapshots")
    def list_snapshots():
        """Automatic + manual snapshots, newest first (LKG on top when present)."""
        return {"snapshots": snapshot_store.list(), "keep": snapshot_store.keep}

    @app.post("/api/config/snapshots")
    def create_snapshot(request: Request, payload: Dict = Body(default={})):
        """Take a manual snapshot of the current config bundle."""
        meta = snapshot_store.create("manual",
                                     user=getattr(request.state, "user", "") or "",
                                     note=str(payload.get("note", "") or ""))
        return {"status": "ok", "snapshot": meta}

    @app.get("/api/config/snapshots/{sid}/download")
    def download_snapshot(request: Request, sid: str):
        """Download a snapshot ZIP. Snapshots are FULL-FIDELITY (secrets and
        identity included — they are local restore points), so downloading one
        is gated exactly like a with-secrets export."""
        # UNCONDITIONAL gate, exactly like the with-secrets export: a snapshot
        # is full-fidelity (MQTT password, InfluxDB token, password hashes), so
        # proving admin (or a valid API key) is required REGARDLESS of whether
        # login is enabled. The old `auth_state.enabled and …` made the whole
        # check vanish on an auth-off box — any LAN peer could GET the secrets.
        _is_admin = auth_state.enabled and getattr(request.state, "role", None) == "admin"
        _key_ok = bool(_api_key) and hmac.compare_digest(
            request.headers.get("X-API-Key", ""), _api_key)
        if not (_is_admin or _key_ok):
            raise HTTPException(status_code=403, detail={"errors": [
                "downloading a snapshot requires the admin role or a valid API key"]})
        p = snapshot_store.get_path(sid)
        if p is None:
            raise HTTPException(status_code=404, detail=f"unknown snapshot {sid!r}")
        audit_log.append(user=getattr(request.state, "user", "") or "-",
                         ip=request.client.host if request.client else "-",
                         action="snapshot download", target=sid, status="ok")
        return Response(content=p.read_bytes(), media_type="application/zip",
                        headers={"Content-Disposition":
                                 f'attachment; filename="config-snapshot-{sid}.zip"'})

    @app.delete("/api/config/snapshots/{sid}")
    def delete_snapshot(sid: str):
        if sid == "lkg":
            raise HTTPException(status_code=400,
                                detail="the last-known-good snapshot cannot be deleted")
        if not snapshot_store.delete(sid):
            raise HTTPException(status_code=404, detail=f"unknown snapshot {sid!r}")
        return {"status": "deleted"}

    @app.get("/api/config/snapshots/{sid}/diff")
    def diff_snapshot(sid: str, against: str = Query(default="live")):
        """Semantic, secrets-masked diff: what changed SINCE this snapshot
        (against=live, default) or between two snapshots (against=<sid>).
        A rollback to the snapshot would undo exactly these changes."""
        from .snapshots import diff_bundles
        p = snapshot_store.get_path(sid)
        if p is None:
            raise HTTPException(status_code=404, detail=f"unknown snapshot {sid!r}")
        if against == "live":
            newer = snapshot_store.build_bundle_bytes()
        else:
            p2 = snapshot_store.get_path(against)
            if p2 is None:
                raise HTTPException(status_code=404, detail=f"unknown snapshot {against!r}")
            newer = p2.read_bytes()
        return {"base": sid, "against": against,
                **diff_bundles(p.read_bytes(), newer)}

    @app.post("/api/config/snapshots/{sid}/restore")
    def restore_snapshot(request: Request, sid: str, apply: bool = Query(default=True)):
        """Roll the config back to a snapshot. The current state is snapshotted
        first ('pre-restore'), so a rollback is itself reversible. Snapshots are
        verbatim, so config.yaml is REPLACED, not merged."""
        p = snapshot_store.get_path(sid)
        if p is None:
            raise HTTPException(status_code=404, detail=f"unknown snapshot {sid!r}")
        try:
            snapshot_store.create("pre-restore",
                                  user=getattr(request.state, "user", "") or "")
        except Exception:  # noqa: BLE001
            logger.exception("pre-restore snapshot failed")
        try:
            with zipfile.ZipFile(io.BytesIO(p.read_bytes())) as zf:
                from .device_template import USER_DIR as _UDIR
                summary = _write_bundle_files(
                    zf, cfg_dir=config.config_path.parent,
                    user_tpl_dir=_Path(getattr(template_registry, "user_dir", _UDIR)),
                    registers_path_for=config.device_registers_path,
                    replace_config=True)
        except (ValueError, zipfile.BadZipFile) as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        _reload_from_disk(apply)
        event_log.add("warn", "snapshots", f"config rolled back to snapshot {sid}")
        logger.warning(f"config restored from snapshot {sid}: {summary}")
        # _reload_from_disk hot-applies the PRIMARY only; non-primary devices are
        # reconstructed on the next restart, so tell the operator when one is
        # needed (more than one device present) rather than leave it implicit.
        restart_required = len(config.devices) > 1
        return {"status": "ok", "restored": sid, "restart_required": restart_required,
                **summary}

    # --- Auth (login / logout / status) ---

    # /api/auth/* → routes/auth_routes.py

    # --- WebSocket ---

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """WebSocket endpoint for real-time data."""
        # The HTTP middlewares (IP allowlist + auth) do not run for the websocket
        # ASGI scope, so enforce both here before streaming the live value cache.
        peer = websocket.client.host if websocket.client else ""
        if not _ip_allowed(peer):
            await websocket.close(code=1008)       # policy violation
            return
        if auth_state is not None and auth_state.enabled:
            token = websocket.cookies.get(_auth.COOKIE_NAME, "")
            if auth_state.role_for(token) is None:
                await websocket.close(code=1008)
                return
        # WebSockets are exempt from the browser same-origin policy, so a page the
        # operator visits could otherwise open ws://<lan-ip>/ws and read the live
        # telemetry stream. Reject a cross-origin Origin (host:port mismatch); a
        # non-browser client (no Origin header) is allowed, like the HTTP guard.
        _origin = websocket.headers.get("origin")
        if _origin:
            from urllib.parse import urlparse
            _o = urlparse(_origin).netloc.lower()
            if _o and _o != websocket.headers.get("host", "").lower():
                await websocket.close(code=1008)
                return
        if not await ws_manager.connect(websocket):
            return                       # refused at the connection cap
        try:
            # Send initial data
            await websocket.send_json({
                'type': 'init',
                'device': config.primary_device.id,   # the snapshot is the primary's
                # snapshot, NOT the live dict (external audit: json.dumps
                # walking the live store while pollers insert raises
                # 'dictionary changed size' and drops the socket — the same
                # bug values_routes.py already documents and fixes)
                'values': dict(current_values),
                'timestamp': last_update['timestamp'],
            })

            # Keep connection alive
            while True:
                try:
                    # Wait for messages (ping/pong handled automatically)
                    data = await asyncio.wait_for(websocket.receive_text(), timeout=30)

                    # Handle client messages
                    try:
                        msg = json.loads(data)
                        if msg.get('type') == 'ping':
                            await websocket.send_json({'type': 'pong'})
                        elif msg.get('type') == 'subscribe':
                            # Client can subscribe to specific addresses
                            pass
                    except json.JSONDecodeError:
                        pass

                except asyncio.TimeoutError:
                    # Send ping — unless the socket already closed under us
                    # (broadcast-path kick or the client vanished mid-timeout).
                    from starlette.websockets import WebSocketState
                    if websocket.application_state != WebSocketState.CONNECTED:
                        break
                    await websocket.send_json({'type': 'ping'})

        except WebSocketDisconnect:
            pass
        except RuntimeError as e:
            # send/receive raced a close ("Unexpected ASGI message ... after
            # 'websocket.close'") — normal client churn, not an error. Was
            # ~3/h of ERROR noise in the production log.
            logger.debug(f"WebSocket closed during send: {e}")
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
        finally:
            await ws_manager.disconnect(websocket)

    # --- Domain routers (janitza/routes/) ---
    # Shared context for the extracted route modules. The publisher slots are
    # MUTABLE: /api/config/apply rebinds them (nonlocal) and mirrors the new
    # object here, so routers reading ctx.<publisher> at request time always
    # see the live one. Everything else is a stable singleton.
    from .routes import (ApiCtx, auth_routes, builder_routes, calculated,
                         commissioning, config as config_routes, device_templates,
                         diagnostics, discovery_routes, energy, general_config,
                         languages, metrics, pq, registers_routes, status_routes,
                         system, values_routes, vmeters)
    ctx = ApiCtx(
        app=app, config=config, registry=registry, calc_engine=calc_engine,
        event_log=event_log, alert_mgr=alert_mgr,
        auth_state=auth_state, api_key=_api_key,
        template_registry=template_registry,
        current_values=current_values, last_update=last_update,
        modbus_client=modbus_client, ws_manager=ws_manager,
        mqtt_publisher=mqtt_publisher, influxdb_publisher=influxdb_publisher,
        audit_log=audit_log,
        ip_allowed=_ip_allowed,   # WS routes enforce the allowlist themselves
    )
    app.state.ctx = ctx
    for _mod in (builder_routes, calculated, commissioning, config_routes,
                 device_templates, diagnostics, discovery_routes, energy,
                 general_config, languages, metrics, pq, registers_routes,
                 status_routes, system, auth_routes, values_routes, vmeters):
        app.include_router(_mod.build(ctx))
    app.include_router(auth_routes.build_passkeys(ctx))

    # --- Static files ---

    # Mount static files last
    app.mount("/static", StaticFiles(directory="ui"), name="static")

    # Register the WRITE-AWARE discovery hooks at boot (external audit):
    # main.py used to append its own hooks that published device discovery
    # WITHOUT write_rules, and _sync_device_discovery — which builds them
    # with write rules — only ran from the device CRUD routes. So after a
    # plain restart the write-blind hooks won the first MQTT connect: every
    # writable register was republished as a plain sensor and its command
    # topic unsubscribed. HA control worked until the first restart, then
    # silently died. ONE owner now: this call (publish is skipped while the
    # broker is still disconnected; the hooks fire on connect).
    _sync_device_discovery()

    return app, ws_manager
