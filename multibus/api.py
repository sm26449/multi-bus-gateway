# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
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
import contextlib
import copy
import hashlib
import hmac
import dataclasses
import json
from html import escape as _html_escape
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Body, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .mqtt_publisher import MQTTPublisher
from .influxdb_publisher import InfluxDBPublisher
from .payload import flag
from . import write_guard as _etag_guard  # ADR-0030: one guard for every device write
from .save_merge import (MASKED, BridgeConflict, keep_unsaid, merge_connection,
                         merge_endpoint_routes)
from .config import (BUS_OVER_TCP_PROTOCOLS, MODBUS_PROTOCOLS, NETWORK_PROTOCOLS,
                     READ_ONLY_PROTOCOLS, SERIAL_PROTOCOLS, VIRTUAL_MAP_OVER_TCP,
                     VIRTUAL_MAP_PROTOCOLS, VIRTUAL_MAP_SERIAL, protocol_family, store_key)
from .config import CAN_PROTOCOLS  # noqa: E402 — CAN (can_client.py), listen-only
from .connection_spec import METER_ONLY_TEXT
from .config import heartbeat_seconds  # noqa: E402 — ADR-0029, the one heartbeat check
_POLLED_MODBUS = NETWORK_PROTOCOLS + SERIAL_PROTOCOLS      # every Modbus way but listening
# what each kind of editor may offer — one place, served to the UI
# (/api/protocols) so a form never lists a protocol the API refuses, nor
# loses one the API accepts
PROTOCOLS_BY_CONTEXT = {
    'device': MODBUS_PROTOCOLS + VIRTUAL_MAP_PROTOCOLS + ('http', 'mqtt') + CAN_PROTOCOLS,
    'source': _POLLED_MODBUS + ('http', 'mqtt'),
    'endpoint': NETWORK_PROTOCOLS + ('rtu_tap',),
    'group': NETWORK_PROTOCOLS + ('rtu_tap', 'http'),
    # a device that IS its meters (a UPS with no communication of its own)
    'meter_only': ('none',),
}
_PROTO_WORDS = {'tcp': 'Modbus TCP', 'udp': 'Modbus UDP', 'rtu-tcp': 'Modbus RTU over TCP',
                'ascii-tcp': 'Modbus ASCII over TCP', 'seplos_v2': 'Seplos V2',
                'seplos_v2-tcp': 'Seplos V2 over TCP'}
_VMAP_MODES = ('poll', 'listen')


def _vmap_mode_missing(conn: Dict, stored: Optional[Dict]) -> bool:
    """A virtual-map connection that does not say its mode. Only a device
    stored that way (before the mode was asked) keeps the old default, poll;
    anything new, or moved onto this protocol, must choose."""
    if str((conn or {}).get('mode') or '').strip():
        return False
    return str((stored or {}).get('protocol', '')).lower() not in VIRTUAL_MAP_PROTOCOLS


def _vmap_mode(conn) -> str:
    """A virtual-map bus's mode as stored (absent = poll)."""
    raw = conn.get('mode') if isinstance(conn, dict) else getattr(conn, 'mode', '')
    return str(raw or 'poll').lower()

# a serial line named where a network address goes: "/dev/ttySEPLOS" as a TCP
# host opened a socket to "/dev/ttySEPLOS:502" and the bank went dark
# (2026-10-20, FINDINGS BANK-1020)
_SERIAL_PATH_RE = re.compile(r'^(?:/|\\\\\.\\|COM\d+$|tty[A-Za-z]*\d+$)', re.I)


def _is_serial_path(host) -> bool:
    return bool(_SERIAL_PATH_RE.match(str(host or '').strip()))


def _serial_host_error(field: str, host) -> str:
    return (f"{field}: '{str(host).strip()}' is a serial port, not a network address "
            f"— it goes in serial_port")

logger = logging.getLogger(__name__)

# a gateway that took over waits this long before its rules write: they act
# on fresh readings of its own, not only on its partner's copy
TAKEOVER_WARMUP_S = 10.0
# group keys the endpoint's group form does not edit: an edit that omits them keeps them
GROUP_KEPT_KEYS = ('mqtt', 'influxdb', 'opcua', 'channels')

# Static assets referenced as /static/<path>?v=<token>. The token is rewritten
# at serve time from the file's mtime (see _render_index_html) so a changed
# app.js/css busts the browser cache automatically — no hand-edited ?v= tokens.
_STATIC_ASSET_RE = re.compile(r'(/static/([^"\'?\s]+))\?v=[^"\'\s]*')


_INDEX_CACHE = {"key": None, "html": None}


def _canonical_redirect_script(canonical_url: str) -> str:
    """The <head> markup that steers the browser to the canonical HTTPS host
    (so TLS + passkeys are the default) WITHOUT locking out the local IP. The
    logic lives in the static ui/js/canonical.js — the page carries no inline
    script, so the CSP can forbid inline code entirely — and the target URL
    travels in a <meta> attribute, HTML-escaped, where no JS-string or
    </script> trick can turn it into code. It is client-side on purpose: the
    page loads first, so a down hostname is recoverable (`?local` in the URL,
    or the sticky `mbg-stay-local` flag the script sets)."""
    if not canonical_url:
        return ""
    return ('<meta name="mbg-canonical" content="' + _html_escape(canonical_url, quote=True)
            + '"><script src="/static/js/canonical.js?v=0"></script>')


def _render_index_html(path: str = "ui/templates/index.html",
                       canonical_url: str = "") -> str:
    """Return the SPA shell with each static asset's ?v= cache-bust token set to
    the asset's mtime (files are served from ui/ at /static/). The rendered HTML
    is cached and only re-read+re-stamped when the template file changes — the
    old path re-read the 138 KB file and ran the regex on EVERY request (page
    load, SPA nav, unauth redirect). The page carries its build (`mbg-build`):
    a tab left open across a deploy compares it with `/api/status`."""
    try:
        mt = os.path.getmtime(path)
    except OSError:
        mt = None
    key = (mt, canonical_url, path)
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
    build = _build_of(rendered)
    # inject the canonical redirect as the FIRST thing in <head> so it runs
    # before the heavy JS loads (no flash of the app on the wrong host)
    head = (_STATIC_ASSET_RE.sub(_stamp, _canonical_redirect_script(canonical_url))
            + f'<meta name="mbg-build" content="{build}">')
    rendered = rendered.replace("<head>", "<head>" + head, 1)
    _INDEX_CACHE.update(key=key, html=rendered, build=build)
    return rendered


def _build_of(stamped_html: str) -> str:
    """The page's build: its shell with every asset's stamp, and the version."""
    return hashlib.sha1((__version__ + stamped_html).encode()).hexdigest()[:12]


def ui_build(path: str = "ui/templates/index.html") -> str:
    """The build a page loaded now would carry ('' when the shell is unreadable)."""
    try:
        k = _INDEX_CACHE["key"]
        if _INDEX_CACHE.get("build") is None or k[0] != os.path.getmtime(path) or k[2] != path:
            _render_index_html(path, k[1] if k else "")
        return _INDEX_CACHE.get("build") or ""
    except (OSError, TypeError):
        return ""


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


def _driver_reg_names(client) -> Optional[set]:
    """The register names the running drivers of a device poll (every source
    of a multi-source device), or None when no driver lists them."""
    parts = getattr(client, 'parts', None)
    drivers = [d for _s, d in parts] if parts else [client]
    names, found = set(), False
    for drv in drivers:
        regs = getattr(drv, 'registers', None)
        if regs is None:
            continue
        found = True
        names |= {r.name for r in list(regs)}
    return names if found else None


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
    # `timestamp` (naive ISO, the container's zone) stays as it always was for
    # whoever parses it; `ts` (epoch seconds) is what a browser can place in
    # its own zone
    last_update = {"timestamp": None, "ts": None}

    # Store event loop reference for thread-safe async calls
    main_loop = {"loop": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup
        main_loop["loop"] = asyncio.get_running_loop()
        # after uvicorn configured its loggers: a refused /ws handshake is
        # one line per client per minute, not two per attempt (WS-LOG-1)
        from . import ws_log
        ws_log.install()
        logger.info("API started, event loop captured")
        rt = getattr(app.state, 'rules_runtime', None)
        if rt is not None and not getattr(app.state, 'rules_runtime_manual', False):
            rt.start()
        disc = getattr(app.state, 'mqtt_discovery', None)
        if disc is not None and config.mqtt.enabled:
            from .device_runtime import gateway_broker
            disc.start(gateway_broker(config, {"broker": "@gateway"}))
        opcua_ctl = getattr(app.state, 'opcua', None)
        if opcua_ctl is not None:
            opcua_ctl.apply()                      # starts the OPC UA server when it is enabled
        yield
        if opcua_ctl is not None:
            opcua_ctl.shutdown()
        if disc is not None:
            disc.stop()
        # Shutdown (cleanup if needed)
        logger.info("API shutting down")
        # what waits for a bus is cancelled and nothing new is taken — but the
        # dead-man's reverts, which main.stop() runs through the lanes next
        # while the devices are still connected (F-28); it stops the
        # dispatcher after them (ADR-0018 D12, CR2-8)
        cd = getattr(app.state, 'command_dispatch', None)
        if cd is not None:
            cd.close()
        if rt is not None:
            rt.stop()
        hs = getattr(app.state, 'harvester_stop', None)
        if hs is not None:
            hs.set()                               # let the event harvester exit its loop
        bd = getattr(app.state, 'bridge_diag', None)
        if bd is not None:
            bd.stop()                              # the bridge diagnostics poller (ADR-0017)

    app = FastAPI(
        title="Multi-Bus Gateway",
        description="Multi-protocol acquisition gateway (Modbus/HTTP/MQTT in — MQTT/InfluxDB/virtual meters out)",
        version=__version__,
        lifespan=lifespan
    )

    # Expose the live value cache so the virtual-meter engine can read it.
    app.state.current_values = current_values

    # config.yaml failed to load: every save is refused in words (409), never
    # an Internal Server Error (QA-D-11)
    from .config import ConfigSaveBlocked

    @app.exception_handler(ConfigSaveBlocked)
    async def _save_blocked(_request, exc):
        return JSONResponse(status_code=409, content={"detail": {"errors": [str(exc)], "config_blocked": True}})

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
            # a client of another origin reads the device tag (ADR-0030)
            expose_headers=["ETag", "MBG-If-Match"],
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
    # named tokens for programs (a companion service, a script): a role like a
    # login, sent as Authorization: Bearer, named in the audit log
    from .service_tokens import ServiceTokens, bearer as _bearer
    service_tokens = ServiceTokens(str(config.config_path.parent / "service_tokens.json"))
    # the gateway's role: active unless a companion service (an `ha` token)
    # makes it standby — kept on disk, never changed by the gateway itself
    from .role import GatewayRole, install as _install_role
    gw_role = GatewayRole(str(config.config_path.parent / "role.json"))
    _install_role(gw_role)
    app.state.role = gw_role
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
    from .write_lease import WriteLeaseManager, revert_budget_s as write_lease_budget_s
    _lease_mgr = WriteLeaseManager(persist_path=config.config_path.parent / "write_leases.json")
    app.state.lease_mgr = _lease_mgr
    # every write a device is told goes through one dispatcher: a lane per
    # wire, a slot per channel (ADR-0018). Built before boot recovery arms a
    # revert, which travels through it. The sweeper itself starts at the end
    # of create_api: a recovered lease fires at once, and its revert needs
    # what is defined further down (_submit_raw, …) — RP-12
    from .command_dispatch import CommandDispatcher, Job as _CmdJob
    _cmd_dispatch = CommandDispatcher()
    app.state.command_dispatch = _cmd_dispatch

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
                # fail CLOSED: an allowlist the operator wrote but that does
                # not parse must not silently become "everyone" (F-18,
                # 3.83.0); a no-match network denies every client until
                # config.yaml is fixed
                logger.critical("security.allowlist: entry %r does not parse — DENYING all "
                                "clients until it is fixed", entry)
                nets.append(_ipaddr.ip_network("255.255.255.255/32"))
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
    # script-src is 'self' only (3.82.0): the page has no inline script or
    # event handler — every action is a data-action attribute dispatched from
    # app-core.js — so an injected string can never run as code, whatever an
    # escaping slip elsewhere lets through. Inline styles stay allowed.
    _CSP = ("default-src 'self'; "
            "script-src 'self'; "
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

    async def _security_headers(request, call_next):
        # registered LAST (see the end of the middleware section) so it wraps
        # every other middleware: a 401/403 produced by the allowlist, write
        # or auth guard carries CSP/nosniff/frame headers too (3.80.0)
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
                    # the credential-establishing POSTs: an API key gate in front
                    # of them made API_KEY + login unusable together (3.80.0)
                    "/api/auth/login", "/api/auth/passkey/login/begin",
                    "/api/auth/passkey/login/finish",
                    "/api/auth/logout"}  # POST but read-only (any role ends its own session)

    # OPERATOR: live actions yes, configuration no. Allowed mutations are the
    # commissioning tools (trace/probe/discovery/query), device tests, device
    # WRITES (bounded by the template's write_min/max — that is exactly the
    # operator's job) and logout. Everything that lands in a config file
    # (devices, registers, templates, vmeters, settings, snapshots) is admin's.
    _OPERATOR_WRITE_PREFIXES = ("/api/bus-trace", "/api/diagnostics", 
                                "/api/discover", "/api/query",
                                "/api/previews",          # the read preview (ADR-0031 §5)
                                "/api/bus-scan",          # the bus scan / Listen for addresses (L3.7)
                                "/api/auth/logout", "/api/alerts/test",
                                "/api/config/reload-registers",
                                # self-service passkey enrollment/removal
                                "/api/auth/passkey", "/api/auth/passkeys")

    # an `ha` token (a high-availability companion): sets the role, raises
    # alerts — and reads, like a viewer
    _HA_WRITES = {"/api/role", "/api/alerts/fire", "/api/state/mirror", "/api/state/config"}

    def _operator_may_write(path: str, method: str = "POST") -> bool:
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
        # an installation's Test: the same read-only probe as a device's, over
        # each of its units (commissioning, the operator's job)
        if (len(parts) == 5 and parts[1] == "api" and parts[2] == "endpoints"
                and parts[3] and parts[4] == "test"):
            return True
        if (len(parts) == 5 and parts[1] == "api" and parts[2] == "devices"
                and parts[3] and parts[3] != "restorable"      # not the admin forget sub-tree
                and parts[4] in ("write", "test", "payload-sample", "preview")):
            return True
        # live TEST actions one level deeper — fire-once, change nothing:
        # /api/devices/<id>/rest-push/test and /api/devices/<id>/calculated/test
        if (len(parts) == 6 and parts[1] == "api" and parts[2] == "devices"
                and parts[3] and parts[3] != "restorable"
                and parts[4] in ("rest-push", "calculated") and parts[5] == "test"):
            return True
        # template-declared COMMANDS — the bounded form of a live write, and on
        # a command-fronted register (Fronius WMaxLimPct) the ONLY form: /write
        # 422s there and points at the command, which used to 403 the operator,
        # so the role's headline job (curtail the inverter) needed admin.
        # normalize_params enforces the template's min/max/allowed per
        # parameter, so this is as bounded as the /write envelope. The GROUP
        # fan-out route stays admin-only (blast radius).
        # /api/devices/<id>/commands/<name> and .../commands/<name>/dry-run,
        # plus the 3.72.0 aliases /api/devices/<id>/actions/power_limit
        # (POST only: PUT …/commands/<name> configures the binding — admin's)
        if (len(parts) == 6 and parts[1] == "api" and parts[2] == "devices"
                and parts[3] and parts[3] != "restorable" and method == "POST"
                and ((parts[4] == "commands" and parts[5])
                     or (parts[4] == "actions" and parts[5] == "power_limit"))):
            return True
        if (len(parts) == 7 and parts[1] == "api" and parts[2] == "devices"
                and parts[3] and parts[3] != "restorable"
                and parts[4] == "commands" and parts[5] and parts[6] == "dry-run"):
            return True
        # running / cancelling a DEFINED sequence is a live action (each of
        # its commands meets every gate); defining one is configuration
        if (len(parts) == 5 and parts[1] == "api" and parts[2] == "sequences"
                and parts[3] and parts[4] in ("run", "cancel")):
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
            # A valid service token is a credential of its own and needs no key.
            if (_api_key and request.url.path not in _open_writes
                    and not hmac.compare_digest(request.headers.get("X-API-Key", ""), _api_key)
                    and not service_tokens.check(_bearer(request.headers))):
                return JSONResponse({"detail": "missing or invalid API key"}, status_code=401)
        return await call_next(request)

    # Login/auth gate (opt-in). Open paths always work so the login flow and
    # static assets load; everything else needs a valid session when enabled.
    # A viewer session is read-only (GET/HEAD only).
    # /metrics joins /health: scrapers (Prometheus) can't log in; both stay
    # behind the IP allowlist and expose operational stats only, never config.
    _auth_open = {"/api/auth/login", "/api/auth/status", "/health", "/metrics", "/favicon.ico",
                  "/api/auth/forward",     # checks the session itself (Traefik forwardAuth)
                  # passkey assertion happens BEFORE a session exists
                  "/api/auth/passkey/login/begin", "/api/auth/passkey/login/finish"}
    _auth_open_prefixes = ("/static/",)

    # On demand, these talk to a device (probe, scan, test, query, identify)
    # or serve the device's values (the JSON feed, a REST push test): a
    # standby does neither — its partner is the one on the wire.
    import re as _re
    _STANDBY_REFUSED = _re.compile(
        r"^/api/(discover/(modbus|sunspec)(/|$)|query/|diagnostics/probe$|meters(/|$)"
        r"|devices/test$|devices/[^/]+/test$|endpoints/[^/]+/test$|devices/[^/]+/rest-push/test$"
        r"|previews$|devices/[^/]+/preview$|bus-scan$"
        r"|bridges/[^/]+/(scan|probe|ports/[^/]+/scan)$|device-templates/identify-test$)")

    @app.middleware("http")
    async def _standby_guard(request, call_next):
        if not gw_role.active and _STANDBY_REFUSED.match(request.url.path):
            return JSONResponse({"detail": gw_role.status_text(), "role": gw_role.status()}, status_code=503)
        return await call_next(request)

    @app.middleware("http")
    async def _auth_guard(request, call_next):
        if not auth_state.enabled:
            return await call_next(request)
        path = request.url.path
        if path in _auth_open or path.startswith(_auth_open_prefixes):
            return await call_next(request)
        if path == "/api/role" and request.method in ("GET", "HEAD"):
            return await call_next(request)            # what a load balancer asks: no login
        token = request.cookies.get(_auth.COOKIE_NAME, "")
        role = auth_state.role_for(token)
        _svc = service_tokens.check(_bearer(request.headers)) if role is None else None
        if _svc:
            # a program with its own token: its role, its name in the audit
            # log, and never a cookie (it holds no session)
            request.state.role = _svc[1]
            request.state.user = f"token:{_svc[0]}"
            request.state.service_token = _svc[0]
            if _svc[1] != "admin" and request.method not in ("GET", "HEAD", "OPTIONS"):
                if not (_svc[1] == "operator" and _operator_may_write(path, request.method)) \
                        and not (_svc[1] == "ha" and path in _HA_WRITES) \
                        and path not in _open_writes:
                    return JSONResponse({"detail": f"the token's role ({_svc[1]}) cannot do this"},
                                        status_code=403)
            return await call_next(request)
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
            if not _operator_may_write(path, request.method):
                return JSONResponse(
                    {"detail": "the operator account cannot change configuration"},
                    status_code=403)
        resp = await call_next(request)
        # the browser's cookie carried a fixed 7-day expiry from login while
        # the server slid the session: re-issue it (at most hourly) with the
        # time left until the absolute 30-day cap (3.80.0)
        max_age = auth_state.cookie_refresh(token)
        if max_age:
            secure = bool(config.ui.tls_enabled) or request.url.scheme == "https"
            resp.set_cookie(_auth.COOKIE_NAME, token, httponly=True, samesite="lax",
                            secure=secure, max_age=max_age, path="/")
        return resp

    # Tier 2: the DeviceRegistry owns the (DeviceConfig, client) pairs and one
    # live-value store per device; device #1's store IS the legacy
    # current_values dict (alias), so the UI/vmeters/api keep reading the same
    # object. Mutations are atomic inside the registry (device CRUD runs in
    # FastAPI's threadpool); reads are lock-free snapshots, as before.
    from .device_registry import (DeviceRegistry, client_health,
                                  client_is_live)
    from .modbus_client import endpoint_bus_stats
    registry = DeviceRegistry(config.primary_id, current_values)
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

    # which tokens of a formula are device ids (`samsung-dvm.x`, `3150-iem.p`)
    from . import expressions as _expressions
    # an installation id reads its totals in a formula: battery-bank.pack_average_soc
    _expressions.set_device_ids_provider(lambda: [d.id for d in config.devices]
                                         + [str(p.get('id')) for p in (config.endpoints or []) if p.get('id')])
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

    # what a device's meters give it is computed when a METER reports: each
    # device's own way of running and routing those values, by id
    calc_runners: Dict[str, Callable] = {}
    app.state.calc_runners = calc_runners
    # a meter-only device asks its meters' clients whether they are alive
    from . import meter_only as _meter_only
    _meter_only.find_client = lambda mid: registry.find(mid)[2]

    def _run_dependents(meter_id: str) -> None:
        """A meter reported: the devices it measures compute what it gives them."""
        for dep in sorted(calc_engine.dependents(meter_id)):
            runner = calc_runners.get(dep)
            if runner is None:
                continue
            try:
                runner()
            except Exception:  # noqa: BLE001 — another device's values never break this one's poll
                logger.debug("linked values of %s after %s reported", dep, meter_id, exc_info=True)

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
        influx_tags = None if primary else (device_cfg.influxdb_tags or None)
        device_id = "" if primary else device_cfg.id
        values_store = (current_values if primary
                        else registry.ensure_store(device_cfg.id))
        # Per-device output sinks (Phase 2): a device can opt out of MQTT and/or
        # InfluxDB while still polling, and publish on its own rhythm (its own
        # publish mode / heartbeat / write interval; unset = Settings). The
        # primary always routes to both, the gateway's way. Read at every
        # poll, so a change from its Outputs tab applies at the next reading
        # without restarting the poller.
        def _routing():
            if not gw_role.active:
                # a standby keeps its values (what it hears on the bus) but
                # publishes none: the active one does
                return False, False, {}, {}
            if device_cfg is None:
                return True, True, {}, {}
            cur = config.get_device(device_cfg.id) or device_cfg
            mp = {k: v for k, v in (('mode', cur.mqtt_publish_mode or None),
                                    ('heartbeat', cur.mqtt_heartbeat_s)) if v is not None}
            ip = {k: v for k, v in (('mode', cur.influxdb_publish_mode or None),
                                    ('interval', cur.influxdb_write_interval_s),
                                    ('heartbeat', cur.influxdb_heartbeat_s)) if v is not None}
            return cur.mqtt_enabled, cur.influxdb_enabled, mp, ip
        calc_key = config.primary_id if (device_cfg is None or device_cfg.primary) else device_cfg.id

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
                    # endpoint fan-out rules the template declared for this
                    # field — the aggregator reads them straight off the store
                    'aggregates': getattr(item.get('register'), 'aggregates', None),
                    # the store key is address + its table's offset; the
                    # register's own address and table, for display
                    'address': getattr(item.get('register'), 'address', address),
                    'register_type': getattr(item.get('register'), 'register_type', 'holding'),
                }

            last_update['timestamp'] = datetime.now().isoformat()
            last_update['ts'] = time.time()

            # Publish to each output sink independently — one sink failing (broker
            # down, bucket missing, network blip) must never skip the other sink or
            # the value store above. The poller thread itself is already isolated
            # per device, so a fault here stays local.
            mqtt_on, influx_on, mqtt_policy, influx_policy = _routing()
            if mqtt_publisher and mqtt_on:
                try:
                    mqtt_publisher.publish_register_data(poll_group, data,
                                                         topic_prefix=topic_prefix, **mqtt_policy)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"MQTT publish failed for {device_id or 'primary'}: {e}")
            if influxdb_publisher and influx_on:
                try:
                    influxdb_publisher.write_register_data(
                        poll_group, data, bucket=bucket,
                        device_tag=device_tag, device_id=device_id,
                        extra_tags=influx_tags, **influx_policy)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"InfluxDB write failed for {device_id or 'primary'}: {e}")

            # Calculated registers: derive formula values from the freshly-updated
            # store and route them to this device's sinks (same routing as above).
            calc_batch = calc_engine.run(calc_key, poll_group, values_store,
                                         topic_prefix=topic_prefix, bucket=bucket,
                                         device_tag=device_tag, device_id=device_id,
                                         mqtt_on=mqtt_on, influx_on=influx_on,
                                         influx_tags=influx_tags,
                                         mqtt_policy=mqtt_policy, influx_policy=influx_policy)

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
                        **({'address': item['register'].address, 'register_type': item['register'].register_type}
                           if getattr(item.get('register'), 'register_type', 'holding') not in ('holding', '', None)
                           else {}),
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
                        'ts': last_update['ts'],
                    }),
                    main_loop["loop"]
                )

            # the devices this one measures compute what it gives them now
            _run_dependents(calc_key)

        # absent values whose clear could not be sent yet (broker away)
        pending_clear: Set[str] = set()

        def run_linked(force: bool = False):
            """What this device's meters give it, routed as its own values;
            a value gone absent has its retained topic cleared (the gateway's
            "no value": an empty retained payload, as for an unticked row)."""
            mqtt_on, influx_on, mqtt_policy, influx_policy = _routing()
            gone: List = []
            batch = calc_engine.run_linked(calc_key, values_store, gone=gone, force=force,
                                           rerun=run_linked,
                                           topic_prefix=topic_prefix, bucket=bucket,
                                           device_tag=device_tag, device_id=device_id,
                                           mqtt_on=mqtt_on, influx_on=influx_on,
                                           influx_tags=influx_tags,
                                           mqtt_policy=mqtt_policy, influx_policy=influx_policy)
            if mqtt_publisher and (gone or pending_clear):
                try:
                    want = pending_clear | {mqtt_publisher._build_topic(r, topic_prefix) for r in gone}
                    # a value now published again is not cleared after all
                    sent = {mqtt_publisher._build_topic(it['register'], topic_prefix)
                            for it in (batch or {}).values()}
                    want -= sent
                    pending_clear.clear()
                    if want and mqtt_on and getattr(mqtt_publisher, 'connected', False):
                        mqtt_publisher.clear_topics(want)
                    elif want:
                        # the broker is away (or this gateway is standby): the
                        # clear goes out at the next evaluation that can send it
                        pending_clear.update(want)
                except Exception:  # noqa: BLE001
                    logger.debug("clearing absent values of %s", calc_key, exc_info=True)
            if batch and main_loop["loop"]:
                asyncio.run_coroutine_threadsafe(
                    ws_manager.broadcast({
                        'type': 'data', 'device': calc_key,
                        'poll_group': next(iter(batch.values()))['register'].poll_group,
                        'values': {str(addr): {'value': it.get('value'),
                                               'name': it['register'].name, 'unit': it['register'].unit,
                                               'label': it['register'].label, 'calculated': True}
                                   for addr, it in batch.items()},
                        'timestamp': datetime.now().isoformat(), 'ts': time.time(),
                    }),
                    main_loop["loop"])
            return batch

        if not primary:
            calc_runners[device_cfg.id] = run_linked
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
    # a device block a hand edit left unreadable was ignored at load (the
    # device kept): said where the operator looks, not only in the log
    for _where, _why in sorted((getattr(config, 'ignored_blocks', None) or {}).items()):
        event_log.add("warn", "config", f"{_where.split('.', 1)[0]}: {_why}")
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

        # A device's "down" alert waits DOWN_GRACE_S: a datalogger that stalls
        # for a few seconds (the Fronius Datamanager, also polled by the
        # Cerbo, does so several times a day) flips a single-source unit
        # down and back within one or two harvests, and an error alert for
        # every such blink is noise. The transition is still logged at once;
        # the alert fires only if the device is still down after the grace,
        # and the recovery alert only if a down alert was sent.
        DOWN_GRACE_S = 45.0
        down_since: Dict[str, float] = {}
        alerted_down: Set[str] = set()
        # template alarms (display.alarms) → alerts; see alarm_alerts.py
        from .alarm_alerts import AlarmTracker
        alarm_tracker = AlarmTracker(lambda *a, **k: alert_mgr.fire(*a, **k))
        alarm_seen: Set[str] = set()
        alarm_evaluated: Set[str] = set()

        def _alarm_transitions(dev_cfg, name, seen):
            try:
                decl = [a for a in (_display_of(dev_cfg).get('alarms') or [])
                        if isinstance(a, dict) and a.get('field')]
                if decl:
                    from .display import is_fresh
                    entries = list((registry.store_for(dev_cfg.id) or {}).values())
                    known = {e.get('name') for e in entries if e.get('name') and is_fresh(e)}
                    alarm_tracker.update(dev_cfg.id, name, decl, _unit_alarms(dev_cfg)['active'], seen,
                                         known=known)
                    alarm_evaluated.add(dev_cfg.id)
            except Exception:  # noqa: BLE001 — an alarm check must not stop the harvester
                logger.debug("alarm check for %s failed", dev_cfg.id, exc_info=True)

        def transition(pk, src, connected, signal):
            now = time.time()
            if prev.get(pk) is not None and connected != prev[pk]:
                event_log.add('info' if connected else 'error', src,
                              'connected' if connected else 'disconnected', 'transition')
                if not connected:
                    down_since[pk] = now
                else:
                    down_since.pop(pk, None)
            fire = (signal == 'device' and alert_mgr.sig_device) or \
                   (signal == 'sink' and alert_mgr.sig_sink)
            if fire:
                if (not connected and pk in down_since and pk not in alerted_down
                        and now - down_since[pk] >= DOWN_GRACE_S):
                    alerted_down.add(pk)
                    alert_mgr.fire('error', f'{pk}:down', src, 'down — not responding')
                elif connected and pk in alerted_down:
                    alerted_down.discard(pk)
                    alert_mgr.fire('info', f'{pk}:up', src, 'recovered — connected')
            prev[pk] = connected

        while not harvester_stop.is_set():
            try:
                pairs = registry.pairs() or ([(None, modbus_client)] if modbus_client else [])
                if not gw_role.active:
                    # a standby does not poll its devices on purpose: no "down"
                    # alert, no availability published over the active one's,
                    # no alarm or threshold from values it only heard. What it
                    # knew is forgotten, so becoming active starts clean.
                    pairs = []
                    for _k in [k for k in prev if not k.startswith(('mqtt', 'influx'))]:
                        prev.pop(_k, None)
                        down_since.pop(_k, None)
                        alerted_down.discard(_k)
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
                    if alert_mgr.sig_alarm and dev_cfg is not None and live:
                        _alarm_transitions(dev_cfg, name, alarm_seen)
                    lat = st.get('last_latency_ms')
                    if alert_mgr.sig_latency and lat and lat > alert_mgr.latency_ms:
                        alert_mgr.fire('warn', 'lat:' + did, name,
                                       f'read latency {lat} ms exceeds {int(alert_mgr.latency_ms)} ms')
                    # per-register threshold crossings → alert events (off by
                    # default). Read-only over the device's live value store, so
                    # this never touches the poll hot path.
                    if alert_mgr.sig_threshold:
                        store = registry.store_for(did) or {}
                        from .threshold_engine import gate_open
                        _by_name = {e.get('name'): e.get('value') for e in list(store.values())}
                        for reg in getattr(client, 'registers', None) or []:
                            th = getattr(reg, 'thresholds', None)
                            entry = store.get(reg.key) if th else None
                            if not entry:
                                continue
                            key = f'thr:{did}:{reg.key}'
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
                                unit=getattr(reg, 'unit', ''),
                                gated=not gate_open(th, _by_name.get))
                            if ev:
                                # band CHANGES are edge-triggered: the clear
                                # must not be swallowed by the per-key rate
                                # limit the alarm just armed
                                alert_mgr.fire(ev['severity'], ev['key'],
                                               ev['source'], ev['message'],
                                               transition=True)
                if alert_mgr.sig_threshold:
                    # drop band state for registers/devices that went away, so a
                    # removed threshold can't leave a stuck alarm behind
                    threshold_engine.retain(thr_seen)
                # deleted devices, and alarms a template no longer declares
                alarm_tracker.retain({d.id for d, _c in pairs if d is not None},
                                     seen=alarm_seen, evaluated=alarm_evaluated)
                alarm_seen.clear()
                alarm_evaluated.clear()
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
    from .canonical_fields import load_user_fields as _load_uf
    _uf_path = config.config_path.parent / "canonical_fields_user.json"
    for _bad in _load_uf(_uf_path):
        logger.warning("canonical field skipped: %s", _bad)

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
                      "/api/calculated", "/api/energy/fields", "/api/poll-groups",
                      "/api/device-types", "/api/buses")
    _SNAP_EXCLUDE = ("/test", "/write", "/payload-sample", "/api/config/import",
                     "/api/device-templates/check", "/identify-test",
                     "/api/config/reload-registers", "/api/config/snapshots",
                     "/components/discover", "/preview")

    @app.middleware("http")
    async def _snapshot_trigger(request: Request, call_next):
        response = await call_next(request)
        try:
            if (request.method in ("POST", "PUT", "PATCH", "DELETE")
                    and response.status_code < 400):
                p = request.url.path
                # a dry run stores nothing: there is nothing to snapshot
                _dry = str(request.query_params.get('dry_run', '')).lower() in ('1', 'true', 'yes', 'on')
                if p.startswith(_SNAP_PREFIXES) and not _dry and not any(x in p for x in _SNAP_EXCLUDE):
                    snapshot_store.schedule(f"{request.method} {p}",
                                            user=getattr(request.state, "user", "") or "")
        except Exception:  # noqa: BLE001 — never fail the request over a snapshot
            logger.exception("snapshot trigger failed")
        return response

    # ── Audit trail (who changed what, when, from where) ────────────────────
    from .audit import AuditLog
    audit_log = AuditLog(str(config.config_path.parent / "audit.jsonl"))
    app.state.audit_log = audit_log

    # ── Asset attributes (docs/attributes.md) ────────────────
    from .attributes import AttributeService
    from .device_template import mppt_count_of as _mppt_count_of
    from .device_kind import attribute_kind as _attribute_kind, device_kind as _device_kind

    def _device_type_of(dev):
        """(type, its English label) of a device, as `<dev>/meta` and the
        device list say it (ADR-0027); None for no such device."""
        if dev is None:
            return None
        from . import device_types as _dtypes
        t_ = _dtypes.device_type(dev, template_registry)
        return t_, _dtypes.label(t_)

    def _req_user(request) -> str:
        """Who asked, for an audit line (a call from inside the gateway: '-')."""
        if request is None:
            return '-'
        return getattr(request.state, "user", None) or ("api-key" if _api_key else "anon")

    def _req_ip(request) -> str:
        return (request.client.host if request is not None and request.client else '-')

    def _attr_devices():
        out = []
        for d in config.devices:
            tid = d.template or next((sx.template for sx in (d.sources or []) if sx.template), '')
            kind = _attribute_kind(d, template_registry)
            mppt = 0
            for t_id in {tid} | {sx.template for sx in (d.sources or []) if sx.template}:
                t_ = template_registry.get(t_id) if t_id else None
                mppt = max(mppt, _mppt_count_of(t_) if t_ else 0)
            out.append((d.id, getattr(d, 'endpoint_id', '') or '', kind or None, mppt or None))
        return out

    attributes = AttributeService(
        config.config_path.parent / "attributes.yaml",
        devices=_attr_devices,
        installations=lambda: [str(p.get('id')) for p in config.endpoints if p.get('id')],
        audit=audit_log,
        on_change=lambda trigger, who: snapshot_store.schedule(trigger, user=who or ''),
        device_type=lambda did: _device_type_of(config.get_device(did)))
    try:
        attributes.store.ensure()         # a bundle always carries the file
    except Exception:  # noqa: BLE001 — a read-only config dir must not stop the gateway
        logger.exception("attributes.yaml could not be created")
    app.state.attributes = attributes
    # formulas and rules read the plant through it: the sun at a place, an attribute
    _expressions.set_place_provider(attributes.place_location, attributes.place_ids)
    _expressions.set_attr_provider(attributes.attr_value)

    def _sun_elev_at(place):
        loc = attributes.place_location(place)
        if not loc:
            return None
        from datetime import datetime as _dt, timezone as _tz
        from . import clock as _clock, sun as _sun
        return _sun.position(loc[0], loc[1], _dt.fromtimestamp(_clock.now(), _tz.utc))[0]
    from . import endpoint_aggregator as _agg_mod
    _agg_mod.set_sun_provider(_sun_elev_at)

    # ── the attributes on MQTT: retained, versioned, from the active gateway ──
    _meta_sent: Dict[str, str] = {}          # topic → its content (without the rev)
    _meta_conn = {'seen': None}

    def _meta_topics() -> Dict[str, Dict]:
        """Where each place's attributes go, on this gateway's own roots so
        two gateways on one broker never share a retained topic:
        <gw>/site/meta, <installation totals root>/meta, <device prefix>/meta."""
        from .endpoint_aggregator import aggregate_topic as _agg_topic
        out = {f"{config.mqtt.topic_prefix}/site/meta": attributes.meta('site')}
        for p_ in config.endpoints:
            if p_.get('id'):
                out[f"{_agg_topic(p_, str(p_['id']), '', True)}/meta"] = attributes.meta('installation', str(p_['id']))
        for d_ in config.devices:
            if getattr(d_, 'mqtt_topic_prefix', '') and getattr(d_, 'mqtt_enabled', True):
                out[f"{d_.mqtt_topic_prefix}/meta"] = attributes.meta('device', d_.id)
        return out

    def _publish_attribute_meta(force: bool = False) -> None:
        """What the plant and its equipment are, for every system that reads
        the broker: retained, published when a place's content changes, again
        in full after a (re)connect, and only by an active gateway (two would
        race on the retained topics). A topic no longer published (a renamed
        prefix, a device whose MQTT was turned off) is cleared."""
        if not mqtt_publisher or not getattr(mqtt_publisher, 'connected', False) or not gw_role.active:
            return
        conns = getattr(mqtt_publisher, 'connects', 0)
        if conns != _meta_conn['seen']:
            force, _meta_conn['seen'] = True, conns
        out = _meta_topics()
        for topic in [t for t in _meta_sent if t not in out]:
            _clear_attribute_meta(topic)
        for topic, body in out.items():
            content = json.dumps({k: v for k, v in body.items() if k != 'rev'}, default=str, sort_keys=True)
            if force or _meta_sent.get(topic) != content:
                if mqtt_publisher._publish(topic, json.dumps(body, default=str, sort_keys=True), retain=True):
                    _meta_sent[topic] = content

    def _clear_attribute_meta(topic: str) -> None:
        _meta_sent.pop(topic, None)
        if mqtt_publisher and getattr(mqtt_publisher, 'connected', False):
            try:
                mqtt_publisher._publish(topic, "", retain=True)
            except Exception:  # noqa: BLE001
                pass

    def _attributes_changed(trigger: str, who: str) -> None:
        snapshot_store.schedule(trigger, user=who or '')
        try:
            _publish_attribute_meta()
        except Exception:  # noqa: BLE001 — publishing never fails a save
            logger.exception("attributes: publishing meta failed")
    attributes._on_change = _attributes_changed
    app.state.publish_attribute_meta = _publish_attribute_meta

    # every mutating /api/ call is recorded — including DENIED ones (401/403
    # are exactly what a security review wants to see). Live-data actions with
    # no config effect (queries, probes, discovery) are skipped; device writes
    # have their own richer entry on the write route.
    _BODY_CAP = 8 * 1024 * 1024                 # every JSON route
    _BODY_CAP_IMPORT = 25 * 1024 * 1024         # matches the import handler's own cap
    _BODY_CAP_LARGE_PATHS = ('/api/config/import',)
    _AUDIT_SKIP = ("/api/query", "/api/diagnostics/probe", "/api/discover",
                   "/api/device-templates/check", "/identify-test",
                   "/api/auth/", "/api/alerts/test", "/api/devices/test",
                   "/api/bus-trace")

    @app.middleware("http")
    async def _audit_mw(request: Request, call_next):
        body_summary = None
        p = request.url.path
        # F-19 (3.83.0): one body cap for every /api/ write. Handlers read
        # the body through Starlette, which buffers it whole — without a cap
        # any authenticated peer (or, on an auth-off box, any LAN peer) could
        # OOM the process with one oversized POST. Import has its own 25 MB
        # cap inside the handler; everything else is JSON and fits 8 MB.
        if request.method in ("POST", "PUT", "PATCH") and p.startswith("/api/"):
            _cap = _BODY_CAP_IMPORT if p in _BODY_CAP_LARGE_PATHS else _BODY_CAP
            _cl0 = request.headers.get("content-length", "")
            if _cl0.isdigit() and int(_cl0) > _cap:
                return JSONResponse({"detail": f"request body too large (max {_cap // 1048576} MB)"},
                                    status_code=413)
            if not _cl0 and "chunked" in request.headers.get("transfer-encoding", "").lower():
                return JSONResponse({"detail": "Content-Length required"}, status_code=411)
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
        both the live write path and boot recovery, so they behave identically.
        A lane `revert` job (ADR-0022 D8): the head of its lane, never
        superseded, `is_current()` asked again under the bus lock (RD-4). A
        clean shutdown's revert waits only within its budget (RC-5, review
        HIGH 1)."""
        def _revert(is_current):
            # Re-check right before the (blocking) write: if the lease was renewed
            # while we waited, abort so we don't clobber the fresh setpoint.
            if not is_current():
                return
            _i, _c, c = _find_device(dev)
            if c is None or _c is None:
                # device gone/restarting — raise so the dead-man retries instead
                # of silently dropping the lease with a live setpoint
                raise RuntimeError(f"device {dev} not running; cannot revert to safe")
            ctx = {'dev': _c, 'rtype': rt, 'address': int(addr), 'data_type': dt, 'scale': float(sc),
                   'offset': float(off or 0.0), 'value': sv, 'via': 'lease-revert', 'who': 'lease', 'ip': '-',
                   'is_revert': True, 'is_current': is_current, 'deadline': _budget_deadline()}
            job = _submit_raw(ctx, c)
            left = None if ctx['deadline'] is None else max(0.0, ctx['deadline'] - time.monotonic())
            got = job.wait(left)
            if got is None:
                # past the budget: withdrawn if it has not started; one that
                # runs gives up at the same deadline. Either way the lease
                # stays on disk for boot recovery
                if _cmd_dispatch.withdraw(job, 'the shutdown budget ran out before the bus was free '
                                               '— left for boot recovery'):
                    raise TimeoutError(f"device {dev}: the bus stayed busy past the shutdown budget; "
                                       "left for boot recovery")
                got = job.wait(0)
                if got is None:
                    raise TimeoutError(f"device {dev}: the revert still runs past the shutdown budget; "
                                       "left for boot recovery")
            code, res = got
            if res.get('lease_renewed'):
                return
            if res.get('status') not in ('success', 'mismatch', 'unverified'):
                raise RuntimeError(f"lease-revert failed: {res.get('reason') or res.get('status')}")
        return _revert

    def _make_command_lease_revert(dev, cmd_name):
        """The dead-man revert for a lease taken by a COMMAND: run the command's
        own `safe` recipe (every register it writes, encoded through its scale
        factors, verified, audited) — never a bare word into the first
        register. Resolved at fire time from the current template, so boot
        recovery and the live path behave identically (3.83.0: recovery used
        to write the raw safe value, which a SunSpec scale factor of -2
        turned into a 1 % limit)."""
        def _revert(is_current):
            if not is_current():
                return
            _i, _c, cl = _find_device(dev)
            if _c is None or cl is None:
                raise RuntimeError(f"device {dev} not running; cannot run {cmd_name} safe")
            c = _find_command(_c, cmd_name)
            if c is None or not c['def'].safe:
                raise RuntimeError(f"device {dev} no longer offers command {cmd_name} with a safe recipe")
            # at the head of its lane, never superseded; is_current() is asked
            # again there, under the bus lock (ADR-0018 D3)
            st, r = _run_named_command(_c, cl, cmd_name, dict(c['def'].safe),
                                       who='lease', ip='-', via='lease-revert', is_current=is_current)
            if r.get('lease_renewed'):
                return
            if r.get('status') not in ('success', 'mismatch'):
                raise RuntimeError(f"lease-revert failed: {r.get('reason')}")
        return _revert

    # Boot recovery: any write-lease left on disk means a previous run may have
    # crashed while holding a device at a non-safe setpoint. Re-arm each as
    # already-expired so the next sweep reverts it to safe (retrying until the
    # device is reachable). This closes the "crash strands a dangerous setpoint"
    # gap that the in-RAM-only lease store had. A record taken by a command
    # reverts through the command's safe recipe, never through a raw write.
    _persisted_leases = _lease_mgr.load_persisted()
    if _persisted_leases and not gw_role.active:
        # a standby holds no device: the leases it took while active belong to
        # the past — reverting them now could undo what the active one set
        logger.warning("standby gateway: %d write lease(s) from when it was active dropped, not reverted",
                       len(_persisted_leases))
        _lease_mgr.drop_persisted()
        _persisted_leases = []
    for _m in _persisted_leases:
        try:
            if _m.get('command'):
                _rv = _make_command_lease_revert(_m['device'], _m['command'])
            else:
                _rv = _make_lease_revert(_m['device'], _m['register_type'], int(_m['address']),
                                         _m['data_type'], float(_m['scale']), _m['safe_value'],
                                         off=float(_m.get('offset', 0.0) or 0.0))
            _lease_mgr.arm(_m['device'], _m['register_type'], int(_m['address']),
                           int(_m.get('lease_ms') or 0), _rv, meta=_m, fire_now=True)
            logger.warning("WRITE-LEASE recovered after restart → reverting to safe: "
                           "device=%s addr=%s safe=%r", _m['device'], _m['address'], _m['safe_value'])
        except Exception as _e:  # noqa: BLE001
            logger.error("write-lease recovery: bad persisted record %r: %s", _m, _e)

    def _writes_by_mqtt(dev_cfg) -> bool:
        """A unit whose writes go out over MQTT: it has an MQTT source and no
        Modbus one. Such a unit takes only commands — there is no raw
        register write to publish."""
        protos = {str(s.protocol or 'tcp').lower() for s in (dev_cfg.sources or [])} or {str(dev_cfg.protocol or 'tcp').lower()}
        return 'mqtt' in protos and not (protos & set(_POLLED_MODBUS))

    def _write_rule(dev_cfg, address: int, rtype: str):
        """The TemplateRegister governing writes to (address, register_type) on
        this device, or None. Carries writable + bounds + safe value. A register
        absent here (or not writable) cannot be written — the safety allowlist.
        A unit read through sources carries its maps on the Modbus SOURCE, so
        those templates are consulted too."""
        tids = [dev_cfg.template] if dev_cfg.template else []
        tids += [s.template for s in (dev_cfg.sources or [])
                 if s.template and str(s.protocol or 'tcp').lower() in _POLLED_MODBUS + ('mqtt',)]
        for tid in tids:
            tpl = template_registry.get(tid)
            if tpl is None:
                continue
            for r in tpl.registers:
                if r.address == address and (r.register_type or 'holding') == rtype:
                    return r
        return None

    def _declared_writes(dev_cfg) -> int:
        """How many registers an operator may write through /write: declared
        writable holdings and coils, as _write_rule finds them (the first
        template wins an address), less what /write refuses even so — a
        register a command writes (ADR-0022 D4) or one scaled by a live
        factor — and nothing for a device written over MQTT (commands only)."""
        if _writes_by_mqtt(dev_cfg):
            return 0
        tids = [dev_cfg.template] if dev_cfg.template else []
        tids += [s.template for s in (dev_cfg.sources or [])
                 if s.template and str(s.protocol or 'tcp').lower() in _POLLED_MODBUS + ('mqtt',)]
        seen = set()
        for tid in dict.fromkeys(tids):
            tpl = template_registry.get(tid) if template_registry else None
            for r in (getattr(tpl, 'registers', None) or []):
                rtype = r.register_type or 'holding'
                if not getattr(r, 'writable', False) or rtype not in ('holding', 'coil'):
                    continue
                if _write_rule(dev_cfg, r.address, rtype) is not r or getattr(r, 'scale_from', None):
                    continue
                try:
                    if _raw_channel_conflict(dev_cfg, rtype, r.address, _words_of(r.data_type or 'uint16', rtype)):
                        continue
                except Exception:  # noqa: BLE001 — a count never fails a list
                    continue
                seen.add((rtype, r.address))
        return len(seen)

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
            # nothing runs for it (disabled, nothing to read): what its meters
            # gave it stops too, until it runs again (review N1); nor is what
            # waited for its bus written (CR2-8)
            calc_runners.pop(dev_cfg.id, None)
            _cmd_dispatch.cancel(device=dev_cfg.id, reason=f"device '{dev_cfg.id}' was disabled — nothing written")
            return None
        client.publish_callback = make_data_callback(dev_cfg)
        # its derived fields (template-shipped or the operator's) — loaded at
        # boot for every device, and here for one created, edited or restarted
        # at runtime: until now those ran only after the next restart
        try:
            calc_engine.load(dev_cfg.id)
        except Exception as e:  # noqa: BLE001 — a bad formula must not stop the device
            logger.warning("device %s: calculated fields not loaded — %s", dev_cfg.id, e)

        def _bg():
            if client.connect():
                logger.info(f"device {dev_cfg.id}: connected")
            else:
                logger.warning(f"device {dev_cfg.id}: connect failed — pollers will retry")
            client.start_polling()

        threading.Thread(target=_bg, daemon=True,
                         name=f"Device-Init-{dev_cfg.id}").start()
        return client

    _INFLUX_NAME_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_.\-]{0,63}$')
    _INFLUX_RESERVED = {'_time', '_measurement', '_field', '_value', '_start', '_stop', 'time'}

    def _influx_block_errors(ix: Dict, w: str, *, outputs: bool) -> List[str]:
        """Shape-check an ``influxdb`` block's operator-editable parts: static
        ``tags`` (device and endpoint) and the endpoint's extra ``outputs``.
        Names must be Influx-safe and never one of its own columns."""
        errors: List[str] = []
        if not isinstance(ix, dict):
            return [f"{w}: must be an object"]

        def _tags(tags, where):
            if tags in (None, {}):
                return
            if not isinstance(tags, dict):
                errors.append(f"{where}: must be a mapping of name: value")
                return
            for k, v in tags.items():
                if not _INFLUX_NAME_RE.match(str(k)) or str(k) in _INFLUX_RESERVED:
                    errors.append(f"{where}.{k}: tag names start with a letter "
                                  "(letters, digits, _ . -) and are not Influx columns")
                elif v is None or not str(v).strip() or len(str(v)) > 128:
                    errors.append(f"{where}.{k}: value required (1-128 chars)")

        _tags(ix.get('tags'), f"{w}.tags")
        outs = ix.get('outputs')
        if outs in (None, []):
            return errors
        if not outputs:
            return errors + [f"{w}.outputs: only an endpoint's totals have extra outputs"]
        if not isinstance(outs, list) or len(outs) > 16:
            return errors + [f"{w}.outputs: a list of at most 16 outputs"]
        ids = set()
        for i, o in enumerate(outs):
            ow = f"{w}.outputs[{i}]"
            if not isinstance(o, dict):
                errors.append(f"{ow}: must be an object")
                continue
            oid = str(o.get('id', '')).strip()
            if not re.match(r'^[a-z0-9][a-z0-9_-]{0,31}$', oid):
                errors.append(f"{ow}.id: a-z 0-9 - _ (1-32 chars)")
            elif oid in ids:
                errors.append(f"{ow}.id: '{oid}' is used twice")
            ids.add(oid)
            if not _INFLUX_NAME_RE.match(str(o.get('measurement', ''))):
                errors.append(f"{ow}.measurement: required, starts with a letter "
                              "(letters, digits, _ . -)")
            b = str(o.get('bucket', '') or '')
            if b and not re.match(r'^[A-Za-z0-9][A-Za-z0-9_.\-${}]{0,63}$', b):
                errors.append(f"{ow}.bucket: invalid name")
            if str(o.get('mode', 'changed') or 'changed') not in ('changed', 'every'):
                errors.append(f"{ow}.mode: changed or every")
            _tags(o.get('tags'), f"{ow}.tags")
            fl = o.get('fields')
            if not isinstance(fl, list) or not fl or len(fl) > 200:
                errors.append(f"{ow}.fields: 1-200 fields")
                continue
            names = set()
            for j, f in enumerate(fl):
                fw = f"{ow}.fields[{j}]"
                if not isinstance(f, dict):
                    errors.append(f"{fw}: must be an object")
                    continue
                n = str(f.get('name', ''))
                if not _INFLUX_NAME_RE.match(n) or n in _INFLUX_RESERVED:
                    errors.append(f"{fw}.name: starts with a letter (letters, digits, _ . -)")
                elif n in names:
                    errors.append(f"{fw}.name: '{n}' is written twice")
                names.add(n)
                if not str(f.get('source', '')).strip():
                    errors.append(f"{fw}.source: which total feeds it")
                try:
                    sc = float(f.get('scale', 1) if f.get('scale', 1) not in (None, '') else 1)
                    if sc == 0 or sc != sc or sc in (float('inf'), float('-inf')):
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append(f"{fw}.scale: a non-zero number")
        return errors

    def _virtual_map_conflict(protocol: str, dproto: str, conn: Dict, d, where: str):
        """Two devices on one bus where either speaks a virtual-map protocol
        (seplos_v2): None when neither does (the Modbus rules apply); else the
        refusal, or [] when they may share it — the same protocol, the same
        mode (a bus is polled or listened to, never both), and each pack its
        own address."""
        if protocol not in VIRTUAL_MAP_PROTOCOLS and dproto not in VIRTUAL_MAP_PROTOCOLS:
            return None
        if protocol != dproto:
            return [f"connection.protocol: {where} is used by device '{d.id}' as {dproto} — "
                    f"one bus speaks one protocol"]
        theirs = _vmap_mode(d.connection)
        if _vmap_mode(conn) != theirs:
            return [f"connection.mode: {where} is {'listened to' if theirs == 'listen' else 'polled'} by "
                    f"device '{d.id}' — a bus is either polled or listened to, never both"]
        try:
            same_unit = int(conn.get('unit_id', 0) or 0) == int(getattr(d.connection, 'unit_id', -1))
        except (TypeError, ValueError):
            same_unit = False
        if same_unit:
            return [f"connection.unit_id: pack {conn.get('unit_id', 0)} on {where} is already read by "
                    f"device '{d.id}' — each pack on a bus has its own address"]
        return []

    def _serial_line_conflicts(protocol: str, sp: str, conn: Dict,
                               existing_id: str = None) -> List[str]:
        """Who else holds serial line `sp`. One physical line cannot be driven
        by two independent masters (each ModbusClient owns its own lock), and a
        tty opens once — so an active RTU device and a tap never share a port.
        Taps DO share one (a single TapReader per port fans frames out by unit
        id), but that reader opens the line once, with the first tap's settings:
        a second tap must agree on them or it would silently decode garbage.
        A line is its canonical path: a tty and its by-id link are one (ADD-13)."""
        from .bus_claims import canonical_serial
        line = canonical_serial(sp)
        for d in config.devices:
            dproto = getattr(d, 'protocol', '')
            if (getattr(d, 'id', None) == existing_id
                    or dproto not in SERIAL_PROTOCOLS + ('rtu_tap',) + VIRTUAL_MAP_SERIAL
                    or canonical_serial(getattr(d.connection, 'serial_port', '')) != line):
                continue
            _vm = _virtual_map_conflict(protocol, dproto, conn, d, f"'{sp}'")
            if _vm is not None:
                if _vm:
                    return _vm
                ours = (int(conn.get('baudrate', 9600) or 9600), str(conn.get('parity', 'N') or 'N').upper())
                theirs = (int(d.connection.baudrate or 9600), str(d.connection.parity or 'N').upper())
                if ours != theirs:
                    return [f"connection.baudrate: '{sp}' is read by '{d.id}' at {theirs[0]} {theirs[1]} — "
                            f"every device on one line uses the same line settings"]
                continue
            if protocol in SERIAL_PROTOCOLS and dproto in SERIAL_PROTOCOLS and protocol != dproto:
                return [f"connection.protocol: '{sp}' is read as {dproto.upper()} by device '{d.id}' — "
                        f"the slaves on one line all speak one framing (RTU or ASCII)"]
            if protocol == dproto and protocol in SERIAL_PROTOCOLS:
                # several slaves on one bus: they share the open line and take
                # turns; the slave address must differ, the line settings agree
                if int(conn.get('unit_id', 1)) == int(getattr(d.connection, 'unit_id', -1)):
                    return [f"connection.unit_id: unit {conn.get('unit_id', 1)} on '{sp}' is "
                            f"already read by device '{d.id}' — each slave on a bus has its own address"]
            if protocol != dproto:
                return [f"connection.serial_port: '{sp}' is already used by device "
                        f"'{d.id}' as {dproto} — a serial line is either polled or "
                        f"tapped, never both"]
            ours = (int(conn.get('baudrate', 9600) or 9600),
                    str(conn.get('parity', 'N') or 'N').upper())
            theirs = (int(d.connection.baudrate or 9600),
                      str(d.connection.parity or 'N').upper())
            if ours != theirs:
                how = 'tapped' if dproto == 'rtu_tap' else 'read'
                return [f"connection.baudrate: '{sp}' is {how} by '{d.id}' at "
                        f"{theirs[0]} {theirs[1]} — every device on one line uses the "
                        f"same line settings"]
        return []

    def _network_bus_conflicts(protocol: str, conn: Dict, existing_id: str = None) -> List[str]:
        """Who else is on the bus behind host:port. An rtu-tcp host:port IS one
        RS-485 bus (and so is a Modbus TCP gateway's port reached through a
        bridge). Several slaves on it are several devices: they share ONE socket
        and take turns on it (the shared transport — a converter that accepts a
        single client is never fought over). What must not repeat is the slave
        address: two devices with one unit id on one bus would read the same
        slave and answer for each other. And, as on a serial line, a bus is
        either polled or tapped: a tap listens to another master, and the
        bridge's one connection cannot be both ours to ask and ours to hear."""
        _host = str(conn.get('host', '')).strip().lower()
        try:
            _port = int(conn.get('port', 502))
            _unit = int(conn.get('unit_id', 1))
        except (TypeError, ValueError):
            return []
        for d in config.devices:
            dproto = getattr(d, 'protocol', '')
            if (getattr(d, 'id', None) == existing_id
                    or dproto not in NETWORK_PROTOCOLS + ('rtu_tap',) + VIRTUAL_MAP_OVER_TCP
                    or str(getattr(d.connection, 'host', '')).strip().lower() != _host
                    or int(getattr(d.connection, 'port', 0) or 0) != _port
                    or (dproto == 'udp') != (protocol == 'udp')):     # a UDP port is not the TCP one
                continue
            _vm = _virtual_map_conflict(protocol, dproto, conn, d, f"{_host}:{_port}")
            if _vm is not None:
                if _vm:
                    return _vm
                continue
            if (dproto != protocol and 'rtu_tap' not in (dproto, protocol)):
                return [f"connection.protocol: {_host}:{_port} is read as {dproto} by device "
                        f"'{d.id}' — one port speaks one framing"]
            if (dproto == 'rtu_tap') != (protocol == 'rtu_tap'):
                return [f"connection.protocol: the bus at {_host}:{_port} is already "
                        f"{'tapped' if dproto == 'rtu_tap' else 'polled'} by device '{d.id}' — "
                        f"a bus is either polled or tapped, never both"]
            if dproto == protocol and int(getattr(d.connection, 'unit_id', -1)) == _unit:
                return [f"connection.unit_id: unit {_unit} on {_host}:{_port} is "
                        f"already {'tapped' if protocol == 'rtu_tap' else 'read'} by device "
                        f"'{d.id}' — each slave on a bus has its own address"]
        return []

    def _refuse_if_listened(what: str, protocol: str, conn) -> None:
        """409 when a device listens on the bus ``conn`` is on (ADR-0025):
        another master owns it, so nothing is sent — no override."""
        from .bus_claims import bus_of, listeners, refusal_detail, where_of
        bus = bus_of(protocol, conn)
        ids = listeners(config, bus, template_registry)
        if ids:
            raise HTTPException(status_code=409, detail=refusal_detail(what, bus, ids, where_of(protocol, conn)))

    def _claim_sig(built, standby=False):
        from .bus_claims import source_claims
        return sorted((b, lis) for d in built for _i, b, lis, _w in source_claims(d, template_registry, standby))

    def _bus_claim_errors(built, exclude, field_of, prev_built=None) -> List[str]:
        """The bus claim on a save (ADR-0025): the same rule as a Test, both
        ways — a polled source on a bus another device only listens to, a
        listening one on a bus another device polls. Judged on what changes
        (ADR-0006): an edit that leaves every source on the bus it was on, in
        the role it had there, is not judged again."""
        from .bus_claims import save_conflicts
        if prev_built is not None and _claim_sig(prev_built) == _claim_sig(built):
            return []
        out: List[str] = []
        for dev, i, why in save_conflicts(config, built, template_registry, exclude):
            e = f"{field_of(dev, i)}: {why}"
            if e not in out:
                out.append(e)
        return out

    def _device_claim_check(raw: Dict, did: str, prev: Optional[Dict] = None) -> None:
        """A device save that breaks the bus claim: 422, field by field."""
        built = config.preview_devices(device=raw)
        prev_built = config.preview_devices(device=prev) if prev else None
        own = {did} | {c.id for c in config.component_devices(did)}   # read through it
        keyed = bool(raw.get('sources'))
        errs = _bus_claim_errors(built, own, lambda _d, i: (f"sources[{i}].protocol" if keyed
                                                            else "connection.protocol"), prev_built)
        if errs:
            raise HTTPException(status_code=422, detail={"errors": errs})

    def _endpoint_claim_check(raw: Dict, pid: str, prev: Optional[Dict] = None) -> None:
        """An installation save that breaks the bus claim, or puts a template
        written for listening on a unit that would poll (ADD-8): 422, each
        error at the group / source it comes from."""
        built = config.preview_devices(endpoint=raw)
        prev_built = config.preview_devices(endpoint=prev) if prev else None
        own = {d.id for d in built} | {d.id for d in config.endpoint_devices(pid)}
        groups = [g for g in (raw.get('groups') or []) if isinstance(g, dict)]
        by_gid = {str(g.get('id', '')).strip().lower(): (gi, g) for gi, g in enumerate(groups)}

        def field_of(d, i: int) -> str:
            gi, g = by_gid.get(str(getattr(d, 'group_id', '') or '').lower(), (None, None))
            if g is not None and g.get('sources'):
                return f"groups[{gi}].sources[{i}].protocol"
            if g is not None and g.get('connection'):
                return f"groups[{gi}].connection.protocol"
            return f"sources[{i}].protocol" if raw.get('sources') else "connection.protocol"

        def heard(devs):
            return sorted((f"{d.group_id}/{i}", str(s.protocol).lower(), s.template or d.template)
                          for d in devs for i, s in enumerate(d.sources or []))
        errs: List[str] = []
        if prev_built is None or heard(prev_built) != heard(built):
            for d in built:
                for i, s in enumerate(d.sources or []):
                    tid = s.template or d.template
                    p = str(s.protocol or '').lower()
                    if p in MODBUS_PROTOCOLS and p != 'rtu_tap' and _listening_template(tid):
                        e = (f"{field_of(d, i)}: template '{tid}' is only heard on a bus another master "
                             f"polls — choose listen only (rtu_tap)")
                        if e not in errs:
                            errs.append(e)
        errs += [e for e in _bus_claim_errors(built, own, field_of, prev_built) if e not in errs]
        if errs:
            raise HTTPException(status_code=422, detail={"errors": errs})

    def _listening_template(tid: str) -> bool:
        """A template that only listens (ADR-0025): its devices never transmit."""
        from .device_template import template_listens
        tpl = template_registry.get(tid) if tid else None
        return tpl is not None and template_listens(tpl)

    def _serial_missing(sp: str, busy_id: str = '') -> str:
        """'' when the serial port is there; else what a Test says (QA-D-10):
        missing, and — when the host has a port by that name the container
        does not — the line that maps it."""
        from . import serial_inventory, serial_ports
        if not sp or os.path.exists(serial_ports._p(serial_inventory.SYSROOT, sp)):
            return ''
        msg = (f"Serial port {sp} does not exist in the gateway's container — nothing was asked"
               + (f", and device '{busy_id}' cannot read on it either" if busy_id else ''))
        try:
            name = os.path.basename(sp)
            port = next((p_ for p_ in serial_inventory.inventory(config, template_registry).get('ports', [])
                         if p_.get('state') == 'not_mapped' and (p_.get('name') == name or p_.get('path') == sp)), None)
        except Exception:  # noqa: BLE001 — the inventory only adds a hint
            port = None
        if port and port.get('fix'):
            msg += (f". The host has {port.get('name')}, but it is not mapped into the gateway's container: "
                    f"add `{port['fix'].get('compose', '')}` under the gateway's devices: in docker-compose "
                    f"(or `{port['fix'].get('docker_run', '')}`) and recreate the container")
        else:
            msg += ": check the path, that the adapter is plugged in and that it is mapped into the container"
        return msg

    def _test_on_shared_bus(conn: Dict, unit_id: int, address: int, timeout: float,
                            where: str, busy_id: str) -> Dict:
        """One read on a bus a running device already holds, through the same
        socket and lock (a temporary unit on the shared transport)."""
        from .config import ModbusConfig
        from .modbus_client import ModbusConnection
        mc = ModbusConnection(ModbusConfig(
            host=str(conn.get('host', '')), port=int(conn.get('port', 502) or 502),
            unit_id=unit_id, timeout=timeout, retry_attempts=1, retry_delay=0,
            protocol=str(conn.get('protocol', 'rtu-tcp')),
            serial_port=str(conn.get('serial_port', '') or ''),
            baudrate=int(conn.get('baudrate', 9600) or 9600), parity=str(conn.get('parity', 'N') or 'N'),
            stopbits=int(conn.get('stopbits', 1) or 1)), trace_label=f"test:{where}")
        t0 = time.perf_counter()
        try:
            words = mc.read_registers(address, 2, "holding")
        except Exception as e:  # noqa: BLE001
            words, err = None, str(e)
        else:
            err = ""
        finally:
            mc.disconnect()                     # releases the claim, keeps the socket
        lat = round((time.perf_counter() - t0) * 1000, 1)
        # only a serial port on this host can be missing (a bus over TCP has none)
        serial = str(conn.get('protocol', '')).lower() in SERIAL_PROTOCOLS
        missing = ('' if words is not None or not serial
                   else _serial_missing(str(conn.get('serial_port', '') or ''), busy_id))
        if missing:
            return {"ok": False, "latency_ms": lat, "shared_with": busy_id, "message": missing}
        if words is not None:
            return {"ok": True, "latency_ms": lat, "shared_with": busy_id,
                    "message": f"Unit {unit_id} answered in {lat} ms — through the connection "
                               f"device '{busy_id}' already uses on this bus (one connection, "
                               f"taking turns)"}
        return {"ok": False, "latency_ms": lat, "shared_with": busy_id,
                "message": f"No answer from unit {unit_id} on {where} — the bus itself works "
                           f"(device '{busy_id}' reads on it): check the unit id and that the "
                           f"slave is wired to this bus{(' — ' + err) if err else ''}"}

    def _meter_only_raw(payload: Dict, did: str, template_id: str, errors: List[str],
                        existing_id: Optional[str]) -> Dict:
        """A device that IS its meters (a UPS with no communication of its
        own): ``connection: {protocol: none}``, no template, no address. It
        is created with its meters (``meters: [...]``, at least one) and its
        kind (``kind``, e.g. ups); it reads nothing, writes nothing, and is
        online when its meters are (MeterOnlyClient)."""
        from . import meter_links
        from .config import DeviceConfig
        from .device_kind import METER_ONLY
        fields: Dict[str, str] = {}
        if template_id:
            fields['template'] = 'a meter-only device has no template — it reads nothing itself'
        extra = sorted(k for k in (payload.get('connection') or {}) if k != 'protocol')
        if extra:
            fields['connection'] = f"a meter-only device has no address ({', '.join(extra)})"
        raw = {'id': did, 'name': str(payload.get('name', '') or did), 'template': '',
               'enabled': flag(payload, 'enabled', True), 'connection': {'protocol': 'none'}}
        if not existing_id:
            from . import device_types as _dtypes
            k, why = _dtypes.from_payload(payload)
            why = why or (_dtypes.override_error(k, meter_only=True) if k is not None else None)
            if k is None or why:
                fields['type' if 'type' in payload else 'kind'] = (
                    why or f"what it is — one of {', '.join(METER_ONLY)} or a user type (it has no template "
                           "to say it; a meter is not measured by meters)")
            else:
                raw['kind'] = _dtypes.stored_form(k)
            probe = DeviceConfig(id=did, name=raw['name'], protocol='none')
            links, lerrs = (meter_links.validate(config, probe, payload.get('meters'))
                            if payload.get('meters') is not None
                            else ([], {'meters': 'a meter-only device is its meters — name at least one'}))
            fields.update(lerrs)
            if not lerrs and not links:
                fields['meters'] = 'a meter-only device is its meters — name at least one'
            if not lerrs and links:
                legacy, listed = meter_links.stored_form(links)
                if legacy:
                    raw['energy_meter'] = legacy
                else:
                    raw['meters'] = listed
            if 'options' in payload:
                opts, oerrs = meter_links.check_options(payload['options'])
                fields.update(oerrs)
                pair = {ln['role'] for ln in (links or [])} >= {'input', 'output'}
                if not oerrs and opts and not pair:
                    fields['options'] = ('thresholds belong to an input and an output meter — '
                                         'this device has no pair')
                elif not oerrs and opts:
                    raw['meter_options'] = opts
        mqtt_block = dict(payload.get('mqtt') or {})
        if 'ha_discovery_enabled' in payload:
            mqtt_block['ha_discovery'] = flag(payload, 'ha_discovery_enabled')
        if mqtt_block:
            raw['mqtt'] = mqtt_block
        if payload.get('influxdb'):
            raw['influxdb'] = payload['influxdb']
        if errors or fields:
            raise HTTPException(status_code=422, detail={
                "errors": errors + [f"{k}: {v}" for k, v in fields.items()], "fields": fields})
        return raw

    def _timeout_error(conn: Dict, stored: Optional[Dict]) -> Optional[str]:
        """A Modbus timeout a save sets: 1..30 s, as the form says. One kept
        as stored (a sub-second value written by hand) is not judged again —
        it never blocks an edit of something else."""
        if 'timeout' not in conn or conn.get('timeout') == (stored or {}).get('timeout'):
            return None
        try:
            t = float(conn['timeout'])
        except (TypeError, ValueError):
            return "connection.timeout: must be 1..30 seconds"
        return None if 1 <= t <= 30 else "connection.timeout: must be 1..30 seconds"

    def _edit_locks(conn: Dict, template_id: str, existing_id: Optional[str]) -> List[Tuple[str, str]]:
        """What the edit locks refuse in a device save (connection_spec.edit_errors):
        the stored device's spec against the one this save would store, with
        its template's family and the components it holds."""
        from .bridges import TYPES as _BT, resolve_connection as _resolve
        from .connection_spec import edit_errors, spec_of
        from .device_template import template_transport
        conn = dict(conn or {})
        if conn.get('bridge') and str(conn.get('protocol', '')).lower() in VIRTUAL_MAP_PROTOCOLS:
            conn = {k: v for k, v in conn.items() if k not in ('bridge', 'bridge_port')}   # refused below
        new = spec_of(conn, config.bridges, _BT)
        old, was = None, None
        if existing_id:
            was = config.get_device(existing_id)
            if was is not None:
                old = spec_of((_stored_device_raw(was).get('connection')
                               or ((config.get_raw_device(existing_id) or {}).get('sources') or [{}])[0]),
                              config.bridges, _BT)
        proto = str((_resolve(conn, config.bridges) if conn.get('bridge') else conn).get('protocol', 'tcp')).lower()
        tpl = template_registry.get(template_id) if template_id else None
        fam = (template_transport(tpl) if tpl is not None and not tpl.component_of
               and proto in PROTOCOLS_BY_CONTEXT['device'] else None)
        return edit_errors(old, new, {
            'id': existing_id or '', 'components': len(config.component_devices(existing_id)) if was else 0,
            'template_changed': was is not None and template_id != (was.template or ''),
            'template': template_id, 'template_family': fam, 'protocol': proto})

    def _validate_device_payload(payload: Dict, *, existing_id: str = None,
                                 stored_conn: Optional[Dict] = None) -> Dict:
        """Normalize + validate the raw device dict from the UI. Raises
        HTTPException(422) with a per-field error list. ``stored_conn``: the
        connection saved before (an edit, a restore), whose values stand."""
        errors = []
        did = str(payload.get('id', existing_id or '')).strip().lower()
        if not _DEVICE_ID_RE.match(did):
            errors.append("id: use a-z 0-9 - _ (2-64 chars, starts alphanumeric)")
        if existing_id and did != existing_id:
            errors.append("id: cannot be changed after creation")
        if not existing_id and registry.has(did):
            errors.append(f"id: '{did}' already exists")
        if not existing_id and any(str(e.get('id')) == did for e in (config.endpoints or [])):
            # a formula asks for the device first: it would hide the installation
            errors.append(f"id: '{did}' is an installation's id — a device by that name would hide it in formulas")
        template_id = str(payload.get('template', '')).strip()
        conn = payload.get('connection', {}) or {}
        _conn_sent = conn          # as sent: a bridge's own values are not the device's to judge
        _none = str(conn.get('protocol', '')).lower() == 'none'
        # the edit locks, in one place (ADR-0026 §2): judged on the stored
        # device's spec and the one this save would store
        _locks = _edit_locks(conn, template_id, existing_id)
        if _locks and _locks[0][1] == METER_ONLY_TEXT:
            # read through a connection, or measured by its meters: decided at
            # creation (its meters and its kind come with it) — never by an
            # edit that would leave a device with neither (review N3)
            raise HTTPException(status_code=422, detail={
                "errors": [METER_ONLY_TEXT], "fields": {"connection.protocol": "fixed after creation"}})
        _lock_fields = {f: e.partition(': ')[2] if e.startswith(f + ': ') else e for f, e in _locks}
        if _none:
            return _meter_only_raw(payload, did, template_id, errors, existing_id)
        _bridge_ref = None
        if conn.get('bridge') and str(conn.get('protocol', '')).lower() in VIRTUAL_MAP_PROTOCOLS:
            errors.append(f"connection.bridge: a {conn.get('protocol')} bus is reached by its serial "
                          f"port or a bridge's host and port, not through a bridge record")
            conn = {k: v for k, v in conn.items() if k not in ('bridge', 'bridge_port')}
        if conn.get('bridge'):
            # a device on a bridge: the bridge must exist and own that port;
            # host, port and protocol then come from it (checked as such below)
            _br = config.get_raw_bridge(str(conn['bridge']))
            if _br is None:
                errors.append(f"connection.bridge: no bridge '{conn['bridge']}' — add it on the Devices page first")
            else:
                _bports = [int(p.get('port')) for p in (_br.get('ports') or []) if p.get('port')]
                from .bridges import TYPES as _BT, resolve_connection as _resolve
                _bt = _BT.get(_br.get('type'), {})
                if conn.get('bridge_port') is not None and _bt.get('discovery') == 'manual' \
                        and int(conn.get('bridge_port')) not in _bports:
                    errors.append(f"connection.bridge_port: {conn.get('bridge_port')} is not a port of "
                                  f"'{_br['id']}' ({', '.join(map(str, _bports)) or 'none declared'})")
                # what is SAVED is the reference; what is CHECKED is what it resolves to
                # (everything but the address, which is the bridge's: a tuning
                # key such as max_gap is the device's own, bridge or not)
                _bridge_ref = {k: v for k, v in conn.items() if v is not None and k not in (
                    'host', 'port', 'serial_port', 'url', 'broker', 'protocol', 'max_connections',
                    'baudrate', 'parity', 'stopbits', 'bytesize')}   # the line is the bridge's
                if str(conn.get('protocol', '')).lower() == 'rtu_tap':
                    # listening needs the bus's own bytes: a gateway turns them
                    # into Modbus TCP and only answers questions asked of it
                    if _bt.get('framing', 'rtu') == 'modbus_tcp':
                        errors.append(f"connection.protocol: '{_br['id']}' is a Modbus TCP gateway — it only "
                                      "answers questions, it does not pass the bus's traffic on; listening "
                                      "needs a transparent bridge or the MBG serial bridge")
                    elif _bt.get('framing', 'rtu') != 'rtu':
                        errors.append(f"connection.protocol: '{_br['id']}' carries {_bt.get('framing')} frames — "
                                      "listening decodes Modbus RTU")
                    _bridge_ref['protocol'] = 'rtu_tap'
                conn = _resolve(conn, config.bridges)
                _bridge_ref.setdefault('bridge_port', conn.get('port'))
        protocol = str(conn.get('protocol', 'tcp')).lower()
        _asks = ((protocol in MODBUS_PROTOCOLS and protocol != 'rtu_tap')
                 or (protocol in VIRTUAL_MAP_PROTOCOLS and _vmap_mode(conn) == 'poll'))
        if protocol not in PROTOCOLS_BY_CONTEXT['device']:
            errors.append("connection.protocol: must be one of " + ", ".join(PROTOCOLS_BY_CONTEXT['device']))
        elif _asks:
            _te = _timeout_error(conn, stored_conn)
            if _te:
                errors.append(_te)
        # A template's register map is transport-specific (Modbus reads by address,
        # HTTP/MQTT by json_path), so the device protocol MUST match the template's
        # transport class — otherwise every read silently resolves to nothing.
        _tpl = template_registry.get(template_id) if template_id else None
        if template_id and _tpl is None:
            errors.append(f"template: '{template_id}' not found")
        elif _tpl is not None and _tpl.component_of:
            errors.append(f"template: '{template_id}' is a component of '{_tpl.component_of}' — "
                          "add the device, then this as one of its components")
            _tpl = None
        if _locks:
            # a template's family, the class a device keeps, a device holding
            # components (edit_errors): where the family check always stood
            errors.extend(e for _f, e in _locks)
        if (_tpl is not None and protocol in PROTOCOLS_BY_CONTEXT['device']
                and not any(f == 'template' for f, _e in _locks)):
            # the template's family matches: does it only ever listen?
            from .device_template import template_listens, template_listens_only
            if (template_listens_only(_tpl) and protocol in VIRTUAL_MAP_PROTOCOLS
                    and _vmap_mode(conn) != 'listen'):
                errors.append(f"connection.mode: template '{template_id}' is only heard on a bus "
                              "another master polls — choose listen")
            elif protocol_family(protocol) == 'modbus' and protocol != 'rtu_tap' and template_listens(_tpl):
                # a map written for listening never polls (ADR-0025, ADD-8)
                errors.append(f"connection.protocol: template '{template_id}' is only heard on a bus "
                              "another master polls — choose listen only (rtu_tap)")
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
            else:
                from .text_check import topic_issue
                _ti = topic_issue(str(conn.get('topic')), "connection.topic")
                if _ti:
                    errors.append(_ti)
            from .mqtt_input import keepalive_errors
            errors.extend(keepalive_errors(conn.get('keepalive'), 'connection.keepalive'))
            if conn.get('tls_ca') is not None and not isinstance(conn.get('tls_ca'), str):
                errors.append("connection.tls_ca: the path of a CA certificate file")
            _vars = conn.get('vars')
            if _vars is not None:
                from .mqtt_input import VAR_VALUE_RE
                if not isinstance(_vars, dict) or len(_vars) > 16:
                    errors.append("connection.vars: a mapping of up to 16 topic variables")
                else:
                    for _k, _v in _vars.items():
                        from .text_check import topic_issue
                        _ti = topic_issue(str(_v), f"connection.vars.{_k}")
                        if _ti:
                            errors.append(_ti)
                            continue
                        if not re.match(r"^[a-z][a-z0-9_]{0,31}$", str(_k)) or not VAR_VALUE_RE.match(str(_v)):
                            errors.append(f"connection.vars.{_k}: a name (a-z 0-9 _) and topic levels without + or #")
            try:
                mp = int(conn.get('port', 1883))
                if not (1 <= mp <= 65535):
                    raise ValueError
            except (TypeError, ValueError):
                errors.append("connection.port: must be 1..65535")
        elif protocol in CAN_PROTOCOLS:
            # a CAN bus listened to through a carrier (can_client.py)
            from .can_client import connection_errors as _can_errors
            errors.extend(_can_errors(conn))
        elif protocol in VIRTUAL_MAP_PROTOCOLS:
            # a non-Modbus bus with its own driver: a serial port here, or a
            # bridge's raw byte stream at host:port; polled or listened to
            if _vmap_mode(conn) not in _VMAP_MODES:
                errors.append("connection.mode: poll (the gateway asks) or listen (it never transmits)")
            elif _vmap_mode_missing(conn, stored_conn):
                errors.append("connection.mode: choose poll (the gateway asks) or listen (it never "
                              "transmits) — a bus a master pack already polls must be listened to")
            if protocol in VIRTUAL_MAP_SERIAL:
                sp = str(conn.get('serial_port', '')).strip()
                if not sp:
                    errors.append(f"connection.serial_port: required for {_PROTO_WORDS.get(protocol, protocol)}")
                else:
                    errors.extend(_serial_line_conflicts(protocol, sp, conn, existing_id))
                for fld, lo, hi in (("baudrate", 300, 4_000_000), ("stopbits", 1, 2), ("bytesize", 5, 8)):
                    if fld in conn:
                        try:
                            if not (lo <= int(conn.get(fld)) <= hi):
                                raise ValueError
                        except (TypeError, ValueError):
                            errors.append(f"connection.{fld}: must be {lo}..{hi}")
                if str(conn.get('parity', 'N')).upper() not in ('N', 'E', 'O'):
                    errors.append("connection.parity: must be N, E or O")
            else:
                if not str(conn.get('host', '')).strip():
                    errors.append(f"connection.host: required for {_PROTO_WORDS.get(protocol, protocol)}")
                elif _is_serial_path(conn.get('host')):
                    errors.append(_serial_host_error("connection.host", conn.get('host')))
                try:
                    if not (1 <= int(conn.get('port', 0)) <= 65535):
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append("connection.port: must be 1..65535")
                else:
                    errors.extend(_network_bus_conflicts(protocol, conn, existing_id))
            try:
                if not (0 <= int(conn.get('unit_id', 0)) <= 255):
                    raise ValueError
            except (TypeError, ValueError):
                errors.append("connection.unit_id: must be 0..255 (the pack address)")
        elif protocol == 'rtu_tap' and _bridge_ref:
            # a tap through a bridge: the bus is the bridge's port
            errors.extend(_network_bus_conflicts(protocol, conn, existing_id))
            try:
                if not (0 <= int(conn.get('unit_id', 1)) <= 255):
                    raise ValueError
            except (TypeError, ValueError):
                errors.append("connection.unit_id: must be 0..255")
        elif protocol in SERIAL_PROTOCOLS + ('rtu_tap',):
            sp = str(conn.get('serial_port', '')).strip()
            if not sp:
                errors.append(f"connection.serial_port: required for Modbus {'ASCII' if protocol == 'ascii' else 'RTU'}")
            else:
                errors.extend(_serial_line_conflicts(protocol, sp, conn, existing_id))
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
            if protocol in NETWORK_PROTOCOLS and not str(conn.get('host', '')).strip():
                errors.append(f"connection.host: required for {_PROTO_WORDS.get(protocol, protocol)}")
            elif _is_serial_path(conn.get('host')):
                errors.append(_serial_host_error("connection.host", conn.get('host')))
            try:
                port = int(conn.get('port', 502))
                if not (1 <= port <= 65535):
                    raise ValueError
            except (TypeError, ValueError):
                errors.append("connection.port: must be 1..65535")
            if protocol in BUS_OVER_TCP_PROTOCOLS or _bridge_ref:
                errors.extend(_network_bus_conflicts(protocol, conn, existing_id))
            try:
                unit = int(conn.get('unit_id', 1))
                if not (0 <= unit <= 255):
                    raise ValueError
            except (TypeError, ValueError):
                errors.append("connection.unit_id: must be 0..255")
        # the advanced keys a save sets anew, each in its range (L3-F7)
        from .conn_advanced import errors as _advanced_errors
        errors.extend(_advanced_errors(_conn_sent if isinstance(_conn_sent, dict) else {}, stored_conn, protocol))
        if errors:
            _d = _errors_detail(errors)
            for _f, _m in _lock_fields.items():
                _d.setdefault('fields', {}).setdefault(_f, _m)
            raise HTTPException(status_code=422, detail=_d)
        raw = {
            'id': did,
            'name': str(payload.get('name', '') or did),
            'template': template_id,
            'enabled': flag(payload, 'enabled', True),
            # a device on a bridge keeps only the reference — host, port and
            # protocol are the bridge's, resolved whenever the list is built
            'connection': _bridge_ref or conn,
        }
        mqtt_block = dict(payload.get('mqtt') or {})
        # per-device HA discovery toggle lives under mqtt.ha_discovery
        if 'ha_discovery_enabled' in payload:
            mqtt_block['ha_discovery'] = flag(payload, 'ha_discovery_enabled')
        if mqtt_block:
            raw['mqtt'] = mqtt_block
        if payload.get('influxdb'):
            raw['influxdb'] = payload['influxdb']
            _e = _influx_block_errors(raw['influxdb'], 'influxdb', outputs=False)
            if _e:
                raise HTTPException(status_code=422, detail={"errors": _e})
        if 'opcua' in payload:
            raw['opcua'] = _opcua_block(payload['opcua'])
        return raw

    def _opcua_sink(dev_cfg) -> Dict:
        ctl = getattr(app.state, 'opcua', None)
        try:
            st = ctl.status() if ctl is not None else {}
        except Exception:  # noqa: BLE001
            st = {}
        running = bool(st.get('running'))
        return {'enabled': dev_cfg.opcua_enabled, 'available': bool(st.get('enabled')),
                'connected': running, 'active': dev_cfg.opcua_enabled and running,
                'fields': list(dev_cfg.opcua_fields), 'endpoint': st.get('endpoint', ''),
                'shared_by': dev_cfg.group_id or dev_cfg.endpoint_id if dev_cfg.endpoint_id else ''}

    def _opcua_block(raw) -> Dict:
        """A device's `opcua:` output: {enabled, fields} (fields: the value
        names served; omitted = every value)."""
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail={"errors": ["opcua: an object {enabled, fields}"]})
        fields = raw.get('fields') or []
        if not isinstance(fields, list) or not all(isinstance(f, str) and f.strip() for f in fields):
            raise HTTPException(status_code=422, detail={"errors": ["opcua.fields: a list of value names"]})
        out = {'enabled': bool(raw.get('enabled', False))}
        names = list(dict.fromkeys(f.strip() for f in fields))
        if names:
            out['fields'] = names
        return out

    def _meters_status(device_id: str) -> Dict:
        """The last evaluation's verdict of a device's input / output pair,
        with its age — never a judgement made again at request time."""
        if device_id not in calc_engine.pairs:
            return {}
        v = calc_engine.pair_verdicts.get(device_id)
        if v is None:
            wait = {'state': 'waiting', 'reason': 'not_evaluated'}
            return {'power_loss': dict(wait), 'efficiency_pct': dict(wait), 'load_pct': dict(wait),
                    'age_s': None}
        return {**{k: dict(x) if isinstance(x, dict) else x for k, x in v.items() if k != 'at'},
                'age_s': round(max(0.0, time.time() - v['at']), 1)}

    def _raw_index() -> Dict[str, Dict]:
        """Every stored entry by device id, units and components expanded
        once — what a whole list reads (review: no re-expansion per row)."""
        out = {str(d.get('id')): d for d in config._raw_devices if isinstance(d, dict)}
        try:
            for _pid, d in config._expand_endpoints():
                out.setdefault(str(d.get('id')), d)
            for _pid, _c, d in config._expand_components():
                out.setdefault(str(d.get('id')), d)
        except Exception:  # noqa: BLE001 — a row falls back to its own lookup
            logger.debug("raw index incomplete", exc_info=True)
        return out

    def _stored_device_raw(dev_cfg, raw_index=None) -> Dict:
        """The device as stored (or, for the primary, an installation's unit
        and a component, as its stored definition expands): what the spec
        reads and the ETag hashes."""
        if raw_index is not None and not dev_cfg.primary and dev_cfg.id in raw_index:
            return raw_index[dev_cfg.id]
        if dev_cfg.primary:
            m = config.modbus
            return {'id': dev_cfg.id, 'connection': {'protocol': 'tcp', 'host': m.host, 'port': m.port,
                                                     'unit_id': m.unit_id, 'timeout': m.timeout,
                                                     'retry_attempts': m.retry_attempts,
                                                     'retry_delay': m.retry_delay},
                    'ha_discovery_enabled': bool(config.mqtt.ha_discovery_enabled)}
        raw = config.get_raw_device(dev_cfg.id)
        if raw is not None:
            return raw
        if dev_cfg.endpoint_id:
            ep = config.get_raw_endpoint(dev_cfg.endpoint_id)
            for _pid, d in (config._expand_endpoints([ep]) if ep else []):
                if d.get('id') == dev_cfg.id:
                    return d
        if dev_cfg.component_of:
            for _pid, _c, d in config._expand_components():
                if d.get('id') == dev_cfg.id:
                    return d
        return {'id': dev_cfg.id, 'connection': {'protocol': dev_cfg.protocol}}

    # the ETag's key: random per process. A bare hash of the stored entry
    # would let a viewer confirm a guessed password against it; keyed, it
    # says only "changed or not" (review). An ETag held across a restart is
    # stale: 412, read again.
    _etag_key = __import__('secrets').token_bytes(32)

    def _primary_kept(which: str) -> Dict[str, Any]:
        """The primary's opcua / rest_push section in the form its file keeps
        (memory set by any route agrees; a hand-loaded one too)."""
        from .config import primary_opcua_kept, primary_rest_push_kept
        return (primary_opcua_kept(config.opcua_primary) if which == 'opcua'
                else primary_rest_push_kept(config.rest_push_primary))

    def _device_etag(dev_cfg, raw_index=None) -> str:
        """An HMAC of the stored entry (sorted keys): the same while nothing
        stored changes, a reload included (ADR-0026 §6)."""
        stored = _stored_device_raw(dev_cfg, raw_index)
        if dev_cfg.primary:
            # the primary's other flat sections are its stored entry too: an
            # output or its write lock changed is a new version (L3.11-F1)
            stored = {**stored, '_flat': {'http_output': bool(config.http_output_primary_enabled),
                                          'opcua_output': _primary_kept('opcua'),
                                          'rest_push': _primary_kept('rest_push'),
                                          'pq_recorder': config.pq_recorder_primary or {},
                                          'write_locked': bool(config.security.primary_write_locked)}}
        body = json.dumps(stored, sort_keys=True, default=str, separators=(',', ':'))
        return '"' + hmac.new(_etag_key, body.encode('utf-8'), hashlib.sha256).hexdigest()[:32] + '"'

    def _check_if_match(if_match: Optional[str], device_id: str) -> None:
        """412 with the current entry when ``If-Match`` names another version
        of the device; nothing to check without the header (as before)."""
        if if_match is None or not device_id:
            return
        _i, dev_cfg, client = _find_device(device_id)
        if dev_cfg is None:
            return                                  # the caller answers 404
        cur = _device_etag(dev_cfg)
        if _etag_guard.matches(if_match, cur):
            return
        raise HTTPException(status_code=412, detail={
            "errors": ["If-Match: the device changed since it was read — reload it and apply the change again"],
            "current": _device_entry(dev_cfg, client), "etag": cur}, headers={'ETag': cur})

    def _device_tag_now(device_id) -> Optional[str]:
        """The device's ETag now (None once it is gone)."""
        if not device_id:
            return None
        _i, dev_cfg, _c = _find_device(str(device_id))
        return _device_etag(dev_cfg) if dev_cfg is not None else None

    # One lock per stored entry, held by a checked write around its check,
    # its write and its tag (ADR-0030): two writes with one tag never both
    # pass. Keyed by what owns the stored entry — a component's parent, a
    # unit's installation. Always taken before _device_mutation_lock (the
    # guard sits above @_serialized_mutation), never after it: one order.
    _entry_locks: Dict[str, threading.Lock] = {}
    _entry_locks_guard = threading.Lock()

    def _entry_lock(device_id) -> Optional[threading.Lock]:
        if not device_id:
            return None
        _i, dev_cfg, _c = _find_device(str(device_id))
        if dev_cfg is None:
            return None                          # the route answers 404
        key = (f"dev:{dev_cfg.component_of}" if dev_cfg.component_of
               else f"ep:{dev_cfg.endpoint_id}" if dev_cfg.endpoint_id else f"dev:{dev_cfg.id}")
        with _entry_locks_guard:
            return _entry_locks.setdefault(key, threading.Lock())

    def _device_write(fn):
        """A route under /api/devices/{device_id} that changes the stored
        entry: a stale If-Match answers 412, every answer says the tag after
        it (ADR-0030). Check, write and tag run under the entry's lock; put
        it ABOVE @_serialized_mutation (lock order)."""
        return _etag_guard.guard(fn, 'check',
                                  check=lambda im, kw: _check_if_match(im, kw.get('device_id')),
                                  tag=lambda kw: _device_tag_now(kw.get('device_id')),
                                  lock=lambda kw: _entry_lock(kw.get('device_id')))

    def _device_write_query(fn):
        """The same, for a route that names its device by the ``device``
        query parameter (empty: the primary) — state kept in the entry but
        written outside /api/devices/{id} (the PQ recorder)."""
        def _did(kw):
            return kw.get('device') or config.primary_id
        return _etag_guard.guard(fn, 'check',
                                 check=lambda im, kw: _check_if_match(im, _did(kw)),
                                 tag=lambda kw: _device_tag_now(_did(kw)),
                                 lock=lambda kw: _entry_lock(_did(kw)))

    def _device_runtime(fn):
        """A route under /api/devices/{device_id} that acts but stores
        nothing (a write to the device, a command, a test): never 412, the
        current tag said (ADR-0030)."""
        return _etag_guard.guard(fn, 'runtime', tag=lambda kw: _device_tag_now(kw.get('device_id')))

    def _device_specs(dev_cfg, raw_index=None):
        """(connection_spec, source_specs) as the API shows them."""
        from .bridges import TYPES as _BT
        from .connection_spec import device_specs, public
        dev, per = device_specs(_stored_device_raw(dev_cfg, raw_index), config.bridges, _BT)
        return public(dev), [{'id': p['id'], **public(p)} for p in per]

    def _refuse_prefix(field: str, prefix, own) -> None:
        """422 when a prefix being set breaks the rule (ADR-0026 §4): a
        duplicate, an overlap either way, a wildcard, the gateway's root."""
        from .topic_prefix import prefix_errors
        errs = prefix_errors(prefix, config, own)
        if errs:
            raise HTTPException(status_code=422, detail={
                "errors": [f"{field}: {e}" for e in errs], "fields": {field: errs[0]}})

    def _drop_spec_keys(payload):
        """A spec is output only (ADR-0026 §1): connection_spec.drop_spec_keys."""
        from .connection_spec import drop_spec_keys
        return drop_spec_keys(payload)

    def _set_new_node(mqtt_block: Dict, did: str) -> None:
        """The Home Assistant node a NEW device (or installation, by
        pattern) stores beside its prefix (ADR-0026 §3): under `gateway`
        `mbg_<gateway>_dev_<id>`; under `legacy` nothing (`mbg_dev_<id>`).
        Never what a payload says."""
        from .identity import new_node
        node = new_node(did, config.identity_scope, config.gateway_id)
        if node:
            mqtt_block['ha_node'] = node
        else:
            mqtt_block.pop('ha_node', None)

    def _device_entry(dev_cfg, client, *, redact=False, raw_index=None, claims=None) -> Dict:
        entry = dev_cfg.summary()
        entry['kind'], entry['kind_source'] = _device_kind(dev_cfg, template_registry)
        entry['type'], entry['type_label'] = _device_type_of(dev_cfg)          # ADR-0027
        try:
            _labels = attributes.labels_of('device', dev_cfg.id)
        except Exception:  # noqa: BLE001 — a label never fails a list
            _labels = []
        if _labels:
            entry['labels'] = _labels
        # whether acquisition runs for it: an enabled device that does not is
        # revived by saving it again (update_device) — the page offers that
        entry['running'] = client is not None
        _nc = sum(1 for d in config.devices if d.component_of == dev_cfg.id)
        if _nc:
            entry['components'] = _nc
        if dev_cfg.component_of:
            entry['component_type_name'] = _component_type_name(dev_cfg)
        # the bridge (and its bus port) this device reaches its bus through
        _bid = config._device_bridge(dev_cfg.id)
        if _bid:
            entry['bridge'] = _bid
            _raw = next((x for x in config._raw_devices if x.get('id') == dev_cfg.id), {}) or {}
            entry['bridge_port'] = (_raw.get('connection') or {}).get('bridge_port') or dev_cfg.connection.port
        regs = config.unit_registers(dev_cfg, shared=True)   # every source's selection, one entry per name (read only)
        entry['selected_registers'] = len(regs)
        # an energy counter to chart (the Energy tab means nothing without one)
        _E_UNITS = ('wh', 'kwh', 'mwh', 'varh', 'kvarh', 'vah', 'kvah', 'mvah')
        entry['energy_fields'] = sum(1 for r in regs if str(getattr(r, 'unit', '') or '').lower() in _E_UNITS
                                     or str(getattr(r, 'name', '')).startswith('energy_'))
        # another device that measures this one's electricity (linked_meter.py)
        from .meter_links import link_for, links_of
        # the consumption link in its earlier shape, wherever it is stored
        # (a sign other than auto says itself)
        _cons = link_for(dev_cfg, 'consumption')
        entry['energy_meter'] = ({k: v for k, v in _cons.items() if k != 'role' and (k != 'sign' or v != 'auto')}
                                 if _cons else {})
        entry['meters'] = links_of(dev_cfg)
        # an input / output pair: its honesty thresholds, and the verdict of
        # its LAST evaluation — what was published, what went absent and why
        # ({} without a pair; `waiting` until a meter has reported)
        from .meter_links import options_of
        entry['meter_options'] = options_of(dev_cfg)
        # the device's own thresholds, apart from the defaults: an editor
        # sends these back (a key left out is its default, null is refused)
        entry['meter_options_own'] = dict(getattr(dev_cfg, 'meter_options', None) or {})
        entry['meters_status'] = _meters_status(dev_cfg.id)
        # its meters' counters (built when its calculated fields loaded — the
        # list never reads the meters' files)
        entry['energy_fields'] += sum(1 for e in calc_engine.store.get(dev_cfg.id, [])
                                      if e.get('_linked') and e['_reg'].name in
                                      ('energy_electric', 'energy_in', 'energy_out'))
        entry['influxdb_device_tag'] = dev_cfg.influxdb_device_tag
        from .pq_recorder import template_supports_pq
        _tpl = (template_registry.get(dev_cfg.template)
                if template_registry and dev_cfg.template else None)
        entry['pq_supported'] = template_supports_pq(_tpl, dev_cfg.template)
        # its template declares components (a GX's batteries): Components…
        # is offered from the row, not after a template list loaded (L3.10 m4)
        entry['components_declared'] = bool(getattr(_tpl, 'components', None) or [])
        entry['template_notes'] = list(getattr(_tpl, 'notes', None) or [])
        entry['pq_recorder'] = dict(dev_cfg.pq_recorder or {})
        # Replace hardware's energy-counter reset markers (PATCH ?intent=replace,
        # L3-F17): [{at, ts, reason}], oldest first — only when there are any
        _resets = ([] if dev_cfg.primary
                   else _stored_device_raw(dev_cfg, raw_index).get('energy_counter_resets') or [])
        if _resets:
            entry['energy_counter_resets'] = list(_resets)
        try:
            _cmds = _commands_for(dev_cfg)
        except Exception:  # noqa: BLE001 — a bad recipe must not hide the device
            _cmds = []
        entry['commands'] = [c['def'].name for c in _cmds if c['enabled']]
        entry['commands_offered'] = [c['def'].name for c in _cmds]
        # the registers its template declares writable: an operator writes
        # only these (a raw write is the admin's) — the ⋯ menu says so before
        # the form is typed (ADR-0032 §3)
        entry['writable_registers'] = _declared_writes(dev_cfg)
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
        if dev_cfg.protocol == 'none':
            # a device that IS its meters has no address: none is shown, and
            # this is what a save sends back (review N3)
            from .meter_links import links_of as _lo
            entry['connection'] = {'protocol': 'none'}
            entry['measured_by'] = list(dict.fromkeys(ln['device'] for ln in _lo(dev_cfg)))
        if dev_cfg.protocol == 'mqtt':
            m = dev_cfg.mqtt_in
            entry['connection'].update({
                'broker': m.get('broker', ''), 'port': m.get('port', 1883),
                'topic': m.get('topic', ''), 'username': m.get('username', ''),
                'tls': bool(m.get('tls', False)),
                'tls_insecure': bool(m.get('tls_insecure', False)),
                'tls_ca': m.get('tls_ca', ''),
                'password': '******' if m.get('password') else '',   # never echo the secret
                'vars': dict(m.get('vars') or {}),
                'stale_after_s': m.get('stale_after_s'),
                # the device's own keepalive (a dict, or false = off), and the
                # template's — what applies when the device says nothing
                'keepalive': m.get('keepalive'),
            })
            _tk = template_registry.get(dev_cfg.template) if dev_cfg.template else None
            entry['connection']['template_keepalive'] = ((getattr(_tk, 'protocol', None) or {}).get('keepalive')
                                                         if _tk is not None else None)
        if dev_cfg.protocol in CAN_PROTOCOLS:
            # what the connection form shows for a CAN link: the carrier, the
            # interface, the bitrate the bus runs at, how long quiet stays fresh
            _can = dict(getattr((dev_cfg.sources or [None])[0], 'can', None) or {})
            entry['connection'].update({
                'carrier': _can.get('carrier', 'socketcan'), 'channel': _can.get('channel', ''),
                'bitrate': _can.get('bitrate'),
                'stale_after_s': _can.get('stale_after_s', c.stale_after_s),
            })
        if dev_cfg.protocol in VIRTUAL_MAP_PROTOCOLS:
            entry['connection']['mode'] = c.mode or 'poll'
        # what the stored connection means, on its axes (ADR-0026 §1): beside
        # `connection`, never inside it — a save echoes `connection` back
        try:
            _spec, _per = _device_specs(dev_cfg, raw_index)
            entry['connection_spec'] = _spec
            if _per:
                entry['source_specs'] = _per
        except Exception:  # noqa: BLE001 — an unreadable shape must not hide the device
            logger.debug("device %s: connection spec unavailable", dev_cfg.id, exc_info=True)
            entry['connection_spec'] = None
        # the one-line connection summary and the edit locks (ADR-0031 §5),
        # beside `connection` like the spec: never sent back by a save
        try:
            from .device_summary import device_view
            entry.update(device_view(config, dev_cfg, template_registry,
                                     raw=_stored_device_raw(dev_cfg, raw_index),
                                     spec=entry.get('connection_spec'), claims=claims))
        except Exception:  # noqa: BLE001 — a summary must not hide the device
            logger.debug("device %s: summary unavailable", dev_cfg.id, exc_info=True)
            entry['summary'], entry['locks'] = None, []
        # what each PATCH section takes for it (ADR-0032 §6): the primary's
        # adapter takes fewer keys; a component or a unit is edited elsewhere
        from .device_patch import edit_sections
        entry['edit_sections'] = edit_sections(dev_cfg.primary,
                                               whole_refused=bool(dev_cfg.component_of or dev_cfg.endpoint_id))
        if dev_cfg.protocol in SERIAL_PROTOCOLS + ('rtu_tap',) + VIRTUAL_MAP_SERIAL:
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
            # OPC UA: served on the gateway's OPC UA server (Settings → OPC UA),
            # every value or the chosen ones; active while the server runs
            'opcua': _opcua_sink(dev_cfg),
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
        entry['publishing'] = _publishing(dev_cfg)
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
        stats, verdicts = {}, {}
        if client:
            stats = client.get_stats()
            connected = stats.get('connected')
            hv = client.data_health()
            health = hv.get('status')
            verdicts = dict(hv.get('sources') or {})
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
                # a push-fed device (MQTT, tap) is never polled: its age is the
                # age of its last message, which data_health() measures
                'staleness_age_s': (stats.get('staleness_age_s')
                                    if stats.get('staleness_age_s') is not None
                                    else hv.get('staleness_age_s')),
                'last_read_ts': stats.get('last_success_ts'),
                'poll_rate': stats.get('poll_rate'),
                'data_health': health,
            })
            from .can_client import stats_summary as _can_summary
            _cs = _can_summary(stats)
            if _cs:
                entry['can'] = _cs          # frames, unknown ids, decode errors, listen-only
        else:
            # disabled or transport not run — idle (grey), not green
            entry.update({'connected': False, 'data_health': 'idle'})
        _unit_read_via(dev_cfg, entry, stats, verdicts, redact=redact)
        return entry

    def _unit_read_via(dev_cfg, entry: Dict, stats: Dict, verdicts: Dict, *, redact=False) -> None:
        """How a unit of an installation is actually read, for its own page.

        The flat ``connection`` block above is the fallback a unit was born with;
        a unit read through sources is described by THEM — protocol, address,
        interval, template, and what each one delivers right now. The unit page
        showed a Modbus form with an empty host for a unit read over HTTP every
        two seconds; this is the truth it shows instead."""
        if not dev_cfg.endpoint_id:
            return
        from .redact import redact_url
        ep = config.get_raw_endpoint(dev_cfg.endpoint_id) or {}
        role = getattr(dev_cfg, 'role', '') or ''
        entry['endpoint_name'] = ep.get('name') or dev_cfg.endpoint_id
        entry['group_id'] = getattr(dev_cfg, 'group_id', '') or 'units'
        entry['role'] = role
        entry['glance'] = list(_glance_names(dev_cfg))
        entry['alarms'] = _unit_alarms(dev_cfg)
        entry['live'] = _unit_live(dev_cfg, entry['glance'])
        _sm = _store_meta([dev_cfg])
        entry['fields'] = {n: m for n, m in ((n, _field_meta_any(n, _sm)) for n in entry['glance']) if m}
        by_id = {r.get('id'): r for r in (stats.get('sources') or [])}
        provides: Dict[str, int] = {}
        for _f, prov in (stats.get('provenance') or {}).items():
            provides[prov.get('source')] = provides.get(prov.get('source'), 0) + 1
        out = []
        for src in dev_cfg.sources or []:
            proto = str(src.protocol or 'tcp').lower()
            if proto == 'http':
                url = str((src.http or {}).get('url', ''))
                address = redact_url(url) if redact else url
            elif proto == 'mqtt':
                address = str((src.mqtt_in or {}).get('topic', ''))
            elif proto in SERIAL_PROTOCOLS + ('rtu_tap',) + VIRTUAL_MAP_SERIAL:
                address = f"{src.connection.bus} · unit {src.connection.unit_id}"
            else:
                address = f"{src.connection.host}:{src.connection.port} · unit {src.connection.unit_id}"
            regs, groups = config.load_device_registers(dev_cfg, source=src)
            used = {r.poll_group for r in regs}
            ivals = [g.interval for n, g in groups.items() if n in used]
            live = by_id.get(src.id) or {}
            r5 = live.get('reads_5m') or 0
            timeout = ((src.http or {}).get('timeout') if proto == 'http'
                       else getattr(src.connection, 'timeout', None))
            out.append({
                'id': src.id, 'protocol': proto, 'address': address,
                'template': src.template or dev_cfg.template,
                'enabled': bool(getattr(src, 'enabled', True)),
                'interval_s': min(ivals) if ivals else None,
                'timeout_s': timeout,
                'stale_after_s': float(getattr(src, 'stale_after_s', 0) or 0),
                'registers': len(regs),
                'provides': provides.get(src.id, 0),
                'status': verdicts.get(src.id, 'idle' if not stats else 'down'),
                'latency_ms': live.get('last_latency_ms'),
                'reads_5m': r5, 'failed_5m': live.get('failed_5m') or 0,
                'fail_pct_5m': (round(100.0 * (live.get('failed_5m') or 0) / r5, 1)
                                if r5 else None),
            })
        entry['read_via'] = out

    # outermost of all: every response, including the guards' own 401/403s
    app.middleware("http")(_security_headers)

    @app.get("/api/devices")
    def list_devices(request: Request, q: Optional[str] = None, kind: Optional[str] = None,
                     state: Optional[str] = None, protocol: Optional[str] = None,
                     installation: Optional[str] = None, sort: Optional[str] = None,
                     type: Optional[str] = None, label: Optional[str] = None):  # noqa: A002
        """All southbound devices with live health (device #1 first).
        Optional: ``q`` (search), ``kind``, ``type`` (the device type, a user
        type included), ``label`` (its identity labels), ``state``
        (online/offline/disabled), ``protocol``, ``installation`` — each a
        comma list — and ``sort`` (name, id, kind, state, protocol; ``-`` for
        descending)."""
        # only admin sees raw credentials; viewer AND operator get redacted URLs
        _redact = getattr(request.state, "role", None) != "admin" and getattr(request.state, "role", None) is not None
        _idx = _raw_index()
        from .device_summary import Claims
        _claims = Claims(config, template_registry)        # the bus claims, once per list
        entries = [(d, _device_entry(d, c, redact=_redact, raw_index=_idx, claims=_claims))
                   for d, c in registry]
        if all(v in (None, '') for v in (q, kind, state, protocol, installation, sort, type, label)):
            return {"devices": [e for _d, e in entries]}
        from .list_filters import FilterError, device_protocols, device_search_text, filter_devices
        rows = [(e, device_protocols(d), device_search_text(d, e, template_registry, redact=_redact))
                for d, e in entries]
        try:
            return {"devices": filter_devices(rows, q=q, kind=kind, state=state, protocol=protocol,
                                              installation=installation, sort=sort, type=type, label=label)}
        except FilterError as err:
            raise HTTPException(status_code=422, detail={"errors": [str(err)], "fields": err.fields})

    # /api/serial-ports, /api/bridge/adapters → routes/commissioning.py

    _late_hooks: Dict[str, Any] = {}

    # devices whose HA discovery this gateway has taken back (MQTT or its
    # discovery off), kept across restarts: a device cleared once is not
    # cleared again at every start (ADR-0025, ADD-14)
    _disc_cleared_path = config.config_path.parent / "ha_discovery_cleared.json"
    try:
        _disc_cleared: Set[str] = set(json.loads(_disc_cleared_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        _disc_cleared = set()

    def _mark_disc_cleared(device_id: str, cleared: bool) -> None:
        if (device_id in _disc_cleared) == cleared:
            return
        (_disc_cleared.add if cleared else _disc_cleared.discard)(device_id)
        try:
            _disc_cleared_path.write_text(json.dumps(sorted(_disc_cleared)), encoding="utf-8")
        except OSError as e:
            logger.warning(f"HA discovery: cannot keep which devices were cleared: {e}")

    def _ha_write_rules(d, regs) -> Dict:
        """The registers a device announces as write entities (number/select),
        by store key: only when FULLY enabled; the template is the write
        allowlist, so writability + bounds come from _write_rule, never from
        the saved register."""
        wrules = {}
        if not (config.mqtt.allow_write_entities and config.security.allow_writes
                and not d.primary and d.protocol != 'http'):
            return wrules
        for r in regs:
            if getattr(r, 'register_type', 'holding') != 'holding':
                continue
            rule = _write_rule(d, r.address, 'holding')
            if rule is None or not rule.writable:
                continue
            # advertise only what the write handler would accept:
            # a scale_from or command-fronted register writes
            # THROUGH its command, so without an enabled HA-faced
            # value command the entity would be dead — or, worse,
            # a raw write with the wrong scale (audit 2026-10)
            _name = getattr(r, 'name', '')
            _cmds = _commands_for(d)
            _ha_cmd = any(
                c['enabled'] and c['faces'].get('ha', True)
                and c['def'].writes and c['def'].value_param is not None
                and c['def'].writes[0].get('register') == _name
                for c in _cmds)
            # what the handler refuses is not advertised: any
            # register any command writes on this slave (D4)
            _fronted = _raw_channel_conflict(
                d, 'holding', int(r.address), _words_of(rule.data_type or 'uint16')) is not None
            if (getattr(rule, 'scale_from', None) or _fronted) and not _ha_cmd:
                continue
            if _writes_by_mqtt(d) and not _ha_cmd:
                continue          # an MQTT unit writes through commands only
            wrules[store_key(r.address, 'holding')] = rule   # keyed by table: a coil at
            # the same address must never pick up this holding row's rule
        return wrules

    def _discovery_regs(d) -> List:
        regs = config.unit_registers(d)     # all of the unit's sources, one entry per name
        # derived measurements (status/text, status/alarm, …) are entities too
        return regs + [e['_reg'] for e in calc_engine.store.get(d.id, []) if e.get('_reg') is not None]

    from .device_types import meter_only_model as _meter_only_model

    def _sync_device_discovery():
        """Rebuild the MQTT discovery hooks from the current non-primary
        devices and publish them now (so HA sees a device the moment it is
        added/edited, not only on the next reconnect). Idempotent (retained)."""
        if not mqtt_publisher or not gw_role.active:   # the active one announces the devices
            return
        hooks = []
        for dev_cfg, _c in registry:
            if dev_cfg.primary:
                continue
            if not (dev_cfg.ha_discovery_enabled and dev_cfg.mqtt_enabled):
                if dev_cfg.id in _disc_cleared:
                    continue                  # taken back already, this run or before
                # no discovery without its values on MQTT (ADR-0025, ADD-14):
                # what it would have announced is taken back, once

                def _unhook(d=dev_cfg):
                    regs = _discovery_regs(d)
                    if mqtt_publisher.clear_device_discovery(d.id, regs, write_keys=set(_ha_write_rules(d, regs)),
                                                             ha_node=d.ha_node):
                        _mark_disc_cleared(d.id, True)
                hooks.append(_unhook)
                continue

            def _hook(d=dev_cfg):
                regs = _discovery_regs(d)
                _mark_disc_cleared(d.id, False)   # announced: a later switch-off clears it again
                mqtt_publisher.publish_device_discovery(
                    d.id, d.name, d.mqtt_topic_prefix, regs,
                    model=(d.template if d.protocol != 'none'
                           else _meter_only_model(d)),
                    write_rules=_ha_write_rules(d, regs), ha_node=d.ha_node)
            hooks.append(_hook)
        hooks.append(_publish_attribute_meta)    # the attributes: all after a (re)connect, else what changed
        mqtt_publisher.discovery_hooks = hooks
        if getattr(mqtt_publisher, "connected", False):
            for h in hooks:
                try:
                    h()
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"device discovery publish failed: {e}")
        # the controller's command topics follow the same lifecycle (the
        # function is defined further down; at boot it runs itself once)
        for _hook in ('command_topics', 'rules_reload'):
            fn = _late_hooks.get(_hook)
            if fn is None:
                continue
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                logger.warning("power-limit command topics: %s", e)

    # Seconds AFTER a command's revert_s at which the read-back group is swept
    # again. Measured 2026-09-14 on a Fronius Symo: the inverter clears
    # WMaxLim_Ena about 120-127 s after a write with revert_s = 120, but a
    # single read 5 s past the mark still saw it enabled once — its own timer
    # ticks coarsely. A short series catches it without polling the hourly
    # controls block continuously.
    REVERT_REREAD_AT_S = (5.0, 20.0, 60.0, 180.0)
    # One series per (device, command). A new command restarts the device-side
    # revert timer, so the series armed for the previous command is cancelled
    # and replaced — otherwise a controller that re-sends every minute stacks
    # its series on the datalogger (1681 extra sweeps in 3.5 h on 2026-09-15,
    # ~50 Solar API ok→degraded flaps an hour). Each series carries a
    # generation; a callback from a superseded one sweeps nothing.
    _revert_rereads: Dict[tuple, tuple] = {}        # (device, command) -> (generation, Timer)
    _revert_rereads_lock = threading.Lock()

    def _reread_after_revert(device_id: str, cmd_name: str, group: str,
                             remaining: tuple = (), gen: Optional[int] = None) -> None:
        """Timer callback: sweep the read-back group once the device-side
        revert timer of ``cmd_name`` should have fired (device looked up
        again — it may have been rebuilt or removed meanwhile), then arm the
        next sweep of the series — unless a newer command superseded it."""
        with _revert_rereads_lock:
            cur = _revert_rereads.get((device_id, cmd_name))
        if gen is not None and cur is not None and cur[0] != gen:
            return
        try:
            found = registry.find(device_id)
            drv = _modbus_driver_of(found[2]) if found and found[2] is not None else None
            if drv is not None and hasattr(drv, 'poll_now'):
                drv.poll_now(group)
                logger.info("command %s on %s: read-back after the revert timer (%s)",
                            cmd_name, device_id, group)
        except Exception:  # noqa: BLE001
            pass
        if remaining:
            _arm_revert_reread(device_id, cmd_name, group, remaining, gen=gen)

    def _arm_revert_reread(device_id: str, cmd_name: str, group: str, delays: tuple,
                           gen: Optional[int] = None) -> None:
        """``delays`` are absolute offsets from the revert mark; arm the first,
        pass the rest on as offsets from that one. Without ``gen`` this starts
        a new series and cancels the one still pending for the same device
        and command; with it, it continues that series if it is still the
        current one."""
        first, rest = delays[0], tuple(d - delays[0] for d in delays[1:])
        key = (device_id, cmd_name)
        with _revert_rereads_lock:
            cur = _revert_rereads.get(key)
            if gen is None:
                if cur is not None:
                    cur[1].cancel()
                gen = (cur[0] + 1) if cur is not None else 1
            elif cur is not None and cur[0] != gen:
                return
            t = threading.Timer(first, _reread_after_revert, args=(device_id, cmd_name, group, rest, gen))
            t.daemon = True
            _revert_rereads[key] = (gen, t)
            t.start()


    def _push_readback_to_store(device_id, address, value, register_type='holding'):
        """Write-then-refresh: push a just-written register's read-back value into
        the live store so the vmeters / UI reflect the new value AT ONCE instead
        of at the next poll (a slow-group setpoint could otherwise lag 60 s)."""
        if value is None:
            return
        store = registry.store_for(device_id)
        address = store_key(address, register_type)        # the key of THAT table's entry
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
        MQTT command worker — never on paho's network thread (audit M4). A raw
        write is handed to the device's lane (ADR-0022 D5): the worker never
        waits for the bus, and every outcome is told on `cmd/result` (D6)."""
        import math as _math
        addr = getattr(register, 'address', '?')
        rname = getattr(register, 'name', '') or ''
        known = {}

        def refuse(why: str, fmt: str, *args) -> None:
            logger.warning(fmt, *args)
            d = known.get('dev')
            if d is not None:
                _publish_raw_result(d, {'command': f"raw:holding@{addr}", 'register': rname or None,
                                        'params': {'value': payload}, 'status': 'rejected', 'reason': why,
                                        'device': d.id, 'via': 'ha', 'by': 'ha-mqtt', 'ts': round(time.time(), 3)})
        refused = _mqtt_writes_refused()
        if refused:
            logger.warning("MQTT write on %s/%s ignored: %s", device_id, rname or '?', refused)
            return
        _idx, dev_cfg, client = _find_device(device_id)
        if dev_cfg is None or client is None or dev_cfg.primary or dev_cfg.protocol == 'http':
            return
        known['dev'] = dev_cfg
        if dev_cfg.write_locked:
            # the lock is per device, whichever face asks (F-13, 3.83.0)
            return refuse(f"device '{device_id}' is write-locked",
                          "MQTT write REJECTED (device write-locked): device=%s addr=%s", device_id, addr)
        rule = _write_rule(dev_cfg, register.address, 'holding')
        if rule is None or not rule.writable:
            return refuse(f"register {addr} is not declared writable",
                          "MQTT write REJECTED (not writable): device=%s addr=%s", device_id, addr)
        cm = next((c for c in _commands_for(dev_cfg) if c['enabled'] and c['faces'].get('ha', True)
                   and c['def'].writes and c['def'].writes[0].get('register') == rname
                   and c['def'].value_param is not None), None)
        if cm is not None:
            # a register that a command writes first (WMaxLimPct) is written
            # THROUGH the command — alone it would do nothing, or harm
            try:
                value = float(payload)
            except (TypeError, ValueError):
                logger.warning("HA command REJECTED (non-numeric %r): device=%s", payload, device_id)
                return
            # handed to the device's lane: the MQTT worker does not wait for
            # the bus (ADR-0018 D11); every outcome is told on cmd/result
            got = _submit_named_command(dev_cfg, client, cm['def'].name, {'value': value},
                                        who='ha', ip='mqtt', via='ha', tell=True)
            if isinstance(got, tuple):
                _publish_command_result(dev_cfg, cm['def'].name, got[1])
            return
        # A device read over MQTT takes only commands; a register whose
        # engineering value depends on a live scale factor, or that ANY command
        # writes at ANY position on this slave, never takes a raw write (D4,
        # ADR-0022): WMaxLim_Ena written alone lifts a curtail the OV rule
        # cannot see. Mirrors the HTTP route.
        if _writes_by_mqtt(dev_cfg):
            return refuse("a device read over MQTT is written only through an enabled command",
                          "HA write REJECTED: device=%s register=%s — a device read over MQTT is "
                          "written only through an enabled command", device_id, rname or '?')
        data_type = (rule.data_type or 'uint16').lower()
        k = _raw_channel_conflict(dev_cfg, 'holding', int(register.address), _words_of(data_type))
        if getattr(rule, 'scale_from', None) or k is not None:
            why = (_conflict_text(int(register.address), rname, k) if k is not None
                   else f"register {addr} ({rname}) scales by a live factor — written through its command")
            return refuse(why, "MQTT write REJECTED (register is written through the command %s): "
                               "device=%s addr=%s", (k or {}).get('command') or "<scale_from>", device_id, addr)
        enum_map = getattr(register, 'enum', None)
        if enum_map:                                   # select: label → code
            value = next((int(k_) for k_, v in enum_map.items() if str(v) == payload), None)
            if value is None:
                return refuse(f"unknown option {payload!r}", "MQTT write REJECTED (unknown option %r): "
                              "device=%s addr=%s", payload, device_id, addr)
        else:                                          # number: parse
            try:
                value = float(payload)
            except (TypeError, ValueError):
                return refuse(f"not a number: {payload!r}", "MQTT write REJECTED (non-numeric %r): "
                              "device=%s addr=%s", payload, device_id, addr)
        # bounds apply to BOTH a typed number and a mapped enum code
        if not _math.isfinite(value):
            return refuse("not a finite number", "MQTT write REJECTED (not finite): device=%s addr=%s",
                          device_id, addr)
        if rule.write_min is not None and value < rule.write_min:
            return refuse(f"{value:g} is below the register minimum {rule.write_min}",
                          "MQTT write REJECTED (%s < min %s): device=%s addr=%s",
                          value, rule.write_min, device_id, addr)
        if rule.write_max is not None and value > rule.write_max:
            return refuse(f"{value:g} is above the register maximum {rule.write_max}",
                          "MQTT write REJECTED (%s > max %s): device=%s addr=%s",
                          value, rule.write_max, device_id, addr)
        # the exact-allowlist guard the HTTP route enforces — a [0, 1] select
        # must not accept 2 just because it arrived over the broker
        if (getattr(rule, 'write_allowed', None)
                and value not in [float(v) for v in rule.write_allowed]):
            return refuse(f"{value:g} is not in the register's allowed set {rule.write_allowed}",
                          "MQTT write REJECTED (%s not in allowed set %s): device=%s addr=%s",
                          value, rule.write_allowed, device_id, addr)
        scale = float(rule.scale if rule.scale is not None else 1.0)
        offset = float(getattr(rule, 'offset', 0.0) or 0.0)
        # what the encoder would have to clamp is refused here (F-15, 3.83.0,
        # same as HTTP): a clamped word is a value nobody asked for
        from .encoder import RegisterEncoder as _Enc
        _rng = _Enc.int_range(data_type)
        if _rng is not None:
            try:
                _raw = int(round((float(value) - offset) * scale))
            except (TypeError, ValueError):
                _raw = None
            if _raw is not None and not (_rng[0] <= _raw <= _rng[1]):
                return refuse(f"{value:g} encodes to {_raw}, outside {data_type} [{_rng[0]}, {_rng[1]}]",
                              "MQTT write REJECTED (%s encodes to %s, outside %s [%s, %s]): "
                              "device=%s addr=%s", value, _raw, data_type, _rng[0], _rng[1], device_id, addr)
        ctx = {'dev': dev_cfg, 'rtype': 'holding', 'address': int(register.address), 'register': rname,
               'data_type': data_type, 'scale': scale, 'offset': offset, 'value': value, 'via': 'ha',
               'who': 'ha-mqtt', 'ip': 'mqtt', 'rate_key': 'mqtt:' + device_id}
        def told(code, res, _d=dev_cfg):
            # what the lane said without running it (superseded, expired,
            # cancelled) carries the write's own fields too
            res = {'command': f"raw:holding@{int(register.address)}", 'register': rname or None,
                   'params': {'value': value}, 'device': device_id, 'via': 'ha', 'by': 'ha-mqtt',
                   'ts': round(time.time(), 3), **res}
            if res.get('status') not in ('success', 'mismatch', 'unverified', 'error'):
                # what did not reach the wire (rate limit, superseded, expired,
                # standby, refused at the head) is said; a written one logged itself
                logger.warning("MQTT write %s: device=%s addr=%s — %s", str(res.get('status')).upper(),
                               device_id, addr, res.get('reason') or '')
            _publish_raw_result(_d, res)
        # in the lane: the rate token is spent there, never by a value a newer
        # one replaced (ADR-0018 D7); every outcome goes to cmd/result
        _submit_raw(ctx, client, on_done=told)

    if mqtt_publisher:
        mqtt_publisher.set_command_write_handler(_mqtt_write_command)

    # ── commands: the universal write path (docs/commands-design.md) ─────────
    #
    # A controller says WHAT it wants; the device's template says HOW that is
    # said to this device; the engine (multibus/commands.py) is vendor-blind.
    # Every face — API, MQTT, HA — lands here, with the same gates, the same
    # verification, the same audit. The gateway never decides WHEN.
    from .commands import (CommandDef, assign_channels, evaluate as _cmd_eval, normalize_params,
                           parse_command_def, parse_payload, run_command, run_mqtt_command)
    # One command at a time PER ENDPOINT (a host:port, a serial port): units
    # behind one datalogger share it and must not interleave a command's
    # guard read → write → verify; a drive on another bus no longer waits for
    # them (a device that does not answer used to hold every command on the
    # gateway for its retries).
    _cmd_locks: Dict[str, Any] = {}
    _cmd_locks_guard = threading.Lock()

    def _lane_key(dev_cfg, client) -> str:
        """The wire a unit's commands travel on — the dispatcher's lane and
        the commands' bus lock (ADR-0018). STABLE: the bus of a Modbus
        connection, else the device, else the device's MQTT source; never an
        object id, which split a lane and leaked a lock whenever an edit
        replaced the client (CMD-Q-10)."""
        did = getattr(dev_cfg, 'id', '?')
        drv = _modbus_driver_of(client) if client is not None else None
        if drv is not None:
            bus = str(getattr(getattr(drv, 'connection', None), 'bus', '') or '')
            return f"bus:{bus}" if bus else f"dev:{did}"
        sid = 'default'
        for src, part in getattr(client, 'parts', []) or []:
            if str(getattr(src, 'protocol', '')).lower() == 'mqtt' and hasattr(part, 'wait_for'):
                sid = str(getattr(src, 'id', '') or 'default')
                break
        # the connection the source actually uses: a component reads (and is
        # written) through its parent's link — one wire, one lane (CR2-10)
        mq = _mqtt_driver_of(client) if client is not None else None
        link = str(getattr(mq, 'link_name', '') or '') if mq is not None else ''
        return f"mqtt:{link or did}/{sid}"

    def _cmd_lock_for(client, dev_cfg=None) -> Any:
        if dev_cfg is not None:
            key = _lane_key(dev_cfg, client)
        else:
            # a caller that does not name the device (kept for the raw write
            # routes' older callers): the connection's bus, else the object
            drv = _modbus_driver_of(client) if client is not None else None
            conn = getattr(drv, 'connection', None) if drv is not None else getattr(client, 'connection', None)
            if conn is None:
                mq = _mqtt_driver_of(client) if client is not None else None
                key = f"mqtt:{id(mq if mq is not None else client)}"
            else:
                bus = str(getattr(conn, 'bus', '') or '')
                key = f"bus:{bus}" if bus else str(id(conn))
        with _cmd_locks_guard:
            lk = _cmd_locks.get(key)
            if lk is None:
                lk = _cmd_locks[key] = threading.Lock()
            return lk
    _cmd_last: Dict[str, Dict] = {}       # (device:command) → last result, for the UI and the state topic

    class _BusBusy(Exception):
        """The bus stayed busy past a shutdown revert's deadline."""

    @contextlib.contextmanager
    def _bus_lock(lock, deadline: Optional[float] = None):
        """The bus lock; with a deadline (a shutdown revert, ADR-0022 D8) at
        most until then — past it, nothing is written and the lease stays on
        disk for boot recovery (RC-5)."""
        if deadline is None:
            lock.acquire()
        elif not lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise _BusBusy("the bus stayed busy past the shutdown budget — nothing written, "
                           "left for boot recovery")
        try:
            yield
        finally:
            lock.release()

    # ── raw register writes (ADR-0022 D4–D8) ─────────────────────────────────
    #
    # A raw write is a register written by address, not through a command: an
    # HA entity, POST /api/devices/{id}/write (guarded or `unguarded`), the raw
    # lease revert. What a command writes — at any position — is never written
    # raw (D4); everything else travels the device's lane as a job (D5).

    _cmd_regs_memo: Dict[str, Tuple] = {}

    def _words_of(data_type, rtype: str = 'holding') -> int:
        if str(rtype).lower() == 'coil':
            return 1
        from .register_parser import RegisterParser as _RP
        return max(1, int(_RP.REGISTER_COUNTS.get(str(data_type or 'uint16').lower(), 1)))

    def _slave_of(dev_cfg) -> Tuple[str, Any]:
        """The physical slave a device entry reads: its Modbus bus and unit id.
        Two entries on one inverter (a twin, another template) are one slave.
        A unit read only over HTTP/MQTT is no Modbus slave: its `protocol`
        stays 'tcp' and its connection the empty default, which made twins
        across installations one slave (':502', unit) — RP-7."""
        srcs = list(dev_cfg.sources or [])
        for s in srcs:
            if str(s.protocol or 'tcp').lower() in _POLLED_MODBUS and getattr(s, 'connection', None) is not None:
                return (s.connection.bus, int(s.connection.unit_id))
        if srcs:
            return ('device', dev_cfg.id)
        if str(dev_cfg.protocol or 'tcp').lower() in _POLLED_MODBUS and getattr(dev_cfg, 'connection', None) is not None:
            return (dev_cfg.connection.bus, int(dev_cfg.connection.unit_id))
        return ('device', dev_cfg.id)

    def _command_registers(dev_cfg) -> List[Tuple[str, int, int, str, str, str]]:
        """(register type, first address, end, command, channel, register) of
        every register any command of the device writes, at any position,
        enabled or not — what assign_channels unites into channels."""
        cmds = _commands_for(dev_cfg)
        if not cmds:
            return []
        tpl = _modbus_template_of(dev_cfg)
        # memoized as _commands_for is (asked per register by HA discovery)
        key = (id(tpl), _cmds_gen[0], repr([(c['def'].name, c['def'].writes) for c in cmds]))
        hit = _cmd_regs_memo.get(dev_cfg.id)
        if hit is not None and hit[0] == key:
            return hit[1]
        regs = {r.name: r for r in (tpl.registers if tpl else [])}
        out = []
        for c in cmds:
            for w in (c['def'].writes or []):
                r = regs.get(w.get('register'))
                if r is None:
                    continue
                rtype = (getattr(r, 'register_type', 'holding') or 'holding').lower()
                start = int(r.address)
                out.append((rtype, start, start + _words_of(getattr(r, 'data_type', None), rtype),
                            c['def'].name, c.get('channel') or c['def'].name, r.name))
        _cmd_regs_memo[dev_cfg.id] = (key, out)
        return out

    def _raw_channel_conflict(dev_cfg, rtype: str, address: int, words: int) -> Optional[Dict[str, Any]]:
        """The command whose register a raw write of [address, address+words)
        would touch on the same physical slave, or None (D4, review MED 4)."""
        rtype = str(rtype or 'holding').lower()
        lo, hi = int(address), int(address) + max(1, int(words))
        slave = _slave_of(dev_cfg)
        for d, _cl in list(registry):
            if d.id != dev_cfg.id and _slave_of(d) != slave:
                continue
            try:
                regs = _command_registers(d)
            except Exception:  # noqa: BLE001 — a device with a broken template writes nothing anyway
                continue
            for t, start, end, cmd, ch, name in regs:
                if t == rtype and start < hi and lo < end:
                    return {'device': d.id, 'command': cmd, 'channel': ch, 'register': name, 'address': start}
        return None

    def _conflict_text(address: int, regname: str, k: Dict[str, Any]) -> str:
        return (f"register {address} ({regname or k['register']}) is written through the command "
                f"{k['command']} (channel {k['channel']}"
                + (f" of {k['device']}" if k.get('device') else '') + "): "
                f"POST /api/devices/{k['device']}/commands/{k['command']}")

    def _raw_exec_timeout(client) -> float:
        """A raw job's running time: one write and one read, each up to the
        connection's timeout × (retries + 1)."""
        drv = _modbus_driver_of(client)
        cc = getattr(getattr(drv, 'connection', None), 'config', None)
        try:
            per = float(getattr(cc, 'timeout', 3) or 3) * (int(getattr(cc, 'retry_attempts', 3) or 0) + 1)
        except (TypeError, ValueError):
            per = 12.0
        return 3 * per

    def _read_back_failure(client) -> str:
        """Why a read-back gave nothing: the connection's word for this
        thread's last read (exception_2, timeout, …), or what None means."""
        kind = None
        try:
            conn = getattr(_modbus_driver_of(client), 'connection', None)
            kind = conn.last_read_error() if conn is not None and hasattr(conn, 'last_read_error') else None
        except Exception:  # noqa: BLE001
            kind = None
        if kind:
            return f"no answer ({kind})"
        return "no value (no refusal from the device: the shared endpoint's turn, a stop or a retired connection)"

    def _execute_raw(ctx: Dict[str, Any], job=None) -> Tuple[int, Dict[str, Any]]:
        """One raw write under the bus lock, in its lane (D5): the lease still
        current (a revert), the role,
        D4 again (a template edited since admission), the rate token, the
        write, the read-back; then the store, the log, the audit, the lease."""
        dev_id, rtype, address = ctx['dev'].id, ctx['rtype'], int(ctx['address'])
        via, who, is_revert = ctx['via'], ctx['who'], ctx.get('is_revert', False)
        value, data_type = ctx['value'], ctx['data_type']
        scale, offset = float(ctx['scale']), float(ctx.get('offset') or 0.0)
        base = {'command': f"raw:{rtype}@{address}", 'register': ctx.get('register') or None,
                'params': {'value': value}, 'device': dev_id, 'via': via, 'by': who}
        _i, cur_cfg, c = _find_device(dev_id)
        if c is None:
            return 409, {**base, 'status': 'error', 'reason': f"device {dev_id} is not running — nothing written",
                         'ts': round(time.time(), 3)}
        cur_cfg = cur_cfg or ctx['dev']
        stamp = None
        try:
            with _bus_lock(_cmd_lock_for(c, cur_cfg), ctx.get('deadline')):
                if is_revert and ctx.get('is_current') is not None and not ctx['is_current']():
                    return 409, {**base, 'status': 'cancelled', 'lease_renewed': True, 'ts': round(time.time(), 3),
                                 'reason': 'the lease was renewed or cleared while the revert waited — nothing written'}
                if not gw_role.active:
                    return 503, {**base, 'status': 'rejected', 'reason': gw_role.status_text(),
                                 'ts': round(time.time(), 3)}
                if cur_cfg.write_locked and not is_revert:
                    # locked while the job waited: the lock is asked again at
                    # the head, as the command path asks it (RP-10); a revert
                    # to the safe value still runs, as a command's does
                    return 403, {**base, 'status': 'rejected', 'ts': round(time.time(), 3),
                                 'reason': f"device '{dev_id}' was write-locked while the write waited — "
                                           "nothing written"}
                k = _raw_channel_conflict(cur_cfg, rtype, address, _words_of(data_type, rtype))
                if k is not None and not is_revert:
                    return 422, {**base, 'status': 'rejected', 'ts': round(time.time(), 3),
                                 'reason': _conflict_text(address, ctx.get('register'), k)}
                if not is_revert and ctx.get('rate_key') and not _write_rate_ok(ctx['rate_key']):
                    return 429, {**base, 'status': 'rejected', 'reason': 'write rate limit exceeded',
                                 'ts': round(time.time(), 3)}
                ok, err, words = c.write_value(address, rtype, data_type, value, scale=scale, offset=offset,
                                               prefer_fc6=bool(ctx.get('prefer_fc6')))
                read_back, verified, rb_why = None, None, None
                if ok and not is_revert:
                    try:
                        raw_back = c.read_register(address, data_type, rtype)
                        if raw_back is None:
                            # the read-back's own word: why it says nothing (RP-11)
                            rb_why = _read_back_failure(c)
                        elif rtype == 'coil':
                            from .modbus_client import coil_truthy as _coil_truthy
                            read_back = bool(raw_back)
                            verified = read_back == _coil_truthy(value)
                        else:
                            read_back = float(raw_back) / (scale or 1.0) + offset
                            verified = abs(read_back - float(value)) <= max(1e-6, abs(float(value)) * 1e-4)
                    except Exception as e:  # noqa: BLE001
                        rb_why = f"{type(e).__name__}: {e}"
                    if rb_why is not None:
                        logger.debug("raw write read-back failed: device=%s %s@%s: %s", dev_id, rtype, address, rb_why)
                if ok and is_revert and k is not None:
                    # a legacy raw lease on what is now a command's register
                    # (ADR-0022 D8): the pace is stamped, not the on/off
                    stamp = k
                    rt = getattr(app.state, 'rules_runtime', None)
                    if rt is not None:
                        try:
                            rt.channel_note(cur_cfg, k['command'], {}, pace_only=True)
                        except Exception:  # noqa: BLE001
                            pass
        except _BusBusy as e:
            return 409, {**base, 'status': 'cancelled', 'reason': str(e), 'ts': round(time.time(), 3)}
        queued_ms = (round((job.started - job.admitted) * 1000.0, 1)
                     if job is not None and job.started is not None else None)
        if ok and read_back is not None:
            _push_readback_to_store(dev_id, address, read_back, rtype)     # write-then-refresh
        # the log and the audit as before 3.95, plus where it waited
        if via == 'ha':
            logger.warning("MODBUS WRITE %s (via HA): device=%s addr=%s dtype=%s value=%r%s",
                           "OK" if ok else "FAILED", dev_id, address, data_type, value, "" if ok else f" err={err}")
            detail = {"device": dev_id, "address": address, "value": value, "via": "ha-write-entity"}
            audit = dict(user="ha-mqtt", ip="mqtt", action="modbus write", status="ok" if ok else "fail")
        elif is_revert:
            logger.warning("MODBUS WRITE (lease-revert) %s: device=%s addr=%s safe=%r%s",
                           "OK" if ok else "FAILED", dev_id, address, value, "" if ok else f" err={err}")
            detail = {"value": value, "data_type": data_type, "words": list(words) if words else None,
                      "via": "lease-revert", **({"stamped": stamp['channel']} if stamp else {})}
            audit = dict(user="lease", ip="-", action="modbus write", target=f"{dev_id} {rtype}@{address}",
                         status="ok" if ok else "failed")
        else:
            logger.warning("MODBUS WRITE %s: by=%s@%s device=%s addr=%s type=%s dtype=%s value=%r words=%s%s",
                           "OK" if ok else "FAILED", who, ctx.get('ip'), dev_id, address, rtype, data_type,
                           value, words, "" if ok else f" err={err}")
            detail = {"value": value, "data_type": data_type, "words": list(words) if words else None,
                      "lease_ms": int(ctx.get('lease_ms') or 0),
                      **({"unguarded": True} if ctx.get('unguarded') else {})}
            audit = dict(user=who, ip=ctx.get('ip') or '?', action="modbus write",
                         target=f"{dev_id} {rtype}@{address}", status="ok" if ok else "failed")
        if queued_ms is not None:
            detail['queued_ms'] = queued_ms
        try:
            audit_log.append(**audit, detail=detail)
        except Exception:  # noqa: BLE001
            pass
        if not ok:
            return 502, {**base, 'status': 'error', 'reason': f"write failed: {err}",
                         'words': list(words) if words else None, 'ts': round(time.time(), 3)}
        if via == 'api':
            # the dead-man of this register: armed / renewed, or cancelled
            lease_ms = int(ctx.get('lease_ms') or 0)
            if lease_ms > 0:
                _lease_mgr.arm(dev_id, rtype, address, lease_ms,
                               _make_lease_revert(dev_id, rtype, address, data_type, scale, ctx.get('safe'),
                                                  off=offset),
                               meta={'device': dev_id, 'register_type': rtype, 'address': address,
                                     'data_type': data_type, 'scale': scale, 'offset': offset,
                                     'safe_value': ctx.get('safe'), 'lease_ms': lease_ms})
            else:
                _lease_mgr.clear(dev_id, rtype, address)
        status = 'success' if verified else 'mismatch' if verified is False else 'unverified'
        res = {**base, 'status': status, 'read_back': read_back, 'verified': verified,
               'words': list(words) if words else None, 'ts': round(time.time(), 3)}
        if status == 'unverified' and rb_why is not None:
            res['reason'] = f"read-back failed: {rb_why}"
        if queued_ms is not None:
            res['queued_ms'] = queued_ms
        return 200, res

    def _submit_raw(ctx: Dict[str, Any], client, on_done=None):
        """A raw write admitted: its lane's job (D5) — the slot is the
        register, so a newer value of the same encoding replaces one still
        waiting, whichever face sent either (review MED 5)."""
        dev, rtype, address = ctx['dev'], ctx['rtype'], int(ctx['address'])
        revert = bool(ctx.get('is_revert'))
        job = _CmdJob(lane=_lane_key(dev, client), device=dev.id, channel=f"reg:{rtype}@{address}",
                      command=f"raw:{rtype}@{address}",
                      params={'value': ctx['value'], 'data_type': ctx['data_type'], 'scale': ctx['scale'],
                              'offset': ctx.get('offset') or 0.0, 'prefer_fc6': bool(ctx.get('prefer_fc6')),
                              'unguarded': bool(ctx.get('unguarded')), 'lease_ms': int(ctx.get('lease_ms') or 0)},
                      run=lambda j: _execute_raw(j.ctx, j), kind='revert' if revert else 'command',
                      coalesce=not revert, max_age_s=None if revert else DEFAULT_MAX_AGE_S,
                      on_done=on_done, ctx=ctx)
        return _cmd_dispatch.submit(job)

    def _publish_raw_result(dev_cfg, res: Dict[str, Any]) -> None:
        """An HA raw write's outcome on `<prefix>/cmd/result`, not retained
        (D6); the retained `cmd/<name>/state` is a command's only. A standby
        answers nothing (ADR-0018 D10)."""
        if not gw_role.active or not mqtt_publisher or not getattr(mqtt_publisher, 'connected', False):
            return
        try:
            mqtt_publisher.client.publish(f"{dev_cfg.mqtt_topic_prefix}/cmd/result",
                                          json.dumps(res, default=str), qos=1, retain=False)
        except Exception:  # noqa: BLE001
            pass

    def _budget_deadline() -> Optional[float]:
        """The clean shutdown's revert deadline on this thread (monotonic),
        or None outside a shutdown revert."""
        left = write_lease_budget_s()
        return None if left is None else time.monotonic() + left

    def _modbus_driver_of(client):
        """The Modbus driver behind a client: the client itself, or the first
        Modbus source of a unit read several ways. None when nothing can write."""
        if client is None:
            return None
        if hasattr(client, 'connection') and hasattr(client.connection, 'read_registers'):
            return client
        for src, drv in getattr(client, 'parts', []) or []:
            if str(getattr(src, 'protocol', 'tcp')).lower() in _POLLED_MODBUS \
                    and hasattr(drv, 'connection'):
                return drv
        return None

    def _gateway_command_topics(dev_cfg) -> List[str]:
        """Topics the gateway itself takes commands on: a device must never
        be commanded by publishing there (the gateway would take it as a new
        command). The device's own tree covers its /cmd/ and HA /set topics."""
        topics = [f"{dev_cfg.mqtt_topic_prefix}/#"] if getattr(dev_cfg, 'mqtt_topic_prefix', '') else []
        if mqtt_publisher is not None:
            topics.extend(list(getattr(mqtt_publisher, '_topic_handlers', {}) or {}))
        return topics

    def _mqtt_driver_of(client):
        """The MQTT input source behind a client, the one that publishes a
        command when the unit has no Modbus source. None when there is none."""
        if client is None:
            return None
        if hasattr(client, 'publish') and hasattr(client, 'wait_for'):
            return client
        for src, drv in getattr(client, 'parts', []) or []:
            if str(getattr(src, 'protocol', '')).lower() == 'mqtt' and hasattr(drv, 'wait_for'):
                return drv
        return None

    def _modbus_template_of(dev_cfg):
        """The template whose registers a command writes: the Modbus source's,
        else the MQTT source's, else the device's own."""
        for proto in (_POLLED_MODBUS, ('mqtt',)):
            for s in (dev_cfg.sources or []):
                if str(s.protocol or 'tcp').lower() in proto and s.template:
                    return template_registry.get(s.template)
        return template_registry.get(dev_cfg.template) if dev_cfg.template else None

    # per-device channel settings a device may override (ADR-0019); `combine`
    # stays the template's — what the setting means is not a device's choice
    CHANNEL_OVERRIDE_KEYS = ('min_interval_s', 'min_on_s', 'min_off_s', 'max_starts_per_h', 'max_age_s')
    from .rules import PACE_KEYS
    from .command_dispatch import DEFAULT_MAX_AGE_S
    _cmds_memo: Dict[str, Tuple] = {}
    _cmds_gen = [0]

    def _commands_changed() -> None:
        """A binding, a channel override or a template changed: the memoized
        command lists and the rules' channel cache are rebuilt (review 11)."""
        _cmds_gen[0] += 1
        _cmds_memo.clear()
        rt = getattr(app.state, 'rules_runtime', None)
        if rt is not None:
            rt.__dict__.setdefault('_chan_cache', {}).clear()

    _chan_conflicts_warned: set = set()

    def _override_cid(key: str, defs: Dict[str, Any], ids: Dict[str, str]) -> Optional[str]:
        declared = {d.channel for d in defs.values() if d.channel}
        return key if key in declared else ids.get(key)

    def _channel_overrides(dev_cfg, defs: Dict[str, Any], ids: Dict[str, str]):
        """The device's overrides by channel id, the key each was taken from,
        the keys that no longer name a channel (kept, listed as orphan) and
        the keys that name a channel another key already overrides. A key is
        a declared channel name or a member command's name — never a derived
        `a+b` id. Two keys for one channel (`motor` and `set_frequency`):
        the declared name wins whatever the order in the file, the other is
        a conflict, listed and warned (CR2-14)."""
        declared = {d.channel for d in defs.values() if d.channel}
        by_cid: Dict[str, Dict] = {}
        key_of: Dict[str, str] = {}
        orphans: List[str] = []
        conflicts: List[str] = []
        stored = getattr(dev_cfg, 'channels', None) or {}
        # declared names first, then member names, each sorted: deterministic
        for key in sorted(stored, key=lambda k: (k not in declared, k)):
            ov = stored[key]
            cid = _override_cid(key, defs, ids)
            if cid is None or not isinstance(ov, dict):
                orphans.append(key)
                continue
            if cid in key_of:
                conflicts.append(key)
                if (dev_cfg.id, key) not in _chan_conflicts_warned:
                    _chan_conflicts_warned.add((dev_cfg.id, key))
                    logger.warning("device %s: channel override '%s' names channel '%s', already set "
                                   "under '%s' — ignored", dev_cfg.id, key, cid, key_of[cid])
                continue
            key_of[cid] = key
            by_cid[cid] = {k: v for k, v in ov.items() if k in CHANNEL_OVERRIDE_KEYS}
        return by_cid, key_of, sorted(orphans), sorted(conflicts)

    def _commands_for(dev_cfg) -> List[Dict]:
        """What this device offers: the template's presets, each enabled only by
        a binding on the device (or its group), plus inline recipes declared
        on the device. Order: as declared in the template, then inline ones.
        Memoized per device id: it is asked several times per command (review
        13); another template, binding or override rebuilds it — keyed by
        what it is built from, not by the object, so the registry's and the
        config's copies of one device share an entry (CR2-9)."""
        tpl = _modbus_template_of(dev_cfg)
        sig = (repr(dev_cfg.commands), repr(getattr(dev_cfg, 'channels', None)))
        hit = _cmds_memo.get(dev_cfg.id)
        if hit is not None and hit[1] is tpl and hit[2] == sig and hit[3] == _cmds_gen[0]:
            return list(hit[4])
        out = _build_commands(dev_cfg, tpl)
        if hit is not None:
            # the device, its template, a binding or an override changed (an
            # upsert, a channel PUT): the rules' channel view goes with it,
            # not at their next tick (review 11)
            rt = getattr(app.state, 'rules_runtime', None)
            if rt is not None:
                rt.__dict__.setdefault('_chan_cache', {}).clear()
        _cmds_memo[dev_cfg.id] = (dev_cfg, tpl, sig, _cmds_gen[0], out)
        return list(out)

    def _build_commands(dev_cfg, tpl) -> List[Dict]:
        presets = dict((tpl.commands or {}) if tpl else {})
        tpl_channels = dict(getattr(tpl, 'channels', None) or {}) if tpl else {}
        bindings = {str(b.get('name', '')): b for b in (dev_cfg.commands or []) if isinstance(b, dict)}
        out = []
        for name, raw in presets.items():
            b = bindings.get(name) or {}
            tpl_safe = dict(raw.get('safe') or {}) or None
            safe_from = 'template' if tpl_safe else None
            if isinstance(b.get('safe'), dict) and b['safe']:
                # the release value belongs to the site (a grid contract, a
                # cable): the device binding sets it OVER the template's, key
                # by key — a binding's {value: 50} keeps the template's
                # revert_s & co. instead of dropping them to defaults (CR2-13)
                raw = {**raw, 'safe': {**(tpl_safe or {}), **dict(b['safe'])}}
                safe_from = 'binding+template' if set(tpl_safe or {}) - set(b['safe']) else 'device'
            out.append({'def': parse_command_def(name, raw), 'from_template': True, 'template_safe': tpl_safe,
                        'binding_safe': dict(b['safe']) if isinstance(b.get('safe'), dict) else None,
                        'enabled': bool(b) and bool(b.get('enabled', True)),
                        'faces': dict(b.get('faces') or {'api': True, 'mqtt': True, 'ha': True}),
                        'confirm': bool(b.get('confirm', raw.get('confirm', True))),
                        'lease_s': int(b.get('lease_s', 0) or 0), 'safe_from': safe_from})
        for name, b in bindings.items():
            if name in presets or not b.get('writes'):
                continue
            out.append({'def': parse_command_def(name, b), 'from_template': False,
                        'enabled': bool(b.get('enabled', True)),
                        'faces': dict(b.get('faces') or {'api': True, 'mqtt': True, 'ha': True}),
                        'confirm': bool(b.get('confirm', True)), 'lease_s': int(b.get('lease_s', 0) or 0),
                        'safe_from': 'device' if b.get('safe') else None})
        defs = {c['def'].name: c['def'] for c in out}
        ids = assign_channels(defs) if defs else {}
        overrides, key_of, orphans, conflicts = _channel_overrides(dev_cfg, defs, ids)
        # template + this device's overrides: the arbiter (rules and every
        # outside writer) reads `channels`, the listing shows both
        channels = {cid: dict(cfg) for cid, cfg in tpl_channels.items()}
        for cid, ov in overrides.items():
            channels[cid] = {**channels.get(cid, {}), **ov}
        # the key an editor saves a channel's override under: the one that
        # won for it (CR2-14), else the declared name, else a member command's
        # name (never a derived `a+b` id, review 11)
        stored_key = key_of
        for c in out:
            cid = ids.get(c['def'].name, c['def'].name)
            members = sorted(n for n, i in ids.items() if i == cid and not defs[n].alias)
            c.update({'channels': channels, 'template_channels': tpl_channels,
                      'channel': cid, 'orphan_channels': orphans, 'channel_conflicts': conflicts,
                      'channel_key': stored_key.get(cid) or (cid if '+' not in cid else (members or [cid])[0]),
                      'channel_override': dict(overrides.get(cid) or {})})
        return out

    def _group_cmd_prefix(pid: str, gid: str, devs) -> str:
        """Where a group is told something: the units' common parent topic
        (`pv/inverters` for `pv/inverters/1`), else a path of the gateway's."""
        prefixes = {d.mqtt_topic_prefix.rsplit('/', 1)[0] for d in devs}
        # only when the units sit at `<parent>/<unit id>` is the parent theirs
        # alone; `pv/${device_id}` shares its parent with every other group
        if len(prefixes) == 1 and all(d.mqtt_topic_prefix == f"{next(iter(prefixes))}/{d.connection.unit_id}" for d in devs):
            return prefixes.pop()
        return f"mbg/endpoints/{pid}/{gid}"

    def _group_commands(pid: str, gid: str) -> List[Dict]:
        first = next((d for d in config.endpoint_devices(pid)
                      if (getattr(d, 'group_id', '') or 'units') == gid), None)
        if first is None:
            return []
        bindings = {str(b.get('name', '')): b for b in (first.commands or []) if isinstance(b, dict)}
        out = []
        for c in _commands_for(first):
            d = c['def'].to_dict()
            out.append({'name': d['name'], 'label': d['label'], 'params': d['params'],
                        'alias': d.get('alias'), 'enabled': c['enabled'], 'confirm': c['confirm'],
                        'from_template': c['from_template'], 'binding': bindings.get(d['name']),
                        # the group editor edits the same binding and channel
                        # fields as a device's Commands tab (ADR-0019)
                        'safe': d.get('safe') or None, **_binding_view(c)})
        return out

    def _binding_view(c: Dict) -> Dict:
        """A command's binding and its channel's pace, as an editor shows
        them: what runs (effective), the template's beside it, and what the
        device (or its group) says of its own (ADR-0019)."""
        cid = c.get('channel') or c['def'].name
        eff = (c.get('channels') or {}).get(cid) or {}
        tch = (c.get('template_channels') or {}).get(cid) or {}
        bsafe = c.get('binding_safe') or {}
        return {'lease_s': c['lease_s'], 'safe_from': c.get('safe_from'),
                # where each key of the effective safe comes from (CR2-13)
                'safe_from_keys': {k: ('device' if k in bsafe else 'template') for k in (c['def'].safe or {})},
                'template_safe': c.get('template_safe'),
                # the channel's pace as this device runs it, and the
                # template's beside it
                'channel': cid, 'channel_key': c.get('channel_key') or cid,
                'min_interval_s': eff.get('min_interval_s') or 0,
                'template_min_interval_s': tch.get('min_interval_s') or 0,
                'max_age_s': eff.get('max_age_s') or DEFAULT_MAX_AGE_S,
                'channel_settings': {k: eff[k] for k in CHANNEL_OVERRIDE_KEYS if k in eff},
                'channel_override': dict(c.get('channel_override') or {}),
                'template_channel': dict(tch)}

    def _command_entry(dev_cfg, c: Dict) -> Dict:
        d = c['def'].to_dict()
        d.update({'enabled': c['enabled'], 'from_template': c['from_template'], 'faces': c['faces'],
                  'confirm': c['confirm'], **_binding_view(c),
                  'counters': _cmd_dispatch.counters(dev_cfg.id, c['def'].name),
                  'mqtt_topic': f"{dev_cfg.mqtt_topic_prefix}/cmd/{c['def'].name}",
                  'last': _cmd_last.get(f"{dev_cfg.id}:{c['def'].name}"),
                  'refreshing': f"{dev_cfg.id}:{c['def'].name}" in _cmd_refresh})
        return d

    def _find_command(dev_cfg, name: str):
        return next((c for c in _commands_for(dev_cfg) if c['def'].name == name), None)

    # commands whose setting the device forgets by itself (refresh_s): the
    # gateway writes the same parameters again until the safe ones are sent
    _cmd_refresh: Dict[str, threading.Timer] = {}
    _cmd_refresh_lock = threading.Lock()
    # every command run on a device's command (other than a refresh) moves its
    # generation on, under the command lock: a refresh armed before it — even
    # one already waiting for that lock — then finds itself superseded and
    # writes nothing (it must never put back what a newer command replaced)
    _cmd_refresh_gen: Dict[str, int] = {}

    def _cancel_refresh(dev_id: str, name: str) -> None:
        with _cmd_refresh_lock:
            t = _cmd_refresh.pop(f"{dev_id}:{name}", None)
        if t is not None:
            t.cancel()

    def _cancel_device_refreshes(dev_id: str) -> None:
        """A deleted or stopped device keeps no refresh: re-enabled days
        later, it must not be told an old setting again. Nor what waits for
        its bus (ADR-0018 D12)."""
        _cmd_dispatch.cancel(device=dev_id, reason=f"device '{dev_id}' was deleted or stopped — nothing written")
        _cmds_memo.pop(dev_id, None)                 # rebuilt if it comes back (CR2-9)
        _cmd_dispatch.forget(dev_id)
        with _cmd_refresh_lock:
            keys = [k for k in _cmd_refresh if k.split(':', 1)[0] == dev_id]
            timers = [_cmd_refresh.pop(k) for k in keys]
            for k in keys:
                _cmd_refresh_gen[k] = _cmd_refresh_gen.get(k, 0) + 1
        for t in timers:
            t.cancel()

    def _arm_refresh(dev_id: str, name: str, params: Dict, every: float) -> None:
        key = f"{dev_id}:{name}"
        with _cmd_refresh_lock:
            gen = _cmd_refresh_gen.get(key, 0)

        def _drop():
            with _cmd_refresh_lock:
                if _cmd_refresh.get(key) is timer:
                    _cmd_refresh.pop(key, None)

        def fire():
            with _cmd_refresh_lock:
                if _cmd_refresh.get(key) is not timer:
                    return                             # replaced or cancelled meanwhile
            _i, dcfg, cl = _find_device(dev_id)
            if dcfg is None or not dcfg.enabled:
                _drop()                                # deleted or stopped: the refresh ends here
                return
            code, res = _run_named_command(dcfg, cl, name, dict(params), who='refresh', ip='-',
                                           via='refresh', refresh_gen=gen)
            if res.get('superseded'):
                _drop()                                # a newer command owns the setting now
            elif code in (409, 429, 502):
                # the device or the bus is away for now: keep the setting alive
                logger.warning("command refresh %s %s: %s — trying again", dev_id, name, res.get('reason') or res.get('status'))
                _arm_refresh(dev_id, name, params, every)
            elif code >= 400:
                _drop()
                logger.warning("command refresh %s %s stopped: %s", dev_id, name, res.get('reason') or res.get('status'))
        timer = threading.Timer(every, fire)
        timer.daemon = True
        with _cmd_refresh_lock:
            old = _cmd_refresh.pop(key, None)
            _cmd_refresh[key] = timer
        if old is not None:
            old.cancel()
        timer.start()

    # ── the run: admission (cheap, final refusals) → the lane → the record ──
    # (ADR-0018). Admission answers at once; what spends something — the rule
    # pause, the write-rate token, the safety holder — happens in the lane,
    # under the bus lock, when the command is about to write (D7).
    _CMD_ID_MAX = 64

    def _cmd_kind(via: str) -> str:
        return ('revert' if via == 'lease-revert' else 'refresh' if via == 'refresh'
                else 'sequence' if via.startswith('sequence:') else 'command')

    def _cmd_id_error(cmd_id) -> Optional[str]:
        if cmd_id is None or (isinstance(cmd_id, str) and 0 < len(cmd_id) <= _CMD_ID_MAX):
            return None
        return f"id: a text of 1..{_CMD_ID_MAX} characters — it is echoed in the result"

    def _admit_command(dev_cfg, client, name: str, params: Dict, *, who, ip, via, dry_run,
                       lease_s, refresh_gen, still_wanted, is_current, cmd_id, recheck=None):
        """Every refusal that costs nothing → (http_status, result); else the
        context the lane runs."""
        why = _cmd_id_error(cmd_id)
        if why:
            return 422, {'status': 'rejected', 'reason': why, 'fields': {'id': f'1..{_CMD_ID_MAX} characters'}}
        # the dead-man's time is checked BEFORE anything is written: a bad
        # lease_s once failed after the write, leaving the setting on the
        # device with no lease to take it back (a 500)
        if lease_s is not None and not (isinstance(lease_s, int) and not isinstance(lease_s, bool)
                                        and 0 <= lease_s <= 86400):
            return 422, {'status': 'rejected', 'reason': 'lease_s: a whole number of seconds, 0..86400',
                         'fields': {'lease_s': '0..86400 seconds'}}
        c = _find_command(dev_cfg, name)
        if c is None:
            return 404, {'status': 'rejected', 'reason': f"device '{dev_cfg.id}' offers no command '{name}'"}
        # an alias is another command with its parameters filled in: resolved
        # here, so the slot is the channel of what is really written (review 8)
        for _hop in range(5):
            if not c['def'].alias:
                break
            target = c['def'].alias.get('command')
            merged = dict(c['def'].alias.get('params') or {})
            merged.update({k: v for k, v in (params or {}).items() if k == 'source'})
            params, name = merged, target
            c = _find_command(dev_cfg, target)
            if c is None:
                return 404, {'status': 'rejected', 'reason': f"device '{dev_cfg.id}' offers no command '{target}'"}
        else:
            return 422, {'status': 'rejected', 'reason': f"command '{name}': aliases loop"}
        cmd: CommandDef = c['def']
        # a dry run only reads: it previews a command before enabling it. A
        # lease revert writes the template's own safe recipe: turning the
        # command off while its lease runs must not strand the device at the
        # commanded value (API-A5)
        if not c['enabled'] and not dry_run and via != 'lease-revert':
            return 403, {'status': 'rejected', 'reason': f"command '{name}' is not enabled on '{dev_cfg.id}'"}
        face = via.split('-')[0]                   # api | mqtt | ha; the lease is the gateway's own
        # a sequence writes from outside the rules too: it pauses the channel's
        # lower rules for its run (override_s) and a safety rule stops it
        outside = face in ('api', 'mqtt', 'ha', 'opcua') or via.startswith('sequence:')
        if face in ('api', 'mqtt', 'ha', 'opcua') and not c['faces'].get(face, True):
            return 403, {'status': 'rejected', 'reason': f"command '{name}' does not accept the {face} face on '{dev_cfg.id}'"}
        rt = getattr(app.state, 'rules_runtime', None)
        _pause = None
        if rt is not None and outside and not dry_run:
            owner = rt.owner_of(dev_cfg.id, cmd.name)
            holder = rt.safety_holder(dev_cfg.id, cmd.name) if owner is not None else None
            if holder is not None:
                # a protection rule acting now is never paused from outside
                # (phase 3: safety sits above manual)
                return 409, {'status': 'rejected', 'reason':
                             f"safety rule '{holder}' holds this channel now — it cannot be paused from outside"}
            if owner is not None:
                from .rules_runtime import MAX_OVERRIDE_S
                try:
                    secs = float((params or {}).get('override_s') or 0)
                except (TypeError, ValueError):
                    secs = 0.0
                if not (0 < secs <= MAX_OVERRIDE_S):      # NaN fails both sides
                    return 409, {'status': 'rejected', 'reason':
                                 f"owned by rule '{owner}' — pass override_s (1..{int(MAX_OVERRIDE_S)} s) to pause it"}
                # the rule is paused only once the command is about to write,
                # in the lane — a rejected, superseded or expired command must
                # not silence a rule (F-09, 3.83.0; ADR-0018 D7)
                _pause = (owner, secs)
        params = {k: v for k, v in (params or {}).items() if k != 'override_s'}
        # a dead-man revert restores the SAFE value the template declares; it
        # runs even if writes were disabled or the device locked after the
        # limit was placed — otherwise the limit would stay forever (F-11,
        # 3.83.0). Every other face meets the gates.
        _is_revert = via == 'lease-revert'
        # a standby gateway writes nothing — not even a revert: the device
        # belongs to the active one (503, so a refresh timer stops quietly)
        if not gw_role.active and not dry_run:
            return 503, {'status': 'rejected', 'reason': gw_role.status_text()}
        if not config.security.allow_writes and not dry_run and not _is_revert:
            return 403, {'status': 'rejected', 'reason': 'Writes are disabled — set security.allow_writes=true to enable'}
        if dev_cfg.write_locked and not dry_run and not _is_revert:
            return 403, {'status': 'rejected', 'reason': f"device '{dev_cfg.id}' is write-locked"}
        try:
            norm = normalize_params(cmd, params or {})
        except ValueError as e:
            return 422, {'status': 'rejected', 'reason': str(e)}
        drv = _modbus_driver_of(client)
        mq = _mqtt_driver_of(client) if drv is None else None
        if drv is None and mq is None:
            return 409, {'status': 'rejected', 'reason': 'no Modbus or MQTT source is running for this unit — nothing can write'}
        tpl = _modbus_template_of(dev_cfg)
        cid = c.get('channel') or cmd.name
        ctx = {'dev': dev_cfg, 'client': client, 'c': c, 'cmd': cmd, 'norm': norm, 'via': via, 'who': who,
               'ip': ip, 'dry_run': dry_run, 'lease_s': lease_s, 'refresh_gen': refresh_gen,
               'still_wanted': still_wanted, 'is_current': is_current, 'id': cmd_id, 'rt': rt,
               'recheck': recheck,
               # a revert run by a clean shutdown waits for the bus only within
               # the shutdown's budget (ADR-0022 D8, review HIGH 1): its deadline
               # travels with the job — the budget is the stopping thread's own
               'deadline': _budget_deadline() if _is_revert else None,
               'pause': _pause, 'outside': outside, 'is_revert': _is_revert, 'drv': drv, 'mq': mq,
               'regs': {r.name: r for r in (tpl.registers if tpl else [])}, 'channel': cid,
               'max_age_s': float(((c.get('channels') or {}).get(cid) or {}).get('max_age_s')
                                  or DEFAULT_MAX_AGE_S)}
        # the held channel answers at once (D5) — when nothing waits ahead of
        # this command in its slot; it is asked again at the head of the lane
        if _hold_applies(ctx) and _cmd_dispatch.pending(_lane_key(dev_cfg, client), dev_cfg.id, cid) == 0:
            held = _held(ctx)
            if held is not None:
                return held
        return ctx

    def _hold_applies(ctx) -> bool:
        # the channel's protection (minimum run / stop, starts per hour, the
        # pace) holds whoever writes — a rule's own writes meet it in the
        # referee, the dead-man never waits
        return (ctx['rt'] is not None and not ctx['dry_run'] and not ctx['via'].startswith('rule:')
                and not ctx['is_revert'])

    def _held(ctx, check_already: bool = True):
        """(409, held) when the channel's protection holds this command now —
        unless the device already holds what it says (a renewal that changes
        nothing is never held: CMD-Q-5)."""
        got = ctx['rt'].channel_hold_s(ctx['dev'], ctx['cmd'].name, ctx['norm'])
        if not got:
            return None
        if check_already and _already_set(ctx) is not None:
            return None
        _cmd_dispatch.count(ctx['dev'].id, ctx['cmd'].name, 'held')
        return 409, {'status': 'rejected', 'held': True, 'reason': f"channel {got[0]}",
                     'retry_after_s': round(max(0.1, got[1]), 1)}

    def _already_set(ctx) -> Optional[Dict]:
        """D6: the device holds what this command says — asked in the lane
        against the reading, after what waited in the slot was superseded
        (never at admission against the reading alone: review 1). An MQTT
        source alive with every check a written register (its own already-set
        test); a Modbus device only by the template's `skip_if_set`."""
        cmd, norm = ctx['cmd'], ctx['norm']
        if ctx['mq'] is not None:
            try:
                pre = run_mqtt_command(cmd, norm, ctx['mq'], ctx['regs'], dry_run=True,
                                       forbidden_topics=_gateway_command_topics(ctx['dev']))
            except ValueError:
                return None
            return pre if pre.get('already_set') else None
        if not cmd.skip_if_set or cmd.refresh_s or 'revert_s' in cmd.params or not cmd.writes:
            return None
        store = registry.store_for(ctx['dev'].id) or {}
        now = time.monotonic()
        held: Dict[str, Any] = {}
        for w in cmd.writes:
            r = ctx['regs'].get(w.get('register'))
            if r is None or (getattr(r, 'register_type', 'holding') or 'holding').lower() != 'holding':
                return None
            e = store.get(store_key(int(r.address), 'holding'))
            # polled, and fresher than two of its poll periods — else a write
            if not e or e.get('value') is None or e.get('mono') is None:
                return None
            every = float(e.get('interval') or 0)
            if every <= 0 or now - float(e['mono']) > 2 * every:
                return None
            try:
                want, got = float(_cmd_eval(w.get('value'), norm)), float(e['value'])
            except (TypeError, ValueError, ArithmeticError):
                return None
            tol = next((float(v.get('tolerance', 0) or 0) for v in cmd.verify if v.get('read') == r.name), 0.0)
            if abs(got - want) > max(tol, 1e-9):
                return None
            held[r.name] = got
        return {'command': cmd.name, 'params': dict(norm), 'status': 'success', 'already_set': True,
                'frames_sent': 0, 'before': dict(held), 'after': dict(held)}

    def _exec_timeout(ctx) -> float:
        """How long a running command may take: every transaction it makes —
        the guard reads (re-read once when one fails), each written frame, the
        verify reads before and after — up to the connection's timeout ×
        (retries + 1), plus the settle (ADR-0018 D4). Counted from the recipe:
        three transactions was too few for a long one (CR2-3)."""
        settle = float(ctx['cmd'].settle_s or 0)
        if ctx['mq'] is not None:
            return settle + 5.0
        cc = getattr(getattr(ctx['drv'], 'connection', None), 'config', None)
        try:
            per = float(getattr(cc, 'timeout', 3) or 3) * (int(getattr(cc, 'retry_attempts', 3) or 0) + 1)
        except (TypeError, ValueError):
            per = 12.0
        cmd = ctx['cmd']
        n = 2 * len(cmd.guard or []) + len(cmd.writes or []) + 2 * len(cmd.verify or []) + 1
        return max(3, n) * per + settle

    def _execute(ctx, job=None):
        """The lane's part, under the bus lock: the last checks (the lease
        still current, the role, the hold), what it spends (the rate token,
        the rule pause), the write, then the record."""
        dev_cfg, cmd, norm, via, who, rt = ctx['dev'], ctx['cmd'], ctx['norm'], ctx['via'], ctx['who'], ctx['rt']
        dry_run, mq, drv, regs = ctx['dry_run'], ctx['mq'], ctx['drv'], ctx['regs']
        queued_ms = (round((job.started - job.admitted) * 1000.0, 1)
                     if job is not None and job.started is not None else None)
        _rkey = f"{dev_cfg.id}:{cmd.name}"
        already = None
        try:
            with _bus_lock(_cmd_lock_for(drv if drv is not None else ctx['client'], dev_cfg), ctx.get('deadline')):
                if ctx['is_revert'] and ctx['is_current'] is not None and not ctx['is_current']():
                    # asked again at the head of the lane (review 2): renewed
                    # or cleared while the revert waited — nothing to undo
                    return 409, {'status': 'cancelled', 'lease_renewed': True,
                                 'reason': 'the lease was renewed or cleared while the revert waited — nothing written'}
                still_wanted = ctx['still_wanted']
                if still_wanted is not None and not dry_run and not still_wanted():
                    # queued behind the bus while its rule left armed: the
                    # rule no longer drives this target — nothing is written
                    res = {'status': 'cancelled', 'params': norm,
                           'reason': 'its rule left armed before the bus was free — nothing written',
                           'device': dev_cfg.id, 'via': via, 'by': who, 'ts': round(time.time(), 3)}
                    audit_log.append(user=who, ip=ctx['ip'], action="command", target=f"{dev_cfg.id} {cmd.name}",
                                     status='cancelled', detail={'params': norm, 'via': via, 'reason': res['reason']})
                    logger.info("COMMAND CANCELLED: device=%s %s %s via %s", dev_cfg.id, cmd.name, norm, via)
                    return 409, res
                recheck = ctx.get('recheck')
                if recheck is not None and not dry_run:
                    # a rule's job is asked again here, under the bus lock,
                    # on what it captured when it decided: paused, yielded or
                    # held since (CMD-Q-8, ADR-0021 D3) — and a standby takes
                    # it back, it is no failure (D6). Before the rate token
                    # and any write (OV-F4-R3)
                    got = recheck()
                    if got is None and not gw_role.active:
                        got = ('standby', gw_role.status_text())
                    if got is not None:
                        return _dropped(ctx, *got)
                if not dry_run and not gw_role.active:
                    return 503, {'status': 'rejected', 'reason': gw_role.status_text()}
                # a rule's write behaves exactly as before 3.93 until the rules
                # lot (CR2-6): no renewal shortcut, the rate token, the stamp
                if not dry_run and not via.startswith('rule:'):
                    already = _already_set(ctx)
                if not dry_run and already is None:
                    # the hold again, under the bus lock: two callers that both
                    # passed it at admission cannot both write (review 12)
                    if _hold_applies(ctx):
                        held = _held(ctx, check_already=False)
                        if held is not None:
                            return held
                    if not _write_rate_ok(f'{via}:{dev_cfg.id}'):
                        return 429, {'status': 'rejected', 'reason': 'write rate limit exceeded'}
                if ctx['outside'] and rt is not None and not dry_run:
                    # asked always, not only when a rule owned the channel at
                    # admission: one armed (or reloaded) while this waited is
                    # a safety rule too (RD-3). Never owner_of here: it takes
                    # the rules lock (ADR-0021, lock order bus → rt._lock)
                    holder = rt.safety_holder(dev_cfg.id, cmd.name)
                    if holder is not None:
                        return 409, {'status': 'rejected', 'reason':
                                     f"safety rule '{holder}' holds this channel now — it cannot be paused from outside"}
                if ctx['pause'] is not None and not dry_run:
                    rt.pause_channel(dev_cfg.id, cmd.name, ctx['pause'][1], who=who, via=via)
                if not dry_run:
                    with _cmd_refresh_lock:
                        if via == 'refresh':
                            if _cmd_refresh_gen.get(_rkey, 0) != ctx['refresh_gen']:
                                return 409, {'status': 'superseded', 'superseded': True,
                                             'reason': 'a newer command replaced this setting — refresh ended'}
                        else:
                            _cmd_refresh_gen[_rkey] = _cmd_refresh_gen.get(_rkey, 0) + 1
                if already is not None:
                    res = dict(already, status='success')
                elif mq is not None:
                    res = run_mqtt_command(cmd, norm, mq, regs, dry_run=dry_run,
                                           forbidden_topics=_gateway_command_topics(dev_cfg))
                else:
                    res = run_command(cmd, norm, drv.connection, regs,
                                      byte_order=getattr(drv, 'byte_order', 'big'), dry_run=dry_run,
                                      max_read=int(getattr(drv, 'max_read', 0) or 0))
                if not dry_run:
                    _stamp(ctx, res)
        except ValueError as e:                       # a bad definition — the operator's to fix
            return 422, {'status': 'rejected', 'reason': f"command definition: {e}"}
        except _BusBusy as e:
            return 409, {'status': 'cancelled', 'reason': str(e)}
        if queued_ms is not None:
            res['queued_ms'] = queued_ms
        return _record(ctx, res)

    def _stamp(ctx, res: Dict) -> None:
        """The channel's view of a command that ran — the last step under the
        bus lock (ADR-0021 D5): a writer waiting for the bus sees it, never a
        gap between the lock released and the stamp (CMD-Q-8, mirror)."""
        rt, via = ctx['rt'], ctx['via']
        ctx['stamped'] = True
        if res.get('status') not in ('success', 'mismatch', 'unverified') or rt is None:
            return
        try:
            if res.get('already_set') and not via.startswith('rule:'):
                # nothing went out: the pace is not stamped, an unknown
                # on/off is learnt without a start (CMD-Q-5, review 10)
                rt.channel_reconcile(ctx['dev'], ctx['cmd'].name, ctx['norm'])
            else:
                rt.channel_note(ctx['dev'], ctx['cmd'].name, ctx['norm'])   # the motor's view, by whoever
        except Exception:  # noqa: BLE001
            pass

    def _dropped(ctx, dropped: str, why: str):
        """A rule's job the head of the bus did not write (ADR-0021 D4):
        cancelled, said why, audited like the disarm cancel — no failure."""
        dev_cfg, cmd, norm, via, who = ctx['dev'], ctx['cmd'], ctx['norm'], ctx['via'], ctx['who']
        res = {'status': 'cancelled', 'dropped': dropped, 'params': norm, 'reason': why,
               'device': dev_cfg.id, 'via': via, 'by': who, 'ts': round(time.time(), 3)}
        audit_log.append(user=who, ip=ctx['ip'], action="command", target=f"{dev_cfg.id} {cmd.name}",
                         status='cancelled', detail={'params': norm, 'via': via, 'dropped': dropped, 'reason': why})
        logger.info("COMMAND CANCELLED (%s): device=%s %s %s via %s — %s", dropped, dev_cfg.id, cmd.name, norm,
                    via, why)
        if dropped == 'held':
            _cmd_dispatch.count(dev_cfg.id, cmd.name, 'held')
        return 409, res

    def _record(ctx, res: Dict):
        """What a command that ran leaves behind: the log, the device's
        events, the audit, the last result, the channel's view, the read-back
        in the store, the result on MQTT, the dead-man, the refresh."""
        dev_cfg, cmd, norm, via, who = ctx['dev'], ctx['cmd'], ctx['norm'], ctx['via'], ctx['who']
        dry_run, mq, drv, regs, c, ip = ctx['dry_run'], ctx['mq'], ctx['drv'], ctx['regs'], ctx['c'], ctx['ip']
        lease_s = ctx['lease_s']
        if ctx.get('id') is not None:
            res['id'] = ctx['id']
        res.update({'device': dev_cfg.id, 'via': via, 'by': who, 'ts': round(time.time(), 3)})
        if dry_run:
            return 200, res
        ok = res['status'] == 'success'
        summary = (f"{cmd.name} {norm} · {res['status']}"
                   + (f" · {res['reason']}" if res.get('reason') else '')
                   + f" · via {via} by {who}")
        logger.warning("COMMAND %s: device=%s %s", res['status'].upper(), dev_cfg.id, summary)
        try:
            (mq if mq is not None else drv.connection).record_event(
                'info' if ok else 'warn' if res['status'] in ('unverified', 'mismatch') else 'error',
                'command', summary)
        except Exception:  # noqa: BLE001
            pass
        audit_log.append(user=who, ip=ip, action="command", target=f"{dev_cfg.id} {cmd.name}",
                         status=res['status'],
                         detail={'params': norm, 'via': via, 'frames': res.get('frames'),
                                 'before': res.get('before'), 'after': res.get('after'),
                                 'reason': res.get('reason'), 'ms': res.get('ms')})
        _cmd_last[f"{dev_cfg.id}:{cmd.name}"] = {k: res.get(k) for k in ('status', 'params', 'before', 'after', 'reason', 'ms', 'via', 'by', 'ts')}
        if not ctx.get('stamped'):
            _stamp(ctx, res)                     # stamped under the bus lock already (D5)
        if res['status'] in ('success', 'mismatch') and mq is None:
            # (an MQTT read-back reached the store through its subscription)
            # the read-back reaches the live store at once, and the group that
            # carries it is swept now so MQTT/InfluxDB/HA follow in seconds
            for n, v in (res.get('after') or {}).items():
                r = regs.get(n)
                if r is not None and v is not None:
                    _push_readback_to_store(dev_cfg.id, int(r.address), v,
                                            getattr(r, 'register_type', 'holding') or 'holding')
            try:
                if cmd.readback_group and hasattr(drv, 'poll_now'):
                    drv.poll_now(cmd.readback_group)
            except Exception:  # noqa: BLE001
                pass
            # A command with a device-side revert timer (``revert_s``) changes
            # the device again when that timer fires, with no write from here
            # to trigger a read-back — the retained limit would say 95 for up
            # to an hour (the controls group is hourly on purpose) while the
            # inverter is back at 100. Sweep the group a few times after the
            # mark (REVERT_REREAD_AT_S), so MQTT/InfluxDB/HA and the rules see
            # the revert within seconds of it.
            try:
                revert = float(norm.get('revert_s') or 0)
                if revert > 0 and cmd.readback_group and hasattr(drv, 'poll_now'):
                    _arm_revert_reread(dev_cfg.id, cmd.name, cmd.readback_group,
                                       tuple(revert + d for d in REVERT_REREAD_AT_S))
            except Exception:  # noqa: BLE001
                pass
        # the dead-man: a controller that dies leaves the device where it put
        # it only until this lease expires — then the gateway runs `safe`
        # a refresh only repeats a setting: it must not renew the dead-man the
        # original command armed, or a dead controller's setting never ends
        # The lease is the OUTSIDE controller's dead-man (ADR-0019): a rule
        # never arms it — 90 s after an OV curtail it would put 100 % back
        # over the rule (RD-1) — and a rule's write that went out ends one an
        # outside controller left, whose revert would otherwise write over the
        # rule (RD-2). The rule's own release value is the rule's business.
        # A sequence is an outside writer: its lease behaves as before.
        is_rule = via.startswith('rule:')
        if is_rule and (ok or int(res.get('frames_sent') or 0) > 0):
            # the whole CHANNEL: an outside lease taken through another command
            # of it (keyed on that command's first register) ends too
            for oc in _commands_for(dev_cfg):
                if oc.get('channel') != ctx['channel'] or not oc['def'].writes:
                    continue
                first = regs.get(oc['def'].writes[0].get('register'))
                if first is not None:
                    _lease_mgr.clear(dev_cfg.id, 'command', int(first.address))
        lease = 0 if (via == 'refresh' or is_rule) else int(lease_s if lease_s is not None else c['lease_s'] or 0)
        vp = cmd.value_param
        safe_keys = [vp.name] if (vp is not None and cmd.safe and vp.name in cmd.safe) else list((cmd.safe or {}).keys())
        is_safe = bool(cmd.safe) and all(norm.get(k) == cmd.safe[k] for k in safe_keys)
        # F-03 (3.83.0): frames that went out are on the device whatever the
        # read-back said — an unverified or mismatched command still needs
        # the dead-man, otherwise a dead controller leaves the limit in place
        sent = int(res.get('frames_sent') or 0) > 0
        if (ok or sent) and lease > 0 and cmd.safe and not is_safe:
            _revert = _make_command_lease_revert(dev_cfg.id, cmd.name)
            first = regs.get(cmd.writes[0]['register']) if cmd.writes else None
            addr = int(first.address) if first is not None else 0
            # the record is declarative for the listing and for boot recovery;
            # `command` is what recovery dispatches on — the raw fields are
            # informational and are never written as such
            # its own namespace ('command'): a raw leased write on the same
            # register must never replace the recipe revert (F-75, 3.83.0)
            _lease_mgr.arm(dev_cfg.id, 'command', addr, lease * 1000, _revert,
                           meta={'device': dev_cfg.id, 'register_type': 'command', 'address': addr,
                                 'data_type': 'command', 'scale': 1.0, 'offset': 0.0,
                                 'safe_value': cmd.safe.get('value'), 'lease_ms': lease * 1000,
                                 'command': cmd.name})
            res['lease_armed'] = True
        elif lease > 0 and not cmd.safe and (ok or sent):
            # a dead-man without a release value never fires: said, not hidden
            res.update(lease_armed=False, lease_reason='no safe value on the binding or the template — '
                                                      'the lease cannot release anything')
        elif ok and is_safe and cmd.writes:
            first = regs.get(cmd.writes[0]['register'])
            if first is not None:
                _lease_mgr.clear(dev_cfg.id, 'command', int(first.address))
        if cmd.refresh_s:
            if is_safe and (ok or sent):
                _cancel_refresh(dev_cfg.id, cmd.name)     # the safe value ends the refresh
            elif ok or sent:
                _arm_refresh(dev_cfg.id, cmd.name, norm, float(cmd.refresh_s))
        _publish_command_result(dev_cfg, cmd.name, res)
        ctx['published'] = True
        code = 200 if res['status'] in ('success', 'mismatch', 'unverified') else 422 if res['status'] == 'rejected' else 502
        return code, res

    def _submit_named_command(dev_cfg, client, name: str, params: Dict, *, who='?', ip='?', via='api',
                              dry_run=False, lease_s=None, refresh_gen=None, still_wanted=None,
                              is_current=None, cmd_id=None, tell=False, recheck=None):
        """Admission, then the command in its lane. Returns (http_status,
        result) when it was refused or ran inline, else the queued Job.
        ``tell``: every outcome goes to cmd/result (a command that came over
        MQTT / HA: CMD-Q-4)."""
        adm = _admit_command(dev_cfg, client, name, params, who=who, ip=ip, via=via, dry_run=dry_run,
                             lease_s=lease_s, refresh_gen=refresh_gen, still_wanted=still_wanted,
                             is_current=is_current, cmd_id=cmd_id, recheck=recheck)
        if isinstance(adm, tuple):
            code, res = adm
            res.update({k: v for k, v in (('device', dev_cfg.id), ('via', via), ('by', who), ('id', cmd_id))
                        if v is not None and k not in res})
            # the parameters as asked: a client without ids still matches a
            # refusal to its request (CR2-12)
            res.setdefault('params', {k: v for k, v in (params or {}).items() if k != 'override_s'})
            return code, res
        if dry_run or via.startswith('rule:'):
            # a dry run only reads; a rule's write travels on its own
            # per-device writer with the referee (D13: moved in the rules lot)
            return _execute(adm)
        cmd = adm['cmd']

        def told(code, res, _ctx=adm):
            # what the lane said without running it (superseded, expired,
            # held at the head, cancelled): an executed one is already out
            if tell and not _ctx.get('published'):
                _publish_command_result(dev_cfg, cmd.name,
                                        {'params': dict(_ctx['norm']), **res, 'device': dev_cfg.id, 'via': via,
                                         'by': who, 'ts': round(time.time(), 3)})
        job = _CmdJob(lane=_lane_key(dev_cfg, client), device=dev_cfg.id, channel=adm['channel'],
                      command=cmd.name, params=dict(adm['norm']), run=lambda j: _execute(j.ctx, j),
                      kind=_cmd_kind(via), coalesce=cmd.coalesce, max_age_s=adm['max_age_s'],
                      id=cmd_id, on_done=told, ctx=adm)
        return _cmd_dispatch.submit(job)

    def _await_command(job) -> Tuple[int, Dict]:
        """A queued command's verdict, waited for at most its max_age_s and
        its execution time (D4) — a shutdown revert at most its budget: past
        it, one not started is withdrawn and one running is said to run; the
        lease stays on disk for boot recovery (ADR-0022, review HIGH 1)."""
        wait = job.ctx['max_age_s'] + _exec_timeout(job.ctx) + 1.0
        deadline = job.ctx.get('deadline')
        if deadline is not None:
            wait = max(0.0, min(wait, deadline - time.monotonic()))
        code, res = _cmd_dispatch.wait_for(job, wait)
        if code == 504 and job.kind == 'revert' and deadline is None:
            # a revert that runs is in flight, not failed: its waiter (the lease
            # sweep's thread, which keeps the key in flight) waits for the end —
            # a 504 here used to read as a failure and fire it again (CR2-3)
            job.wait()
            code, res = job.result
        res.setdefault('device', job.device)
        return code, res

    def _run_named_command(dev_cfg, client, name: str, params: Dict, *, who='?', ip='?',
                           via='api', dry_run=False, lease_s=None, refresh_gen=None, still_wanted=None,
                           is_current=None, cmd_id=None, recheck=None):
        """Run one command on one unit with every gate, then record it —
        whichever face asked — and wait for it. Returns (http_status, result).
        `still_wanted` (a rule's write) is asked once the bus is ours: a rule
        that left armed meanwhile writes nothing (OV-F4); `is_current` (a
        lease revert) likewise. `recheck` (a rule's write, rules.recheck on)
        is asked there too: paused, yielded or held since it decided
        (ADR-0021)."""
        got = _submit_named_command(dev_cfg, client, name, params, who=who, ip=ip, via=via, dry_run=dry_run,
                                    lease_s=lease_s, refresh_gen=refresh_gen, still_wanted=still_wanted,
                                    is_current=is_current, cmd_id=cmd_id, recheck=recheck)
        return got if isinstance(got, tuple) else _await_command(got)

    _APPLIED = ('success', 'mismatch', 'unverified')

    def _publish_command_result(dev_cfg, name: str, res: Dict) -> None:
        # a standby hears the same broker: it never answers for the active
        # gateway (review 5)
        if not gw_role.active:
            return
        if not mqtt_publisher or not getattr(mqtt_publisher, 'connected', False):
            return
        try:
            body = json.dumps({'command': name, **res}, default=str)
            mqtt_publisher.client.publish(f"{dev_cfg.mqtt_topic_prefix}/cmd/result", body, qos=1, retain=False)
            # the last APPLIED command, retained: a controller that restarts
            # learns what the device was told without asking the wire — a
            # refusal or an error is not what the device holds (CMD-Q-9)
            if res.get('status') in _APPLIED:
                mqtt_publisher.client.publish(f"{dev_cfg.mqtt_topic_prefix}/cmd/{name}/state",
                                              json.dumps({k: res.get(k) for k in ('status', 'params', 'after', 'ts', 'via', 'by')}, default=str),
                                              qos=1, retain=True)
        except Exception:  # noqa: BLE001
            pass

    def _cmd_gate(request: Request, dry_run=False):
        if not dry_run and not config.security.allow_writes:
            raise HTTPException(status_code=403, detail={"errors": [
                "Writes are disabled — set security.allow_writes=true to enable"]})
        if not dry_run and not (auth_state.enabled or _api_key):
            raise HTTPException(status_code=403, detail={"errors": [
                "writes require authentication — enable login (ui.auth) or set an API_KEY"]})
        who = getattr(request.state, "user", None) or ("api-key" if _api_key else "anon")
        return who, (request.client.host if request.client else "?")

    @app.get("/api/devices/{device_id}/commands")
    def list_device_commands(device_id: str):
        """What this device can be told: each command's parameters (bounds,
        defaults, units), faces, whether it is enabled, its last result. A
        controller discovers this instead of hard-coding registers."""
        _i, dev_cfg, _c = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        cmds = _commands_for(dev_cfg)
        return {'device': device_id, 'commands': [_command_entry(dev_cfg, c) for c in cmds],
                # overrides whose channel the template no longer has: kept, said
                'orphan_channels': list(cmds[0].get('orphan_channels') or []) if cmds else [],
                # two keys for one channel: the one not taken (CR2-14)
                'channel_conflicts': list(cmds[0].get('channel_conflicts') or []) if cmds else []}

    def _own_device_for_binding(device_id: str, what: str):
        """(dev_cfg, client) of a device of its own; an installation's unit
        takes its bindings and channels from its group (ADR-0019)."""
        _i, dev_cfg, client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        if dev_cfg.endpoint_id:
            # an installation with groups keeps them per group; one without, on
            # itself (CR2-4: the pointer named a group that does not exist)
            grouped = bool((config.get_raw_endpoint(dev_cfg.endpoint_id) or {}).get('groups'))
            where = (f"in the installation's group '{getattr(dev_cfg, 'group_id', '') or 'units'}'" if grouped
                     else "on the installation itself")
            raise HTTPException(status_code=422, detail={"errors": [
                f"'{device_id}' belongs to installation '{dev_cfg.endpoint_id}' — set its {what} {where}"]})
        return dev_cfg, client

    def _store_device_raw(device_id: str, client, mutate) -> Tuple[Dict, Dict]:
        """Change the stored device under the mutation lock — a write-lock or
        an edit landing between the read and the upsert would otherwise be
        lost. ``mutate(raw)`` edits the copy; returns (old raw, new raw)."""
        with _device_mutation_lock:
            raw = next((copy.deepcopy(d) for d in config._raw_devices if d.get('id') == device_id), None)
            if raw is None:
                raise HTTPException(status_code=404, detail="device not found")
            old = copy.deepcopy(raw)
            mutate(raw)
            try:
                new_cfg = config.upsert_raw_device(raw)
            except ValueError as e:
                raise HTTPException(status_code=422, detail={"errors": [str(e)]})
            registry.replace(device_id, new_cfg, client=client, add_if_missing=False)
            _commands_changed()
        return old, raw

    _BINDING_KEYS = ('enabled', 'lease_s', 'safe', 'faces', 'confirm')

    def _binding_errors(c: Dict, merged: Dict, changed, tpl_safe) -> Dict[str, str]:
        """What a save CHANGES in a binding: the fields' shapes, `safe`
        through the command's own bounds (CMD-Q-11), and a dead-man that has
        something to release (CMD-Q-6). What is stored and not touched is
        not judged (ADR-0006): turning a command off is never refused (CR2-1)."""
        errs: List[str] = []
        _validate_commands([{'name': merged['name'], **{k: merged[k] for k in changed if k in merged}}],
                           errs, safe_def=c['def'])
        fields = {e.split(':', 1)[0].replace('commands[0].', ''): e.split(':', 1)[1].strip() for e in errs}
        safe = merged.get('safe') if isinstance(merged.get('safe'), dict) else tpl_safe
        if not fields and ({'lease_s', 'safe'} & set(changed)) and int(merged.get('lease_s') or 0) > 0 \
                and not safe:
            fields['lease_s'] = ('a lease without a safe value never fires — set safe '
                                 '(what the gateway writes when the lease runs out)')
        return fields

    def _save_binding(device_id: str, name: str, changes: Dict, request: Optional[Request]) -> Dict:
        dev_cfg, client = _own_device_for_binding(device_id, 'command bindings')
        c = next((x for x in _commands_for(dev_cfg) if x['def'].name == name and x['from_template']), None)
        if c is None:
            raise HTTPException(status_code=404, detail=f"the template offers no command '{name}'")
        said: Dict[str, Dict] = {}

        def mutate(raw):
            # read from what is stored, under the lock: the file is the truth
            prev = next((dict(b) for b in (raw.get('commands') or [])
                         if isinstance(b, dict) and b.get('name') == name), {})
            # off keeps the binding and what it says (safe values, faces,
            # lease): turning it back on finds them, not the template's. A
            # null removes a field (the template's again)
            merged = {**prev, 'name': name, 'enabled': bool(prev.get('enabled', True)) if prev else False}
            for k, v in changes.items():
                if v is None:
                    merged.pop(k, None)
                else:
                    merged[k] = v
            _tpl = _modbus_template_of(dev_cfg)
            fields = _binding_errors(c, merged, [k for k in changes if k != 'enabled'],
                                     dict(((_tpl.commands or {}).get(name) or {}).get('safe') or {}) if _tpl else {})
            if fields:
                raise HTTPException(status_code=422, detail={"errors": [f"{k}: {v}" for k, v in fields.items()],
                                                             "fields": fields})
            binds = [dict(b) for b in (raw.get('commands') or []) if isinstance(b, dict) and b.get('name') != name]
            binds.append(merged)
            raw['commands'] = binds
            said.update(prev=prev, merged=merged)
        _store_device_raw(device_id, client, mutate)
        prev, merged = said['prev'], said['merged']
        if not merged.get('enabled'):
            _cancel_refresh(device_id, name)            # a disabled command stops refreshing
        if request is not None:
            audit_log.append(user=getattr(request.state, "user", None) or ("api-key" if _api_key else "anon"),
                             ip=request.client.host if request.client else "?", action="command binding",
                             target=f"{device_id} {name}", status="ok",
                             detail={'old': prev or None, 'new': merged})
        # rules bound to it re-bind, its MQTT command topic appears or goes
        _sync_device_discovery()
        return merged

    @app.put("/api/devices/{device_id}/commands/{name}/enabled")
    @_device_write
    def set_device_command_enabled(device_id: str, name: str, request: Request, payload: Dict = Body(...)):
        """Enable or disable one of the template's commands on a device of its
        own (an installation's units are enabled from the installation's
        group). Nothing is written to the device."""
        _own_device_for_binding(device_id, 'commands')
        # a JSON true/false, said: "false" (a string) once read as true and
        # ENABLED the command; a missing key disabled it
        on = payload.get('enabled')
        if not isinstance(on, bool):
            raise HTTPException(status_code=422, detail={"errors": ["enabled: true or false"],
                                                         "fields": {"enabled": "true or false"}})
        _save_binding(device_id, name, {'enabled': on}, request)
        return {'device': device_id, 'command': name, 'enabled': on}

    @app.put("/api/devices/{device_id}/commands/{name}")
    @_device_write
    def set_device_command_binding(device_id: str, name: str, request: Request, payload: Dict = Body(...)):
        """A template command's binding on a device of its own: {enabled,
        lease_s, safe, faces, confirm}, merged into what is stored (a key not
        sent is kept, null removes it; off keeps the binding). `safe` is
        checked against the command's bounds; a lease needs a safe value.
        Audited old / new; nothing is written to the device (ADR-0019)."""
        if not isinstance(payload, dict) or not payload:
            raise HTTPException(status_code=422, detail={"errors": [
                f"send any of {', '.join(_BINDING_KEYS)}"]})
        unknown = sorted(set(payload) - set(_BINDING_KEYS))
        if unknown:
            raise HTTPException(status_code=422, detail={"errors": [
                f"{', '.join(unknown)}: not a binding field ({', '.join(_BINDING_KEYS)})"],
                "fields": {k: 'unknown' for k in unknown}})
        merged = _save_binding(device_id, name, dict(payload), request)
        dev_cfg = config.get_device(device_id)
        c = _find_command(dev_cfg, name) if dev_cfg is not None else None
        warns = _lease_pace_warnings(dev_cfg, command=name) if dev_cfg is not None else []
        return {'device': device_id, 'command': name, 'binding': merged,
                **({'entry': _command_entry(dev_cfg, c)} if c is not None else {}),
                **({'warnings': warns} if warns else {})}

    def _lease_pace_warnings(dev_cfg, channel: Optional[str] = None, command: Optional[str] = None) -> List[str]:
        """A binding's dead-man shorter than its channel's min_interval_s: a
        controller renewing at the lease's pace is held, and the lease may
        run out between renewals (ADR-0022, starvation said)."""
        out = []
        for c in _commands_for(dev_cfg):
            cid = c.get('channel') or c['def'].name
            if (channel is not None and cid != channel) or (command is not None and c['def'].name != command):
                continue
            lease = int(c.get('lease_s') or 0)
            try:
                mi = float((((c.get('channels') or {}).get(cid)) or {}).get('min_interval_s') or 0)
            except (TypeError, ValueError):
                mi = 0.0
            if lease > 0 and mi > lease:
                out.append(f"{c['def'].name}: lease_s {lease} is shorter than channel {cid}'s min_interval_s "
                           f"{mi:g} — a controller renewing at the lease's pace is held, and the dead-man may "
                           f"fire between renewals")
        return out

    def _channel_override_errors(value) -> Dict[str, str]:
        if not isinstance(value, dict) or not value:
            return {'channel': f"an object of {', '.join(CHANNEL_OVERRIDE_KEYS)} (null removes one)"}
        errs: Dict[str, str] = {}
        for k, v in value.items():
            if k not in CHANNEL_OVERRIDE_KEYS:
                errs[k] = (f"not a device's to set ({', '.join(CHANNEL_OVERRIDE_KEYS)}; "
                           "`combine` is the template's)")
            elif v is None:
                continue
            elif isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 86400:
                errs[k] = '0..86400'
            elif k == 'max_starts_per_h' and not float(v).is_integer():
                errs[k] = 'a whole number'
            elif k == 'max_age_s' and v < 1:
                errs[k] = '1..86400 (0 would expire every command)'
        return errs

    @app.put("/api/devices/{device_id}/channels/{key}")
    @_device_write
    def set_device_channel(device_id: str, key: str, request: Request, payload: Dict = Body(...)):
        """This device's override of one of its template's channels — the key
        is a declared channel name or a member command's name: {min_interval_s,
        min_on_s, min_off_s, max_starts_per_h, max_age_s}, a null removes one
        (the template's again). Lower than the template is allowed (60 → 5 is
        the point); audited old / new (ADR-0019)."""
        dev_cfg, client = _own_device_for_binding(device_id, 'channel settings')
        cmds = _commands_for(dev_cfg)
        defs = {c['def'].name: c['def'] for c in cmds}
        ids = assign_channels(defs) if defs else {}
        declared = {d.channel for d in defs.values() if d.channel}
        if key not in declared and key not in ids:
            raise HTTPException(status_code=422, detail={"errors": [
                f"'{key}' is not a channel of '{device_id}' — name a channel "
                f"({', '.join(sorted(declared)) or 'none declared'}) or one of its commands"],
                "fields": {'key': 'unknown channel'}})
        errs = _channel_override_errors(payload)
        if errs:
            raise HTTPException(status_code=422, detail={"errors": [f"{k}: {v}" for k, v in errs.items()],
                                                         "fields": errs})
        # one channel, one key: `motor` and `set_frequency` both name the VFD's
        # motor — which one won depended on the file's order (CR2-14)
        _cid = _override_cid(key, defs, ids)
        other = next((k for k in (getattr(dev_cfg, 'channels', None) or {})
                      if k != key and _override_cid(k, defs, ids) == _cid), None)
        if other is not None:
            raise HTTPException(status_code=422, detail={"errors": [
                f"channel '{_cid}' is already overridden under '{other}' — edit that one"],
                "fields": {'key': f"already set under '{other}'"}})
        # a pace on a channel an armed steps rule with a fast step drives is
        # refused (ADR-0022, operator Q5) — judged on what this save changes:
        # a pace already stored is not set again by an edit of max_age_s (RP-8)
        _rt = getattr(app.state, 'rules_runtime', None)
        _before = ((getattr(dev_cfg, 'channels', None) or {}).get(key)) or {}
        _before = _before if isinstance(_before, dict) else {}
        _paced = sorted(k for k, v in payload.items() if k in PACE_KEYS and v and v != _before.get(k))
        _fast = _rt.fast_rule_on(device_id, _cid) if (_rt is not None and _paced) else None
        if _fast is not None:
            raise HTTPException(status_code=422, detail={"errors": [
                f"channel '{_cid}' is driven by the armed rule '{_fast}', a steps rule with a fast step: a pace "
                f"({', '.join(_paced)}) would hold its other reasserts while its emergency step passes it — "
                f"put the rule in shadow or remove `fast` first"],
                "fields": {k: f"refused: rule '{_fast}' has a fast step" for k in _paced}})
        said: Dict[str, Dict] = {}

        def mutate(raw):
            chans = {k: dict(v) for k, v in (raw.get('channels') or {}).items() if isinstance(v, dict)}
            prev = dict(chans.get(key) or {})
            merged = {**prev, **{k: v for k, v in payload.items() if v is not None}}
            for k, v in payload.items():
                if v is None:
                    merged.pop(k, None)
            if merged:
                chans[key] = merged
            else:
                chans.pop(key, None)
            if chans:
                raw['channels'] = chans
            else:
                raw.pop('channels', None)
            said.update(prev=prev, merged=merged)
        _store_device_raw(device_id, client, mutate)
        prev, merged = said['prev'], said['merged']
        audit_log.append(user=getattr(request.state, "user", None) or ("api-key" if _api_key else "anon"),
                         ip=request.client.host if request.client else "?", action="channel override",
                         target=f"{device_id} {key}", status="ok",
                         detail={'old': prev or None, 'new': merged or None,
                                 'template': (cmds[0].get('template_channels') or {}).get(ids.get(key, key))
                                 if cmds else None})
        dev_cfg = config.get_device(device_id)
        warns = _lease_pace_warnings(dev_cfg, channel=_cid)
        return {'device': device_id, 'channel': key, 'override': merged or None,
                'commands': [_command_entry(dev_cfg, c) for c in _commands_for(dev_cfg)
                             if c.get('channel') == (key if key in declared else ids.get(key))],
                **({'warnings': warns} if warns else {})}

    def _device_reads(dev_cfg) -> set:
        """The names a device reads itself (registers and its own calculated
        fields) — a meter link may not give it one of them a second time."""
        names = {r.name for r in config.unit_registers(dev_cfg)}
        try:
            names |= {str(c.get('name')) for c in config.load_calculated(dev_cfg.id) or [] if isinstance(c, dict)}
        except Exception:  # noqa: BLE001 — unreadable calculated file: the registers still judge
            pass
        return names

    _ABSENT = object()

    def _save_meters(device_id: str, raw_links, dry_run: bool = False, consumption=None,
                     raw_options=_ABSENT) -> Dict:
        """Validate and store a device's meter links (meter_links.py), in the
        device lock: the device re-reads its linked values, what a rule may
        read is judged afresh, and HA is offered the new fields. A save of
        what is stored answers `unchanged`; nothing is written to any meter.
        ``consumption`` (the earlier route): only the consumption link is
        set, from {device, power, energy} or {} — the other links, and the
        sign of a link to the same meter, are read inside the lock."""
        from . import meter_links
        with _device_mutation_lock:
            try:
                _i, dev_cfg, client = _find_device(device_id)
                if dev_cfg is None:
                    raise HTTPException(status_code=404, detail="device not found")
                if consumption is not None:
                    now = meter_links.link_for(dev_cfg, 'consumption') or {}
                    raw_links = [ln for ln in meter_links.links_of(dev_cfg) if ln['role'] != 'consumption']
                    if consumption.get('device'):
                        sign = now.get('sign', 'auto') if now.get('device') == consumption['device'] else 'auto'
                        raw_links.append({'role': 'consumption', **consumption, 'sign': sign})
                links, errs = meter_links.validate(config, dev_cfg, raw_links,
                                                   reserved_names=_device_reads(dev_cfg))
                # the honesty thresholds: absent keeps the stored ones, {} the
                # defaults; they belong to an input / output pair only
                options = None
                has_pair = ({ln['role'] for ln in links} >= {'input', 'output'}) if not errs else True
                if raw_options is not _ABSENT:
                    options, oerrs = meter_links.check_options(raw_options)
                    errs = {**errs, **oerrs}
                    if not oerrs and not has_pair and raw_options:
                        errs['options'] = 'thresholds belong to an input and an output meter — this device has no pair'
                if not errs and not has_pair and getattr(dev_cfg, 'meter_options', None):
                    options = {}              # the pair is gone: its thresholds with it
                if not errs and dev_cfg.protocol == 'none' and not links:
                    errs['meters'] = ('a meter-only device is its meters — '
                                      'delete the device instead of removing them all')
                if errs:
                    raise HTTPException(status_code=422, detail={"errors": [f"{k}: {v}" for k, v in errs.items()],
                                                                 "fields": errs})
                same_opts = options is None or options == dict(getattr(dev_cfg, 'meter_options', None) or {})
                if links == meter_links.links_of(dev_cfg) and same_opts:
                    return {'status': 'unchanged', 'device': device_id, 'meters': links,
                            'options': meter_links.options_of(dev_cfg)}
                if dry_run:
                    return {'status': 'valid', 'would': 'update', 'device': device_id, 'meters': links,
                            'options': {**meter_links.OPTION_DEFAULTS,
                                        **(options if options is not None
                                           else getattr(dev_cfg, 'meter_options', None) or {})}}
                try:
                    config.set_meters(device_id, links, options=options)
                except ValueError as e:
                    raise HTTPException(status_code=422, detail={"errors": [str(e)]})
                new_cfg = config.get_device(device_id)
                # no restart: what a device reads through its meters is a
                # calculated value, recomputed in place
                registry.replace(device_id, new_cfg, client=client, add_if_missing=False)
                calc_engine.load(device_id)
            finally:
                _fields_cache.clear()
        _sync_device_discovery()
        return {'status': 'updated', 'device': device_id, 'meters': meter_links.links_of(new_cfg),
                'energy_meter': dict(new_cfg.energy_meter or {}),
                'options': meter_links.options_of(new_cfg)}

    @app.get("/api/meter-roles")
    def meter_roles():
        """The roles a meter can have on a device, and how its power reads."""
        from . import meter_links
        return meter_links.catalog()

    @app.put("/api/devices/{device_id}/meters")
    @_device_write
    def set_device_meters(device_id: str, payload: Dict = Body(...), dry_run: bool = False):
        """The meters that measure this device, each in a role — the whole
        list: {meters: [{role, device, power, energy, sign}]}, [] for none —
        and, for an input / output pair, when its loss and efficiency are
        published: {options: {min_load_w, tolerance_pct, max_skew_s}}
        (absent: kept; {} or a key left out: its default; null refused).
        `?dry_run=true`: every check, nothing stored. Refusals carry `fields`
        keyed `meters[i].power` / `options.min_load_w`."""
        if not isinstance(payload, dict) or 'meters' not in payload:
            raise HTTPException(status_code=422, detail={"errors": ["send {meters: [...]}"],
                                                         "fields": {"meters": "required"}})
        return _save_meters(device_id, payload.get('meters'), dry_run=dry_run,
                            raw_options=payload['options'] if 'options' in payload else _ABSENT)

    @app.put("/api/devices/{device_id}/energy-meter")
    @_device_write
    def set_device_energy_meter(device_id: str, payload: Dict = Body(...)):
        """The earlier form: name the device that measures this one's
        electricity — {device, power, energy} — or {} to have none. It sets
        the consumption link (meters with input / output: use /meters)."""
        from . import meter_links
        link = {k: str(payload.get(k) or '') for k in ('device', 'power', 'energy') if payload.get(k)}
        try:
            _save_meters(device_id, None, consumption=link)
        except HTTPException as e:
            d = e.detail
            if e.status_code == 422 and isinstance(d, dict):
                # the earlier answer: "power: …", no meters[i] paths
                raise HTTPException(status_code=422, detail={
                    "errors": [f"{k.rsplit('.', 1)[-1]}: {v}" if '.' in k else v
                               for k, v in (d.get('fields') or {}).items()] or d.get('errors', [])})
            raise
        dev = config.get_device(device_id)
        cons = meter_links.link_for(dev, 'consumption') if dev else None
        return {'device': device_id,
                'energy_meter': {k: v for k, v in (cons or {}).items()
                                 if k != 'role' and (k != 'sign' or v != 'auto')}}

    @app.post("/api/devices/{device_id}/commands/{name}")
    @_device_runtime
    def run_device_command(device_id: str, name: str, request: Request, payload: Dict = Body(default={})):
        """Run a command on one unit. Body: its parameters (a bare number is
        `value`). Answers the result with its verdict: 200 success / mismatch /
        unverified, 422 rejected, 502 error, 403 gates, 404 unknown, 409 no
        Modbus source. Audited."""
        who, ip = _cmd_gate(request)
        _i, dev_cfg, client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        params = dict(payload or {})
        lease = params.pop('lease_s', None)
        cmd_id = params.pop('id', None)
        code, res = _run_named_command(dev_cfg, client, name, params, who=who, ip=ip, via='api', lease_s=lease,
                                       cmd_id=cmd_id)
        return JSONResponse(res, status_code=code)

    @app.post("/api/devices/{device_id}/commands/{name}/dry-run")
    @_device_runtime
    def dry_run_device_command(device_id: str, name: str, request: Request, payload: Dict = Body(default={})):
        """The guard's current readings and the exact frames a run would write
        — nothing on the wire beyond the reads."""
        who, ip = _cmd_gate(request, dry_run=True)
        _i, dev_cfg, client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        params = dict(payload or {})
        # the id a run takes is taken (and echoed) by its preview too (CR2-11)
        cmd_id = params.pop('id', None)
        code, res = _run_named_command(dev_cfg, client, name, params, who=who, ip=ip, via='api', dry_run=True,
                                       cmd_id=cmd_id)
        return JSONResponse(res, status_code=code)

    @app.post("/api/endpoints/{endpoint_id}/groups/{group_id}/commands/{name}")
    def run_group_command(endpoint_id: str, group_id: str, name: str, request: Request, payload: Dict = Body(default={})):
        """The same command on every unit of a group, one result each; a unit
        that refuses does not stop the others, and nothing is rolled back."""
        who, ip = _cmd_gate(request)
        if config.get_raw_endpoint(endpoint_id) is None:
            raise HTTPException(status_code=404, detail="endpoint not found")
        results = []
        params = dict(payload or {})
        # the lease is the dead-man's time, as on one device — never a command
        # parameter, and checked (once, by the runner) before anything is written
        lease = params.pop('lease_s', None)
        cmd_id = params.pop('id', None)
        # every unit is submitted first, then all are waited for: units on
        # different wires run in parallel (ADR-0018 D4)
        queued = []
        for dev in config.endpoint_devices(endpoint_id):
            if (getattr(dev, 'group_id', '') or 'units') != group_id:
                continue
            _i, _c, client = registry.find(dev.id)
            queued.append((dev, _submit_named_command(dev, client, name, dict(params), who=who, ip=ip,
                                                      via='api', lease_s=lease, cmd_id=cmd_id)))
        for dev, got in queued:
            code, res = got if isinstance(got, tuple) else _await_command(got)
            results.append({'device': dev.id, 'unit_id': dev.connection.unit_id, 'http_status': code, **res})
        if not results:
            raise HTTPException(status_code=404, detail="group has no units")
        return {'endpoint': endpoint_id, 'group': group_id, 'command': name,
                'ok': all(r.get('status') == 'success' for r in results), 'units': results}

    @app.get("/api/commands/history")
    def command_history(request: Request, device: str = "", limit: int = 100):
        """Past invocations, from the audit log (newest first). It IS the audit
        log, filtered — admin only while login is on (F-33, 3.83.0)."""
        if auth_state.enabled and getattr(request.state, "role", None) != "admin":
            raise HTTPException(status_code=403, detail={"errors": ["the command history is the audit log — admin only"]})
        rows = []
        try:
            for rec in audit_log.recent(limit=max(limit * 10, 500), q='"command"'):
                if rec.get('action') != 'command':
                    continue
                if device and not str(rec.get('target', '')).startswith(device + ' '):
                    continue
                d = rec.get('detail')
                if isinstance(d, str):
                    try:
                        rec = {**rec, 'detail': json.loads(d)}
                    except ValueError:
                        pass
                rows.append(rec)
                if len(rows) >= limit:
                    break
        except Exception:  # noqa: BLE001
            pass
        return {'history': rows}

    # the 3.72.0 routes stay as aliases of the power_limit command
    @app.post("/api/devices/{device_id}/actions/power_limit")
    @_device_runtime
    def device_power_limit(device_id: str, request: Request, payload: Dict = Body(...)):
        return run_device_command(device_id, 'power_limit', request, payload)

    @app.post("/api/endpoints/{endpoint_id}/groups/{group_id}/actions/power_limit")
    def group_power_limit(endpoint_id: str, group_id: str, request: Request, payload: Dict = Body(...)):
        return run_group_command(endpoint_id, group_id, 'power_limit', request, payload)

    def _mqtt_writes_refused() -> Optional[str]:
        """Why a write arriving over MQTT must be ignored, or None. Both gates
        must be on, AND the broker session must be authenticated: with
        allow_write_entities on, a broker publish IS a hardware write, so an
        anonymous broker would hand that to anyone on the LAN (3.80.0)."""
        if not gw_role.active:
            return gw_role.status_text()
        if not (config.security.allow_writes and config.mqtt.allow_write_entities):
            return "writes over MQTT are off (security.allow_writes + mqtt.allow_write_entities)"
        if not str(config.mqtt.username or "").strip():
            return ("mqtt.allow_write_entities is on but the broker connection is "
                    "anonymous — set mqtt.username/password (broker ACLs are the "
                    "only authentication a publish carries)")
        return None

    def _mqtt_command(device_ids, name: str, payload: str) -> None:
        """A controller's command over MQTT. The broker is not a trusted
        caller: the same gates as the API, plus mqtt.allow_write_entities.
        Decoded and handed to the device's lane — the MQTT worker never waits
        for a bus (CMD-Q-1) — and every outcome, a refusal included, is told
        on cmd/result with the request's `id` (CMD-Q-4)."""
        refused = _mqtt_writes_refused()
        if refused:
            logger.warning("MQTT command %s ignored: %s", name, refused)
        for did in device_ids:
            _i, d, c = _find_device(did)
            if d is None:
                continue
            cm = _find_command(d, name)
            if cm is None:
                continue
            try:
                params = parse_payload(cm['def'], payload) if str(payload).strip() else {}
            except ValueError as e:
                # nothing parsed: the payload as it came, for a client to match (CR2-12)
                _publish_command_result(d, name, {'status': 'rejected', 'reason': str(e), 'device': d.id, 'via': 'mqtt',
                                                  'payload': str(payload)[:200]})
                continue
            cmd_id = params.pop('id', None)
            if isinstance(cmd_id, (int, float)) and not isinstance(cmd_id, bool):
                cmd_id = str(cmd_id)                  # a number is an id too
            who = str(params.pop('source', '') or 'mqtt')[:64]
            # a controller's envelope (the legacy collector's schema: command,
            # device_id, ts) says who and what — the parameters are the rest
            for k in ('command', 'device_id', 'ts'):
                params.pop(k, None)
            why = refused or _cmd_id_error(cmd_id)
            if why:
                _publish_command_result(d, name, {'status': 'rejected', 'reason': why, 'device': d.id, 'via': 'mqtt',
                                                  'by': who, 'params': dict(params),
                                                  **({} if _cmd_id_error(cmd_id) else {'id': cmd_id})})
                continue
            got = _submit_named_command(d, c, name, params, who=who, ip='mqtt', via='mqtt', cmd_id=cmd_id, tell=True)
            if isinstance(got, tuple):
                _publish_command_result(d, name, got[1])

    _device_cmd_topics: set = set()

    def _sync_command_topics() -> None:
        """Subscribe `<unit prefix>/cmd/<name>` for every enabled command with
        an MQTT face — endpoint units and devices of their own alike — and
        `<group prefix>/cmd/<name>` for an endpoint's group. Only the topics
        this function made are dropped on a rebuild: the rules' and the
        sequences' own command topics are not its to remove (they used to be,
        and a sequence's run topic stayed gone until a restart)."""
        if not mqtt_publisher or not hasattr(mqtt_publisher, 'register_command'):
            return
        for t in list(_device_cmd_topics):
            if hasattr(mqtt_publisher, 'unregister_command'):
                mqtt_publisher.unregister_command(t)
            else:
                mqtt_publisher.unregister_commands(t)
        _device_cmd_topics.clear()

        def reg(topic, fn):
            mqtt_publisher.register_command(topic, fn)
            _device_cmd_topics.add(topic)

        def unit_topics(dev):
            names = []
            for c in _commands_for(dev):
                if not c['enabled'] or not c['faces'].get('mqtt', True):
                    continue
                name = c['def'].name
                reg(f"{dev.mqtt_topic_prefix}/cmd/{name}",
                    lambda payload, ids=(dev.id,), n=name: _mqtt_command(ids, n, payload))
                names.append(name)
            return names

        in_endpoints = set()
        for p in config.endpoints:
            pid = p.get('id')
            by_group: Dict[str, Dict[str, list]] = {}
            for dev in config.endpoint_devices(pid):
                in_endpoints.add(dev.id)
                for name in unit_topics(dev):
                    by_group.setdefault(getattr(dev, 'group_id', '') or 'units', {}).setdefault(name, []).append(dev)
            for gid, cmds in by_group.items():
                for name, devs in cmds.items():
                    gp = _group_cmd_prefix(pid, gid, devs)
                    reg(f"{gp}/cmd/{name}",
                        lambda payload, ids=tuple(d.id for d in devs), n=name: _mqtt_command(ids, n, payload))
        for dev in list(config.devices):
            if dev.id not in in_endpoints and not dev.primary and getattr(dev, 'mqtt_topic_prefix', ''):
                unit_topics(dev)

    app.state.commands_for = _commands_for
    app.state.run_command = _run_named_command
    app.state.make_lease_revert = _make_lease_revert
    app.state.cmd_lock_for = _cmd_lock_for
    app.state.lane_key = _lane_key
    app.state.exec_timeout = _exec_timeout
    app.state.commands_changed = _commands_changed
    app.state.lease_manager = _lease_mgr
    app.state.mqtt_command = _mqtt_command
    app.state.mqtt_write_command = _mqtt_write_command
    app.state.sync_command_topics = _sync_command_topics
    _late_hooks['command_topics'] = _sync_command_topics
    try:
        _sync_command_topics()                       # boot: the topics exist from the first second
    except Exception as e:  # noqa: BLE001
        logger.warning("command topics at boot: %s", e)

    # ── rules: the declarative controller (docs/rules-design.md) ────────────
    from . import clock as _clock
    from .rules_runtime import RulesRuntime
    _clock.set_timezone(config.ui.timezone or "Europe/Bucharest")   # hour(), between(), "today"

    def _rule_poll_now(dev_cfg, client, group):
        drv = _modbus_driver_of(client)
        if drv is not None and hasattr(drv, 'poll_now'):
            drv.poll_now(group or None)
            return
        # an MQTT source cannot be swept; one with a keepalive is asked to
        # republish everything (a Venus GX on an empty keepalive)
        mq = _mqtt_driver_of(client)
        if mq is not None and getattr(mq, 'keepalive', None):
            mq.send_keepalive(first=True)

    # what an id reads, for a rule's `inputs` (input_check): the registers its
    # running drivers poll (memory only — a GET never touches the disk), the
    # calculated fields, whatever the live store holds, a linked meter's
    # fields; an installation: its totals. Never under the rules lock; cached,
    # and cleared whenever a device or its register selection changes.
    _fields_cache: Dict[str, Tuple[float, Any]] = {}
    _fields_warned: set = set()


    def _rule_fields_of(dev_id: str):
        hit = _fields_cache.get(dev_id)
        if hit is not None and time.monotonic() - hit[0] < 30.0:
            return hit[1]
        dcfg = config.get_device(dev_id)
        if dcfg is None:
            if not any(str(p.get('id')) == dev_id for p in (config.endpoints or [])):
                names = False
            else:
                from .endpoint_aggregator import latest_totals
                got = latest_totals(dev_id)
                # empty totals (every unit asleep at night) cannot judge a
                # name: "reads nothing" would light the rule amber nightly
                names = set(got[0]) if got and got[0] else None
        else:
            try:
                _i, _c, client = _find_device(dev_id)
                names = _driver_reg_names(client) if client is not None else None
            except Exception:  # noqa: BLE001
                names = None
            try:
                held = {it.get('name') for it in list((registry.store_for(dev_id) or {}).values()) if it.get('name')}
                if names is None and held:  # no driver lists its registers: what it has read
                    names = set()
                if names is not None:      # nothing running and nothing read: cannot judge
                    names |= held
                    names |= {e['_reg'].name for e in calc_engine.store.get(dev_id, []) if e.get('_reg') is not None}
                    from .linked_meter import names_for
                    from .meter_links import links_of
                    names |= set(names_for(links_of(dcfg)))
            except Exception:  # noqa: BLE001 — cannot judge, so no problem
                if dev_id not in _fields_warned:
                    _fields_warned.add(dev_id)
                    logger.warning("rule inputs: cannot list what %s reads; its references are not judged",
                                   dev_id, exc_info=True)
                names = None
            # an MQTT input learns its names from what arrives: an empty list
            # is "not yet", not "nothing"
            if names is not None and not names and str(getattr(dcfg, 'protocol', '')).lower() == 'mqtt':
                names = None
        _fields_cache[dev_id] = (time.monotonic(), names)
        return names

    _rule_fields_of.cache_clear = _fields_cache.clear

    def _rules_reload():
        _fields_cache.clear()
        rules_runtime.load()

    rules_runtime = RulesRuntime(
        config_dir=config.config_path.parent, resolver_factory=calc_engine.resolver,
        find_device=_find_device, endpoint_devices=config.endpoint_devices,
        commands_for=_commands_for, run_command=_run_named_command, poll_now=_rule_poll_now,
        event_log=event_log, alert_mgr=alert_mgr, mqtt=mqtt_publisher, audit_log=audit_log,
        influx=influxdb_publisher,
        # a gateway that just became active acts on fresh readings, not only on
        # its partner's copy: rules wait a moment before their first write
        gates=lambda: (bool(config.security.allow_writes) and gw_role.active
                       and time.monotonic() >= getattr(app.state, 'role_warm_until', 0.0)),
        mqtt_refused=_mqtt_writes_refused, fields_of=_rule_fields_of)
    try:
        rules_runtime.load()
    except Exception as e:  # noqa: BLE001
        logger.error("rules.yaml: %s", e)
    app.state.rules_runtime = rules_runtime
    _late_hooks['rules_reload'] = _rules_reload         # devices changed: rebind targets

    # ── sequences: commands in order, with waits and checks (rules phase 3 §4)
    from .sequences import SequenceRunner, SequenceStore

    def _seq_run_command(device_id, name, params, *, who, via, dry_run=False):
        _i, dcfg, cl = _find_device(device_id)
        if dcfg is None:
            return 404, {'status': 'rejected', 'reason': f"no device '{device_id}'"}
        return _run_named_command(dcfg, cl, name, dict(params), who=who, ip='-', via=via, dry_run=dry_run)

    app.state.sequence_store = SequenceStore(config.config_path.parent / 'sequences.yaml')
    app.state.sequences = SequenceRunner(run_command=_seq_run_command, resolver_factory=calc_engine.resolver,
                                         audit=audit_log, mqtt=mqtt_publisher)
    app.state.mqtt_writes_refused = _mqtt_writes_refused

    # ── OPC UA server: the northbound face for SCADA / HMI (docs/opcua.md) ──
    class _OpcUaControl:
        """Starts, stops and restarts the server as the configuration says;
        imports asyncua only when it is enabled."""

        def __init__(self):
            self.server = None
            self.lock = threading.Lock()
            self._declared: Dict[str, Tuple[float, list]] = {}

        @staticmethod
        def _ua_type(r) -> str:
            """The OPC UA type a declared value will have: text for strings and
            enum/bit maps, a boolean for coils, a number otherwise. A value that
            turns out otherwise corrects its node in place (server._put)."""
            dt = str(getattr(r, 'data_type', '') or '').lower()
            if getattr(r, 'enum', None) or getattr(r, 'bits', None) or dt.startswith('string'):
                return 'String'
            if str(getattr(r, 'register_type', '') or '').lower() in ('coil', 'coils', 'discrete', 'discrete_input'):
                return 'Boolean'
            return 'Double'

        def _declared_for(self, dcfg) -> list:
            """[(name, label, unit, type)] the device's template and choices say
            it will read: its registers and calculated values. Known before the
            first reading, so its nodes exist from the start. Cached 30 s (the
            server asks every pass; the registers are files)."""
            now = time.monotonic()
            hit = self._declared.get(dcfg.id)
            if hit and now - hit[0] < 30.0:
                return hit[1]
            out = []
            try:
                regs = list(config.unit_registers(dcfg))
                regs += [e['_reg'] for e in calc_engine.store.get(dcfg.id, []) if e.get('_reg') is not None]
                seen = set()
                for r in regs:
                    if r.name and r.name not in seen:
                        seen.add(r.name)
                        out.append((r.name, r.label or '', r.unit or '', self._ua_type(r)))
            except Exception as e:  # noqa: BLE001 — a device without registers just has none declared
                logger.debug("opcua: declared fields of %s: %s", dcfg.id, e)
            self._declared[dcfg.id] = (now, out)
            return out

        def _devices(self):
            """What the server serves: the devices whose OPC UA output is on, from
            the configuration (a unit found seconds after the start is already
            here), each with its values as read, what it declares and which of
            its values are chosen."""
            for dcfg in list(config.devices):
                if not (dcfg.enabled and dcfg.opcua_enabled):
                    continue
                _i, _c, client = registry.find(dcfg.id)
                try:
                    health = (client.data_health() or {}).get('status', '') if client is not None else 'down'
                except Exception:  # noqa: BLE001
                    health = ''
                live = client is not None and client_is_live(client)
                cmds = {}
                try:
                    for c in _commands_for(dcfg):
                        if c['enabled'] and not c['def'].alias and c['faces'].get('opcua', True):
                            cmds[c['def'].name] = {'label': c['def'].label, 'params': {
                                n: {k: getattr(p, k) for k in ('label', 'unit', 'min', 'max', 'default', 'required')}
                                for n, p in c['def'].params.items()}}
                except Exception:  # noqa: BLE001
                    cmds = {}
                try:
                    attrs = attributes.flat('device', dcfg.id)
                except Exception:  # noqa: BLE001 — attributes never stop the values
                    attrs = {}
                yield (dcfg.id, dcfg.name or dcfg.id, live, health, registry.store_for(dcfg.id) or {}, cmds,
                       self._declared_for(dcfg), set(dcfg.opcua_fields) or None, attrs)

        def _run(self, device_id, name, params, user):
            _i, dcfg, cl = _find_device(device_id)
            if dcfg is None:
                return 404, {'status': 'rejected', 'reason': f"no device '{device_id}'"}
            return _run_named_command(dcfg, cl, name, dict(params), who=f"opcua:{user}", ip='-', via='opcua')

        def _authenticate(self, user, password):
            """A gateway account, or a service token: user = the token's name,
            password = the token (a SCADA or HMI is a program — Settings →
            Security → Service tokens, its role, revocable alone)."""
            if str(password or '').startswith('mbgst_'):
                svc = service_tokens.check(str(password))
                return svc[1] if svc and svc[0] == str(user or '').strip().lower() else None
            return auth_state.authenticate(user, password) if auth_state.enabled else None

        def apply(self):
            with self.lock:
                if self.server is not None:
                    self.server.stop()
                    self.server = None
                if not config.opcua.enabled or not gw_role.active:   # a standby serves nothing
                    return None
                from .opcua import OpcUaServer
                self.server = OpcUaServer(config.opcua, devices=self._devices, authenticate=self._authenticate,
                                          config_dir=config.config_path.parent, version=__version__,
                                          run_command=self._run)
                if not self.server.start():
                    if self.server.error:
                        logger.error("OPC UA server did not start: %s", self.server.error)
                    else:   # a slow host: it keeps starting, status() says so
                        logger.warning("OPC UA server is still starting; it reports running when ready")
                return self.server.status()

        def shutdown(self):
            with self.lock:
                if self.server is not None:
                    self.server.stop()
                    self.server = None

        def status(self):
            s = self.server.status() if self.server is not None else {'running': False}
            return {**s, 'enabled': bool(config.opcua.enabled), 'port': config.opcua.port,
                    'host': config.opcua.host, 'encryption': config.opcua.encryption,
                    'allow_anonymous': bool(config.opcua.allow_anonymous), 'update_s': config.opcua.update_s,
                    'public_url': config.opcua.public_url,
                    'auth_enabled': bool(auth_state.enabled)}

    app.state.opcua = _OpcUaControl()

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

    # what a device's own routes manage (Outputs tab, write lock, Commands
    # tab, linked meter, components): an edit never changes them
    _DEVICE_OWN_KEYS = ('write_locked', 'http_output', 'rest_push', 'pq_recorder',
                        'energy_meter', 'meters', 'meter_options', 'components', 'commands', 'channels',
                        'opcua', 'kind', 'energy_counter_resets')

    def _device_put_raw(device_id: str, dev_cfg, payload: Dict, prev: Dict) -> Dict:
        """What saving ``payload`` over the stored device writes: the stored
        connection merged under the sent one (save_merge.py), validated, with
        everything the payload did not speak of kept and the routing identity
        pinned. Raises HTTPException / BridgeConflict."""
        payload = dict(payload)
        for k in ('name', 'template', 'enabled'):
            if k not in payload and k in prev:
                payload[k] = prev[k]
        conn = payload.get('connection')
        if isinstance(conn, dict):
            conn = dict(conn)
            if conn.get('password') in ('', MASKED):
                conn.pop('password')               # echoed masked / left blank = keep
            old_h = (prev.get('connection') or {}).get('headers') or {}
            if isinstance(conn.get('headers'), dict):
                # a masked echo is the stored value; masked with nothing stored is nothing
                conn['headers'] = {k: (old_h[k] if v == MASKED else v)
                                   for k, v in conn['headers'].items() if v != MASKED or k in old_h}
        payload['connection'] = merge_connection(conn, prev.get('connection'), config.bridges)
        raw = _apply_routing_defaults(_validate_device_payload(payload, existing_id=device_id,
                                                               stored_conn=prev.get('connection')))
        raw = keep_unsaid(raw, prev, payload, own=_DEVICE_OWN_KEYS)
        _device_claim_check(raw, device_id, prev)
        # Routing identity (topic prefix / bucket / Influx tag) is FIXED after
        # creation — changing it would re-route future data and orphan the
        # device's history + Home Assistant entities (the enable flags and
        # HA discovery stay editable); an API caller cannot bypass it either.
        raw.setdefault('mqtt', {})['topic_prefix'] = dev_cfg.mqtt_topic_prefix
        # …and so is its Home Assistant node (stored, or none: the legacy one)
        _node = ((prev.get('mqtt') or {}).get('ha_node'))
        if _node:
            raw['mqtt']['ha_node'] = _node
        else:
            raw['mqtt'].pop('ha_node', None)
        raw.setdefault('influxdb', {})['bucket'] = dev_cfg.influxdb_bucket
        raw['influxdb']['device_tag'] = dev_cfg.influxdb_device_tag
        return raw

    def _built_sig(cfgs, also_skip: Tuple[str, ...] = ()) -> Optional[str]:
        """What a set of devices RUNS on — every built field but the name and
        the kind (labels, refreshed in place) and ``also_skip`` — or None when
        it cannot be told."""
        def _runs_on(d: Dict) -> Dict:
            # a block another protocol reads means nothing here (a TCP device's
            # `http` echoes its timeout — not a reason to restart it)
            proto = str(d.get('protocol', ''))
            for k, keep in (('http', proto == 'http'), ('mqtt_in', proto == 'mqtt')):
                if not keep:
                    d.pop(k, None)
            if proto not in SERIAL_PROTOCOLS + ('rtu_tap',) + VIRTUAL_MAP_SERIAL:
                # a line another protocol never opens (an MQTT device's echoed
                # `serial_port: ''`) is not what it runs on
                d.pop('serial', None)
            if d.get('serial'):
                # the raw line as written; a default spelled out is the same line
                d['serial'] = {'baudrate': 9600, 'parity': 'N', 'stopbits': 1, 'bytesize': 8, **d['serial']}
            if isinstance(d.get('mqtt_in'), dict):
                # what the MQTT input reads absent the same as empty / false /
                # the default port (mqtt_input.py) — not a reason to reconnect
                mi = dict(d['mqtt_in'])
                for k in ('username', 'password', 'tls', 'tls_ca', 'tls_insecure', 'accept_retained',
                          'stale_after_s', 'vars'):
                    if k in mi and not mi[k]:
                        mi.pop(k)
                if str(mi.get('port', '')) == '1883':
                    mi.pop('port')
                d['mqtt_in'] = mi
            for src in d.get('sources') or []:
                if isinstance(src, dict):
                    _runs_on(src)
            return d
        try:
            # (a kind and a meter link change what is shown and computed, not
            # what is read)
            skip = ('name', 'kind', 'energy_meter', 'meters', 'meter_options') + tuple(also_skip)
            return json.dumps([_runs_on({k: v for k, v in dataclasses.asdict(c).items() if k not in skip})
                               for c in cfgs], sort_keys=True, default=str)
        except Exception:  # noqa: BLE001
            return None

    def _drop_echoed_defaults(raw: Dict, base: Dict) -> Dict:
        """A key the stored device does not have, sent with the value the
        device already runs on (a form spells out timeout 3, username '',
        tls false, outputs enabled…), is not written: the first save of an
        untouched device is then `unchanged`, not a rewrite — or, for an MQTT
        input, a reconnect. `base` is the stored device through
        the same save pipeline. Judged on the built device, so a key that
        changes anything stays; the whole built device must match, so the
        shown values are kept by everything that reads them."""
        def _extra(r):
            out = [(None, k) for k in r if k not in base and not isinstance(r[k], dict)]
            for blk in ('connection', 'mqtt', 'influxdb'):
                if isinstance(r.get(blk), dict):
                    old = base.get(blk) if isinstance(base.get(blk), dict) else {}
                    out += [(blk, k) for k in r[blk] if k not in old]
            return out
        extra = _extra(raw)
        if not extra:
            return raw
        try:
            sig = _built_sig(config.preview_devices(device=raw))
            if sig is None:
                return raw
            for blk, k in extra:
                trial = copy.deepcopy(raw)
                (trial[blk] if blk else trial).pop(k, None)
                if blk and not trial[blk] and blk not in base:
                    trial.pop(blk)
                if _built_sig(config.preview_devices(device=trial)) == sig:
                    raw = trial
        except Exception as e:  # noqa: BLE001 — never fails a save
            logger.debug("echoed-default check skipped (%s)", e)
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

    # a path names a field: lowercase keys; a later segment may be a name
    # of the user's (an HTTP header: X-Auth)
    _FIELD_PATH_RE = re.compile(r'^[a-z_]+(\.[a-z_0-9]+)*(\.[A-Za-z0-9_-]+)?$')

    def _errors_detail(errors: List[str]) -> Dict:
        """A 422's detail: `errors` as always, plus `fields` — each error that
        names a field (`connection.host: required`), by that field, so a form
        can say it next to the input. Additive: `errors` stays the whole truth."""
        fields: Dict[str, str] = {}
        for e in errors:
            path, sep, msg = str(e).partition(': ')
            if sep and _FIELD_PATH_RE.match(path):
                fields.setdefault(path, msg)
        return {"errors": list(errors), **({"fields": fields} if fields else {})}

    def _field_errors(fn):
        """Every 422 a device save answers carries `fields` (_errors_detail)."""
        import functools

        @functools.wraps(fn)
        def _w(*a, **kw):
            try:
                return fn(*a, **kw)
            except HTTPException as e:
                d = e.detail
                if e.status_code == 422 and isinstance(d, dict) and isinstance(d.get('errors'), list) \
                        and 'fields' not in d:
                    raise HTTPException(status_code=422, detail={**d, **_errors_detail(d['errors'])})
                raise
        return _w

    def _serialized_mutation(fn):
        import functools

        @functools.wraps(fn)
        def _w(*a, **kw):
            with _device_mutation_lock:
                try:
                    return fn(*a, **kw)
                finally:
                    # what a rule may read changed (or may have): judged afresh
                    _fields_cache.clear()
        return _w

    @app.post("/api/devices")
    @_serialized_mutation
    @_field_errors
    def create_device(payload: Dict = Body(...), dry_run: bool = False, only: Optional[str] = None,
                      request: Request = None):
        """Create a device: validate → persist → auto-select its template
        registers + ensure its InfluxDB bucket → hot-start its poller and
        publish HA discovery (no restart). `?dry_run=true`: everything a save
        checks, nothing stored or started — {status: valid, would: create,
        entry: the would-be entry (ADR-0031 §5)}. `?dry_run=true&only=connection`:
        the connection alone (fields, matrix, bus claims, Seplos mode),
        without an id, a name or a template (L3-F3). ``components``
        ([{type, instance, name?}]) are created with the device, all or
        nothing.
        (The id is checked against every device, unit and installation; a
        component the new device declares is checked for a clash only by the
        real save.) A ``type`` (or ``kind``, its older word) sent is filed as
        the device's type (as ``/organise`` would); later edits keep it and
        only ``/organise`` changes it. ``attributes`` ({category: value}) are
        stored with the device; refused, nothing is created (ADR-0027)."""
        from . import device_types as _dtypes
        if only is not None:
            if only != 'connection' or not dry_run:
                _msg = "only=connection goes with dry_run=true"
                raise HTTPException(status_code=422, detail={"errors": [f"only: {_msg}"], "fields": {"only": _msg}})
            return _dry_run_connection(payload)
        # how Add's Check ended (L3.9-F3): the create's word only, for its
        # audit line — never stored on the device
        _check, _check_job = None, None
        if isinstance(payload, dict) and ('check' in payload or 'check_job' in payload):
            payload = dict(payload)
            _check, _check_job = payload.pop('check', None), payload.pop('check_job', None)
            if _check is not None and _check not in ('ok', 'failed', 'skipped'):
                _m = "check: ok, failed or skipped (how Add's Check ended)"
                raise HTTPException(status_code=422, detail={"errors": [_m], "fields": {"check": _m.split(': ', 1)[1]}})
            if _check_job is not None and not (isinstance(_check_job, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', _check_job)):
                _m = "check_job: the preview job's id (letters, digits, - and _)"
                raise HTTPException(status_code=422, detail={"errors": [_m], "fields": {"check_job": _m.split(': ', 1)[1]}})
        payload = _drop_spec_keys(payload)
        raw = _apply_routing_defaults(_validate_device_payload(payload))
        _set_new_node(raw['mqtt'], raw['id'])
        _refuse_prefix('mqtt.topic_prefix', raw['mqtt']['topic_prefix'].replace('${device_id}', raw['id'])
                       .replace('${id}', raw['id']), {f"device:{raw['id']}"})
        _k, _why = _dtypes.from_payload(payload)
        if _k is not None or _why:
            _field = 'type' if isinstance(payload, dict) and 'type' in payload else 'kind'
            _why = _why or _dtypes.override_error(_k)
            if _why:
                raise HTTPException(status_code=422, detail={"errors": [f"{_field}: {_why}"],
                                                             "fields": {_field: _why}})
            raw['kind'] = _dtypes.stored_form(_k)
        _attrs = None
        if isinstance(payload, dict) and payload.get('attributes') is not None:
            _tpl = template_registry.get(raw.get('template') or '') if raw.get('template') else None
            _attrs, _aerr = attributes.check_many('device', payload['attributes'],
                                                  mppt_count=(_mppt_count_of(_tpl) or None) if _tpl else None)
            if _aerr:
                raise HTTPException(status_code=422, detail={"errors": _aerr, "fields": {"attributes": '; '.join(_aerr)}})
            if _attrs and attributes.store.broken:
                raise HTTPException(status_code=503, detail={"errors": [
                    "attributes.yaml cannot be read — fix or restore it, or create the device without attributes"]})
        _comps = _components_on_create(raw, payload)
        if _comps:
            raw['components'] = _comps
        _device_claim_check(raw, raw['id'])
        _why = config.raw_device_refusal(raw['id'])     # what the store refuses, dry run or not
        if _why:
            raise HTTPException(status_code=422, detail={"errors": [_why]})
        if dry_run:
            if not config.preview_devices(device=raw):
                raise HTTPException(status_code=422, detail={"errors": [
                    f"device {raw['id']!r} could not be built from the given data"]})
            return {"status": "valid", "would": "create", "entry": _would_be_entry(raw)}
        # a new device is not served on OPC UA until someone says so (its
        # Outputs tab): a SCADA sees what it was given, not every new device
        raw.setdefault('opcua', {'enabled': False})
        # …and writes nothing until an admin unlocks it (/write-lock),
        # whatever the payload says (ADR-0025, ADD-12)
        raw['write_locked'] = True
        try:
            dev_cfg = config.upsert_raw_device(raw)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        if _comps:
            # all or nothing: a component the store could not build takes
            # its parent back with it
            _lost = [c['id'] for c in _comps if getattr(config.get_device(c['id']), 'component_of', None) != dev_cfg.id]
            if _lost:
                try:
                    config.discard_raw_device(dev_cfg.id)
                except Exception:  # noqa: BLE001
                    logger.exception("device %s: rollback failed", dev_cfg.id)
                raise HTTPException(status_code=422, detail={"errors": [
                    f"components: {', '.join(_lost)} could not be built (see the log) — nothing was created"]})
        _autoselect_template_registers(dev_cfg)   # before start → poller loads them
        # a NEW device at an old id: nothing an earlier device kept survives
        # (as for its registers); a restore keeps them
        try:
            attributes.drop('device', dev_cfg.id, reason='a new device at this id')
        except Exception:  # noqa: BLE001 — the device is saved; its attributes must not fail it
            logger.exception("attributes: could not clear what %s held", dev_cfg.id)
        if _attrs:
            try:
                attributes.put_many('device', dev_cfg.id, _attrs, who=_req_user(request), ip=_req_ip(request))
            except Exception as e:  # noqa: BLE001 — checked above: only a file that broke meanwhile
                # all or nothing: the device goes again
                logger.exception("attributes of %s could not be stored: the device is not created", dev_cfg.id)
                try:
                    config.discard_raw_device(dev_cfg.id)      # no tombstone: an older one stays as it was
                except Exception:  # noqa: BLE001
                    logger.exception("device %s: rollback failed", dev_cfg.id)
                raise HTTPException(status_code=503, detail={"errors": [
                    f"its attributes could not be stored ({e}) — nothing was created"]})
        _ensure_device_bucket(dev_cfg)
        client = _start_device_client(dev_cfg)
        registry.add(dev_cfg, client)
        _made = [_start_new_component(config.get_device(c['id'])) for c in _comps]
        _sync_device_discovery()
        logger.info(f"device {dev_cfg.id}: created "
                    f"({dev_cfg.protocol}, template={dev_cfg.template or '—'})"
                    + (f" with {len(_made)} component(s)" if _made else ""))
        try:
            audit_log.append(user=_req_user(request), ip=_req_ip(request), action="device created",
                             target=dev_cfg.id, status="ok", detail={
                                 "check": _check or "not said", "check_job": _check_job,
                                 "template": dev_cfg.template or "", "protocol": dev_cfg.protocol,
                                 "components": len(_made)})
        except Exception:  # noqa: BLE001 — the device is made; its trail line must not fail it
            logger.exception("audit: device %s created, line not written", dev_cfg.id)
        out = {"status": "created", "device": _device_entry(dev_cfg, client)}
        if _made:
            out["components"] = [_component_view(c) for c in _made]
        return out

    def _dry_run_connection(payload: Dict, existing_id: Optional[str] = None) -> Dict:
        """``POST /api/devices?dry_run=true&only=connection`` (L3-F3): the
        connection judged as a save judges it — its fields, the matrix and
        the edit locks, the bus claims (ADR-0025), the Seplos mode rule —
        without an id, a name or a template (one sent is used: a map written
        for listening decides the role). Nothing is stored, no probe device
        is ever made. 422 names only the connection's fields.
        ``existing_id`` (the device page's preview of a saved device): judged
        as a save of THAT device — its own address is not a clash with
        itself, its edit locks apply (L3.9)."""
        import secrets as _secrets
        conn = payload.get('connection') if isinstance(payload, dict) else None
        if not isinstance(conn, dict) or not conn:
            raise HTTPException(status_code=422, detail={"errors": ["connection: required"],
                                                         "fields": {"connection": "required"}})
        tid = str(payload.get('template') or '').strip()
        if existing_id and registry.has(existing_id):
            probe = existing_id
        else:
            existing_id = None
            probe = 'probe-' + _secrets.token_hex(6)
            while registry.has(probe):
                probe = 'probe-' + _secrets.token_hex(6)
        body = _drop_spec_keys({'id': probe, 'name': probe, 'template': tid, 'enabled': False,
                                'connection': dict(conn)})

        def _own(e) -> bool:
            path, sep, _m = str(e).partition(': ')
            if not sep or not _FIELD_PATH_RE.match(path):
                return existing_id is not None or probe not in str(e)   # a bus claim or a store refusal
            return path.split('.', 1)[0] in ('connection', 'sources')

        errs: List[str] = []
        fields: Dict[str, str] = {}
        try:
            raw = _validate_device_payload(body, existing_id=existing_id)
        except HTTPException as e:
            d = e.detail if isinstance(e.detail, dict) else {}
            if e.status_code != 422 or not isinstance(d.get('errors'), list):
                raise
            errs = [x for x in d['errors'] if _own(x)]
            fields = {f: m for f, m in (d.get('fields') or {}).items()
                      if f.split('.', 1)[0] in ('connection', 'sources')}
            raw = None
        # the bus claim is said whatever else the fields said (m6): judged on
        # the connection as sent when the fields did not pass
        claim_raw = raw if raw is not None else {'id': probe, 'name': probe, 'template': tid,
                                                 'enabled': False, 'connection': dict(conn)}
        try:
            _device_claim_check(claim_raw, probe)
        except HTTPException as ce:
            cd = ce.detail if isinstance(ce.detail, dict) else {}
            errs += [x for x in (cd.get('errors') or []) if x not in errs]
        except Exception:  # noqa: BLE001 — a shape that cannot be built: the bus is unknown
            logger.debug("only=connection: claim check skipped", exc_info=True)
        try:
            bus = _bus_of_raw(claim_raw, probe)
        except Exception:  # noqa: BLE001
            bus = None
        if bus is None and raw is None:
            bus = 'unknown'                 # not told: the connection did not pass
        if errs:
            det = _errors_detail(errs)
            for f, m in fields.items():
                det.setdefault('fields', {}).setdefault(f, m)
            det['bus'] = bus
            raise HTTPException(status_code=422, detail=det)
        return {"status": "valid", "would": "create", "only": "connection", "bus": bus}

    def _bus_of_raw(raw: Dict, did: str) -> Optional[Dict]:
        """The bus a device definition would join: {key, where, role, pollers,
        listeners} (the others already on it), or None (HTTP, MQTT, CAN…)."""
        from .bus_claims import listeners, pollers, source_claims
        built = config.preview_devices(device=raw)
        if not built:
            return None
        claims = source_claims(built[0], template_registry)
        if not claims:
            return None
        _i, key, listens, where = claims[0]
        return {'key': key, 'where': where, 'role': 'listen' if listens else 'master',
                'pollers': pollers(config, key, template_registry, {did}),
                'listeners': listeners(config, key, template_registry, {did}, standby=False)}

    def _would_be_entry(raw: Dict) -> Dict:
        """What a create would store, as the device will be seen: the resolved
        topic prefix, bucket and Home Assistant node, the wire it speaks, its
        write lock (always on), its setup (pending when its template has
        setup writes), the bus it joins with the devices already on it, and
        the components that come with it (ADR-0031 §5)."""
        from .bridges import TYPES as _BT
        from .connection_spec import public, spec_of
        did = raw['id']
        built = config.preview_devices(device=raw)
        dev = built[0] if built else None
        tpl = template_registry.get(raw.get('template') or '') if raw.get('template') else None
        try:
            wire = public(spec_of(raw.get('connection'), config.bridges, _BT))
        except Exception:  # noqa: BLE001 — the entry is a courtesy of the dry run
            wire = None
        return {
            'id': did, 'name': raw.get('name') or did, 'template': raw.get('template') or '',
            'enabled': bool(raw.get('enabled', True)), 'type': raw.get('kind') or '',
            'protocol': dev.protocol if dev else str((raw.get('connection') or {}).get('protocol', '')),
            'wire': wire,
            'topic_prefix': dev.mqtt_topic_prefix if dev else raw['mqtt']['topic_prefix'],
            'bucket': dev.influxdb_bucket if dev else raw['influxdb']['bucket'],
            'device_tag': dev.influxdb_device_tag if dev else did,
            'ha_node': (raw.get('mqtt') or {}).get('ha_node') or f"mbg_dev_{did}",
            'write_locked': True,
            'opcua_enabled': bool((raw.get('opcua') or {}).get('enabled', False)),
            'setup': 'pending' if getattr(tpl, 'setup', None) else 'none',
            'bus': _bus_of_raw(raw, did),
            'components': [{'id': c['id'], 'type': c['type'], 'instance': c['instance'], 'name': c['name']}
                           for c in raw.get('components') or []],
        }

    def _components_on_create(raw: Dict, payload: Dict) -> List[Dict]:
        """The components a create brings ([{type, instance, name?}]), checked
        as one added later is (_new_component) — the entries to store, or 422
        for any of them: all or nothing."""
        comps = payload.get('components') if isinstance(payload, dict) else None
        if comps is None or comps == []:
            return []
        if not isinstance(comps, list) or len(comps) > 64:
            raise HTTPException(status_code=422, detail={"errors": [
                "components: a list of {type, instance, name} (up to 64)"]})
        did = raw['id']
        errors: List[str] = []
        if str((raw.get('connection') or {}).get('protocol', 'tcp')).lower() != 'mqtt':
            errors.append("components: they are read over MQTT only, for now")
        tpl = template_registry.get(raw.get('template') or '') if raw.get('template') else None
        types = dict(getattr(tpl, 'components', None) or {})
        held: Set[tuple] = set()
        taken: Set[str] = {did}
        out: List[Dict] = []
        for i, c in enumerate(comps):
            if not isinstance(c, dict):
                errors.append(f"components[{i}]: an object {{type, instance, name}}")
                continue
            entry, cid, errs = _new_component(did, types, c, held, taken)
            errors += [f"components[{i}].{e}" for e in errs]
            if entry is not None:
                held.add((entry['type'], entry['instance']))
                taken.add(cid)
                out.append(dict(entry, id=cid))
        if errors:
            raise HTTPException(status_code=422, detail=_errors_detail(errors))
        return out

    def _start_new_component(new):
        """A component just stored: its template's registers, a clean slate of
        attributes, its bucket, its client."""
        from .device_seed import autoselect_template_registers
        autoselect_template_registers(config, template_registry, new, topics_must_match=True)
        try:
            attributes.drop('device', new.id, reason='a new component at this id')
        except Exception:  # noqa: BLE001
            logger.exception("attributes: could not clear what %s held", new.id)
        _ensure_device_bucket(new)
        registry.add(new, _start_device_client(new))
        return new

    # ── devices that announce themselves over MQTT (mqtt_discovery.py) ──────
    from .mqtt_discovery import MqttDiscovery
    discovery_state = MqttDiscovery(str(config.config_path.parent / "mqtt_discovery.json"))
    app.state.mqtt_discovery = discovery_state

    @app.get("/api/mqtt-discovery")
    def mqtt_discovery_list():
        """Devices found announcing themselves on the gateway's broker, each
        new / in_mbg / template_missing / ignored. Nothing is made by itself."""
        items = discovery_state.listing([d for d, _c in registry],
                                        lambda tid: template_registry.get(tid) is not None)
        return {"enabled": discovery_state.enabled, "connected": discovery_state.connected,
                "error": discovery_state.error, "items": items,
                "rules": [{"id": r["id"], "label": r.get("label") or r["id"], "topic": r["topic"]}
                          for r in discovery_state.rules()],
                "rule_problems": discovery_state.rule_problems, "nodes_url": config.ui.nodes_url}

    @app.post("/api/mqtt-discovery/{key}/adopt")
    def mqtt_discovery_adopt(key: str, request: Request, payload: Dict = Body(default={})):
        """Make a found device a device of the gateway's, OFF: its template and
        topics from its announcement; an id and a name may be given."""
        item = discovery_state.get(key)
        if item is None:
            raise HTTPException(status_code=404, detail="no such found device (it may have gone: reload)")
        if template_registry.get(item["template"]) is None:
            raise HTTPException(status_code=409, detail=f"template {item['template']} is not here: "
                                                        + item.get("missing_template", "add it first"))
        ids = {d.id for d, _c in registry}
        did = str(payload.get("id") or item["device_id"]).strip().lower()
        base, n = did, 2
        while did in ids:
            did, n = f"{base}-{n}", n + 1
        out = create_device({"id": did, "name": str(payload.get("name") or item["name"])[:80], "enabled": False,
                             "template": item["template"], "connection": dict(item["connection"])})
        audit_log.append(user=getattr(request.state, "user", "") or "-",
                         ip=request.client.host if request.client else "-",
                         action="mqtt device adopted", target=did, status="ok",
                         detail={"found": key, "template": item["template"]})
        return {"status": "adopted", "device_id": did, "device": out.get("device")}

    @app.post("/api/mqtt-discovery/{key}/ignore")
    def mqtt_discovery_ignore(key: str, payload: Dict = Body(default={})):
        """Hide a found device from the list (`ignore: false` shows it again)."""
        ignore = flag(payload, "ignore", True)
        discovery_state.ignore(key, ignore)
        return {"key": key, "ignored": ignore}

    @app.post("/api/mqtt-discovery/enabled")
    def mqtt_discovery_enabled(payload: Dict = Body(...)):
        """Listen for announcements or not (from the next start of the gateway)."""
        discovery_state.set_enabled(flag(payload, "enabled"))
        return {"enabled": discovery_state.enabled}

    def _restore_resets(raw: Optional[Dict]) -> Dict[str, Any]:
        """What a restore of ``raw`` (a device's stored entry) sets back to safe
        and so does NOT bring back as it was (ADR-0025 ADD-12): the write lock
        when it was off, the command bindings that were on. ``{}``: a restore
        brings it back exactly. Said by the delete and by the restore, so
        Undo's toast says it plainly (ADR-0032 §8)."""
        out: Dict[str, Any] = {}
        raw = raw if isinstance(raw, dict) else {}
        if raw.get('write_locked') is not True:
            out['write_locked'] = True
        cmds = raw.get('commands') if isinstance(raw.get('commands'), list) else []
        # a binding with no name is said by its place (review n4)
        on = [str(c.get('name') or f"#{i + 1}") for i, c in enumerate(cmds)
              if isinstance(c, dict) and c.get('enabled')]
        if on:
            out['commands'] = on
        # its components come back write-locked too (review M1, ADR-0025)
        comps = raw.get('components') if isinstance(raw.get('components'), list) else []
        unlocked = [_comp_id(raw, c) for c in comps if isinstance(c, dict) and c.get('write_locked') is False]
        if unlocked:
            out['components'] = unlocked
        return out

    def _comp_id(parent: Dict, c: Dict) -> str:
        """A component entry's id: its own, else the one the config derives."""
        return str(c.get('id') or config.component_id(str(parent.get('id', '')), str(c.get('type', '')),
                                                       str(c.get('instance', ''))))

    def _restore_resumes(raw: Optional[Dict]) -> List[Dict[str, str]]:
        """What a restore of ``raw`` starts again beside reading (review m6):
        its REST push (and its components'), each with the host it pushes to —
        never the URL's path, query or credentials."""
        from urllib.parse import urlparse
        raw = raw if isinstance(raw, dict) else {}
        out: List[Dict[str, str]] = []
        comps = raw.get('components') if isinstance(raw.get('components'), list) else []
        for did, ent in [(raw.get('id'), raw)] + [(_comp_id(raw, c), c) for c in comps if isinstance(c, dict)]:
            rp = ent.get('rest_push') if isinstance(ent, dict) else None
            if isinstance(rp, dict) and rp.get('enabled') and str(rp.get('url') or '').strip():
                host = ''
                try:
                    u = urlparse(str(rp['url']))
                    host = u.hostname or ''
                    if ':' in host:
                        host = f"[{host}]"            # IPv6, as a URL writes it (R2)
                    if u.port:
                        host = f"{host}:{u.port}"
                except ValueError:
                    pass                              # a port that is not one: the host is still said
                out.append({'what': 'rest_push', 'device': str(did or ''), 'host': host})
        return out

    @app.get("/api/devices/restorable")
    def list_restorable_devices(check: bool = False):
        """Deleted devices whose full definition was kept (id no longer active).
        Restoring one rebuilds the exact device — connection, template AND its
        register selection — instead of starting from scratch.
        ``?check=true``: each also says, before the click, what a restore
        would refuse (``problems``, the restore's own words; [] = it can come
        back), what it would set back to safe (``resets``) and what it would
        start again (``resumes``). One kept device that cannot even be read
        says so in its own row; the list stands (review M2). The checks run
        outside the device lock: read-only, but one that runs during a
        mutation may report a passing problem (review n1)."""
        items = config.list_deleted_devices()
        if check:
            for it in items:
                try:
                    _restore_plan(it['id'])
                    it['problems'] = []
                except HTTPException as e:
                    det = e.detail
                    it['problems'] = ([str(x) for x in det.get('errors') or []] if isinstance(det, dict)
                                      else [str(det)])
                except Exception:  # noqa: BLE001 — a malformed kept device: said in its row
                    logger.warning("restorable %s: kept settings unreadable", it['id'], exc_info=True)
                    it['problems'] = ["its kept settings are damaged and cannot be restored — Forget them"]
                try:
                    raw = config.load_deleted_device(it['id'])
                    it['resets'] = _restore_resets(raw)
                    it['resumes'] = _restore_resumes(raw)
                except Exception:  # noqa: BLE001
                    it['resets'], it['resumes'] = {}, []
        return {"devices": items}

    def _restore_plan(device_id: str) -> Dict:
        """The entry a restore of ``device_id`` would store, every check a
        restore makes passed — or the HTTPException it refuses with. Nothing
        is stored; the restore and the list's ``?check=true`` ask the same.
        Kept settings the checks cannot even read (a planted or hand-edited
        file) are a 422, never a 500 (review M2)."""
        try:
            return _restore_plan_checked(device_id)
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 — the detail is the log's, not the operator's (R4)
            logger.warning("restore %s: kept settings unreadable", device_id, exc_info=True)
            raise HTTPException(status_code=422, detail={"errors": [
                "its kept settings are damaged and cannot be restored — Forget them"]})

    def _restore_plan_checked(device_id: str) -> Dict:
        if any(d.id == device_id for d in config.devices):
            raise HTTPException(status_code=409, detail={"errors": [
                f"device '{device_id}' already exists"], "code": "exists"})
        try:
            raw = config.load_deleted_device(device_id)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        if raw is None:
            raise HTTPException(status_code=404, detail="no restorable device with that id")
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail={"errors": ["its kept settings are not a device"]})
        # re-validate the tombstone through the same SSRF/LAN + protocol/port
        # checks create_device enforces (raises 422 on failure) — a planted or
        # edited tombstone must not reactivate a definition that create would
        # reject. Returns the normalized raw dict.
        checked = _validate_device_payload(raw, stored_conn=raw.get('connection'))
        # what validation does not rebuild is the device's too — its enabled
        # commands, linked meter, write lock, outputs, components — and comes
        # back, each checked; nothing else from the file does
        _errs: List[str] = []
        # the tombstone's bindings were stored: a safe the template's bounds
        # moved away from does not block the restore (CR2-5)
        _validate_commands(raw.get('commands'), _errs, tpl=template_registry.get(raw.get('template') or ''),
                           stored=raw.get('commands'))
        _validate_channels(raw.get('channels'), _errs)
        _comps = raw.get('components')
        if _comps is not None and (not isinstance(_comps, list) or any(
                not isinstance(c, dict) or (c.get('id') and not _DEVICE_ID_RE.match(str(c['id'])))
                for c in _comps)):
            _errs.append("components: a list of {type, instance, id?, name?, enabled?}")
        if _errs:
            raise HTTPException(status_code=422, detail={"errors": _errs})
        for _k in ('mqtt', 'influxdb'):
            if isinstance(raw.get(_k), dict) and isinstance(checked.get(_k), dict):
                checked[_k] = {**raw[_k], **checked[_k]}
        # every key a device's own routes manage comes back as it was (the
        # validation above rebuilds only what the device form edits)
        for _k in _DEVICE_OWN_KEYS:
            if _k in raw and _k not in checked:
                checked[_k] = raw[_k]
        raw = checked
        # a restored device comes back safe (ADR-0025, ADD-12): write-locked,
        # every command binding off until someone turns it on again
        raw['write_locked'] = True
        if isinstance(raw.get('commands'), list):
            raw['commands'] = [{**c, 'enabled': False} if isinstance(c, dict) else c for c in raw['commands']]
        # its components too: each is a device a write could reach (review M1)
        if isinstance(raw.get('components'), list):
            raw['components'] = [{**c, 'write_locked': True} if isinstance(c, dict) else c for c in raw['components']]
        _device_claim_check(raw, device_id)
        return raw

    @app.post("/api/devices/{device_id}/restore")
    @_device_write
    @_serialized_mutation
    def restore_device(device_id: str, deleted_ts: Optional[float] = None):
        """Re-create a previously deleted device from its kept tombstone. Its
        selected-registers file (kept on disk) is loaded by the poller, so the
        measurement selection returns intact; its attributes were kept. What
        ran beside it starts again (its REST push; its retained attributes
        meta follows as before). ``reset``: what came back set to safe instead of as it was;
        ``resumed``: what started again. ``deleted_ts`` (an Undo): the delete
        it undoes — a device kept by a later delete is not restored in its
        place (409 ``tombstone_changed``, review m1)."""
        raw = _restore_plan(device_id)
        if deleted_ts is not None:
            kept = config.deleted_device_ts(device_id)
            if kept is None or abs(kept - float(deleted_ts)) > 0.0005:
                raise HTTPException(status_code=409, detail={"errors": [
                    f"'{device_id}' was deleted again since this Undo was offered: its kept settings are another "
                    "device's — see Deleted devices"], "code": "tombstone_changed"})
        kept_raw = config.load_deleted_device(device_id)
        resets, resumes = _restore_resets(kept_raw), _restore_resumes(kept_raw)
        try:
            dev_cfg = config.upsert_raw_device(raw)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        # registers file is already on disk → _autoselect skips (keeps the
        # user's exact selection); only seeds if the file was lost.
        for d in [dev_cfg] + config.component_devices(dev_cfg.id):
            _autoselect_template_registers(d)
            _ensure_device_bucket(d)
            registry.add(d, _start_device_client(d))
            _apply_rest_push(d)                   # the delete stopped it (L3.11c)
        client = registry.find(dev_cfg.id)[2]
        _sync_device_discovery()
        logger.info(f"device {device_id}: restored from tombstone")
        return {"status": "restored", "device": _device_entry(dev_cfg, client), "reset": resets,
                "resumed": resumes}

    @app.delete("/api/devices/restorable/{device_id}")
    @_serialized_mutation
    def forget_restorable_device(device_id: str):
        """Permanently drop a deleted device's kept settings (tombstone +
        registers). Irreversible; refuses to touch an active device."""
        try:
            kept = config.load_deleted_device(device_id) or {}
        except ValueError:
            kept = {}
        try:
            ok = config.forget_deleted_device(device_id)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        if not ok:
            raise HTTPException(status_code=404, detail="nothing to forget for that id")
        # its components kept their measurements beside it
        for c in config.component_entries(kept) if kept.get('components') else []:
            try:
                config.forget_deleted_device(c['id'])
                attributes.drop('device', c['id'], reason='forgotten')
            except Exception:  # noqa: BLE001 — an active id or a bad one: leave it
                pass
        try:
            attributes.drop('device', device_id, reason='forgotten')
        except Exception:  # noqa: BLE001
            logger.exception("attributes: could not drop what %s held", device_id)
        return {"status": "forgotten", "id": device_id}

    def _primary_connection(conn: Dict) -> Dict[str, Any]:
        """The primary's connection checked (PUT and PATCH, one validator —
        ADR-0032 §6): {host, port, unit_id, timeout, retry_attempts,
        retry_delay} for config.update_modbus (a retry key left out: None,
        kept). A key left out keeps what the meter runs on; an empty host is
        still refused. Raises the 422: the class lock first, alone (with its
        field), then every value refused."""
        from .connection_spec import edit_errors, spec_of
        _lock = edit_errors(None, spec_of({'protocol': conn.get('protocol', 'tcp')}, []), {'primary': True})
        if _lock:
            raise HTTPException(status_code=422, detail={"errors": [e for _f, e in _lock],
                                                         "fields": {f: e for f, e in _lock}})
        # Validate like a secondary device — the primary was skipping this, so a
        # bad port/unit_id (e.g. "abc") was persisted and broke the primary client.
        errors = []
        m = config.modbus
        host = str(conn.get('host', m.host) or '').strip()
        if not host:
            errors.append("connection.host: required for Modbus TCP")
        elif _is_serial_path(host):
            errors.append(_serial_host_error("connection.host", host))
        from .conn_advanced import _number

        def _whole(v):
            # a whole number given as a number or its text; never a boolean
            # or a fraction (int(2.9) was 2, int(True) was 1 — review #3)
            n = _number(v)
            return int(n) if n is not None and n.is_integer() else None
        port = _whole(conn.get('port', m.port))
        if port is None or not (1 <= port <= 65535):
            errors.append("connection.port: must be 1..65535")
        unit = _whole(conn.get('unit_id', m.unit_id))
        if unit is None or not (0 <= unit <= 255):
            errors.append("connection.unit_id: must be 0..255")
        timeout = _number(conn.get('timeout', m.timeout))
        if timeout is None:
            errors.append("connection.timeout: must be a number")
        else:
            _te = _timeout_error({'timeout': timeout}, {'timeout': float(m.timeout)})
            if _te:
                errors.append(_te)
        # the retries by the rule every device's advanced keys follow (a value
        # as stored is never judged again — ADR-0006); text that is a number
        # is stored as one (L3.11-F2: it was stored as text and broke the meter)
        from .conn_advanced import errors as _advanced_errors
        retry = {k: conn.get(k) for k in ('retry_attempts', 'retry_delay')}
        errors += _advanced_errors({k: v for k, v in retry.items() if v is not None},
                                   {'retry_attempts': m.retry_attempts, 'retry_delay': m.retry_delay})
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})
        # stored as the numbers they are, whatever form they came in (a 4.0
        # stored as a float broke range() in the meter's read loop — review #2)
        if retry['retry_attempts'] is not None:
            retry['retry_attempts'] = int(_number(retry['retry_attempts']))
        if retry['retry_delay'] is not None:
            retry['retry_delay'] = float(_number(retry['retry_delay']))
        return {'host': host, 'port': port, 'unit_id': unit, 'timeout': timeout, **retry}

    def _primary_connection_same(vals: Dict[str, Any]) -> bool:
        """What it reads with now: a save that says the same is no save — it
        would rewrite config.yaml and reconnect the meter live systems read."""
        m = config.modbus
        return (vals['host'] == str(m.host) and vals['port'] == int(m.port) and vals['unit_id'] == int(m.unit_id)
                and float(vals['timeout']) == float(m.timeout)
                and vals['retry_attempts'] in (None, m.retry_attempts)
                and vals['retry_delay'] in (None, m.retry_delay))

    def _apply_primary_or_say(apply: Dict[str, Any], pg_saved: bool = False) -> None:
        """config.apply_primary, its failures said as what was stored: a save
        that failed stored nothing (memory put back) — 503, with the poll
        intervals as a partial save when they went in first; a rebuild that
        failed after the save — 503 saying it is saved and the gateway must
        be restarted to run on it (review #9), never a bare 500."""
        from .config import PrimaryRebuildError
        try:
            config.apply_primary(**apply)
        except PrimaryRebuildError as e:
            logger.error("primary: saved, but the devices could not be rebuilt (%s)", e)
            saved = (['polling.poll_groups'] if pg_saved else []) + ['entry']
            raise HTTPException(status_code=503, detail={
                "errors": [f"saved to config.yaml, but the running devices could not be rebuilt from it ({e}) — "
                           "restart the gateway to run on it"],
                "partial": {"saved": saved, "failed": ["apply"]}})
        except Exception as e:  # noqa: BLE001 — put back in memory by apply_primary
            logger.warning("primary: config not written (%s)", e)
            det: Dict[str, Any] = {"errors": [f"the configuration could not be written ({type(e).__name__}) — "
                                              + ("only the poll intervals were saved" if pg_saved
                                                 else "nothing was saved")]}
            if pg_saved:
                det["partial"] = {"saved": ["polling.poll_groups"], "failed": ["entry"]}
            raise HTTPException(status_code=503, detail=det)
        # _build_devices() rebuilt EVERY DeviceConfig — re-sync the whole list
        # (clients matched by id), so pollers and config.get_device() never
        # diverge on derived fields after a primary edit.
        registry.resync(config.devices, modbus_client)

    def _write_primary_modbus(conn: Dict[str, Any]) -> None:
        """Settings → Modbus (POST /api/config/modbus), the third writer of
        ``modbus:`` (review #7): the primary's one validator and one write,
        under its entry lock; no reconnect (Settings' Apply does it)."""
        import contextlib
        # the lock order of every checked device write: the entry's, then
        # the device-mutation lock
        entry = (_entry_lock(config.primary_id) if config.primary_id else None) or contextlib.nullcontext()
        with entry, _device_mutation_lock:
            vals = _primary_connection(conn)
            if not _primary_connection_same(vals):
                config.apply_primary(modbus=vals)
                registry.resync(config.devices, modbus_client)

    def _update_primary_device(payload: Dict, dry_run: bool = False):
        """Edit device #1 in place: connection → config.modbus, HA flag →
        global mqtt; name, template and routing identity (topic prefix /
        bucket / device tag) stay FIXED for byte-identical migration.
        Reconnects the primary client live. `dry_run`: checked, never saved."""
        vals = _primary_connection(payload.get('connection', {}) or {})
        same = (_primary_connection_same(vals)
                and ('ha_discovery_enabled' not in payload
                     or flag(payload, 'ha_discovery_enabled') == bool(config.mqtt.ha_discovery_enabled)))
        if same:
            if dry_run:
                return {"status": "valid", "would": "unchanged"}
            prim = config.primary_device
            return {"status": "unchanged", "device": _device_entry(prim, modbus_client)}
        if dry_run:
            # anything else rewrites the config and reconnects the meter
            return {"status": "valid", "would": "update_restart"}
        _apply_primary_or_say(dict(modbus=vals, ha_discovery=(flag(payload, 'ha_discovery_enabled')
                                                              if 'ha_discovery_enabled' in payload else None)))
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
    @_device_write
    @_serialized_mutation
    @_field_errors
    def update_device(device_id: str, payload: Dict = Body(...), dry_run: bool = False):
        """Update a device: stop its poller, persist, restart. The primary
        (UMG512) is editable too — its connection maps to the flat Modbus
        config; its routing identity stays fixed. `?dry_run=true`: everything
        the save checks, nothing stored, nothing restarted — {status: valid,
        would: unchanged | update | update_restart} (whether it would restart
        acquisition)."""
        payload = _drop_spec_keys(payload)
        idx, dev_cfg, client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        # a stale If-Match was answered 412 by @_device_write, under the lock
        return _update_device_locked(device_id, payload, dry_run)

    def _update_device_locked(device_id: str, payload: Dict, dry_run: bool):
        idx, dev_cfg, client = _find_device(device_id)
        if dev_cfg.primary:
            return _update_primary_device(payload, dry_run=dry_run)
        if dev_cfg.component_of:
            raise HTTPException(status_code=422, detail={"errors": [
                f"'{device_id}' is a component of '{dev_cfg.component_of}' — manage it from that device"]})
        _why = config.raw_device_refusal(device_id)     # an installation's unit: dry run or not
        if _why:
            raise HTTPException(status_code=422, detail={"errors": [_why]})
        _prev_raw = copy.deepcopy(config.get_raw_device(device_id) or {})
        try:
            raw = _device_put_raw(device_id, dev_cfg, payload, _prev_raw)
        except BridgeConflict as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        # an unchanged save changes nothing: the stored device through the
        # same pipeline is what this save would write. A device that should
        # run and does not is saved anyway — saving is how one reconnects it.
        try:
            _base = _device_put_raw(device_id, dev_cfg, copy.deepcopy(_prev_raw), _prev_raw)
        except Exception as e:  # noqa: BLE001 — the comparison never fails a save
            logger.debug("device %s: unchanged-check skipped (%s)", device_id, e)
            _base = None
        if _base is not None:
            raw = _drop_echoed_defaults(raw, _base)
        _same = _base is not None and raw == _base
        _stalled = dev_cfg.enabled and client is None
        if _same and not _stalled:
            if dry_run:
                return {"status": "valid", "would": "unchanged"}
            return {"status": "unchanged", "device": _device_entry(dev_cfg, client)}
        # (its components read through its connection: while they exist, the
        # device keeps its template and stays MQTT — an edit lock, held by
        # _validate_device_payload with the others)
        # Persist FIRST (upsert is transactional and rolls back on failure);
        # only tear down the old client once the new config is committed, so a
        # rejected edit leaves the running device untouched instead of
        # disconnected-and-not-restarted.
        # nothing it runs on changed (a rename, an explicit default written
        # down): the config is saved and the running device keeps going —
        # a cosmetic save must never punch a hole in acquisition
        _old_sig = _built_sig(config.preview_devices(device=_prev_raw))
        _quiet = (not _stalled and _old_sig is not None
                  and _old_sig == _built_sig(config.preview_devices(device=raw)))
        if dry_run:
            if not config.preview_devices(device=raw):
                raise HTTPException(status_code=422, detail={"errors": [
                    f"device {device_id!r} could not be built from the given data"]})
            return {"status": "valid", "would": "update" if _quiet else "update_restart"}
        try:
            new_cfg = config.upsert_raw_device(raw)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        if _quiet:
            registry.replace(device_id, new_cfg)
            _sync_device_discovery()
            logger.info(f"device {device_id}: updated (nothing it runs on changed — kept running)")
            return {"status": "updated", "restarted": False,
                    "device": _device_entry(new_cfg, registry.find(device_id)[2])}
        if client:
            client.disconnect()
        _autoselect_template_registers(new_cfg)   # first-time template assignment
        _ensure_device_bucket(new_cfg)            # influx just enabled / bucket changed
        new_client = _start_device_client(new_cfg)
        # re-resolve by id: a concurrent delete may have shifted the index
        registry.replace(device_id, new_cfg, client=new_client, add_if_missing=True)
        _apply_rest_push(new_cfg)                  # restart pusher with new config
        # its components read through its connection: they follow it
        _restart_devices([d.id for d in config.component_devices(device_id)])
        _sync_device_discovery()
        logger.info(f"device {device_id}: updated")
        return {"status": "updated", "device": _device_entry(new_cfg, new_client)}

    # what a device publishes and how: applied live (the poller reads the
    # switches at every reading), never a reason to restart acquisition
    _LIVE_FIELDS = ('ha_discovery_enabled', 'mqtt_enabled', 'influxdb_enabled', 'mqtt_publish_mode',
                    'mqtt_heartbeat_s', 'influxdb_publish_mode', 'influxdb_write_interval_s',
                    'influxdb_heartbeat_s', 'http_output_enabled', 'opcua_enabled', 'opcua_fields',
                    'rest_push')
    _COUNTER_RESETS_KEPT = 20

    def _patch_locks(dev_cfg) -> List[Dict[str, Any]]:
        """The dry run's ``locks``: the device's edit locks exactly as
        ``GET /api/devices/{id}`` says them (device_summary.locks, L3.2a)."""
        from .device_summary import device_view
        try:
            spec, _per = _device_specs(dev_cfg)
        except Exception:  # noqa: BLE001 — device_view derives it then
            spec = None
        return device_view(config, dev_cfg, template_registry, raw=_stored_device_raw(dev_cfg),
                           spec=spec)['locks']

    def _poll_groups_now(dev_cfg) -> Dict[str, Dict]:
        try:
            _regs, groups = config.load_device_registers(dev_cfg)
        except Exception:  # noqa: BLE001
            return {}
        return {n: {"interval": g.interval, "description": g.description} for n, g in (groups or {}).items()}

    def _primary_state() -> Dict[str, Any]:
        """The primary's flat sections as one entry-shaped dict — what its
        PATCH diffs (device_patch.changes places each key in its section),
        each output in the form its file keeps (review #5: a change that
        never reaches the file is no change)."""
        m = config.modbus
        return {'connection': {'host': m.host, 'port': m.port, 'unit_id': m.unit_id, 'timeout': m.timeout,
                               'retry_attempts': m.retry_attempts, 'retry_delay': m.retry_delay},
                'http_output': {'enabled': bool(config.http_output_primary_enabled)},
                'opcua': copy.deepcopy(_primary_kept('opcua')),
                'rest_push': copy.deepcopy(_primary_kept('rest_push'))}

    def _patch_primary(dev_cfg, secs: Dict[str, Dict], dry_run: bool, intent: Optional[str]):
        """PATCH on the primary meter (ADR-0032 §6, L3-F19): the sections
        mapped onto its flat config — connection → ``modbus:`` (the PUT's
        validator, _primary_connection), http_output / opcua / rest_push →
        their primary sections (the sub-routes' validators),
        polling.poll_groups → its register file (the poll-group route's).
        Anything else — the gateway's HA switch included — is refused per
        field in its locks' words. One config write (config.apply_primary),
        one restart decision: the meter reconnects only when its connection
        changes."""
        from .config import primary_opcua_kept, primary_rest_push_kept
        from . import device_patch as _dp
        device_id = dev_cfg.id
        if intent == 'replace':
            raise HTTPException(status_code=422, detail=_errors_detail([
                "intent: replace is not offered for the primary meter — its counters keep no reset marker"]))
        if 'connection' in secs:          # a spec is output only (ADR-0026 §1): dropped, as for any device
            secs = {**secs, 'connection': _drop_spec_keys({'connection': secs['connection']})['connection']}
        errors = _dp.primary_refusals(secs, {
            'name': dev_cfg.name, 'type': _device_type_of(dev_cfg)[0], 'template': dev_cfg.template,
            'ha_discovery': bool(config.mqtt.ha_discovery_enabled),
            # what GET serves: sent back as served, a key it does not use is no change
            'connection': _device_entry(dev_cfg, modbus_client)['connection'] if 'connection' in secs else {}})
        if errors:
            raise HTTPException(status_code=422, detail=_errors_detail(errors))
        before = _primary_state()
        after = copy.deepcopy(before)
        apply: Dict[str, Any] = {}
        restart = False
        if 'connection' in secs:
            vals = _primary_connection(_dp.primary_connection_keys(secs['connection']))
            if not _primary_connection_same(vals):
                # the meter reconnects only when what it reads with changes;
                # said as it is, modbus: is not rewritten (as PUT: unchanged)
                after['connection'].update({k: v for k, v in vals.items() if v is not None})
                apply['modbus'] = vals
                restart = True
        pol, plan = secs.get('polling') or {}, {}
        if 'outputs' in secs:
            plan, oerr = _dp.outputs_plan(_dp.primary_outputs(secs['outputs']),
                                          old_rest_headers=dev_cfg.rest_push.get('headers') or {},
                                          mask=_REST_HDR_MASK, flag=flag, heartbeat_ok=heartbeat_seconds,
                                          opcua_block=_opcua_block)
            errors += oerr
        pg_clean: Dict = {}
        if 'poll_groups' in pol:
            pg_clean, why = _dp.poll_groups_clean(pol['poll_groups'])
            if why:
                errors.append(f"polling.poll_groups: {why}")
        if errors:
            raise HTTPException(status_code=422, detail=_errors_detail(errors))
        pg_changes: List[Dict] = []
        if pg_clean:
            _refuse_busy_poll_groups(dev_cfg, pg_clean, field='polling.poll_groups')
            pg_changes = _dp.poll_group_changes(_poll_groups_now(dev_cfg), pg_clean)
        if 'http_output' in plan:
            after['http_output']['enabled'] = plan['http_output']
        if 'opcua' in plan:
            after['opcua'] = primary_opcua_kept(plan['opcua'])
        if 'rest_push' in plan:
            after['rest_push'] = primary_rest_push_kept(plan['rest_push'])
        # only what changes is written (an output said as it is stays as stored)
        if after['http_output'] != before['http_output']:
            apply['http_output'] = after['http_output']['enabled']
        if after['opcua'] != before['opcua']:
            apply['opcua'] = plan['opcua']
        if after['rest_push'] != before['rest_push']:
            apply['rest_push'] = plan['rest_push']
        entry_changes = _dp.changes(before, after)
        changes = entry_changes + pg_changes
        effects: List[str] = []
        if restart:
            effects.append("acquisition restarts (connection): the meter reconnects")
        elif changes:
            effects.append("applies live — acquisition keeps running")
        if 'rest_push' in apply:
            effects.append("the REST push restarts with its new settings")
        if pg_changes:
            effects.append("the new poll intervals apply at once")
        would = 'update_restart' if restart else ('update' if changes else 'unchanged')
        if dry_run:
            return {"status": "valid", "would": would, "changes": changes, "restart_required": restart,
                    "effects": effects, "locks": _patch_locks(dev_cfg)}
        if not changes:
            return {"status": "unchanged", "restarted": False, "changes": [],
                    "device": _device_entry(dev_cfg, modbus_client)}
        if pg_changes:
            # the intervals first, as for any device: a file that cannot be
            # written stores nothing at all
            try:
                config.save_device_poll_groups(device_id, pg_clean)
            except Exception as e:  # noqa: BLE001 — a corrupt file, a full disk: said, nothing stored
                logger.warning("primary: poll intervals not written (%s) — the edit is not saved", e)
                raise HTTPException(status_code=422 if isinstance(e, ValueError) else 503, detail={
                    "errors": [f"polling.poll_groups: the register file could not be written ({type(e).__name__}: "
                               f"{e}) — nothing was saved"],
                    "fields": {"polling.poll_groups": "could not be written"}})
        if entry_changes:
            try:
                _apply_primary_or_say(apply, pg_saved=bool(pg_changes))     # the one config write
            except HTTPException:
                if pg_changes:            # the intervals went in: they apply
                    _reload_poll_groups(dev_cfg, modbus_client)
                raise
        prim = config.primary_device
        if restart and modbus_client:
            modbus_client.update_config(config.modbus)
            modbus_client.reconnect()
        if pg_changes:
            _reload_poll_groups(prim, modbus_client)
        if 'rest_push' in apply:
            _apply_rest_push(prim)
        logger.info(f"device {device_id}: patched ({', '.join(sorted({c['section'] for c in changes}))})"
                    f"{' — reconnected' if restart else ' — kept running'}")
        return {"status": "updated", "restarted": restart, "changes": changes,
                "device": _device_entry(prim, modbus_client)}

    @app.patch("/api/devices/{device_id}")
    @_device_write
    @_serialized_mutation
    @_field_errors
    def patch_device(device_id: str, request: Request, payload: Dict = Body(...), dry_run: bool = False,
                     intent: Optional[str] = None):
        """Edit a device in sections (ADR-0031 §4–§5): {sections: {connection?,
        polling?: {enabled, poll_groups}, identity?: {name, type}, outputs?:
        {mqtt, influxdb, ha_discovery, http_output, opcua, rest_push},
        model?: {template}}}. ``If-Match`` is required (428 without; a stale
        one 412). Every section is judged by the validator its own route
        uses; then one config write, one audit record, one restart decision
        (acquisition restarts only when what it runs on changes; outputs and
        the name apply live). `?dry_run=true` stores nothing and answers
        {status, would, changes: [{section, field, from, to}],
        restart_required, effects, locks}; it does not need If-Match.
        `?intent=replace` (Replace hardware) also writes an energy-counter
        reset marker (``energy_counter_resets``). The write lock is not a
        section (its own route). The primary meter takes the same body
        through an adapter onto its flat config (_patch_primary, ADR-0032 §6):
        what applies to it is GET's ``edit_sections``, the rest is refused
        per field in the words of its ``locks``."""
        from . import device_patch as _dp
        _i, dev_cfg, client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        if not dry_run and request.headers.get('if-match') is None:
            raise HTTPException(status_code=428, detail={"errors": [
                "If-Match: required — send the ETag of GET /api/devices/{id}; a PATCH never overwrites a "
                "version it did not read"]})
        if intent not in (None, '', 'replace'):
            raise HTTPException(status_code=422, detail={"errors": ["intent: replace, or none"],
                                                         "fields": {"intent": "replace, or none"}})
        try:
            secs = _dp.parse(payload)
        except _dp.PatchError as e:
            raise HTTPException(status_code=422, detail=_errors_detail(e.errors))
        if dev_cfg.primary:
            return _patch_primary(dev_cfg, secs, dry_run, intent)
        if dev_cfg.component_of:
            raise HTTPException(status_code=422, detail={"errors": [
                f"'{device_id}' is a component of '{dev_cfg.component_of}' — manage it from that device"]})
        _why = config.raw_device_refusal(device_id)     # an installation's unit
        if _why:
            raise HTTPException(status_code=422, detail={"errors": [_why]})
        prev = copy.deepcopy(config.get_raw_device(device_id) or {})
        errors: List[str] = []
        ident, model, pol = secs.get('identity', {}), secs.get('model', {}), secs.get('polling', {})
        put: Dict[str, Any] = {}
        if 'connection' in secs:
            put['connection'] = secs['connection']
        if 'name' in ident:
            if not isinstance(ident['name'], str) or len(ident['name'].strip()) > 80:
                errors.append("identity.name: text, up to 80 characters ('' = the id)")
            else:
                put['name'] = ident['name'].strip()
        if 'template' in model:
            if not isinstance(model['template'], str):
                errors.append("model.template: a template id")
            else:
                put['template'] = model['template']
        if 'enabled' in pol:
            if not isinstance(pol['enabled'], bool):
                errors.append("polling.enabled: true or false")
            else:
                put['enabled'] = pol['enabled']
        if errors:
            raise HTTPException(status_code=422, detail=_errors_detail(errors))
        before = prev
        raw = copy.deepcopy(prev)
        if put:
            try:
                raw = _device_put_raw(device_id, dev_cfg, _drop_spec_keys(put), prev)
            except BridgeConflict as e:
                raise HTTPException(status_code=422, detail={"errors": [str(e)]})
            try:
                base = _device_put_raw(device_id, dev_cfg, copy.deepcopy(prev), prev)
            except Exception as e:  # noqa: BLE001 — the comparison never fails a save
                logger.debug("device %s: unchanged-check skipped (%s)", device_id, e)
                base = None
            if base is not None:
                raw = _drop_echoed_defaults(raw, base)
                if raw == base:
                    raw = copy.deepcopy(prev)    # said what it was: the entry is not rewritten
                else:
                    before = base
        # identity.type: the rule of /organise
        if 'type' in ident:
            kind, why = _kind_of_payload(dev_cfg, {'type': ident['type']})
            if why:
                errors.append(f"identity.type: {why}")
            elif kind:
                raw['kind'] = kind
            else:
                raw.pop('kind', None)
        plan: Dict[str, Any] = {}
        if 'outputs' in secs:
            plan, oerr = _dp.outputs_plan(secs['outputs'], old_rest_headers=dev_cfg.rest_push.get('headers') or {},
                                          mask=_REST_HDR_MASK, flag=flag, heartbeat_ok=heartbeat_seconds,
                                          opcua_block=_opcua_block)
            errors += oerr
        pg_clean: Dict = {}
        pg_changes: List[Dict] = []
        if 'poll_groups' in pol:
            pg_clean, why = _dp.poll_groups_clean(pol['poll_groups'])
            if why:
                errors.append(f"polling.poll_groups: {why}")
        if errors:
            raise HTTPException(status_code=422, detail=_errors_detail(errors))
        if pg_clean:
            _refuse_busy_poll_groups(dev_cfg, pg_clean, field='polling.poll_groups')
            pg_changes = _dp.poll_group_changes(_poll_groups_now(dev_cfg), pg_clean)
        _dp.apply_outputs(raw, plan)
        if intent == 'replace':
            import datetime as _dt
            now = time.time()
            marks = [m for m in (raw.get('energy_counter_resets') or []) if isinstance(m, dict)]
            marks.append({'at': _dt.datetime.fromtimestamp(now, _dt.timezone.utc).isoformat(timespec='seconds'),
                          'ts': round(now, 3), 'reason': 'replace'})
            raw['energy_counter_resets'] = marks[-_COUNTER_RESETS_KEPT:]
        changes = _dp.changes(before, raw) + pg_changes
        entry_changed = raw != prev
        built = config.preview_devices(device=raw)
        if not built:
            raise HTTPException(status_code=422, detail={"errors": [
                f"device {device_id!r} could not be built from the given data"]})
        # a device that should run and does not: its connection saved is how
        # one reconnects it, changed or not (as PUT)
        reconnect = bool(dev_cfg.enabled and client is None and 'connection' in secs)
        old_sig = _built_sig(config.preview_devices(device=prev), _LIVE_FIELDS)
        new_sig = _built_sig(built, _LIVE_FIELDS)
        restart = reconnect or (entry_changed and (old_sig is None or new_sig is None or old_sig != new_sig))
        comps = [d.id for d in config.component_devices(device_id)]
        effects: List[str] = []
        if restart:
            why = sorted({c['section'] for c in changes if c['section'] in ('connection', 'model', 'polling')})
            effects.append("acquisition restarts" + (f" ({', '.join(why)})" if why else
                                                     " (it should run and does not: it reconnects)"))
            if comps:
                effects.append(f"its {len(comps)} component(s) restart with it")
        elif changes:
            effects.append("applies live — acquisition keeps running")
        if 'template' in put and any(c['field'] == 'template' for c in changes):
            effects.append("the new template's registers are selected where the device has none selected")
        if plan.get('sinks') and ({'mqtt', 'ha_discovery'} & set(plan['sinks'])):
            effects.append("Home Assistant discovery is published again")
        if 'rest_push' in plan and any(c['field'].startswith('rest_push') for c in changes):
            effects.append("the REST push restarts with its new settings")
        if pg_changes:
            effects.append("the new poll intervals apply at once")
        if intent == 'replace':
            effects.append("an energy-counter reset marker is written: the new meter's counters start a new "
                           "series (the Energy tab does not read it yet)")
        would = 'update_restart' if restart else ('update' if changes else 'unchanged')
        if dry_run:
            return {"status": "valid", "would": would, "changes": changes, "restart_required": restart,
                    "effects": effects, "locks": _patch_locks(dev_cfg)}
        if not changes and not restart:
            return {"status": "unchanged", "restarted": False, "changes": [],
                    "device": _device_entry(dev_cfg, client)}
        if pg_changes:
            # the intervals (already checked) live in the device's register
            # file: written FIRST, so a file that cannot be written stores
            # nothing at all — never an entry saved without its intervals
            try:
                config.save_device_poll_groups(device_id, pg_clean)
            except Exception as e:  # noqa: BLE001 — a corrupt file, a full disk: said, nothing stored
                logger.warning("device %s: poll intervals not written (%s) — the edit is not saved", device_id, e)
                raise HTTPException(status_code=422 if isinstance(e, ValueError) else 503, detail={
                    "errors": [f"polling.poll_groups: the register file could not be written ({type(e).__name__}: "
                               f"{e}) — nothing was saved"],
                    "fields": {"polling.poll_groups": "could not be written"}})
        new_cfg = dev_cfg
        if entry_changed:
            try:
                new_cfg = config.upsert_raw_device(raw)        # the one config write
            except ValueError as e:
                det: Dict[str, Any] = {"errors": [str(e)]}
                if pg_changes:
                    # the intervals went in: they apply, and the answer says so
                    _reload_poll_groups(dev_cfg, client)
                    det["partial"] = {"saved": ["polling.poll_groups"], "failed": ["entry"]}
                raise HTTPException(status_code=422, detail=det)
        if restart:
            if client:
                client.disconnect()
            _autoselect_template_registers(new_cfg)
            _ensure_device_bucket(new_cfg)
            new_client = _start_device_client(new_cfg)
            registry.replace(device_id, new_cfg, client=new_client, add_if_missing=True)
            _apply_rest_push(new_cfg)
            _restart_devices(comps)
        else:
            registry.replace(device_id, new_cfg)            # keep the running client
            new_client = registry.find(device_id)[2]
            if 'rest_push' in plan:
                _apply_rest_push(new_cfg)
            if pg_changes:
                _reload_poll_groups(new_cfg, new_client)
        _sync_device_discovery()
        if 'type' in ident:
            try:
                attributes.invalidate()
                _publish_attribute_meta()
            except Exception:  # noqa: BLE001 — publishing never fails a save
                logger.exception("attributes meta after a type change")
        logger.info(f"device {device_id}: patched ({', '.join(sorted({c['section'] for c in changes}))})"
                    f"{' — restarted' if restart else ' — kept running'}"
                    f"{' [replace: counter reset marker]' if intent == 'replace' else ''}")
        return {"status": "updated", "restarted": restart, "changes": changes,
                "device": _device_entry(new_cfg, new_client)}

    def _refresh_shared(dev_cfg, scope: str) -> None:
        """An installation's unit sets an output for every unit of its
        installation (scope 'endpoint': HTTP / JSON, REST push) or of its
        group ('group': OPC UA, the publishing rhythm): every one of them
        is drawn again in the registry (the unit written is already), so
        each says it at once (review of L3.11b, m3). A REST push is
        (re)started for each."""
        if not getattr(dev_cfg, 'endpoint_id', None):
            return
        for d in list(config.devices):
            if d.id == dev_cfg.id or d.endpoint_id != dev_cfg.endpoint_id:
                continue
            if scope == 'group' and d.group_id != dev_cfg.group_id:
                continue
            try:
                registry.replace(d.id, d)               # keep the running client
                if scope == 'rest':
                    _apply_rest_push(d)
            except Exception:  # noqa: BLE001 — a sibling's refresh never fails the write
                logger.exception(f"device {d.id}: refresh after {dev_cfg.id}'s shared output")

    @app.post("/api/devices/{device_id}/http-output")
    @_device_write
    def set_device_http_output(device_id: str, payload: Dict = Body(...)):
        """Toggle the HTTP/JSON output sink for a device. Read-only feed, so no
        poller restart — just flip the flag, persist, and refresh the runtime
        cfg so /api/devices reflects it. Works for the primary too."""
        dev_cfg = config.get_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        enabled = flag(payload, 'enabled', False)
        try:
            config.set_http_output(device_id, enabled)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        new_cfg = config.get_device(device_id)
        registry.replace(device_id, new_cfg)       # keep the running client
        _refresh_shared(new_cfg, 'endpoint')
        logger.info(f"device {device_id}: http output {'enabled' if enabled else 'disabled'}")
        return {"status": "ok", "device_id": device_id,
                "http_output_enabled": enabled,
                "path": f"/api/meters/{device_id}"}

    @app.post("/api/devices/{device_id}/outputs")
    @_device_write
    def set_device_outputs(device_id: str, payload: Dict = Body(...)):
        """Switch a device's MQTT / InfluxDB output or its Home Assistant
        discovery at once ({mqtt, influxdb, ha_discovery}, each optional). No
        poller restart: the poller reads the switches at every reading."""
        if config.get_device(device_id) is None:
            raise HTTPException(status_code=404, detail="device not found")
        flags = {k: payload[k] for k in ('mqtt', 'influxdb', 'ha_discovery') if k in payload}
        if not flags or not all(isinstance(v, bool) for v in flags.values()):
            raise HTTPException(status_code=422, detail={"errors": [
                "send at least one of mqtt, influxdb, ha_discovery, each true or false"]})
        try:
            config.set_device_sinks(device_id, **flags)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        new_cfg = config.get_device(device_id)
        registry.replace(device_id, new_cfg)       # keep the running client
        if 'ha_discovery' in flags or 'mqtt' in flags:
            _sync_device_discovery()
        logger.info(f"device {device_id}: outputs {flags}")
        return {"status": "ok", "device_id": device_id, "mqtt_enabled": new_cfg.mqtt_enabled,
                "influxdb_enabled": new_cfg.influxdb_enabled,
                "ha_discovery_enabled": new_cfg.ha_discovery_enabled}

    @app.post("/api/devices/{device_id}/publishing")
    @_device_write
    def set_device_publishing(device_id: str, payload: Dict = Body(...)):
        """A device's own publishing rhythm, apart from Settings:
        {mqtt: {publish_mode, heartbeat_interval}, influxdb: {publish_mode,
        write_interval, heartbeat_interval}}; null = back to Settings, a key
        left out stays.
        Applies at the next reading, no poller restart."""
        if config.get_device(device_id) is None:
            raise HTTPException(status_code=404, detail="device not found")
        from .device_patch import publishing_clean
        out, errors = publishing_clean(payload, heartbeat_seconds)
        if not out and not errors:
            errors.append("send mqtt and/or influxdb")
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})
        try:
            config.set_device_publishing(device_id, **out)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        new_cfg = config.get_device(device_id)
        registry.replace(device_id, new_cfg)
        _refresh_shared(new_cfg, 'group')
        logger.info(f"device {device_id}: publishing {out}")
        return {"status": "ok", "device_id": device_id, "publishing": _publishing(new_cfg)}

    def _publishing(dev_cfg) -> Dict:
        """How a device publishes: its own choice where it has one (`own`), the
        gateway's otherwise (`settings`), and what applies (`effective`)."""
        settings = {'mqtt': {'publish_mode': config.mqtt.publish_mode,
                             'heartbeat_interval': config.mqtt.heartbeat_interval},
                    'influxdb': {'publish_mode': config.influxdb.publish_mode,
                                 'write_interval': config.influxdb.write_interval,
                                 'heartbeat_interval': config.influxdb.heartbeat_interval}}
        own = {'mqtt': {'publish_mode': dev_cfg.mqtt_publish_mode or None,
                        'heartbeat_interval': dev_cfg.mqtt_heartbeat_s},
               'influxdb': {'publish_mode': dev_cfg.influxdb_publish_mode or None,
                            'write_interval': dev_cfg.influxdb_write_interval_s,
                            'heartbeat_interval': dev_cfg.influxdb_heartbeat_s}}
        if dev_cfg.primary:
            own = {s: {k: None for k in v} for s, v in own.items()}
        eff = {s: {k: (own[s][k] if own[s][k] is not None else settings[s][k]) for k in settings[s]}
               for s in settings}
        return {'own': own, 'settings': settings, 'effective': eff, 'editable': not dev_cfg.primary}

    @app.post("/api/devices/{device_id}/opcua-output")
    @_device_write
    def set_device_opcua_output(device_id: str, payload: Dict = Body(...)):
        """Serve a device on the OPC UA server or not, and which of its values
        (`fields`, omitted or [] = every one). No poller restart: the server
        picks it up on its next pass. An endpoint unit sets its whole group."""
        if config.get_device(device_id) is None:
            raise HTTPException(status_code=404, detail="device not found")
        block = _opcua_block(payload)
        try:
            config.set_opcua_output(device_id, block['enabled'], block.get('fields'))
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        new_cfg = config.get_device(device_id)
        registry.replace(device_id, new_cfg)       # keep the running client
        _refresh_shared(new_cfg, 'group')
        shared = [d.id for d in config.devices if new_cfg.endpoint_id and d.endpoint_id == new_cfg.endpoint_id
                  and d.group_id == new_cfg.group_id]
        logger.info(f"device {device_id}: opc ua output {'on' if block['enabled'] else 'off'}"
                    f"{' (' + ', '.join(block['fields']) + ')' if block.get('fields') else ''}")
        return {"status": "ok", "device_id": device_id, "opcua_enabled": new_cfg.opcua_enabled,
                "opcua_fields": list(new_cfg.opcua_fields), "applies_to": shared or [device_id]}

    @app.post("/api/devices/{device_id}/rest-push")
    @_device_write
    def set_device_rest_push(device_id: str, payload: Dict = Body(...)):
        """Configure the generic REST push sink for a device (POST its values to
        an external URL on an interval). Header VALUES are masked on read and
        preserved on save; the pusher thread is (re)started in place."""
        dev = config.get_device(device_id)
        if dev is None:
            raise HTTPException(status_code=404, detail="device not found")
        # Preserve masked header secrets: a value equal to the mask keeps the stored one.
        from .device_patch import rest_push_clean
        cfg, errors = rest_push_clean(payload, dev.rest_push.get('headers') or {}, _REST_HDR_MASK, flag)
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})
        enabled = cfg['enabled']
        try:
            config.set_rest_push(device_id, cfg)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        new_cfg = config.get_device(device_id)
        registry.replace(device_id, new_cfg)       # keep the running client
        _apply_rest_push(new_cfg)
        _refresh_shared(new_cfg, 'rest')
        logger.info(f"device {device_id}: REST push {'enabled' if enabled else 'disabled'}")
        return {"status": "ok", "rest_push": _rest_push_public(new_cfg)}

    @app.post("/api/devices/{device_id}/rest-push/test")
    @_device_runtime
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
    @_device_runtime
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
        if not gw_role.active:
            raise HTTPException(status_code=503, detail={"errors": [gw_role.status_text()]})
        if not config.security.allow_writes:
            raise HTTPException(status_code=403, detail={"errors": [
                "Writes are disabled — set security.allow_writes=true to enable"]})
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
        # what can never be written is said first (a listened bus stays one
        # whether locked or not), then the lock — the order the device
        # menus give their reasons in (ADR-0032 §3, L3.10 review m2)
        if dev_cfg.protocol == 'http':
            raise HTTPException(status_code=400, detail={"errors": [
                "HTTP/JSON devices cannot be written"]})
        _protos = [str(getattr(x, 'protocol', '') or '').lower()
                   for x in (dev_cfg.sources or []) if getattr(x, 'enabled', True)] or [dev_cfg.protocol]
        if all(p_ in READ_ONLY_PROTOCOLS for p_ in _protos):
            raise HTTPException(status_code=400, detail={"errors": [
                f"{', '.join(dict.fromkeys(_protos))} devices are read-only — the gateway never "
                f"writes to that bus"]})
        if dev_cfg.write_locked:
            raise HTTPException(status_code=403, detail={"errors": [
                f"device '{device_id}' is write-locked — unlock it "
                + ("via security.primary_write_locked" if dev_cfg.primary
                   else "from the device's ⋯ → Unlock writes… (admin)")]})
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
        if _writes_by_mqtt(dev_cfg):
            # as on HA (ADR-0022, review MED 4e): there is no register to write
            raise HTTPException(status_code=422, detail={"errors": [
                f"'{device_id}' is read over MQTT — it is written only through an enabled command: "
                f"GET /api/devices/{device_id}/commands"]})
        # ── write envelope: declared path (guards enforce) or raw path (opt-in) ──
        rule = _write_rule(dev_cfg, address, rtype)
        unguarded = False
        if rule is not None and rule.writable:
            # What a command writes is refused raw (D4, below); a register whose
            # engineering value depends on a live scale factor is written
            # THROUGH its command — alone, the raw path would encode it without
            # the scale factor and "verify" the wrong value (3.83.0)
            _k = _raw_channel_conflict(dev_cfg, rtype, address, _words_of(rule.data_type or 'uint16', rtype))
            if _k is not None:
                raise HTTPException(status_code=422, detail={"errors": [
                    _conflict_text(address, getattr(rule, 'name', ''), _k)],
                    "status": "rejected", "through_command": _k['command'], "channel": _k['channel']})
            if getattr(rule, 'scale_from', None):
                raise HTTPException(status_code=422, detail={"errors": [
                    f"register {address} ({getattr(rule, 'name', '?')}) scales by a live factor — "
                    f"it is written through the command that owns it"]})
            # DECLARED: encoding is a property of the register (its template
            # row), NOT caller-controlled — a declared register always writes
            # with its declared type and scale, so a caller can't corrupt it
            # (or an adjacent register) with a mismatched data_type/word-count.
            data_type = (rule.data_type or 'uint16').lower()
            scale = float(rule.scale if rule.scale is not None else 1.0)
            _w_offset = float(getattr(rule, 'offset', 0.0) or 0.0)
        elif flag(payload, 'unguarded', False):
            # RAW (L0): the caller explicitly states no envelope exists. The
            # payload owns the encoding; nothing is clamped or checked beyond
            # type sanity. Master switch + device lock + auth + rate limit +
            # audit still apply — safety by configuration, not prohibition.
            # Bypassing the template envelope is an ADMIN act: the operator
            # role is "live actions within the declared envelope" (3.80.0).
            # With login on, the API key is NOT a substitute for the role —
            # every operator must present it on every write, so it would be
            # a free escalation (3.83.0); it stands in only on an auth-off box.
            _role = getattr(request.state, 'role', None)
            if auth_state.enabled and _role != 'admin':
                raise HTTPException(status_code=403, detail={"errors": [
                    "unguarded (raw) writes need the admin account"]})
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
        # What any command writes, at any position, on this physical slave is
        # never written raw — guarded or unguarded, admin or not (ADR-0022 D4,
        # operator Q2): WMaxLim_Ena = 0 alone lifts an OV curtail while the
        # percentage still reads 50, a drift the rule never sees
        _k = _raw_channel_conflict(dev_cfg, rtype, address, _words_of(data_type, rtype))
        if _k is not None:
            raise HTTPException(status_code=422, detail={"errors": [
                _conflict_text(address, getattr(rule, 'name', '') if rule is not None else '', _k)],
                "status": "rejected", "through_command": _k['command'], "channel": _k['channel']})
        prefer_fc6 = flag(payload, 'prefer_fc6', False)
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
        # what the encoder would have to clamp is refused here (F-15, 3.83.0):
        # a clamped word is a value nobody asked for, and it was "verified"
        from .encoder import RegisterEncoder as _Enc
        _rng = _Enc.int_range(data_type)
        if _rng is not None and rtype != 'coil':
            try:
                _raw = int(round((float(payload.get('value')) - _w_offset) * float(scale)))
            except (TypeError, ValueError):
                _raw = None
            if _raw is not None and not (_rng[0] <= _raw <= _rng[1]):
                raise HTTPException(status_code=422, detail={"errors": [
                    f"value {payload.get('value')} encodes to {_raw}, outside {data_type} "
                    f"[{_rng[0]}, {_rng[1]}]"]})
        _who = getattr(request.state, "user", None) or ("api-key" if _api_key else "anon")
        _src = request.client.host if request.client else "?"
        ctx = {'dev': dev_cfg, 'rtype': rtype, 'address': address,
               'register': getattr(rule, 'name', '') if rule is not None else '',
               'data_type': data_type, 'scale': float(scale), 'offset': _w_offset, 'value': payload.get('value'),
               'prefer_fc6': prefer_fc6, 'unguarded': unguarded, 'lease_ms': lease_ms, 'safe': _safe,
               'via': 'api', 'who': _who, 'ip': _src}
        # a lane job (ADR-0022 D5): latest-wins on the register, an expiry,
        # the bus never held by the HTTP thread; waited for here
        job = _submit_raw(ctx, client)
        code, res = _cmd_dispatch.wait_for(job, DEFAULT_MAX_AGE_S + _raw_exec_timeout(client) + 1.0)
        if code != 200:
            # the shape the UI reads (res.detail.errors), with the lane's word
            raise HTTPException(status_code=code, detail={
                "errors": [res.get('reason') or res.get('status') or 'not written'],
                "status": res.get('status'),
                **{k: res[k] for k in ('queued_ms', 'words', 'id') if res.get(k) is not None}})
        out = {"ok": True, "device": device_id, "address": address, "register_type": rtype,
               "data_type": data_type, "written": payload.get('value'),
               "words": res.get('words'), "read_back": res.get('read_back'), "verified": res.get('verified'),
               "lease_ms": lease_ms or None,
               "reverts_to": _safe if lease_ms > 0 else None,
               "unguarded": unguarded or None}
        if res.get('queued_ms') is not None:
            out['queued_ms'] = res['queued_ms']
        if res.get('reason'):
            out['reason'] = res['reason']                   # why it is unverified (RP-11)
        return out

    @app.post("/api/devices/{device_id}/write-lock")
    @_device_write
    @_serialized_mutation
    def set_device_write_lock(device_id: str, request: Request, payload: Dict = Body(...)):
        """Set/clear the per-device write lock (F3a). A locked device refuses
        every write regardless of guards. Endpoint units lock at the ENDPOINT level
        (one physical endpoint — its units lock together); the primary maps to
        security.primary_write_locked. Audited."""
        if 'locked' not in payload:
            raise HTTPException(status_code=422, detail={"errors": ["'locked' (bool) is required"]})
        locked = flag(payload, 'locked')
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
        # a register a command writes is never written raw (ADR-0022 D4): the
        # dialog points to the command instead
        try:
            _k = _raw_channel_conflict(dev_cfg, rtype, int(address),
                                       _words_of(rule.data_type if declared else 'uint16', rtype))
        except Exception:  # noqa: BLE001
            _k = None
        return {
            "device": device_id, "address": address, "register_type": rtype,
            "through_command": _k['command'] if _k else None,
            "channel": _k['channel'] if _k else None,
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
    @_device_write
    def set_device_poll_groups(device_id: str, payload: Dict = Body(...)):
        """Update a device's poll-group intervals and live-restart its pollers so
        each group re-samples at its own rate (e.g. slow down a fragile HTTP/
        gateway source)."""
        idx, dev_cfg, client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        from .device_patch import poll_groups_clean
        groups_in = payload.get("poll_groups", payload) or {}
        if not isinstance(groups_in, dict):
            groups_in = {}
        clean, why = poll_groups_clean(groups_in)
        if why:
            raise HTTPException(status_code=400, detail=why)
        _refuse_busy_poll_groups(dev_cfg, clean)
        try:
            clean = config.save_device_poll_groups(device_id, clean)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        _reload_poll_groups(dev_cfg, client)
        return {"status": "ok", "device": device_id, "poll_groups": clean}

    def _refuse_busy_poll_groups(dev_cfg, clean: Dict, field: str = "poll_groups") -> None:
        """A group removed falls back to the gateway's own group of that name;
        one the gateway does not have would send its registers to 'normal'
        (an hourly group polled every few seconds) — refused (422) while in use."""
        dropped = [n for n, g in clean.items() if g is None and n not in config.default_poll_groups]
        if not dropped:
            return
        used = set()
        for src in [None] + list(getattr(dev_cfg, 'sources', None) or []):
            try:
                regs, _g = config.load_device_registers(dev_cfg, source=src)
            except Exception:  # noqa: BLE001
                continue
            used |= {r.poll_group for r in regs}
        busy = [n for n in dropped if n in used]
        if busy:
            msgs = {f"{field}.{n}": (f"{n}: selected registers still poll in this group and the gateway "
                                     f"has no group of that name — removing it would poll them at "
                                     f"'normal'; move them to another group first") for n in busy}
            raise HTTPException(status_code=422, detail={"errors": list(msgs.values()), "fields": msgs})

    def _reload_poll_groups(dev_cfg, client) -> None:
        """Live-restart a device's pollers with its new intervals."""
        target = modbus_client if dev_cfg.primary else client
        if target:
            regs, groups = config.load_device_registers(dev_cfg)
            target.update_registers(regs, groups)
            if hasattr(target, 'reload_registers'):
                target.reload_registers()

    def _vmeter_sources() -> Dict[str, Set[str]]:
        """Virtual-meter template id -> the device ids it reads: the
        instance's own source device and every `<device>.<register>` in its
        rows — live, sum and failover (`combined`) alike. Each candidate id
        is every prefix of a reference up to one of its dots; the caller
        keeps those that are devices. A template that cannot be read raises
        (deleting a device it may read is then refused, not allowed)."""
        from .dependents import Unreadable
        from .virtual_meter import load_template as _lt
        mgr = getattr(app.state, "vmeter_manager", None)
        out: Dict[str, Set[str]] = {}
        if mgr is None:
            return out

        def strings(x):
            if isinstance(x, str):
                yield x
            elif isinstance(x, dict):
                for v in x.values():
                    yield from strings(v)
            elif isinstance(x, (list, tuple)):
                for v in x:
                    yield from strings(v)
        try:
            instances = mgr._load_cfg().get("instances", []) or []
        except Exception as e:  # noqa: BLE001
            raise Unreadable(f"the virtual meters' configuration could not be read ({e})")
        for i in instances:
            tid = str(i.get("template") or "")
            names = out.setdefault(tid, set())
            if i.get("device"):
                names.add(str(i["device"]))
            try:
                t = _lt(str(mgr.templates_dir / f"{tid}.yaml"))
            except Exception as e:  # noqa: BLE001
                raise Unreadable(f"virtual meter template {tid!r} could not be read ({e})")
            for r in t.registers:
                if r.source_kind in ("const", "const_str"):
                    continue
                for ref in strings(r.source):
                    j = ref.find('.')
                    while j > 0:
                        names.add(ref[:j])
                        j = ref.find('.', j + 1)
        return out

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
                regs = config.unit_registers(dev_cfg) + [e['_reg'] for e in calc_engine.store.get(device_id, []) if e.get('_reg') is not None]
                pref = mqtt_publisher.config.ha_discovery_prefix
                for r in regs:
                    if not r.mqtt_enabled:
                        continue
                    sn = r.name.lower().replace('[', '_').replace(']', '').replace('_g_', '')
                    dk = getattr(r, 'discovery_key', '') or f"{r.address}_{sn}"
                    mqtt_publisher._publish(
                        f"{pref}/sensor/{mqtt_publisher._ha_node(device_id, dev_cfg.ha_node)}/{dk}/config",
                        "", retain=True)          # empty retained payload = delete
            except Exception as e:  # noqa: BLE001
                logger.warning(f"clearing discovery for {device_id} failed: {e}")
        # what its meters gave it stops here, and a device later created with
        # this id starts with no memory of this one (review N1, N6)
        calc_runners.pop(device_id, None)
        calc_engine.forget(device_id)

    def _device_value_topics(dev_cfg) -> set:
        """The retained value topics a device publishes (rows and calculated)."""
        if not mqtt_publisher:
            return set()
        try:
            regs = config.unit_registers(dev_cfg) + [
                e['_reg'] for e in calc_engine.store.get(dev_cfg.id, []) if e.get('_reg') is not None]
            topics = set(mqtt_publisher.register_topics(regs, dev_cfg.mqtt_topic_prefix))
            if dev_cfg.mqtt_topic_prefix:
                topics.add(f"{dev_cfg.mqtt_topic_prefix}/meta")       # its attributes go with it
                _meta_sent.pop(f"{dev_cfg.mqtt_topic_prefix}/meta", None)
            return topics
        except Exception as e:  # noqa: BLE001
            logger.warning(f"listing the topics of {dev_cfg.id} failed: {e}")
            return set()

    def _clear_device_values(topics: set) -> None:
        """A device that is GONE (deleted, not restarted) takes its retained
        values with it: nobody must read a deleted device as live. Run AFTER
        its client stopped, or a last poll would publish them again. Not part
        of the teardown, which an edit also runs."""
        if topics and mqtt_publisher and getattr(mqtt_publisher, "connected", False):
            try:
                mqtt_publisher.clear_topics(topics)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"clearing retained values failed: {e}")

    def _reload_linked_to(meter_id: str) -> None:
        """Devices whose electricity the deleted device measured lose their
        power_electric / energy_electric now, not at the next restart."""
        from .meter_links import served_by
        for did in {d for d, _r, _i in served_by(config, meter_id)}:
            try:
                calc_engine.load(did)
            except Exception:  # noqa: BLE001
                pass

    # ── what depends on a device, an installation or a template ─────────────
    # (multibus/dependents.py) Nothing is deleted while something uses it.
    def _dep_sources():
        from .dependents import Sources
        rt = getattr(app.state, 'rules_runtime', None)

        def _rules():
            if rt is None:
                return {}
            with rt._lock:                  # a copy, briefly: parsed outside the lock
                return dict(rt.raw)

        def _sequences():
            store = getattr(app.state, 'sequence_store', None)
            return store.load() if store is not None else []

        def _attributes():
            svc = getattr(app.state, 'attributes', None)
            return svc.store.read()[0] if svc is not None else {}

        def _calculated(device_id: str):
            # straight from the file, and loud: a file that cannot be read
            # must not read as "no formulas here"
            path = config.device_registers_path(device_id)
            try:
                with open(path) as f:
                    return (json.load(f) or {}).get("calculated", []) or []
            except FileNotFoundError:
                return []

        def _unreadable():
            out = []
            if rt is not None and getattr(rt.store, 'load_error', None):
                out.append(f"rules.yaml could not be read ({rt.store.load_error})")
            store = getattr(app.state, 'sequence_store', None)
            if store is not None and getattr(store, 'load_error', None):
                out.append(f"sequences.yaml could not be read ({store.load_error})")
            return out
        return Sources(config=config, rules=_rules, sequences=_sequences,
                       vmeter_sources=_vmeter_sources, templates=template_registry.all,
                       attributes=_attributes, calculated=_calculated, unreadable=_unreadable)

    def _dependents(kind: str, oid: str) -> Dict:
        """The answer, or 503 when a source of references cannot be read —
        then what uses it is not known, and nothing is deleted."""
        from .dependents import Unreadable, dependents_of
        try:
            return dependents_of(kind, oid, _dep_sources())
        except Unreadable as e:
            raise HTTPException(status_code=503, detail={"errors": [
                f"what uses {oid!r} cannot be told: {e} — fix that first"]})

    def _refuse_if_used(kind: str, oid: str, what: str, dry_run: bool) -> Optional[Dict]:
        """409 while anything blocks; on a dry run the answer that a delete
        would get. None: go ahead."""
        from .dependents import summary
        deps = _dependents(kind, oid)
        if deps['blocking']:
            raise HTTPException(status_code=409, detail={
                "errors": [summary(deps, what)], "dependents": deps})
        if dry_run:
            return {"status": "valid", "would": "delete", "dependents": deps}
        return None

    @app.get("/api/devices/{device_id}/dependents")
    def device_dependents(device_id: str):
        """What uses this device (blocking) and what goes with it (follows)."""
        if config.get_device(device_id) is None:
            raise HTTPException(status_code=404, detail="device not found")
        return _dependents('device', device_id)

    @app.get("/api/endpoints/{endpoint_id}/dependents")
    def endpoint_dependents(endpoint_id: str):
        """What uses this installation or one of its units."""
        if config.get_raw_endpoint(endpoint_id) is None:
            raise HTTPException(status_code=404, detail="endpoint not found")
        return _dependents('installation', endpoint_id)

    @app.get("/api/dependents")
    def dependents_counts():
        """How many things use each device and installation, in one pass: a
        list shows "used by N" without asking each one."""
        from .dependents import Unreadable, dependents_index
        try:
            return {"counts": dependents_index(_dep_sources())}
        except Unreadable as e:
            raise HTTPException(status_code=503, detail={"errors": [str(e)]})

    def _device_deletable(device_id: str):
        """404 / 422 for what is never deleted here; else (dev_cfg, client)."""
        _idx, dev_cfg, client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        if dev_cfg.primary:
            raise HTTPException(status_code=422, detail={"errors": [
                "the primary device cannot be deleted"]})
        if dev_cfg.endpoint_id:
            raise HTTPException(status_code=422, detail={"errors": [
                f"device '{device_id}' is materialized from endpoint "
                f"'{dev_cfg.endpoint_id}' — edit or delete the endpoint instead"]})
        if dev_cfg.component_of:
            raise HTTPException(status_code=422, detail={"errors": [
                f"'{device_id}' is a component of '{dev_cfg.component_of}' — remove it there"]})
        return dev_cfg, client

    @app.delete("/api/devices/{device_id}")
    @_device_write
    def delete_device(device_id: str, request: Request, dry_run: bool = False):
        """Delete a non-primary device (its selected-registers file is kept
        on disk for safety). Refused (409) while a rule, a sequence, another
        device's formula, a device it measures or a virtual meter uses it or
        one of its components. `?dry_run=true`: the same checks, nothing
        removed.

        Asked first outside the device lock (a dry run never takes it, and a
        disable or a write lock never waits on the walk), then asked again
        under the lock, where the delete happens."""
        _device_deletable(device_id)
        _if_match = request.headers.get('if-match')     # checked by @_device_write; again under the lock
        ans = _refuse_if_used('device', device_id, f"'{device_id}'", dry_run)
        if ans is not None:
            return ans
        return _delete_device_locked(device_id, _if_match)

    @_serialized_mutation
    def _delete_device_locked(device_id: str, if_match: Optional[str] = None):
        dev_cfg, client = _device_deletable(device_id)
        _check_if_match(if_match, device_id)          # again, now nothing can change under it
        # anything that uses it (or one of its components) would break —
        # again, now nothing can change under it
        _refuse_if_used('device', device_id, f"'{device_id}'", False)
        comps = config.component_devices(device_id)
        # what Undo (its restore) will set back to safe, said with the answer
        _raw_now = config.get_raw_device(device_id)
        resets, resumes = _restore_resets(_raw_now), _restore_resumes(_raw_now)
        # its components go with it (the tombstone keeps their list)
        for c in comps:
            _drop_component_runtime(c)
        _cancel_device_refreshes(device_id)
        _topics = _device_value_topics(dev_cfg)
        _teardown_device(device_id, dev_cfg, client)
        _clear_device_values(_topics)
        config.remove_raw_device(device_id)
        # re-resolve by id (idx may be stale after a concurrent mutation);
        # drops the pair AND its value store in one atomic step
        registry.remove(device_id)
        for _x in [device_id] + [c.id for c in comps]:
            _reload_linked_to(_x)
        _sync_device_discovery()
        logger.info(f"device {device_id}: deleted")
        # what an Undo names (review m1); none kept: nothing to undo (m2)
        _ts = config.last_tombstone_ts(device_id)
        return {"status": "deleted", "restorable": _ts is not None, "deleted_ts": _ts,
                "restore_resets": resets, "restore_resumes": resumes}

    # ── components: what a device holds, added by instance ───────────────────
    # A component is an ordinary device
    # everywhere else; it is made, renamed, switched and removed here.

    def _drop_component_runtime(dev) -> None:
        """Stop a component and clear what it published (its entry stays)."""
        _i, _c, client = registry.find(dev.id)
        _cancel_device_refreshes(dev.id)
        topics = _device_value_topics(dev)
        _teardown_device(dev.id, dev, client)
        _clear_device_values(topics)
        registry.remove(dev.id)

    def _component_types(dev_cfg) -> Dict[str, Dict]:
        tpl = template_registry.get(dev_cfg.template) if dev_cfg.template else None
        return dict(getattr(tpl, 'components', None) or {})

    def _component_type_name(dev) -> str:
        """A component's type as its parent's template names it ("Battery"),
        else the type key."""
        parent = config.get_device(dev.component_of) if dev.component_of else None
        ct = _component_types(parent).get(dev.component_type) if parent is not None else None
        return str((ct or {}).get('name') or dev.component_type or '')

    def _component_view(dev) -> Dict:
        _i, _c, client = registry.find(dev.id)
        e = _device_entry(dev, client)
        return {'id': dev.id, 'type': dev.component_type, 'type_name': _component_type_name(dev),
                'instance': dev.component_instance,
                'name': dev.name, 'enabled': dev.enabled, 'connected': e.get('connected'),
                'data_health': e.get('data_health'), 'template': dev.template,
                'mqtt_topic_prefix': dev.mqtt_topic_prefix}

    def _parent_or_404(device_id: str):
        _i, dev_cfg, client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        return dev_cfg, client

    def _component_or_404(device_id: str, component_id: str):
        _i, dev, client = _find_device(component_id)
        if dev is None or dev.component_of != device_id:
            raise HTTPException(status_code=404, detail=f"no component '{component_id}' on '{device_id}'")
        return dev, client

    @app.get("/api/devices/{device_id}/components")
    def list_components(device_id: str):
        """The kinds of things this device can hold (its template's) and the
        ones it holds."""
        dev_cfg, _c = _parent_or_404(device_id)
        types = [{'type': k, 'name': str(c.get('name') or k), 'kind': str(c.get('kind', '') or ''),
                  'description': str(c.get('description', '') or ''),
                  'instance_label': str((c.get('instance') or {}).get('label', '') or ''),
                  'instance_hint': str((c.get('instance') or {}).get('hint', '') or ''),
                  'discover': bool(c.get('discover'))}
                 for k, c in _component_types(dev_cfg).items()]
        return {'device': device_id, 'types': types,
                'components': [_component_view(d) for d in config.component_devices(device_id)]}

    @app.post("/api/devices/{device_id}/components")
    @_device_write
    @_serialized_mutation
    def add_component(device_id: str, payload: Dict = Body(...)):
        """Add one component: {type, instance, name?, id?, enabled?}. It reads
        through this device's connection and starts at once."""
        dev_cfg, _c = _parent_or_404(device_id)
        types = _component_types(dev_cfg)
        errors = []
        if dev_cfg.primary or dev_cfg.endpoint_id or dev_cfg.component_of:
            errors.append(f"'{device_id}' cannot hold components (the primary device, an "
                          "installation's unit or a component itself)")
        elif dev_cfg.protocol != 'mqtt':
            errors.append("components are read over MQTT only, for now")
        held = {(d.component_type, d.component_instance) for d in config.component_devices(device_id)}
        entry, cid, errs = _new_component(device_id, types, payload, held, set())
        errors += errs
        if errors:
            raise HTTPException(status_code=422, detail=_errors_detail(errors))
        try:
            new = config.add_component(device_id, dict(entry, id=cid))
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        _start_new_component(new)
        _sync_device_discovery()
        logger.info(f"device {device_id}: component {cid} added ({entry['type']} {entry['instance']})")
        return {"status": "created", "component": _component_view(new)}

    def _new_component(parent_id: str, types: Dict, payload: Dict, held: Set[tuple],
                       taken: Set[str]) -> Tuple[Optional[Dict], str, List[str]]:
        """One component a request adds, checked: (its entry without the id,
        its id, errors). ``held``: the (type, instance) the parent holds
        already; ``taken``: ids the same request takes (a create's own)."""
        errors: List[str] = []
        ctype = str(payload.get('type', '') or '').strip()
        inst = '' if payload.get('instance') is None else str(payload.get('instance')).strip()
        if ctype not in types:
            errors.append(f"type: '{ctype}' is not one of {', '.join(sorted(types)) or 'none (the template declares no components)'}")
        if not config.COMPONENT_INSTANCE_RE.match(inst):
            errors.append("instance: 1-32 letters, digits, _ . -")
        elif (ctype, inst) in held:
            errors.append(f"instance: {ctype} {inst} is already a component of '{parent_id}'")
        cid = str(payload.get('id') or config.component_id(parent_id, ctype, inst)).strip().lower()
        if not _DEVICE_ID_RE.match(cid):
            errors.append(f"id: '{cid}' is not a device id (a-z 0-9 - _, 2-64 chars) — give it a shorter id")
        elif cid in taken or registry.has(cid) or config.get_device(cid) is not None:
            errors.append(f"id: '{cid}' already exists")
        else:
            from .topic_prefix import prefix_errors
            errors += [f"mqtt.topic_prefix: {e}"
                       for e in prefix_errors(config.default_topic_prefix(cid), config, {f"device:{cid}"})]
        if errors:
            return None, cid, errors
        name = str(payload.get('name') or f"{types[ctype].get('name') or ctype} {inst}").strip()[:80]
        entry = {'type': ctype, 'instance': inst, 'name': name,
                 'enabled': flag(payload, 'enabled', True),
                 # its routing identity, fixed from now on
                 'mqtt': {'topic_prefix': config.default_topic_prefix(cid)}}
        _set_new_node(entry['mqtt'], cid)
        return entry, cid, []

    def _kind_of_payload(dev, payload: Dict) -> Tuple[str, Optional[str]]:
        """The stored form of the type a payload gives a device ({type} or
        {kind}; null = derive it again): (kind, None), or ('', why not).
        The rule of /organise, which PATCH's identity.type shares."""
        from . import device_types as _dtypes
        raw_kind, why = _dtypes.from_payload(payload)
        if why:
            return '', why
        if raw_kind is None:
            kind = ''
        else:
            msg = _dtypes.override_error(raw_kind)
            if msg:
                return '', msg
            kind = _dtypes.stored_form(raw_kind)
        if dev.protocol == 'none':
            # a device measured only by its meters has no template to derive a
            # kind from, and is never a meter itself (nor `other`)
            from .device_kind import METER_ONLY
            msg = (_dtypes.override_error(kind, meter_only=True) if kind else
                   f"a device measured only by its meters is one of {', '.join(METER_ONLY)} or a user type "
                   "(it has no template to derive a kind from, and it is not a meter)")
            if msg:
                return '', msg
        return kind, None

    @app.patch("/api/devices/{device_id}/organise")
    @_device_write
    @_serialized_mutation
    def organise_device(device_id: str, payload: Dict = Body(...), dry_run: bool = False):
        """How a device is filed, not how it is read: ``{type}`` (or ``{kind}``,
        its older word) — a built-in kind or a user type (device_types.py), or
        null to derive it again. Never restarts it. `?dry_run=true` checks and
        changes nothing."""
        dev = config.get_device(device_id)
        if dev is None:
            raise HTTPException(status_code=404, detail="device not found")
        _field = 'type' if isinstance(payload, dict) and 'type' in payload else 'kind'
        if not isinstance(payload, dict) or ('kind' not in payload and 'type' not in payload):
            _msg = "required: type (or kind) — null to derive it again"
            raise HTTPException(status_code=422, detail={"errors": [f"type: {_msg}"],
                                                         "fields": {"type": _msg, "kind": _msg}})
        kind, msg = _kind_of_payload(dev, payload)
        if msg:
            raise HTTPException(status_code=422, detail={"errors": [f"{_field}: {msg}"], "fields": {_field: msg}})
        why = ("the primary device's kind comes from its template" if dev.primary
               else f"a unit's kind is its group's role on its installation ({dev.endpoint_id})"
               if dev.endpoint_id else None)
        if why:
            raise HTTPException(status_code=422, detail={"errors": [f"kind: {why}"], "fields": {"kind": why}})
        # judged on what is STORED: a value load could not read (and dropped)
        # is still in the file, and null must be able to clear it
        try:
            stored = config._own_entry(device_id).get('kind')
        except ValueError:
            stored = None
        if stored == (kind or None) or (stored in (None, '') and not kind):
            k, src = _device_kind(dev, template_registry)
            return {"status": "unchanged", "device_id": device_id, "kind": k, "kind_source": src,
                    **dict(zip(("type", "type_label"), _device_type_of(dev)))}
        if dry_run:
            return {"status": "valid", "would": "update"}
        try:
            config.set_device_kind(device_id, kind or None)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)], "fields": {"kind": str(e)}})
        new_cfg = config.get_device(device_id)
        registry.replace(device_id, new_cfg)          # keep the running client
        k, src = _device_kind(new_cfg, template_registry)
        logger.info(f"device {device_id}: kind {'set to ' + kind if kind else 'derived again'} → {k} ({src})")
        try:
            attributes.invalidate()         # its meta says its new kind at once
            _publish_attribute_meta()       # and its type (ADR-0027)
        except Exception:  # noqa: BLE001 — publishing never fails a save
            logger.exception("attributes meta after a type change")
        return {"status": "updated", "device_id": device_id, "kind": k, "kind_source": src,
                **dict(zip(("type", "type_label"), _device_type_of(new_cfg)))}

    @app.patch("/api/devices/{device_id}/components/{component_id}")
    @_device_write
    @_serialized_mutation
    def update_component(device_id: str, component_id: str, payload: Dict = Body(...)):
        """Rename a component or switch it on or off: {name?, enabled?}."""
        dev, client = _component_or_404(device_id, component_id)
        changes: Dict[str, Any] = {}
        if 'name' in payload:
            name = str(payload.get('name') or '').strip()[:80]
            if not name:
                raise HTTPException(status_code=422, detail={"errors": ["name: required"]})
            changes['name'] = name
        if 'enabled' in payload:
            changes['enabled'] = flag(payload, 'enabled')
        try:
            new = config.update_component(device_id, component_id, changes)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        if client:
            client.disconnect()
        registry.replace(component_id, new, client=_start_device_client(new), add_if_missing=True)
        _sync_device_discovery()
        return {"status": "updated", "component": _component_view(new)}

    @app.delete("/api/devices/{device_id}/components/{component_id}")
    @_device_write
    def delete_component(device_id: str, component_id: str, dry_run: bool = False):
        """Remove a component. Its measurements stay on disk: adding the same
        instance again resumes them. Refused (409) while anything uses it
        (asked outside the device lock, then again under it)."""
        _component_or_404(device_id, component_id)
        ans = _refuse_if_used('device', component_id, f"'{component_id}'", dry_run)
        if ans is not None:
            return ans
        return _delete_component_locked(device_id, component_id)

    @_serialized_mutation
    def _delete_component_locked(device_id: str, component_id: str):
        dev, _client = _component_or_404(device_id, component_id)
        _refuse_if_used('device', component_id, f"'{component_id}'", False)
        _drop_component_runtime(dev)
        try:
            config.remove_component(device_id, component_id)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        _reload_linked_to(component_id)         # gone from the config now
        _sync_device_discovery()
        logger.info(f"device {device_id}: component {component_id} removed")
        return {"status": "deleted", "id": component_id}

    @app.post("/api/devices/{device_id}/components/discover")
    @_device_runtime
    def discover_components(device_id: str):
        """What the device says it holds: listens through its connection for a
        few seconds after asking it to republish everything (a read; nothing
        is written). [{type, instance, product, name, added, device_id}]"""
        from . import component_discover as _cd
        from . import mqtt_link
        dev_cfg, _c = _parent_or_404(device_id)
        types = _cd.discoverable(_component_types(dev_cfg))
        if not types:
            raise HTTPException(status_code=422, detail={"errors": [
                "the template does not say how to find its components"]})
        link = next((ln for ln in mqtt_link.links() if ln.name == device_id and ln.connected), None)
        if link is None:
            raise HTTPException(status_code=409, detail={"errors": [
                f"'{device_id}' is not connected — finding its components reads through its connection"]})
        tvars = {str(k): str(v) for k, v in ((dev_cfg.mqtt_in or {}).get('vars') or {}).items()}

        def keepalive():
            # the device's own keepalive, as its input keeps it, asked for
            # everything: what was asked at connect has gone by already
            drv = _mqtt_driver_of(_c)
            if drv is not None:
                drv.send_keepalive(first=True)
        found = _cd.listen(link, types, tvars, config.COMPONENT_INSTANCE_RE, 4.0, before=keepalive)
        have = {(d.component_type, d.component_instance): d.id for d in config.component_devices(device_id)}
        out = [dict(f, added=(f['type'], f['instance']) in have, device_id=have.get((f['type'], f['instance']), ''))
               for f in found]
        return {'device': device_id, 'found': out}

    @app.post("/api/devices/components/discover")
    def discover_components_unsaved(request: Request, payload: Dict = Body(...)):
        """What a device not saved yet holds (ADR-0031 §5): {template,
        connection (MQTT), seconds?} — a short-lived connection of its own to
        the broker, subscribe only, for ``seconds`` (1..10, default 4); it
        publishes nothing (a GX that nobody keeps alive says nothing: the
        answer is then empty). [{type, instance, product, name}]. Admin only
        (it makes the gateway connect where the body says); two at a time
        (429 beyond); a broker not reached answers the same 409, after the
        same wait, whatever the port did."""
        from . import component_discover as _cd
        if auth_state.enabled and getattr(request.state, 'role', None) != 'admin':
            raise HTTPException(status_code=403, detail="finding components before saving is for the admin")
        errors: List[str] = []
        tid = str((payload or {}).get('template') or '').strip()
        tpl = template_registry.get(tid) if tid else None
        if tpl is None:
            errors.append(f"template: '{tid}' not found" if tid else "template: required")
        conn = (payload or {}).get('connection')
        if not isinstance(conn, dict) or str(conn.get('protocol', '')).lower() != 'mqtt':
            errors.append("connection.protocol: components are found over MQTT only, for now")
        elif not str(conn.get('broker', '')).strip():
            errors.append("connection.broker: required")
        else:
            try:
                if not (1 <= int(conn.get('port', 1883)) <= 65535):
                    raise ValueError
            except (TypeError, ValueError):
                errors.append("connection.port: must be 1..65535")
        types = _cd.discoverable(dict(getattr(tpl, 'components', None) or {})) if tpl is not None else {}
        if tpl is not None and not types:
            errors.append("template: it does not say how to find its components")
        if errors:
            raise HTTPException(status_code=422, detail=_errors_detail(errors))
        tvars = {str(k): str(v) for k, v in (conn.get('vars') or {}).items()} if isinstance(conn.get('vars'), dict) else {}
        if not _cd.SLOTS.acquire(blocking=False):
            raise HTTPException(status_code=429, headers={'Retry-After': str(int(_cd.CONNECT_S + _cd.MAX_S))},
                                detail={"errors": [f"busy: {_cd.MAX_AT_ONCE} searches are running — try again "
                                                   "in a few seconds"]})
        try:
            link = _cd.open_link(conn)
            if link is None:
                raise HTTPException(status_code=409, detail={"errors": [
                    f"connection.broker: {conn['broker']}:{conn.get('port', 1883)} could not be reached "
                    f"within {_cd.CONNECT_S:g} s"]})
            try:
                found = _cd.listen(link, types, tvars, config.COMPONENT_INSTANCE_RE,
                                   _cd.seconds_of((payload or {}).get('seconds')))
            finally:
                _cd.close(link)
        finally:
            _cd.SLOTS.release()
        return {'template': tid, 'found': found}

    # ── endpoints: one template × N unit ids behind one endpoint ──────────────
    # An endpoint materializes into N ordinary devices (own socket each — unit-
    # switching on a shared socket corrupts some gateway buffers, and units
    # must fail independently). The devices are managed THROUGH the endpoint:
    # device CRUD refuses them; edit/delete the endpoint instead.

    def _endpoint_sources(p: Dict, pid: str, holder: Dict = None,
                          group_id: str = '') -> List[Dict]:
        """Every declared way of reaching this endpoint's units, in precedence
        order, with what each one is actually contributing right now.

        Configuration and live state in one object on purpose: "which source is
        this value from" and "is that source still alive" are the same question
        for an operator, and answering them from two endpoints would invite them
        to drift. Flat JSON, so a Node-RED http-request node reads it directly.
        """
        h = holder if holder is not None else p
        raw = h.get('sources') or []
        if not raw:
            conn = dict(h.get('connection', {}) or {})
            if not conn:
                return []
            raw = [{'id': 'default',
                    'template': h.get('template', '') or p.get('template', ''),
                    **conn}]
        # roll the live picture up across the endpoint's units
        live: Dict[str, Dict] = {}
        wire: Dict[str, Any] = {}     # a listen-only tap's shared-reader health
        for dev in config.endpoint_devices(pid):
            if group_id and (getattr(dev, 'group_id', '') or 'units') != group_id:
                continue
            _i, _c, client = registry.find(dev.id)
            if client is None:
                continue
            try:
                st = client.get_stats() or {}
            except Exception:  # noqa: BLE001
                continue
            for row in (st.get('sources') or []):
                agg = live.setdefault(row['id'], {
                    'units_ok': 0, 'units_total': 0, 'reads': 0, 'failed': 0,
                    'reads_5m': 0, 'failed_5m': 0, 'latency_ms': [], 'owns': 0})
                agg['units_total'] += 1
                agg['units_ok'] += 1 if row.get('connected') else 0
                agg['reads'] += int(row.get('successful_reads') or 0)
                agg['failed'] += int(row.get('failed_reads') or 0)
                agg['reads_5m'] += int(row.get('reads_5m') or 0)
                agg['failed_5m'] += int(row.get('failed_5m') or 0)
                if row.get('last_latency_ms') is not None:
                    agg['latency_ms'].append(row['last_latency_ms'])
            for _f, prov in (st.get('provenance') or {}).items():
                a = live.get(prov.get('source'))
                if a is not None:
                    a['owns'] += 1
            if not wire and isinstance(st.get('bus'), dict):
                # one reader per port: any unit's view of it is THE view
                ec = st.get('error_counts') or {}
                wire = {'frames': st['bus'].get('frames'),
                        'writes_seen': st['bus'].get('writes_seen'),
                        'open_error': st['bus'].get('open_error') or '',
                        'crc_errors': ec.get('crc_errors'),
                        'orphan_frames': ec.get('orphan_frames'),
                        'exception_responses': ec.get('exception_responses'),
                        'resync_dropped_bytes': ec.get('resync_dropped_bytes')}

        out = []
        for rank, r in enumerate(raw):
            sid = str(r.get('id') or f'source{rank + 1}')
            agg = live.get(sid) or {}
            lat = agg.get('latency_ms') or []
            pg = {k: (v.get('interval') if isinstance(v, dict) else v)
                  for k, v in (r.get('poll_groups') or {}).items()}
            ivals = [float(v) for v in pg.values() if isinstance(v, (int, float))]
            r5 = agg.get('reads_5m') or 0
            out.append({
                'id': sid, 'rank': rank,
                'enabled': bool(r.get('enabled', True)),
                'protocol': str(r.get('protocol', 'tcp')).lower(),
                'template': (r.get('template', '') or h.get('template', '')
                             or p.get('template', '')),
                'address': (r.get('url')
                            or (r.get('serial_port') if r.get('protocol') in SERIAL_PROTOCOLS + ('rtu_tap',)
                                and r.get('serial_port')
                                else f"{r.get('host', '')}:{r.get('port', 502)}")),
                # a serial line's settings, so an editor that writes the source
                # back does not drop them (a 19200-baud bus read at 9600)
                **({k: r[k] for k in ('baudrate', 'parity', 'stopbits', 'bytesize') if k in r}
                   if r.get('serial_port') else {}),
                'poll_groups': pg,
                # what is stored, verbatim (secrets left out — a save that does
                # not carry them keeps them): an editor starts from this and
                # sends back what it did not change, tuning included
                'declared': {k: copy.deepcopy(v) for k, v in r.items()
                             if k not in ('password', 'token', 'headers')},
                # the fastest rhythm this source reads at — what "every N s"
                # means to an operator; None when it inherits the template's
                'interval_s': min(ivals) if ivals else None,
                'timeout': r.get('timeout'),
                'stale_after_s': float(r.get('stale_after_s', 0) or 0),
                # live
                'units_ok': agg.get('units_ok', 0),
                'units_total': agg.get('units_total', 0),
                'successful_reads': agg.get('reads', 0),
                'failed_reads': agg.get('failed', 0),
                # the last five minutes, not since boot: a counter that grew
                # for a week says nothing about whether the source is fine NOW
                'reads_5m': r5,
                'failed_5m': agg.get('failed_5m') or 0,
                'fail_pct_5m': (round(100.0 * (agg.get('failed_5m') or 0) / r5, 1)
                                if r5 else None),
                'latency_ms': round(sum(lat) / len(lat), 1) if lat else None,
                # how many fields this source is currently authoritative for —
                # the number that tells an operator whether it earns its place
                'fields_owned': agg.get('owns', 0),
                # a tap's wire: frames heard, CRC errors, orphans — the health
                # of a bus nobody here masters
                'wire': (wire or None) if str(r.get('protocol', '')).lower() == 'rtu_tap' else None,
            })
        return out

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

    # ── presentation, from the template's `display` block ─────────────────────
    def _display_of(dev) -> Dict:
        from .display import display_of
        return display_of(template_registry, dev)

    def _unit_alarms(dev) -> Dict:
        """What the unit itself says is wrong — the template-declared alarm
        fields that are active now (fresh values only)."""
        from .display import active_alarms
        return active_alarms(_display_of(dev),
                             list((registry.store_for(dev.id) or {}).values()))

    def _glance_names(dev) -> tuple:
        names = _display_of(dev).get('glance')
        if names:
            return tuple(names)
        return _ROLE_LIVE.get(getattr(dev, 'role', '') or '', ('power_active_total',))

    def _store_meta(devs) -> Dict[str, Dict[str, str]]:
        """label/unit per field name, as the units' registers carry them."""
        meta: Dict[str, Dict[str, str]] = {}
        for dev in devs:
            for e in list((registry.store_for(dev.id) or {}).values()):
                n = e.get('name')
                if n and n not in meta:
                    meta[n] = {'label': e.get('label') or n, 'unit': e.get('unit') or ''}
        return meta

    def _field_meta_any(name: str, smeta: Dict) -> Optional[Dict]:
        # what the unit itself says (its row's label and unit) first; the
        # canonical dictionary fills in only what the row does not say
        from .canonical_fields import field_meta
        own, canon = smeta.get(name) or {}, field_meta(name) or {}
        if not own and not canon:
            return None
        mine = {k: v for k, v in own.items() if v not in (None, '')}
        if mine.get('label') == name and canon.get('label'):
            mine.pop('label')          # a bare name is no label; the dictionary's is
        return {**canon, **mine}

    _OP_WORD = {'sum': 'total', 'avg': 'average', 'min': 'min', 'max': 'max',
                'spread': 'spread', 'mode': ''}

    def _aggregate_meta(pid: str, devs, agg: Dict, display: Dict) -> tuple:
        """label/unit for every total of a group — canonical names keep their
        vocabulary; a template- or operator-declared total inherits its source
        field's label and unit, with the operation spelled out; the template's
        headline labels win. Also the declaration ORDER (template first)."""
        from .canonical_fields import field_meta
        from .endpoint_aggregator import endpoint_totals
        smeta = _store_meta(devs)
        meta: Dict[str, Dict[str, str]] = {}
        order: List[str] = []

        def put(out, srcs, op):
            if out in meta:
                return
            sm = smeta.get(srcs[0] if srcs else '') or {}
            word = _OP_WORD.get(op, op)
            if len(srcs) == 1:
                base = sm.get('label') or srcs[0]
                label = f"{base} ({word})" if word else base
            else:
                # pooled over several fields: no one field names it — its own
                # name does ("pack_min_temp" → "Pack min temp")
                label = out.replace('_', ' ').strip().capitalize()
            meta[out] = {'label': label,
                         'unit': '' if op == 'mode' else (sm.get('unit') or '')}
            order.append(out)
        own = endpoint_totals(config, pid)
        # the headline's totals first, then the template's declaration order,
        # then the operator's own totals
        heads = [h.get('field') for h in (display.get('headline') or []) if h.get('field')]
        declared: Dict[str, tuple] = {}
        for dev in devs:
            for e in list((registry.store_for(dev.id) or {}).values()):
                for out, op in (e.get('aggregates') or {}).items():
                    declared.setdefault(str(out), ([], op))[0].append(e.get('name') or '')
        for out in heads:
            if out in own:
                put(out, own[out].get('from') or [], own[out].get('op'))
            elif out in declared:
                put(out, sorted(set(declared[out][0])), declared[out][1])
        for out, (srcs, op) in declared.items():
            if out in own:
                put(out, own[out].get('from') or [], own[out].get('op'))
            else:
                put(out, sorted(set(srcs)), op)
        for out, t in own.items():
            put(out, t.get('from') or [], t.get('op'))
        for n in agg:
            if n not in meta:
                m = field_meta(n)
                if m:
                    meta[n] = m
                order.append(n)
        for h in (display.get('headline') or []):
            f = h.get('field')
            if f in meta and h.get('label'):
                meta[f] = dict(meta[f], label=h['label'])
        return meta, [n for n in order if n in agg]

    def _headline_items(pid: str, devs, agg: Dict, display: Dict) -> List[Dict]:
        meta, _o = _aggregate_meta(pid, devs, agg, display)
        out = []
        for h in (display.get('headline') or []):
            f = h.get('field')
            if f in agg:
                m = meta.get(f) or {}
                out.append({'field': f, 'label': h.get('label') or m.get('label') or f,
                            'hint': h.get('hint', ''), 'unit': m.get('unit', ''),
                            'value': agg[f]})
        return out

    def _endpoint_group_entries(p: Dict, pid: str, units: List[Dict]) -> List[Dict]:
        """One entry per unit group, with its units, its sources and its own
        total. The FIRST group owns the endpoint's headline aggregate, which is
        what keeps every topic and series that predates groups unchanged."""
        from .endpoint_aggregator import (aggregate_topic,
                                          compute_endpoint_aggregates)
        out = []
        for i, g in enumerate(config._endpoint_groups(p)):
            gid = g['id']
            try:
                agg = compute_endpoint_aggregates(config, registry, pid,
                                                  group_id=gid)
            except Exception:  # noqa: BLE001 — a total must not break the page
                agg = {}
            mine = [u for u in units if u.get('group_id') == gid]
            # a total of one unit is that unit again under another name — the
            # aggregator does not publish it, so the page must not promise it
            one = len(mine) < 2
            gm = dict(p.get('mqtt') or {}, **(g.get('mqtt') or {}))
            gx = dict(p.get('influxdb') or {}, **(g.get('influxdb') or {}))
            gdevs = [d for d in config.endpoint_devices(pid)
                     if (getattr(d, 'group_id', '') or 'units') == gid]
            disp = _display_of(gdevs[0]) if gdevs else {}
            ameta, aorder = _aggregate_meta(pid, gdevs, agg, disp) if not one else ({}, [])
            out.append({
                'id': gid,
                'role': g.get('role', '') or '',
                # how the template presents its units (absent → role defaults)
                'unit_label': disp.get('unit_label', ''),
                'unit_label_plural': disp.get('unit_label_plural', ''),
                'icon': disp.get('icon', ''),
                'glance': list(_glance_names(gdevs[0])) if gdevs else [],
                'headline_items': [] if one else _headline_items(pid, gdevs, agg, disp),
                'enabled': bool(g.get('enabled', True)),
                'template': g.get('template', '') or p.get('template', ''),
                # the group's own, '' when it takes the installation's: an
                # editor writes back this one, never the effective template
                # (writing that down froze an inherited one into the group)
                'template_declared': g.get('template', '') or '',
                'units': mine,
                # a group that declares no connection and no sources is read the
                # way the installation is; an editor must not write that back as
                # the group's own (it did, and lost a serial port's settings)
                'inherits': not (g.get('sources') or g.get('connection')),
                # how it is declared, so an editor writes it back in the same
                # shape (a tap bus is a group connection, never a source)
                'route': 'sources' if g.get('sources') else 'connection' if g.get('connection') else 'inherit',
                'sources': _endpoint_sources(p, pid, g if (g.get('sources') or g.get('connection')) else p, gid),
                # what the group's units can be told — the template's presets,
                # each enabled by a binding on the group (raw binding kept so
                # the editor round-trips faces / lease untouched)
                'commands': _group_commands(pid, gid),
                # the group's own channel overrides, as stored: the editor
                # writes them back (ADR-0019)
                'channels': {k: dict(v) for k, v in (g.get('channels') or {}).items() if isinstance(v, dict)},
                'cmd_topic_prefix': _group_cmd_prefix(pid, gid, [d for d in config.endpoint_devices(pid)
                                                                 if (getattr(d, 'group_id', '') or 'units') == gid]),
                'aggregates': {} if one else agg,
                'aggregate_fields': ameta,
                'aggregate_order': aorder,
                'online_units': sum(1 for u in mine if u['connected']),
                'total_units': len(mine),
                # the bare endpoint path belongs to the first group; the others
                # publish under their own name
                'topic': None if one else aggregate_topic(p, pid, gid, i == 0),
                # where THIS group's units publish — the page states the real
                # paths, not the endpoint default no group may be using
                'outputs': {'topic_prefix': gm.get('topic_prefix', ''),
                            'bucket': gx.get('bucket', ''),
                            'device_tag': gx.get('device_tag', '')},
            })
        return out

    # What a unit row shows at a glance, by what the unit IS. An inverter is
    # judged by its power and its two voltages, a meter by power and the two
    # energy directions, the site by the balance the datalogger computes.
    _ROLE_LIVE = {
        'inverter': ('power_active_total', 'voltage_ln_avg', 'voltage_dc', 'power_limit_pct'),
        'meter': ('power_active_total', 'energy_active_import', 'energy_active_export'),
        'site': ('power_pv', 'power_load', 'power_grid', 'autonomy',
                 'self_consumption', 'energy_today'),
        'battery': ('power_active_total', 'voltage_dc'),
    }
    _LIVE_FALLBACK = {'voltage_ln_avg': ('voltage_l1',)}

    def _unit_live(dev, names, now: float = None) -> Dict[str, Any]:
        """``{field: value}`` for the named canonical fields of one unit, FRESH
        values only — the same freshness rule the aggregator applies, so a row
        never shows a number the total has already dropped."""
        now = now if now is not None else time.time()
        store = registry.store_for(dev.id) or {}
        by_name = {}
        for entry in list(store.values()):
            n, v, ts = entry.get('name'), entry.get('value'), entry.get('ts')
            # numbers, and text a template asks to glance at (an enum's word:
            # a pack's "Charge")
            if not n or isinstance(v, bool) or not (
                    isinstance(v, (int, float)) or (isinstance(v, str) and v.strip())):
                continue
            interval = float(entry.get('interval') or 30)
            if ts is not None and (now - ts) <= max(4 * interval, 60.0):
                by_name[n] = v
        out = {}
        for n in names:
            for cand in (n,) + _LIVE_FALLBACK.get(n, ()):
                if cand in by_name:
                    out[n] = by_name[cand]
                    break
        return out

    def _endpoint_totals_view(pid: str) -> Dict:
        """The template-declared totals as the units actually carry them
        ({name: {op, from}}) and the union of the units' field names."""
        declared: Dict[str, Dict] = {}
        fields: Set[str] = set()
        for dev in config.endpoint_devices(pid):
            for entry in list((registry.store_for(dev.id) or {}).values()):
                name = entry.get('name')
                if not name:
                    continue
                fields.add(name)
                for out, op in (entry.get('aggregates') or {}).items():
                    d = declared.setdefault(str(out), {'op': op, 'from': []})
                    if name not in d['from']:
                        d['from'].append(name)
        return {'totals_declared': declared, 'unit_fields': sorted(fields)}

    def _validate_totals(t, errors: List[str]) -> None:
        if t in (None, {}):
            return
        if not isinstance(t, dict) or len(t) > 200:
            errors.append("totals: a mapping of name: {op, from} (at most 200)")
            return
        for name, d in t.items():
            w = f"totals.{name}"
            if not _INFLUX_NAME_RE.match(str(name)):
                errors.append(f"{w}: names start with a letter (letters, digits, _ . -)")
            if not isinstance(d, dict):
                errors.append(f"{w}: must be {{op, from}}")
                continue
            if d.get('op') not in ('sum', 'avg', 'min', 'max', 'spread', 'mode'):
                errors.append(f"{w}.op: sum, avg, min, max, spread or mode")
            src = d.get('from')
            if not isinstance(src, list) or not [x for x in src if str(x).strip()]:
                errors.append(f"{w}.from: at least one unit field")

    def _template_update_summary(pid: str) -> Dict:
        from .template_refresh import plan_devices
        try:
            plan = plan_devices(config, template_registry, config.endpoint_devices(pid))
        except Exception:  # noqa: BLE001 — a status view must never 500 on this
            return {'pending': False, 'changes': 0, 'new': 0, 'missing_templates': []}
        return {'pending': plan['pending'], 'changes': len(plan['changes']),
                'new': len(plan['new']), 'units_affected': plan['units_affected'],
                'missing_templates': plan['missing_templates']}

    def _endpoint_entry(p: Dict) -> Dict:
        pid = p.get('id')
        units = []
        now = time.time()
        for dev in config.endpoint_devices(pid):
            _i, _cfg, client = registry.find(dev.id)
            st, src_verdicts = {}, {}
            if client is not None:
                try:
                    st = client.get_stats() or {}
                except Exception:  # noqa: BLE001 — a stats blip must not 500 the list
                    st = {}
                try:
                    src_verdicts = dict(client.data_health().get('sources') or {})
                except Exception:  # noqa: BLE001
                    src_verdicts = {}
            _seen = st.get('last_success_ts')
            _groups = st.get('poll_groups_detail') or []
            units.append({
                'unit_id': dev.connection.unit_id,
                'device_id': dev.id,
                'name': dev.name,
                'enabled': dev.enabled,
                'group_id': getattr(dev, 'group_id', '') or 'units',
                'role': getattr(dev, 'role', '') or '',
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
                # what the unit shows at a glance, and each source's verdict
                # for it — "read fine over HTTP, Modbus side dead" in one row
                'live': _unit_live(dev, _glance_names(dev), now),
                'alarms': _unit_alarms(dev),
                'sources': src_verdicts,
            })
        from .canonical_fields import field_meta
        from .endpoint_aggregator import compute_endpoint_aggregates
        grouped = bool(p.get('groups'))
        agg = {}
        if not grouped:
            # a flat endpoint IS one group, so its total is meaningful; a
            # grouped installation has no whole-installation sum — inverter
            # generation added to a meter's import describes nothing
            try:
                agg = compute_endpoint_aggregates(config, registry, pid)
            except Exception:  # noqa: BLE001 — aggregates must never break the list
                agg = {}
        groups = _endpoint_group_entries(p, pid, units)
        bus = _endpoint_bus(p, units)
        # the four numbers at the top: the site's balance when the datalogger
        # gives one, else the inverters' sum and nothing invented for the rest
        headline = {'power_now': None, 'energy_today': None,
                    'autonomy': None, 'self_consumption': None}
        site = next((u for u in units if u.get('role') == 'site'
                     and (u.get('live') or {}).get('power_pv') is not None), None)
        if site is not None:
            lv = site['live']
            headline.update({'power_now': lv.get('power_pv'),
                             'energy_today': lv.get('energy_today'),
                             'autonomy': lv.get('autonomy'),
                             'self_consumption': lv.get('self_consumption')})
        else:
            # the inverters' fresh powers, summed — the same values their rows
            # show, so the top number and the rows can never disagree
            pw = [(u.get('live') or {}).get('power_active_total') for u in units
                  if u.get('role') == 'inverter']
            pw = [x for x in pw if isinstance(x, (int, float))]
            if pw:
                headline['power_now'] = round(sum(pw), 3)
            elif not grouped and isinstance(agg.get('power_active_total'), (int, float)):
                headline['power_now'] = agg['power_active_total']
        # every distinct way the installation is read, rolled up across groups
        read_via: Dict[tuple, Dict] = {}
        for g in groups:
            for sx in g.get('sources') or []:
                key = (sx['id'], sx['protocol'], sx.get('address') or '')
                r = read_via.setdefault(key, {
                    'id': sx['id'], 'protocol': sx['protocol'],
                    'address': sx.get('address') or '', 'interval_s': None,
                    'units_ok': 0, 'units_total': 0, 'reads_5m': 0, 'failed_5m': 0,
                    '_lat': [], 'groups': []})
                r['groups'].append(g['id'])
                r['units_ok'] += sx.get('units_ok') or 0
                r['units_total'] += sx.get('units_total') or 0
                r['reads_5m'] += sx.get('reads_5m') or 0
                r['failed_5m'] += sx.get('failed_5m') or 0
                if sx.get('latency_ms') is not None:
                    r['_lat'].append(sx['latency_ms'])
                iv = sx.get('interval_s')
                if iv is not None and (r['interval_s'] is None or iv < r['interval_s']):
                    r['interval_s'] = iv
        for r in read_via.values():
            lat = r.pop('_lat')
            r['latency_ms'] = round(sum(lat) / len(lat), 1) if lat else None
            r['fail_pct_5m'] = (round(100.0 * r['failed_5m'] / r['reads_5m'], 1)
                                if r['reads_5m'] else None)
            r['status'] = ('down' if not r['units_ok'] else
                           'ok' if r['units_ok'] == r['units_total'] else 'degraded') \
                if r['units_total'] else 'idle'
        live_names = {n for u in units for n in (u.get('live') or {})}
        _ep_smeta = _store_meta(config.endpoint_devices(pid))
        online = sum(1 for u in units if u['connected'])
        # inverters asleep at night are not missing (endpoint_aggregator)
        asleep = int(agg.get('units_asleep') or 0) + sum(int((g.get('aggregates') or {}).get('units_asleep') or 0)
                                                         for g in groups)
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
                # (a one-unit installation's group has no meta: fall back, so the
                # field keeps the shape it had before groups carried their own)
                'aggregate_fields': ((groups[0].get('aggregate_fields') if (groups and not grouped) else None)
                                     or {k: m for k, m in ((k, field_meta(k)) for k in agg) if m}),
                'aggregate_order': (groups[0].get('aggregate_order') or []) if (groups and not grouped) else [],
                # how the totals are made, in the open: the operator's own
                # (`totals`), what the units' templates declare, and every field
                # the units carry — what a total can be built from
                'totals': dict(p.get('totals') or {}),
                **_endpoint_totals_view(pid),
                # the endpoint's own settings, so the page can render + edit them
                'aggregates_enabled': bool(p.get('aggregates', True)),
                'write_locked': bool(p.get('write_locked', False)),
                'http_output_enabled': bool((p.get('http_output') or {}).get('enabled')),
                'rest_push': dict(p.get('rest_push', {}) or {}),
                'bus': bus,
                'sources': _endpoint_sources(p, pid),
                # An installation is not one kind of thing: a plant holds
                # inverters and the meter at its grid connection. Each group
                # carries its own units, sources and total, because adding a
                # meter's power to the inverters' would describe nothing.
                'groups': groups,
                'headline': headline,
                'read_via': list(read_via.values()),
                # label/unit per live field name, so a row can say "DC voltage
                # 636 V" without re-deriving the vocabulary client-side
                'fields': {n: m for n, m in ((n, _field_meta_any(n, _ep_smeta))
                                             for n in live_names) if m},
                # the template's headline for the installation, from its totals
                # (the first group that declares one) — beside the legacy
                # power/energy/autonomy headline the page falls back to
                'headline_items': next((g['headline_items'] for g in groups
                                        if g.get('headline_items')), []),
                # units whose own declared alarm fields are active right now
                'units_alarming': sum(1 for u in units
                                      if (u.get('alarms') or {}).get('danger')
                                      or (u.get('alarms') or {}).get('warning')),
                # the template has something to bring in (the page offers the
                # update only then); details at /template-changes
                'template_update': _template_update_summary(pid),
                'status': ('' if not units else 'online' if online == len(units)
                           else 'asleep' if online == 0 and asleep == len(units)
                           else 'online' if online + asleep == len(units)
                           else 'offline' if online == 0 else 'partial'),
                'online_units': online,
                'asleep_units': asleep,
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

    def _validate_commands(cmds, errors, prefix: str = "", tpl=None, safe_def=None, warnings=None, stored=None):
        """Bindings: `{name}` enables a template preset; one that carries
        `writes` is an inline recipe and must parse as a command. A `safe`
        is checked through the command's own bounds — a revert that would
        fail only when the dead-man fires is refused now (CMD-Q-11); a lease
        without any safe value is said in ``warnings`` (stored configs keep
        loading, ADR-0019)."""
        if cmds is None:
            return
        if not isinstance(cmds, list):
            errors.append(f"{prefix}commands: must be a list")
            return
        seen = set()
        for i, b in enumerate(cmds):
            w = f"{prefix}commands[{i}]"
            if not isinstance(b, dict):
                errors.append(f"{w}: must be an object")
                continue
            name = str(b.get('name', '') or '')
            if not re.match(r'^[a-z][a-z0-9_]{0,31}$', name):
                errors.append(f"{w}.name: use a-z 0-9 _ (1-32 chars, starts with a letter)")
            elif name in seen:
                errors.append(f"{w}.name: '{name}' is declared twice")
            seen.add(name)
            for k in ('enabled', 'confirm'):
                if k in b and not isinstance(b[k], bool):
                    errors.append(f"{w}.{k}: must be true or false")
            if 'faces' in b and (not isinstance(b['faces'], dict)
                                 or set(b['faces']) - {'api', 'mqtt', 'ha'}
                                 or not all(isinstance(v, bool) for v in b['faces'].values())):
                errors.append(f"{w}.faces: an object of api / mqtt / ha → true|false")
            if 'lease_s' in b and not (isinstance(b['lease_s'], int) and not isinstance(b['lease_s'], bool)
                                       and 0 <= b['lease_s'] <= 86400):
                errors.append(f"{w}.lease_s: 0..86400 seconds")
            cdef = safe_def
            if b.get('writes') is not None:
                try:
                    cdef = parse_command_def(name or 'x', b)
                except (ValueError, TypeError, KeyError) as e:
                    errors.append(f"{w}: {e}")
            elif cdef is None and tpl is not None and name in (getattr(tpl, 'commands', None) or {}):
                try:
                    cdef = parse_command_def(name, tpl.commands[name])
                except (ValueError, TypeError, KeyError):
                    cdef = None
            safe = b.get('safe')
            # a stored safe that this save does not change is not judged again:
            # a template update that moved a bound must not block an unrelated
            # save (ADR-0006, CR2-5)
            kept = next((x.get('safe') for x in (stored or []) if isinstance(x, dict)
                         and x.get('name') == name), None)
            if safe is not None and not (stored is not None and safe == kept):
                if not isinstance(safe, dict) or not safe:
                    errors.append(f"{w}.safe: an object {{param: value}}, e.g. {{value: 0}}")
                elif cdef is not None:
                    try:
                        # every parameter it names exists and is within the
                        # command's bounds, as a run would be judged
                        normalize_params(cdef, {**{k: v.default for k, v in cdef.params.items()
                                                   if v.default is not None},
                                                **(cdef.safe or {}), **safe})
                    except ValueError as e:
                        errors.append(f"{w}.safe: {e}")
            if isinstance(b.get('lease_s'), int) and b['lease_s'] > 0 \
                    and not safe and not (cdef is not None and cdef.safe):
                msg = f"{w}: lease_s {b['lease_s']} without a safe value never fires"
                if warnings is not None:
                    warnings.append(msg)
                else:
                    logger.warning("command binding: %s", msg)

    def _validate_channels(chs, errors, prefix: str = "", tpl=None, stored=None, units=None):
        """Channel overrides `{<channel or command name>: {min_interval_s, …}}`
        (ADR-0019). The keys are judged against the template only when the
        block changes (ADR-0006): a template update that drops a channel
        leaves an orphan, listed, never a save that fails."""
        if chs is None:
            return
        if not isinstance(chs, dict):
            errors.append(f"{prefix}channels: an object {{channel: {{{', '.join(CHANNEL_OVERRIDE_KEYS)}}}}}")
            return
        for key, val in chs.items():
            for k, why in _channel_override_errors(val).items():
                errors.append(f"{prefix}channels.{key}.{k}: {why}" if k != 'channel'
                              else f"{prefix}channels.{key}: {why}")
        if tpl is not None and chs != (stored or {}):
            defs = {}
            for n, raw in (getattr(tpl, 'commands', None) or {}).items():
                try:
                    defs[n] = parse_command_def(n, raw)
                except (ValueError, TypeError, KeyError):
                    continue
            declared = {d.channel for d in defs.values() if d.channel}
            ids = assign_channels(defs) if defs else {}
            taken: Dict[str, str] = {}
            for key in sorted(chs, key=lambda k: (k not in declared, k)):
                if key not in declared and key not in defs:
                    errors.append(f"{prefix}channels.{key}: not a channel or a command of template '{tpl.id}'")
                    continue
                cid = _override_cid(key, defs, ids)
                if cid in taken:
                    # one channel, one key (CR2-14)
                    errors.append(f"{prefix}channels.{key}: channel '{cid}' is already overridden under "
                                  f"'{taken[cid]}'")
                else:
                    taken[cid] = key
                # a pace this save sets on a channel an armed steps rule with a
                # fast step drives on one of these units: refused (ADR-0022, Q5)
                val, before = chs.get(key), ((stored or {}).get(key) or {})
                paced = sorted(k for k in PACE_KEYS if isinstance(val, dict) and val.get(k)
                               and val.get(k) != (before.get(k) if isinstance(before, dict) else None))
                _rt = getattr(app.state, 'rules_runtime', None)
                for dev in (units or []) if (paced and _rt is not None) else []:
                    rid = _rt.fast_rule_on(dev, cid)
                    if rid is not None:
                        errors.append(f"{prefix}channels.{key}: channel '{cid}' of {dev} is driven by the armed "
                                      f"rule '{rid}', a steps rule with a fast step — a pace ({', '.join(paced)}) "
                                      f"would hold its other reasserts while its emergency step passes it; put "
                                      f"the rule in shadow or remove `fast` first")
                        break

    _HTTP_URL_RE = re.compile(r'^https?://\S+$')

    def _http_url_error(url, stored, w: str):
        """An http URL that is not one: the Add group form sent a Modbus host
        as the URL. Judged only when the URL is new or changed — a stored one
        never blocks a save that does not touch it (an installation written
        before this check must still be renamed, or saved unchanged)."""
        url = str(url or '').strip()
        if not url or url == str(stored or '').strip() or _HTTP_URL_RE.match(url):
            return None
        return (f"{w}: '{url}' is not an http(s) URL — a Modbus host goes in host, "
                f"with protocol tcp")

    def _validate_sources(srcs, errors, inherit_template: str, prefix: str = "", stored=None):
        """Validate one ordered source list. Shared by an endpoint's flat form
        and by every group, so a rule written once applies everywhere.
        `stored`: the same list as saved, for the checks that judge only what
        changed."""
        if srcs is None:
            return
        stored_by = {str(x.get('id', '')).strip().lower(): x for x in (stored or []) if isinstance(x, dict)}
        if not isinstance(srcs, list) or not srcs:
            errors.append(f"{prefix}sources: must be a non-empty list")
            return
        seen = set()
        _SID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,31}$')
        for i, r in enumerate(srcs):
            w = f"{prefix}sources[{i}]"
            if not isinstance(r, dict):
                errors.append(f"{w}: must be an object")
                continue
            sid = str(r.get('id', '') or '').strip().lower()
            if not _SID_RE.match(sid):
                errors.append(f"{w}.id: use a-z 0-9 - _ (1-32 chars)")
            elif sid in seen:
                # two sources with one name make provenance ambiguous, which
                # defeats the point of recording it
                errors.append(f"{w}.id: '{sid}' is declared twice")
            seen.add(sid)
            proto = str(r.get('protocol', 'tcp')).lower()
            if proto not in PROTOCOLS_BY_CONTEXT['source']:
                errors.append(f"{w}.protocol: must be one of {', '.join(PROTOCOLS_BY_CONTEXT['source'])}")
            if proto == 'http':
                if not str(r.get('url', '')).strip():
                    errors.append(f"{w}.url: required for an http source")
                else:
                    e = _http_url_error(r.get('url'), (stored_by.get(sid) or {}).get('url'), f"{w}.url")
                    if e:
                        errors.append(e)
            elif proto == 'mqtt':
                if not str(r.get('topic', '')).strip():
                    errors.append(f"{w}.topic: required for an mqtt source")
            elif proto not in SERIAL_PROTOCOLS and not str(r.get('host', '')).strip():
                errors.append(f"{w}.host: required")
            tid = str(r.get('template', '') or inherit_template).strip()
            if not tid:
                errors.append(f"{w}.template: required (nothing to inherit)")
            elif template_registry.get(tid) is None:
                errors.append(f"{w}.template: '{tid}' not found")
            try:
                if float(r.get('stale_after_s', 0) or 0) < 0:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f"{w}.stale_after_s: must be >= 0 "
                              f"(0 = never yields, right for a counter)")
            for gname, g in (r.get('poll_groups') or {}).items():
                iv = g.get('interval') if isinstance(g, dict) else g
                try:
                    if float(iv) < 0.05:
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append(f"{w}.poll_groups.{gname}: interval must be "
                                  f"a number >= 0.05")

    def _validate_group_connection(conn: Dict, errors: List[str], w: str, stored=None) -> None:
        """A group's own connection, checked like the installation's. A serial
        device path is never a network host: that mistake made a tap open a
        TCP connection to "/dev/ttySEPLOS:502" and the batteries went dark."""
        protocol = str(conn.get('protocol', 'tcp')).lower()
        allowed = PROTOCOLS_BY_CONTEXT['group']
        if protocol not in allowed:
            errors.append(f"{w}.connection.protocol: must be one of " + ", ".join(allowed))
        host = str(conn.get('host', '') or '').strip()
        if _is_serial_path(host):
            errors.append(_serial_host_error(f"{w}.connection.host", host))
        if protocol == 'http':
            if not str(conn.get('url', '')).strip():
                errors.append(f"{w}.connection.url: required for http")
            else:
                e = _http_url_error(conn.get('url'), (stored or {}).get('url'), f"{w}.connection.url")
                if e:
                    errors.append(e)
        elif protocol == 'rtu_tap':
            if not str(conn.get('serial_port', '')).strip() and not host:
                errors.append(f"{w}.connection: rtu_tap needs a serial_port (or a bridge host)")
        elif not host:
            errors.append(f"{w}.connection.host: required")

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
        prev = config.get_raw_endpoint(existing_id) if existing_id else None
        if prev is not None:
            # An edit REPLACES the stored entry. A form that does not speak of
            # groups or sources — the settings dialog of an installation — keeps
            # the stored ones: taken literally, that same form flattened a plant
            # to one Modbus group, dropped every source and renamed its site
            # unit, on a save meant to change the name.
            if 'groups' not in payload and prev.get('groups'):
                groups = [dict(g) for g in prev['groups']]
                # a rename sent the flat way — `units: [{unit_id, id, name}]` —
                # lands on the unit inside its group; the rest of the flat list
                # is discarded, because membership is the groups' to say
                named = {u['unit_id']: u for u in (payload.get('units') or [])
                         if isinstance(u, dict) and 'unit_id' in u and 'name' in u}
                if named:
                    for g in groups:
                        gu = []
                        for u in config._endpoint_units({'id': pid}, g):
                            hit = named.get(u['unit_id'])
                            item = {'unit_id': u['unit_id'], 'id': u['id']}
                            name = hit['name'] if hit else u.get('name')
                            if name:
                                item['name'] = name
                            gu.append(item)
                        g['units'] = gu
                payload = {**payload, 'groups': groups}
                payload.pop('units', None)          # units live in the groups
            if 'sources' not in payload and prev.get('sources'):
                payload = {**payload, 'sources': prev['sources']}
            # same rule for FLAT units: a settings dialog that does not list
            # them must not dissolve the endpoint's membership
            if 'units' not in payload and prev.get('units'):
                payload = {**payload, 'units': prev['units']}
        conn = payload.get('connection', {}) or {}
        # A form that does not SPEAK of the connection keeps the stored one —
        # the settings dialog saved with an empty conn and wiped the serial
        # port out from under 8 running units (seplos cutover, 2026-10-07).
        if prev is not None and not conn and prev.get('connection'):
            conn = dict(prev['connection'])
        # An endpoint that declares GROUPS carries its address per group — each
        # holds a different kind of thing and may be reached a different way. The
        # top-level connection is then only the shorthand for a single implicit
        # group, so it is required exactly when no group says otherwise.
        _grouped = bool(payload.get('groups'))
        if not _grouped:
            protocol = str(conn.get('protocol', 'tcp')).lower()
            if protocol not in PROTOCOLS_BY_CONTEXT['endpoint']:
                # a serial line here is read by one device per slave (Devices);
                # an installation reaches its units through a network master
                # or listens (rtu_tap: it masters nothing, and a multi-drop
                # bus is exactly what it is for)
                errors.append("connection.protocol: must be one of " + ", ".join(PROTOCOLS_BY_CONTEXT['endpoint']))
            if protocol == 'rtu_tap':
                if not str(conn.get('serial_port', '')).strip():
                    errors.append("connection.serial_port: required for rtu_tap")
            elif not str(conn.get('host', '')).strip():
                errors.append("connection.host: required")
            if str(conn.get('host', '') or '').strip().startswith('/'):
                errors.append(f"connection.host: '{conn.get('host')}' is a serial port, not a "
                              f"network address — it goes in serial_port")
        if conn:
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
        # a flat installation's role: a type from the catalog, judged when it
        # changes (ADR-0027, ADR-0006); a hand-written one stored is kept
        from .device_types import role_error as _flat_role_error
        _frerr = _flat_role_error(payload.get('role'), (prev or {}).get('role'))
        if _frerr:
            errors.append(f"role: {_frerr}")
        # ── groups: the kinds of thing this installation holds ──
        groups = payload.get('groups')
        if groups is not None:
            if not isinstance(groups, list) or not groups:
                errors.append("groups: must be a non-empty list")
                groups = []
            seen_gid, seen_uid_all = set(), set()
            _GID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,31}$')
            stored_groups = {str(x.get('id', '')).strip().lower(): x
                             for x in ((prev or {}).get('groups') or []) if isinstance(x, dict)}
            stored_template = str((prev or {}).get('template', '') or '').strip()
            for gi, g in enumerate(groups):
                w = f"groups[{gi}]"
                if not isinstance(g, dict):
                    errors.append(f"{w}: must be an object")
                    continue
                gid = str(g.get('id', '') or '').strip().lower()
                if not _GID_RE.match(gid):
                    errors.append(f"{w}.id: use a-z 0-9 - _ (1-32 chars)")
                elif gid in seen_gid:
                    errors.append(f"{w}.id: '{gid}' is declared twice")
                seen_gid.add(gid)
                gunits = config._endpoint_units({'id': pid}, g)
                if not gunits:
                    errors.append(f"{w}.units: at least one valid unit id "
                                  f"(0..255) is required")
                for u in gunits:
                    # a unit id is the address on the wire: the same one in two
                    # groups of one endpoint would be two devices racing it
                    if u['unit_id'] in seen_uid_all:
                        errors.append(f"{w}.units: unit {u['unit_id']} is "
                                      f"already used by another group")
                    seen_uid_all.add(u['unit_id'])
                gt = str(g.get('template', '') or template_id).strip()
                if gt and template_registry.get(gt) is None:
                    errors.append(f"{w}.template: '{gt}' not found")
                sg = stored_groups.get(gid)
                # its role: a type from the catalog, judged when it changes (ADR-0027)
                from .device_types import role_error as _role_error
                _rerr = _role_error(g.get('role'), (sg or {}).get('role'))
                if _rerr:
                    errors.append(f"{w}.role: {_rerr}")
                # read through a connection (its own or the installation's)
                # with no map anywhere: a device with nothing to read. Judged
                # on a new group, or when its way in or its map changes
                if not gt and (g.get('connection') or (not g.get('sources') and not payload.get('sources'))) \
                        and (sg is None or g.get('connection') != sg.get('connection')
                             or str(g.get('template', '') or '') != str(sg.get('template', '') or '')
                             or template_id != stored_template):
                    errors.append(f"{w}.template: required — the group's or the installation's")
                if g.get('connection'):
                    _validate_group_connection(dict(g['connection']), errors, w,
                                               stored=(sg or {}).get('connection'))
                elif not g.get('sources') and not conn and not payload.get('sources'):
                    errors.append(f"{w}: no way to reach its units — give the group a "
                                  f"connection or sources, or the installation one")
                _validate_sources(g.get('sources'), errors, gt, prefix=w + '.',
                                  stored=(sg or {}).get('sources'))
                # the commands' template: the one a command writes through —
                # a Modbus source's, else an MQTT one's, else the group's (as
                # _modbus_template_of); a group read through sources has no
                # template of its own and its `safe` went unchecked (CR2-5)
                _ctid = next((str(s.get('template')) for proto in (_POLLED_MODBUS, ('mqtt',))
                              for s in (g.get('sources') or payload.get('sources') or [])
                              if isinstance(s, dict) and s.get('template')
                              and str(s.get('protocol') or 'tcp').lower() in proto), '') or gt
                _ctpl = template_registry.get(_ctid) if _ctid else None
                _validate_commands(g.get('commands'), errors, prefix=w + '.', tpl=_ctpl,
                                   stored=(sg or {}).get('commands'))
                _validate_channels(g.get('channels'), errors, prefix=w + '.', tpl=_ctpl,
                                   stored=(sg or {}).get('channels'),
                                   units=[u['id'] for u in (gunits or []) if isinstance(u, dict) and u.get('id')])
        # ── sources: the ordered ways of reaching this endpoint's units ──
        srcs = payload.get('sources')
        _validate_sources(srcs, errors, template_id, stored=(prev or {}).get('sources'))
        _validate_commands(payload.get('commands'), errors,
                           tpl=template_registry.get(template_id) if template_id else None,
                           stored=(prev or {}).get('commands'))
        # an installation without groups: its units' channel overrides live on
        # it, as their bindings do (CR2-4)
        _validate_channels(payload.get('channels'), errors,
                           tpl=template_registry.get(template_id) if template_id else None,
                           stored=(prev or {}).get('channels'),
                           units=[u['id'] for u in config._endpoint_units({'id': pid, 'units': payload.get('units')})
                                  if isinstance(u, dict) and u.get('id')])
        probe = {'id': pid, 'units': payload.get('units')}
        units = config._endpoint_units(probe)
        if not units and not _grouped:
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
        raw = {
            'id': pid,
            'name': str(payload.get('name', '') or pid),
            'template': template_id,
            'enabled': flag(payload, 'enabled', True),
            'connection': conn,
            'units': _merge_unit_overrides(pid, payload.get('units'), prev),
            **({'sources': srcs} if srcs else {}),
            **({'groups': groups} if groups else {}),
            **({'commands': payload['commands']} if payload.get('commands') else {}),
            **({'channels': payload['channels']} if payload.get('channels') else {}),
            # a flat installation's role: a catalog type when it changes (ADR-0027)
            **({'role': payload['role']} if isinstance(payload.get('role'), str) and payload['role'] else {}),
        }
        if payload.get('aggregates') is not None:
            raw['aggregates'] = flag(payload, 'aggregates')
        for opt in ('mqtt', 'influxdb'):
            if payload.get(opt):
                raw[opt] = dict(payload[opt])
        if 'opcua' in payload:
            raw['opcua'] = _opcua_block(payload['opcua'])
        if 'totals' in payload:
            _terr: List[str] = []
            _validate_totals(payload['totals'], _terr)
            if _terr:
                raise HTTPException(status_code=422, detail={"errors": _terr})
            if payload['totals']:
                raw['totals'] = {str(k): {'op': v['op'],
                                          'from': [str(x) for x in v['from'] if str(x).strip()]}
                                 for k, v in payload['totals'].items()}
        if prev is not None:
            # An edit REPLACES the stored entry, so anything the endpoint owns but
            # the form does not send would vanish: a locked endpoint silently
            # unlocked itself and `aggregates: false` came back on, one save
            # after the operator set them.
            for key in ('write_locked', 'aggregates', 'http_output', 'rest_push', 'opcua', 'role'):
                if key not in raw and key in prev:
                    raw[key] = prev[key]
            # What a group owns but the group form does not edit — its own
            # routing (a meter on `pv/meter/grid`, not `pv/units/240`) and its
            # OPC UA choice (a unit's Outputs tab) — is kept when the form
            # rebuilds the group without it. Dropping the routing moved a
            # group's topics and series on an unrelated save.
            _prev_groups = {str(g.get('id')): g for g in (prev.get('groups') or []) if isinstance(g, dict)}
            for g in raw.get('groups') or []:
                pg = _prev_groups.get(str(g.get('id'))) if isinstance(g, dict) else None
                for key in GROUP_KEPT_KEYS if pg else ():
                    if key in pg and key not in g:
                        g[key] = dict(pg[key])
            # totals: an empty mapping sent on purpose clears them; a form that
            # does not send the key keeps them
            if 'totals' not in payload and prev.get('totals'):
                raw['totals'] = prev['totals']
            # a form that omits a whole sink block keeps the stored one — the
            # pin below would otherwise rebuild it from the routing keys ALONE
            # and drop ha_discovery/enabled with it
            for sect in ('mqtt', 'influxdb'):
                if sect not in raw and prev.get(sect):
                    raw[sect] = dict(prev[sect])
            # Routing identity is FIXED after creation, exactly as for a device
            # (see update_device): changing it re-routes every unit's future
            # data and orphans their history + Home Assistant entities.
            for sect, keys in (('mqtt', ('topic_prefix', 'aggregate_prefix')),
                               ('influxdb', ('bucket', 'device_tag'))):
                for k in keys:
                    old = (prev.get(sect) or {}).get(k)
                    if old is not None:
                        raw.setdefault(sect, {})[k] = old
            # the units' Home Assistant node: as stored, or none (ADR-0026 §3)
            _node = (prev.get('mqtt') or {}).get('ha_node')
            if _node:
                raw.setdefault('mqtt', {})['ha_node'] = _node
            elif isinstance(raw.get('mqtt'), dict):
                raw['mqtt'].pop('ha_node', None)
            # Static tags and extra outputs are the operator's to edit, from
            # the endpoint page — but a form that does not speak of them (the
            # settings dialog) must not wipe them.
            for k in ('tags', 'outputs'):
                old = (prev.get('influxdb') or {}).get(k)
                if old is not None and k not in (raw.get('influxdb') or {}):
                    raw.setdefault('influxdb', {})[k] = old
        errors = _influx_block_errors(raw.get('influxdb') or {}, 'influxdb', outputs=True)
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})
        _agg = str((raw.get('mqtt') or {}).get('aggregate_prefix', '') or '')
        if _agg and _agg != str(((prev or {}).get('mqtt') or {}).get('aggregate_prefix', '') or ''):
            _agg = _agg.strip().strip('/')            # as endpoint_aggregator reads it
            # an aggregate root being set (ADR-0026 §4); its own units may sit under it
            from .topic_prefix import aggregate_roots, conflicts, claims, shape_error
            _shape = shape_error(_agg.replace('${endpoint_id}', pid).replace('${group_id}', 'g')
                                 .replace('${id}', pid))
            _errs = [_shape] if _shape else [
                c for root in aggregate_roots(raw) for c in conflicts(root, claims(config), {f"installation:{pid}"})]
            if _errs:
                raise HTTPException(status_code=422, detail={
                    "errors": [f"mqtt.aggregate_prefix: {e}" for e in _errs],
                    "fields": {"mqtt.aggregate_prefix": _errs[0]}})
        _endpoint_claim_check(raw, pid, prev)
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

    def _connection_matrix_extras() -> Dict[str, Any]:
        from .connection_matrix import INVALID, defaults
        return {'invalid': copy.deepcopy(INVALID), 'defaults': defaults(config)}

    @app.get("/api/protocols")
    def list_protocols():
        """The protocols each editor may offer (device, installation source,
        installation, group), and which of them are serial lines here."""
        from .connection_spec import LABELS, MATRIX
        return {**{k: list(v) for k, v in PROTOCOLS_BY_CONTEXT.items()},
                'serial': list(SERIAL_PROTOCOLS + ('rtu_tap',) + VIRTUAL_MAP_SERIAL),
                'network': list(NETWORK_PROTOCOLS + VIRTUAL_MAP_OVER_TCP),
                # the valid cells per editor, each with the connection it is
                # saved as, and the words for the axes (ADR-0026 §1)
                'matrix': copy.deepcopy(MATRIX), 'labels': copy.deepcopy(LABELS),
                # the cells no device may have, with their reasons, and every
                # advanced key's default with its source (ADR-0031 §5)
                **_connection_matrix_extras()}

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

    def _source_probe(dev, src, address_of, client=None) -> Dict:
        """Probe ONE unit over ONE of its sources, the way that source reads it.

        An HTTP source is asked for its document and judged by how many of the
        unit's selected paths it carries; a Modbus source gets one FC3 read on
        its own connection; a pushed input (MQTT) cannot be probed — we say so
        rather than pretend."""
        proto = str(src.protocol or 'tcp').lower()
        if proto == 'http':
            from .http_client import HttpClient
            from .redact import redact_url
            regs, _g = config.load_device_registers(dev, source=src)
            hc = HttpClient(dict(src.http or {}), regs, {},
                            allow_nonlan=config.security.allow_nonlan_http_devices)
            t0 = time.perf_counter()
            res = hc.test_read()
            res.setdefault('latency_ms', round((time.perf_counter() - t0) * 1000, 1))
            res['where'] = redact_url(hc.url)
            if hc.url and isinstance(res.get('message'), str):
                # an error text may quote the URL, token and all
                res['message'] = res['message'].replace(hc.url, res['where'])
            return res
        if proto in _POLLED_MODBUS and _listening_template(src.template or dev.template):
            # a map written for listening: the runtime taps it (ADR-0025) —
            # what it hears, never a question on the bus
            drv = _driver_of(client, src) if client is not None else None
            res = _tap_hearing(dev, drv) if drv is not None else {
                'ok': None, 'message': "listen-only — this source taps its bus; nothing to ask"}
            res['where'] = src.connection.bus
            return res
        if proto in _POLLED_MODBUS:
            conn = dataclasses.asdict(src.connection)
            conn['protocol'] = proto
            timeout = float(conn.get('timeout', 3) or 3)
            res = _modbus_probe(conn, dev.connection.unit_id, timeout,
                                address_of(src))
            res['where'] = (conn.get('serial_port') if proto in SERIAL_PROTOCOLS
                            else f"{conn.get('host', '')}:{conn.get('port', 502)}")
            return res
        if proto == 'mqtt':
            return _mqtt_hearing(_driver_of(client, src))
        return {'ok': None, 'message': 'not probed — this source is pushed to '
                                       'the gateway, it cannot be asked',
                'where': str((src.mqtt_in or {}).get('topic', ''))}

    @app.post("/api/endpoints/{endpoint_id}/test")
    def test_endpoint(endpoint_id: str):
        """Probe every unit of an endpoint over EVERY source it is read through.

        One answer per (source, unit): only that tells "the datalogger is deaf"
        from "unit 3 is not configured on it" from "Modbus is fine but the Solar
        API is off". Before sources existed this probed the endpoint's top-level
        Modbus connection alone, which a grouped installation does not have — so
        an installation reading perfectly over HTTP reported every unit blocked.
        CAUTION: a Modbus probe opens ANOTHER client on that endpoint, and
        dataloggers serve only a few at once — operator-triggered, never a
        background poll."""
        p = config.get_raw_endpoint(endpoint_id)
        if p is None:
            raise HTTPException(status_code=404, detail="endpoint not found")
        _one_test_at_a_time(f"endpoint:{endpoint_id}")
        try:
            return _test_endpoint_units(endpoint_id, p)
        finally:
            _test_done(f"endpoint:{endpoint_id}")

    def _test_endpoint_units(endpoint_id: str, p: Dict) -> Dict:
        conn = dict(p.get('connection', {}) or {})
        timeout = float(conn.get('timeout', 3) or 3)
        rows = []
        for dev in config.endpoint_devices(endpoint_id):
            def address_of(src=None, _dev=dev):
                regs, _g = config.load_device_registers(_dev, source=src)
                # a real address beats address 0 — some gateways answer 0 blindly
                return regs[0].address if regs else 0
            srcs = [s for s in (dev.sources or []) if getattr(s, 'enabled', True)]
            if not srcs:
                res = _modbus_probe(conn, dev.connection.unit_id, timeout, address_of())
                rows.append({'source': 'default', 'protocol': conn.get('protocol', 'tcp'),
                             'unit_id': dev.connection.unit_id, 'device_id': dev.id,
                             'where': f"{conn.get('host', '')}:{conn.get('port', 502)}",
                             **res})
                continue
            for src in srcs:
                res = _source_probe(dev, src, address_of, registry.find(dev.id)[2])
                rows.append({'source': src.id, 'protocol': src.protocol,
                             'unit_id': dev.connection.unit_id, 'device_id': dev.id,
                             **res})
        # one line per source: how many of its units answered
        summary, order = {}, []
        for r in rows:
            k = r['source']
            if k not in summary:
                order.append(k)
                summary[k] = {'id': k, 'protocol': r['protocol'], 'answered': 0,
                              'probed': 0, 'total': 0, 'where': r.get('where', '')}
            summary[k]['total'] += 1
            if r.get('ok') is not None:
                summary[k]['probed'] += 1
                summary[k]['answered'] += 1 if r['ok'] else 0
        for v in summary.values():
            v['ok'] = (v['answered'] == v['probed']) if v['probed'] else None
        probed = [r for r in rows if r.get('ok') is not None]
        return {'endpoint': endpoint_id,
                'ok': bool(probed) and all(r['ok'] for r in probed),
                'units': rows,
                'sources': [summary[k] for k in order]}

    @app.post("/api/endpoints")
    @_serialized_mutation
    def create_endpoint(payload: Dict = Body(...)):
        """Create an endpoint: validate → persist → materialize N devices, seed
        each from the template and hot-start their pollers (no restart)."""
        raw = _validate_endpoint_payload(payload)
        raw.setdefault('opcua', {'enabled': False})      # a new endpoint: not on OPC UA until chosen
        _set_new_node(raw.setdefault('mqtt', {}), '${device_id}')   # each unit's node, by pattern
        if not raw['mqtt']:
            # nothing to store (legacy scope, no route): an empty block would
            # read as changed against the validated one on every later save (QA-R1-05)
            raw.pop('mqtt')
        raw['write_locked'] = True                       # …and writes nothing until unlocked (ADR-0025)
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
            # extra outputs are the aggregator's, read live each cycle — an
            # edit to them must not restart a single unit
            'influxdb': {k: v for k, v in (raw.get('influxdb') or {}).items()
                         if k != 'outputs'},
            'write_locked': bool(raw.get('write_locked', False)),
            'http_output': raw.get('http_output') or {},
            'rest_push': raw.get('rest_push') or {},
        }, sort_keys=True, default=str)

    # exposed for tests: the restart-or-not contract is worth pinning
    app.state.endpoint_runtime_sig = _endpoint_runtime_sig

    # what an installation's own routes manage: an edit never changes them
    _ENDPOINT_OWN_KEYS = ('write_locked', 'http_output', 'rest_push', 'opcua')

    def _drop_endpoint_echoes(pid: str, payload: Dict, prev: Dict) -> Dict:
        """What a page sends back only because it was shown — `enabled: true`
        on a group or source that never said it, a unit's derived name, the
        flat `units` beside groups — is not written. Written, a
        derived name stopped following the installation's name (P11), and an
        untouched installation read as changed. Matched by id with what is
        stored; anything else in the payload is the caller's."""
        if not isinstance(payload.get('groups'), list) or not payload['groups'] or not prev.get('groups'):
            return payload
        payload = copy.deepcopy(payload)
        groups = payload['groups']
        if 'units' in payload:                    # with groups the flat list builds nothing
            if 'units' in prev:
                payload['units'] = copy.deepcopy(prev['units'])
            else:
                payload.pop('units')
        derived = {f"{n} unit {{}}" for n in (payload.get('name'), prev.get('name'), pid) if n}
        by_id = lambda xs: {str(x.get('id')): x for x in xs or [] if isinstance(x, dict)}  # noqa: E731
        stored = by_id(prev.get('groups'))
        for g in groups:
            pg = stored.get(str(g.get('id'))) if isinstance(g, dict) else None
            if pg is None:
                continue
            if g.get('enabled') is True and 'enabled' not in pg:
                g.pop('enabled')
            old_src = by_id(pg.get('sources'))
            for src in g.get('sources') or []:
                ps = old_src.get(str(src.get('id'))) if isinstance(src, dict) else None
                if ps is not None and src.get('enabled') is True and 'enabled' not in ps:
                    src.pop('enabled')
            old_units = {}
            for u in pg.get('units') or []:
                uid = u.get('unit_id') if isinstance(u, dict) else u
                old_units[uid] = u
            units = []
            for u in g.get('units') or []:
                pu = old_units.get(u.get('unit_id')) if isinstance(u, dict) else None
                if (pu is not None and not (isinstance(pu, dict) and pu.get('name'))
                        and any(u.get('name') == d.format(u['unit_id']) for d in derived)
                        and u.get('id') in (None, f"{pid}-u{u['unit_id']}",
                                            pu.get('id') if isinstance(pu, dict) else None)):
                    u = copy.deepcopy(pu)
                units.append(u)
            if 'units' in g:
                g['units'] = units
        return payload

    def _endpoint_put_raw(endpoint_id: str, payload: Dict, prev: Dict) -> Dict:
        """What saving ``payload`` over the stored installation writes: every
        route merged with the stored one (save_merge.py), validated, with what
        the payload did not speak of kept."""
        payload = _drop_endpoint_echoes(endpoint_id, dict(payload), prev)
        for k in ('name', 'template', 'enabled'):
            if k not in payload and k in prev:
                payload[k] = prev[k]
        payload = merge_endpoint_routes(payload, prev)
        raw = _validate_endpoint_payload(payload, existing_id=endpoint_id)
        return keep_unsaid(raw, prev, payload, own=_ENDPOINT_OWN_KEYS)

    def _refuse_removed_units(endpoint_id: str, raw: Dict) -> List[Dict]:
        """An edit that takes units or groups away (fewer units, another
        address, a renamed group) is a delete of those units: refused (409)
        while anything names them, or a rule commands a group that goes.
        A rule that commands the group a unit leaves (dropped, or moved to
        another group) keeps working with the units that remain: said in
        the answer's ``warnings``, not refused. Disabling a group or the
        installation is neither — its units stay."""
        from .dependents import Unreadable, summary, units_dependents
        before = {d.id: (d.group_id or 'units') for d in config.endpoint_devices(endpoint_id)}
        after = {d.id: (d.group_id or 'units') for d in config.preview_devices(endpoint=raw)}
        gone = set(before) - set(after)
        gone_groups = set(before.values()) - set(after.values())
        moved = {u: (g, after[u]) for u, g in before.items() if u in after and after[u] != g}
        if not gone and not gone_groups and not moved:
            return []
        try:
            res = units_dependents(_dep_sources(), endpoint_id, gone, gone_groups, moved)
        except Unreadable as e:
            raise HTTPException(status_code=503, detail={"errors": [
                f"what uses the units this edit removes cannot be told: {e} — fix that first"]})
        if res['blocking']:
            deps = {"blocking": res['blocking'], "follows": [], "warnings": res['warnings']}
            groups = [str(g.get('id') or '') for g in (raw.get('groups') or []) if isinstance(g, dict)]
            msg = summary(deps, f"what this edit removes ({', '.join(sorted(gone | gone_groups))})")
            fields: Dict[str, str] = {}
            for u, g in before.items():
                if u in gone or g in gone_groups:
                    fields[f"groups[{groups.index(g)}].units" if g in groups else "groups"] = msg
            raise HTTPException(status_code=409, detail={"errors": [msg], "fields": fields, "dependents": deps})
        return res['warnings']

    @app.put("/api/endpoints/{endpoint_id}")
    @_serialized_mutation
    def update_endpoint(endpoint_id: str, payload: Dict = Body(...)):
        """Update an endpoint. An edit that changes what the units are built from
        re-materializes them (stop → rebuild → start); an edit that only changes
        endpoint-level settings keeps every poller running and just refreshes the
        config behind it."""
        return _update_endpoint_locked(endpoint_id, payload)

    def _unit_device_id(endpoint_id: str, unit: str) -> Optional[str]:
        """The device id of an installation's unit, named by its id or by its
        unit address; None when it has no such unit."""
        u = str(unit or '').strip().lower()
        for d in config.endpoint_devices(endpoint_id):
            if d.id == u:
                return d.id
        for d in config.endpoint_devices(endpoint_id):
            if u.isdigit() and int(d.connection.unit_id) == int(u):
                return d.id
        return None

    def _unit_write(fn):
        """A write to one unit of an installation, checked against the UNIT's
        device ETag (the tag its page holds, ADR-0030) and answering its new
        one; under the installation's entry lock."""
        def _did(kw):
            return _unit_device_id(kw.get('endpoint_id'), kw.get('unit'))
        return _etag_guard.guard(fn, 'check',
                                 check=lambda im, kw: _check_if_match(im, _did(kw)),
                                 tag=lambda kw: _device_tag_now(_did(kw)),
                                 lock=lambda kw: _entry_lock(_did(kw)))

    @app.patch("/api/endpoints/{endpoint_id}/units/{unit}")
    @_unit_write
    @_serialized_mutation
    def rename_endpoint_unit(endpoint_id: str, unit: str, payload: Dict = Body(...)):
        """Rename one unit of an installation (L3-F12): {name} ('' = the
        installation's naming again). Only that unit's entry changes; nothing
        restarts. `If-Match`: the unit's own ETag (checked when sent)."""
        prev = config.get_raw_endpoint(endpoint_id)
        if prev is None:
            raise HTTPException(status_code=404, detail="endpoint not found")
        u = str(unit or '').strip()
        if u.isdigit():
            # a unit address written in several groups names no one unit
            where = []
            for g in ([g for g in (prev.get('groups') or []) if isinstance(g, dict)] or [prev]):
                for x in g.get('units') or []:
                    try:
                        if int(x.get('unit_id') if isinstance(x, dict) else x) == int(u):
                            where.append(str(g.get('id') or 'units'))
                    except (TypeError, ValueError):
                        continue
            if len(where) > 1:
                msg = (f"unit {u} is in several groups ({', '.join(where)}) — name it by its id "
                       f"({', '.join(d.id for d in config.endpoint_devices(endpoint_id) if int(d.connection.unit_id) == int(u))})")
                raise HTTPException(status_code=422, detail={"errors": [f"unit: {msg}"], "fields": {"unit": msg}})
        did = _unit_device_id(endpoint_id, unit)
        if did is None:
            raise HTTPException(status_code=404, detail=f"no unit '{unit}' on '{endpoint_id}'")
        if not isinstance(payload, dict) or set(payload) != {'name'} or not isinstance(payload.get('name'), str):
            raise HTTPException(status_code=422, detail={"errors": ["name: required, the only key — text ('' = "
                                                                    "the installation's naming)"],
                                                         "fields": {"name": "required"}})
        name = payload['name'].strip()
        if len(name) > 80:
            raise HTTPException(status_code=422, detail={"errors": ["name: up to 80 characters"],
                                                         "fields": {"name": "up to 80 characters"}})
        body = copy.deepcopy(prev)
        pid = str(body.get('id', endpoint_id))
        holders = [g for g in (body.get('groups') or []) if isinstance(g, dict)] or [body]
        hit = False
        for h in holders:
            units = h.get('units') or []
            for i, u in enumerate(units):
                uid = u.get('unit_id') if isinstance(u, dict) else u
                try:
                    own = str((u.get('id') if isinstance(u, dict) else None) or f"{pid}-u{int(uid)}").strip()
                except (TypeError, ValueError):
                    continue
                if own != did:
                    continue
                new = dict(u) if isinstance(u, dict) else {'unit_id': u}
                if name:
                    new['name'] = name
                else:
                    new.pop('name', None)
                units[i] = new if set(new) != {'unit_id'} or isinstance(u, dict) else u
                hit = True
                break                           # the one unit named, never a repeat of it
            if hit:
                break
        if not hit:
            raise HTTPException(status_code=404, detail=f"no unit '{unit}' on '{endpoint_id}'")
        out = _update_endpoint_locked(endpoint_id, body)
        _i, dev, client = registry.find(did)
        return {"status": out.get("status"), "endpoint_id": endpoint_id, "unit": did,
                "device": _device_entry(dev, client) if dev is not None else None,
                "restarted": out.get("restarted", [])}

    def _update_endpoint_locked(endpoint_id: str, payload: Dict):
        prev_raw = config.get_raw_endpoint(endpoint_id)
        if prev_raw is None:
            raise HTTPException(status_code=404, detail="endpoint not found")
        prev_raw = copy.deepcopy(prev_raw)
        raw = _endpoint_put_raw(endpoint_id, payload, prev_raw)
        # an unchanged save changes nothing (the stored installation through
        # the same pipeline is what this save would write)
        try:
            _same = raw == _endpoint_put_raw(endpoint_id, copy.deepcopy(prev_raw), prev_raw)
        except Exception as e:  # noqa: BLE001 — the comparison never fails a save
            logger.debug("endpoint %s: unchanged-check skipped (%s)", endpoint_id, e)
            _same = False
        if _same:
            return {"status": "unchanged", "endpoint": _endpoint_entry(raw),
                    "devices": [_device_entry(d, registry.find(d.id)[2])
                                for d in config.endpoint_devices(endpoint_id)]}
        warnings = _refuse_removed_units(endpoint_id, raw)
        # restart only the units whose RUNNING shape changed: compared unit by
        # unit on what each builds (groups and sources included — a source moved
        # to another port must reach its pollers), not on the raw text. A new
        # interval on the site meter's source restarts the meter, never the
        # four inverters beside it (their sockets are shared and ref-counted).
        restart = _units_to_restart(prev_raw, raw)
        old_ids = {d.id for d in config.endpoint_devices(endpoint_id)}
        if restart is None:                       # cannot be told unit by unit
            restart = set() if _endpoint_runtime_sig(prev_raw) == _endpoint_runtime_sig(raw) \
                else old_ids | {d.id for d in config.preview_devices(endpoint=raw)}
        for dev in config.endpoint_devices(endpoint_id):
            if dev.id in restart:
                _i, _cfg, client = registry.find(dev.id)
                _teardown_device(dev.id, dev, client)
                registry.remove(dev.id)
        try:
            made = config.upsert_raw_endpoint(raw)
        except ValueError as e:
            # config restored the previous definition — restart the units this
            # save stopped, so a failed edit doesn't leave them off
            _start_endpoint_devices([d for d in config.endpoint_devices(endpoint_id) if d.id in restart])
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        devices_out = []
        for dev_cfg in made:
            if dev_cfg.id in restart:
                devices_out += _start_endpoint_devices([dev_cfg])
            else:
                # rebuilt by upsert; swapped in behind the RUNNING client so a
                # rename shows up without a restart
                registry.replace(dev_cfg.id, dev_cfg)
                devices_out.append(_device_entry(dev_cfg, registry.find(dev_cfg.id)[2]))
        _sync_device_discovery()
        restarted = sorted(restart & {d.id for d in made})
        logger.info("endpoint %s: updated (%d unit(s))%s", endpoint_id, len(made),
                    f" — restarted {', '.join(restarted)}; the others kept running" if restarted
                    else " — settings only, pollers kept running")
        out = {"status": "updated", "endpoint": _endpoint_entry(raw), "devices": devices_out,
               "restarted": restarted}
        if warnings:
            out["warnings"] = warnings
        return out

    def _units_to_restart(prev_raw: Dict, raw: Dict) -> Optional[set]:
        """The ids of the units whose running shape differs between two
        definitions of an installation (added and removed ones included), or
        None when a unit's shape cannot be told."""
        def _sigs(r):
            return {d.id: _built_sig([d]) for d in config.preview_devices(endpoint=r)}
        a, b = _sigs(prev_raw), _sigs(raw)
        if None in a.values() or None in b.values():
            return None
        return {uid for uid in a.keys() | b.keys() if a.get(uid) != b.get(uid)}

    @app.get("/api/endpoints/{endpoint_id}/template-changes")
    def endpoint_template_changes(endpoint_id: str):
        """What "Update from template" would change on this installation's
        units — nothing is written. The page offers the update only when
        ``pending``, and shows exactly this list before it is applied."""
        from .template_refresh import plan_devices
        if config.get_raw_endpoint(endpoint_id) is None:
            raise HTTPException(status_code=404, detail="endpoint not found")
        return plan_devices(config, template_registry, config.endpoint_devices(endpoint_id))

    @app.get("/api/devices/{device_id}/template-changes")
    def device_template_changes(device_id: str):
        """The same for one standalone device."""
        from .template_refresh import plan_devices
        _idx, dev_cfg, _client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        if dev_cfg.primary:
            return {"pending": False, "units_total": 1, "units_affected": 0,
                    "templates": [], "changes": [], "new": [], "missing_templates": []}
        return plan_devices(config, template_registry, [dev_cfg])

    @app.post("/api/endpoints/{endpoint_id}/refresh-from-template")
    @_serialized_mutation
    def refresh_endpoint_from_template(endpoint_id: str, add_new: bool = False):
        """Bring every unit's register metadata (labels, units, categories,
        bank totals) up to date with its template — the selection, dashboard
        flags, sinks and calculated expressions stay the operator's. The units
        restart so the new metadata reaches the pipeline."""
        from .template_refresh import refresh_device
        if config.get_raw_endpoint(endpoint_id) is None:
            raise HTTPException(status_code=404, detail="endpoint not found")
        made = config.endpoint_devices(endpoint_id)
        _stop_endpoint_devices(endpoint_id)
        try:
            results = [refresh_device(config, template_registry, d, add_new=add_new) for d in made]
        finally:
            _start_endpoint_devices(config.endpoint_devices(endpoint_id))
            # the stop withdrew the units' HA entities — publish them again
            _sync_device_discovery()
        logger.info("endpoint %s: updated from template — %d register rows, "
                    "%d calculated rows changed, %d new calculated fields added "
                    "across %d units", endpoint_id,
                    sum(r['registers'] for r in results), sum(r['calculated'] for r in results),
                    sum(r['added'] for r in results), len(results))
        return {"status": "refreshed", "units": results}

    @app.post("/api/devices/{device_id}/refresh-from-template")
    @_device_write
    @_serialized_mutation
    def refresh_device_from_template(device_id: str, add_new: bool = False):
        """The same for one device (an installation's unit refreshes through
        its installation, so every unit stays alike)."""
        from .template_refresh import refresh_device
        _idx, dev_cfg, client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        if dev_cfg.endpoint_id:
            raise HTTPException(status_code=422, detail={"errors": [
                f"'{device_id}' belongs to installation '{dev_cfg.endpoint_id}' — "
                "update it from the installation"]})
        if dev_cfg.primary:
            raise HTTPException(status_code=422, detail={"errors": [
                "the primary device's map is edited in Measurements"]})
        res = refresh_device(config, template_registry, dev_cfg, add_new=add_new)
        if client:
            client.disconnect()
        new_client = _start_device_client(dev_cfg)
        registry.replace(device_id, dev_cfg, client=new_client, add_if_missing=True)
        _sync_device_discovery()            # HA picks up the new labels
        logger.info("device %s: updated from template — %d register rows, %d calculated "
                    "rows changed, %d new calculated fields added", device_id,
                    res['registers'], res['calculated'], res['added'])
        return {"status": "refreshed", **res}

    @app.delete("/api/endpoints/{endpoint_id}")
    def delete_endpoint(endpoint_id: str, dry_run: bool = False):
        """Delete an endpoint and stop all its units (their register files are
        kept on disk, so re-adding the endpoint restores the selection).
        Refused (409) while a rule targets or reads it or one of its units,
        or a sequence, a formula or a virtual meter uses a unit (asked
        outside the device lock, then again under it)."""
        if config.get_raw_endpoint(endpoint_id) is None:
            raise HTTPException(status_code=404, detail="endpoint not found")
        ans = _refuse_if_used('installation', endpoint_id, f"installation '{endpoint_id}'", dry_run)
        if ans is not None:
            return ans
        return _delete_endpoint_locked(endpoint_id)

    @_serialized_mutation
    def _delete_endpoint_locked(endpoint_id: str):
        if config.get_raw_endpoint(endpoint_id) is None:
            raise HTTPException(status_code=404, detail="endpoint not found")
        _refuse_if_used('installation', endpoint_id, f"installation '{endpoint_id}'", False)
        _topics = set()
        for d in config.endpoint_devices(endpoint_id):
            _cancel_device_refreshes(d.id)
            _topics |= _device_value_topics(d)
        _stop_endpoint_devices(endpoint_id)
        _clear_device_values(_topics)
        try:
            removed = config.delete_endpoint(endpoint_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        _sync_device_discovery()
        _publish_attribute_meta()      # the installation's meta is no longer published: cleared
        logger.info(f"endpoint {endpoint_id}: deleted ({len(removed)} unit(s))")
        return {"status": "deleted", "removed_devices": removed}

    def _modbus_probe(conn: Dict, unit_id: int, timeout: float,
                      address: int = 0) -> Dict:
        """One ad-hoc Modbus probe (TCP or RTU): connect + FC3 read. ANY
        protocol-level answer (even a Modbus exception) proves a live device;
        only silence/timeouts fail. Used by the wizard's Test connection button."""
        from pymodbus.pdu import ExceptionResponse
        from .bus_claims import canonical_serial
        from .config import ModbusConfig
        from .modbus_client import _build_client
        proto = str(conn.get('protocol', 'tcp')).lower()
        rtu = proto in SERIAL_PROTOCOLS              # a serial port here (RTU or ASCII)
        # a bus a device only listens to is never asked (ADR-0025)
        _refuse_if_listened("A Test", proto, conn)
        if not rtu:
            # same LAN-egress policy as every /api/discover/* route — without
            # it the probe doubles as an internal TCP port scanner (audit L1)
            from .discovery import lan_host_error
            _e = lan_host_error(conn.get('host', ''),
                                config.security.allow_nonlan_http_devices)
            if _e:
                return {"ok": False, "message": f"blocked: {_e}"}
        t0 = time.perf_counter()
        sp = str(conn.get('serial_port', '') or '')
        where = (f"{sp}@{conn.get('baudrate', 9600)}" if rtu
                 else f"{conn.get('host', '')}:{conn.get('port', 502)}")
        if proto in BUS_OVER_TCP_PROTOCOLS + SERIAL_PROTOCOLS:
            # A bus already read by a running device: ask THROUGH its
            # connection (the shared transport), in turn with the slaves on
            # it — a private socket would make a single-client bridge drop
            # the live one (audit DP-19), and a tty opens once. Only the unit
            # id is the test's own.
            _th = str(conn.get('host', '')).strip().lower()
            _tp = int(conn.get('port', 502) or 502)
            _busy = next((_d for _d in config.devices
                          if getattr(_d, 'enabled', False) and getattr(_d, 'protocol', '') == proto
                          and ((rtu and canonical_serial(getattr(_d.connection, 'serial_port', ''))
                                == canonical_serial(sp))
                               or (not rtu and str(getattr(_d.connection, 'host', '')).strip().lower() == _th
                                   and int(getattr(_d.connection, 'port', 0) or 0) == _tp))), None)
            if _busy is not None:
                return _test_on_shared_bus(conn, unit_id, address, timeout, where, _busy.id)
        c = _build_client(ModbusConfig(
            host=str(conn.get('host', '') or ''), port=int(conn.get('port', 502) or 502),
            unit_id=unit_id, timeout=timeout, protocol=proto, serial_port=sp,
            baudrate=int(conn.get('baudrate', 9600) or 9600), parity=str(conn.get('parity', 'N') or 'N'),
            stopbits=int(conn.get('stopbits', 1) or 1), bytesize=int(conn.get('bytesize', 8) or 8)))
        try:
            if not c.connect():
                return {"ok": False,
                        "message": ((_serial_missing(sp) or f"Serial open of {where} failed — check the port/permissions")
                                    if rtu else
                                    f"{_PROTO_WORDS.get(proto, 'TCP')} connect to {where} failed — check IP/port/firewall")}
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
        """Probe a connection that is not saved (yet): the wizard's step 1, or
        a device page whose connection was edited. With ``device_id`` the
        stored connection is merged under the form's, as a save would — so a
        password left blank, the headers, the rest of a serial line are the
        stored ones (used here, never echoed)."""
        conn = payload.get('connection', payload) or {}
        _did = str(payload.get('device_id') or '').strip()
        _stored = config.get_raw_device(_did) if _did else None
        if _stored and isinstance(conn, dict):
            conn = dict(conn)
            if conn.get('password') in ('', MASKED):
                conn.pop('password')
            try:
                conn = merge_connection(conn, _stored.get('connection'), config.bridges)
            except BridgeConflict as e:
                return {"ok": False, "message": str(e)}
        if conn.get('bridge'):
            from .bridges import resolve_connection as _rb
            conn = _rb(conn, config.bridges)     # test the bus the device will use
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
            from .discovery import lan_host_error
            _e = lan_host_error(broker, config.security.allow_nonlan_http_devices)
            if _e:
                raise HTTPException(status_code=422, detail={"errors": [f"connection.broker: {_e}"]})
            import paho.mqtt.client as mqtt
            got = {"connected": False, "msg": None, "topic": None}
            ev = threading.Event()
            cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            if conn.get('username'):
                cli.username_pw_set(str(conn.get('username')), str(conn.get('password', '')))
            if conn.get('tls'):
                try:
                    if conn.get('tls_ca'):
                        cli.tls_set(ca_certs=str(conn['tls_ca']))
                    else:
                        cli.tls_set()
                    if conn.get('tls_insecure'):
                        cli.tls_insecure_set(True)      # a device's own certificate (a Victron GX)
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
        if protocol in CAN_PROTOCOLS:
            from .can_client import connection_errors as _can_errors
            _ce = _can_errors(conn)
            if _ce:
                raise HTTPException(status_code=422, detail={"errors": _ce})
            return _can_test(conn)
        if protocol == 'rtu_tap':
            # nothing to ask: a tap never transmits, and the tty may already be
            # held by a running tap — the saved device's test reports what it hears
            return {"ok": None, "message": "listen-only — nothing to probe; save "
                                           "the device, then Test shows what the tap hears"}
        _tid = str(payload.get('template') or (conn.get('template') if isinstance(conn, dict) else '')
                   or (_stored or {}).get('template') or '').strip()
        if protocol in VIRTUAL_MAP_PROTOCOLS:
            # a map only ever heard (the Seplos inter-pack tap) is never asked,
            # whatever mode the form says: a question there collides with the
            # master pack (SV2-6)
            from .device_template import template_listens_only
            _ttpl = template_registry.get(_tid) if _tid else None
            if _ttpl is not None and template_listens_only(_ttpl):
                return {"ok": None, "message": f"listen-only: template '{_tid}' is heard on a bus another "
                                               "master polls — nothing to ask; save the device, then Test "
                                               "shows what it hears"}
            if _vmap_mode_missing(conn, (_stored or {}).get('connection')):
                raise HTTPException(status_code=422, detail={"errors": [
                    "connection.mode: choose poll or listen before a test"]})
            return _virtual_map_probe(protocol, conn)
        if _listening_template(_tid):
            # a Modbus map written for listening runs as a tap whatever the
            # form says (ADR-0025): nothing to ask
            return {"ok": None, "message": f"listen-only: template '{_tid}' is heard on a bus another "
                                           "master polls — nothing to ask; save the device as listen only, "
                                           "then Test shows what it hears"}
        if protocol in SERIAL_PROTOCOLS and not str(conn.get('serial_port', '')).strip():
            raise HTTPException(status_code=422, detail={"errors": ["connection.serial_port required"]})
        if protocol in NETWORK_PROTOCOLS and not str(conn.get('host', '')).strip():
            raise HTTPException(status_code=422, detail={"errors": ["connection.host required"]})
        return _modbus_probe(conn, int(conn.get('unit_id', 1)),
                             float(conn.get('timeout', 3)),
                             int(payload.get('address', 0)))

    def _virtual_map_bus_refusal(protocol: str, conn: Dict) -> str:
        """Why a poll Test must not transmit on this bus, or '' when it may.
        A bus another device holds is asked only when that device POLLS it
        with the same protocol (the Test then takes turns on its transport);
        a bus listened to (a master already polls it), a tap, or another
        protocol (Modbus) is never written to."""
        from .bus_claims import canonical_serial
        serial = protocol in VIRTUAL_MAP_SERIAL
        sp = canonical_serial(conn.get('serial_port', ''))
        host = str(conn.get('host', '') or '').strip().lower()
        try:
            port = int(conn.get('port', 0) or 0)
        except (TypeError, ValueError):
            port = 0
        for d in config.devices:
            dproto = str(getattr(d, 'protocol', '') or '').lower()
            c = d.connection
            if serial:
                same = (dproto in SERIAL_PROTOCOLS + ('rtu_tap',) + VIRTUAL_MAP_SERIAL
                        and canonical_serial(getattr(c, 'serial_port', '')) == sp)
                where = f"'{sp}'"
            else:
                same = (dproto not in ('http', 'mqtt', 'none') and dproto not in SERIAL_PROTOCOLS + VIRTUAL_MAP_SERIAL
                        and str(getattr(c, 'host', '') or '').strip().lower() == host
                        and int(getattr(c, 'port', 0) or 0) == port)
                where = f"{host}:{port}"
            if not same:
                continue
            if dproto != protocol:
                return (f"{where} is used by device '{d.id}' as {dproto} — a Seplos question there would "
                        f"land on another protocol's bus; nothing sent")
            if _vmap_mode(c) == 'listen':
                return (f"{where} is listened to by device '{d.id}' — a master pack polls that bus, and a "
                        f"question from here would collide with it; nothing sent")
        return ''

    def _virtual_map_probe(protocol: str, conn: Dict) -> Dict:
        """One read request to an unsaved virtual-map device (listen mode asks
        nothing), through the bus's shared transport."""
        import importlib
        from .config import ModbusConfig
        from .device_runtime import VIRTUAL_MAP_DRIVERS
        if protocol in VIRTUAL_MAP_SERIAL and not str(conn.get('serial_port', '')).strip():
            raise HTTPException(status_code=422, detail={"errors": ["connection.serial_port required"]})
        if protocol in VIRTUAL_MAP_OVER_TCP and not str(conn.get('host', '')).strip():
            raise HTTPException(status_code=422, detail={"errors": ["connection.host required"]})
        if _vmap_mode(conn) == 'poll':
            if protocol in VIRTUAL_MAP_OVER_TCP:
                # the LAN-egress policy of every probe (audit L1)
                from .discovery import lan_host_error
                _e = lan_host_error(conn.get('host', ''), config.security.allow_nonlan_http_devices)
                if _e:
                    return {"ok": False, "message": f"blocked: {_e}"}
            _refuse_if_listened("A Test", protocol, conn)       # ADR-0025
            _busy = _virtual_map_bus_refusal(protocol, conn)
            if _busy:
                return {"ok": False, "message": _busy}
        try:
            mc = ModbusConfig(host=str(conn.get('host', '') or ''), port=int(conn.get('port', 0) or 0),
                              unit_id=int(conn.get('unit_id', 0) or 0), protocol=protocol,
                              serial_port=str(conn.get('serial_port', '') or ''),
                              baudrate=int(conn.get('baudrate', 9600) or 9600),
                              parity=str(conn.get('parity', 'N') or 'N'),
                              stopbits=int(conn.get('stopbits', 1) or 1),
                              bytesize=int(conn.get('bytesize', 8) or 8), mode=_vmap_mode(conn))
            timeout = float(conn.get('timeout', 3) or 3)
        except (TypeError, ValueError) as e:
            raise HTTPException(status_code=422, detail={"errors": [f"connection: {e}"]})
        mod = importlib.import_module(f".{VIRTUAL_MAP_DRIVERS[protocol][0]}", __package__)
        return mod.probe(mc, timeout=timeout)

    def _virtual_map_hearing(dev_cfg, client) -> Dict:
        """A saved virtual-map device's test: what its running client has got
        (polled: answers and errors; listening: what it heard) — never a
        second master on the bus."""
        c = dev_cfg.connection
        srcs = dev_cfg.sources or []
        drv = _driver_of(client, srcs[0]) if (client is not None and srcs) else None
        if drv is None or not hasattr(drv, 'get_stats'):
            return {"ok": False, "message": f"not reading {c.bus} (device disabled or not started)"}
        st = drv.get_stats()
        bus = st.get('bus') or {}
        if bus.get('open_error'):
            return {"ok": False, "message": f"{c.bus}: {bus['open_error']}"}
        ec = st.get('error_counts') or {}
        good, age = int(st.get('successful_reads') or 0), st.get('staleness_age_s')
        errs = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in ec.items() if v)
        listen = _vmap_mode(c) == 'listen'
        if not good:
            if listen:
                frames = int(bus.get('frames') or 0)
                why = (f"the bus is live ({frames} frames) but no answer from pack {c.unit_id} yet"
                       if frames else f"{c.bus} is open but silent (master pack off? A/B swapped? "
                                      f"line not {c.baudrate} {c.parity})")
                if bus.get('warning'):
                    why += f" — {bus['warning']}"
            else:
                why = f"no answer from pack {c.unit_id} yet" + (f" — {errs}" if errs else "")
            return {"ok": False, "message": why}
        said = (f"{'hearing' if listen else 'reading'} pack {c.unit_id} — {good} answers, "
                f"last {age} s ago" + (f" · {errs}" if errs else ""))
        if st.get('last_latency_ms') is not None:
            said += f" · {st['last_latency_ms']} ms"
        return {"ok": True, "message": said}

    def _tap_hearing(dev_cfg, client) -> Dict:
        """A tap's "test": not a probe (it never transmits) but what it has
        HEARD — the line open, the bus alive, this unit's windows arriving.
        Each failure names the layer, so "wrong A/B" reads differently from
        "the master never asks unit 7"."""
        c = dev_cfg.connection
        if client is None or not hasattr(client, 'get_stats'):
            return {"ok": False, "message": f"not listening on {c.bus} "
                                            "(device disabled or not started)"}
        st = client.get_stats()
        bus = st.get('bus') or {}
        if bus.get('open_error'):
            return {"ok": False, "message": f"{c.bus}: {bus['open_error']}"}
        frames = int(bus.get('frames') or 0)
        if not frames:
            return {"ok": False, "message": f"{c.bus} is open but silent — no "
                                            "valid frame yet (master off? A/B swapped? "
                                            f"line not {c.baudrate} {c.parity})"}
        wins, age = st.get('successful_reads') or 0, st.get('staleness_age_s')
        mm = st.get('table_mismatch')
        if mm and not st.get('updates'):
            fc = {'holding': 'FC3', 'input': 'FC4'}
            return {"ok": False, "message": f"the master reads unit {c.unit_id} @{mm['address']} as "
                                            f"{mm['heard']} registers ({fc.get(mm['heard'], '')}) but the "
                                            f"map declares them {mm['map']} — set those rows' register "
                                            f"type to {mm['heard']} in the template"}
        if not wins:
            return {"ok": False, "message": f"the bus is live ({frames} frames) but "
                                            f"nothing for unit {c.unit_id} yet"}
        return {"ok": True, "message": f"hearing unit {c.unit_id} — {wins} windows, "
                                       f"last {age} s ago · {frames} frames on "
                                       f"{c.bus}"}

    # one Test at a time per device / installation: a probe opens another
    # client on hardware that serves few (a Fronius datalogger falls over
    # past ~26 transactions a minute) — a second click waits its turn
    _tests_running: Set[str] = set()
    _tests_lock = threading.Lock()

    def _one_test_at_a_time(key: str):
        with _tests_lock:
            if key in _tests_running:
                raise HTTPException(status_code=409, detail="a test of this is already running — wait for its answer")
            _tests_running.add(key)

    def _test_done(key: str):
        with _tests_lock:
            _tests_running.discard(key)

    def _mqtt_hearing(drv) -> Dict:
        """A pushed MQTT source's "test": what its running input hears — never
        a second connection to the broker (a GX allows few). ``drv`` is the
        source's own input (MqttInput), or None when nothing runs."""
        if drv is None or not hasattr(drv, 'get_stats'):
            # nothing runs to ask: said plainly, not counted as a failed source
            return {"ok": None, "message": "not listening (device disabled or not started) — "
                                           "a pushed source cannot be asked, only heard"}
        where = f"{getattr(drv, 'broker', '')}:{getattr(drv, 'port', '')}"
        st = drv.get_stats() or {}
        try:
            dh = drv.data_health() or {}
        except Exception:  # noqa: BLE001
            dh = {}
        if not st.get('connected'):
            return {"ok": False, "message": f"not connected to {where}", "where": where}
        # the device's own word first: an availability message is no value
        if dh.get('available') is False:
            return {"ok": False, "message": f"connected to {where} — the device says it is offline",
                    "where": where}
        if dh.get('status') == 'idle':
            return {"ok": None, "message": f"connected to {where} — no value selected to read",
                    "where": where}
        n = int(st.get('messages') or 0)
        if not n:
            dropped = int(getattr(drv, 'retained_dropped', 0) or 0)
            why = (f" — {dropped} retained message(s) ignored (accept_retained is off)" if dropped
                   else " — nothing on its topics yet")
            return {"ok": None, "message": f"connected to {where}{why}", "where": where}
        age = dh.get('staleness_age_s')
        said = f"connected to {where} · {n} messages" + (f", last {age} s ago" if age is not None else "")
        if dh.get('stale') or dh.get('status') == 'down':
            return {"ok": False, "message": said + " — older than its stale limit", "where": where}
        return {"ok": True, "message": said, "where": where}

    def _driver_of(client, src):
        """The running input of one source of a device (MultiSourceClient.parts)."""
        for s, drv in (getattr(client, 'parts', None) or []):
            if getattr(s, 'id', None) == getattr(src, 'id', None):
                return drv
        return None

    @app.post("/api/devices/{device_id}/test")
    @_device_runtime
    def test_device(device_id: str):
        """Probe a saved device (TCP or RTU). Uses its first selected register
        (a real address beats address 0) when one exists."""
        _idx, dev_cfg, _client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        if dev_cfg.protocol == 'none':
            from .meter_links import links_of as _lo
            by = ', '.join(dict.fromkeys(ln['device'] for ln in _lo(dev_cfg))) or 'no meter'
            return {'ok': None, 'message': f"nothing to test — measured by {by}; test those devices"}
        _one_test_at_a_time(f"device:{device_id}")
        try:
            return _test_saved_device(dev_cfg, _client)
        finally:
            _test_done(f"device:{device_id}")

    def _can_test(can_cfg: Dict, client=None) -> Dict:
        """A CAN device's Test: the interface (exists, up, listen-only) and
        what it carries for ~2 s, heard on a receive-only socket; for a saved
        device, what its running driver has counted too. One at a time per
        interface."""
        from .can_client import hear, hear_verdict, stats_summary
        key = f"can:{can_cfg.get('channel') or 'can0'}"
        _one_test_at_a_time(key)
        try:
            h = hear(can_cfg, seconds=2.0)
        finally:
            _test_done(key)
        ok, msg = hear_verdict(h)
        out = {"ok": ok, "message": msg, "can": h}
        drv = stats_summary(client.get_stats() or {}) if client is not None and hasattr(client, 'get_stats') else None
        if drv:
            out["driver"] = drv
            out["message"] += (f" · the device has counted {drv.get('frames') or 0} frames, "
                               f"{drv.get('unknown_frames') or 0} on unknown ids, "
                               f"{drv.get('decode_errors') or 0} decode errors")
        return out

    def _test_saved_device(dev_cfg, _client) -> Dict:
        if dev_cfg.protocol in CAN_PROTOCOLS:
            srcs = dev_cfg.sources or []
            return _can_test(dict(getattr(srcs[0], 'can', None) or {}) if srcs else {}, _client)
        if dev_cfg.protocol == 'rtu_tap' or (dev_cfg.protocol in MODBUS_PROTOCOLS
                                             and _listening_template(dev_cfg.template)):
            # a tap, or a stored device the runtime starts as one (ADR-0025)
            return _tap_hearing(dev_cfg, _client)
        if dev_cfg.protocol in VIRTUAL_MAP_PROTOCOLS:
            return _virtual_map_hearing(dev_cfg, _client)
        # every source it is read through, each the way that source reads it
        # (a pushed MQTT source: what its running input hears)
        srcs = [x for x in (dev_cfg.sources or []) if getattr(x, 'enabled', True)]
        if srcs and (len(srcs) > 1 or str(srcs[0].protocol).lower() in ('mqtt', 'http')):
            def address_of(src=None):
                rr, _g = config.load_device_registers(dev_cfg, source=src)
                return rr[0].address if rr else 0
            rows = [{'source': x.id, **_source_probe(dev_cfg, x, address_of, _client)} for x in srcs]
            if len(rows) == 1:
                return {k: v for k, v in rows[0].items() if k != 'source'}
            oks = [r.get('ok') for r in rows]
            return {"ok": all(o is True for o in oks) if None not in oks else (False if False in oks else None),
                    "message": " · ".join(f"{r['source']}: {r.get('message', '')}" for r in rows),
                    "sources": rows}
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
                        has_primary=config.primary_device is not None,
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
                     ("ui", "auth", "password"), ("ui", "auth", "viewer_password"),
                     ("ui", "auth", "operator_password"),
                     ("alerts", "webhook_headers"),   # holds the webhook X-API-Key/bearer
                     ("rest_push", "headers")]        # primary device REST-push auth headers

    def _strip_device_secrets(data: dict):
        """Redact the secrets that live inside the devices[] and endpoints[]
        trees, wherever they sit: a device's connection / http / mqtt_in /
        rest_push blocks, each of its sources, every installation group's
        sources. Secret-named fields are dropped and every URL goes through
        redact_url (userinfo and secret query values). These are NOT top-level
        so _strip_paths misses them. The import side (snapshots.
        _reinject_secret_tree) recognizes the stripped and redacted forms and
        keeps the live originals on a merge-import. (3.83.0: the old version
        only knew connection.* and rest_push.*, so http.headers, mqtt_in.
        password and per-source credentials went out in a "sanitized" export
        any viewer could download.)"""
        from .redact import redact_url
        from .snapshots import SECRET_FIELDS

        def _strip(node):
            if isinstance(node, dict):
                for k in list(node.keys()):
                    if k in SECRET_FIELDS:
                        node.pop(k, None)
                    elif k == "url" and isinstance(node[k], str) and node[k]:
                        node[k] = redact_url(node[k])
                    else:
                        _strip(node[k])
            elif isinstance(node, list):
                for item in node:
                    _strip(item)
        for sect in ("devices", "endpoints"):
            _strip(data.get(sect))
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
            # admin only while login is on; on an auth-off box the API key is
            # the only gate there is (3.83.0: the key no longer stands in for
            # the role — every operator holds it)
            _is_admin = auth_state.enabled and getattr(request.state, "role", None) == "admin"
            _key_ok = (not auth_state.enabled) and bool(_api_key) and hmac.compare_digest(
                request.headers.get("X-API-Key", ""), _api_key)
            if not (_is_admin or _key_ok):
                raise HTTPException(status_code=403, detail={"errors": [
                    "exporting secrets/identity requires the admin role"
                    + ("" if auth_state.enabled else " or a valid API key")]})
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
            # calculated presets and the asset attributes travel with every
            # backup; the passkey registry (identity) only when secrets do.
            # (rules.yaml / sequences.yaml are not in this export: an import
            # would replace the live rules, armed modes included — a decision
            # of its own, not a side effect)
            for name in ("calculated_templates.json",):
                p = cfg_dir / name
                if p.exists():
                    z.writestr(name, p.read_text())
            _attrs_p = cfg_dir / "attributes.yaml"
            if _attrs_p.exists():
                from .snapshots import strip_attribute_locations as _strip_locs
                _raw_attrs = _attrs_p.read_bytes()
                # coordinates are identity: only with include_identity
                try:
                    z.writestr("attributes.yaml", _raw_attrs if include_identity else _strip_locs(_raw_attrs))
                except ValueError as e:      # a broken file is left out, never half-stripped
                    logger.error("export: %s — attributes.yaml left out", e)
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
    async def import_config(request: Request, apply: bool = Query(default=True),
                            restore_identity: bool = Query(default=False)):
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
        from .snapshots import validate_bundle as _validate_bundle
        _verrs = _validate_bundle(zf, allow_nonlan=config.security.allow_nonlan_http_devices)
        if _verrs:
            raise HTTPException(status_code=422, detail={"errors": _verrs})
        # safety net: the pre-import state is one click away if the backup is bad
        try:
            snapshot_store.create("pre-import",
                                  user=getattr(request.state, "user", "") or "")
        except Exception:  # noqa: BLE001
            logger.exception("pre-import snapshot failed")
        def _import_on_disk():
            # sanitized backups merge config.yaml over the live file so stripped
            # secrets/identity survive; per-device register paths are mapped
            # through device_registers_path (the primary keeps its legacy root
            # file — writing devices/<primary>/ would be a dead copy).
            s = _write_bundle_files(
                zf, cfg_dir=cfg_dir,
                user_tpl_dir=_Path(getattr(template_registry, "user_dir", _UDIR)),
                registers_path_for=config.device_registers_path,
                replace_config=False)
            s["_identity_rotated"] = _reload_from_disk(apply, restore_identity=restore_identity)
            return s
        try:
            # N fsyncs and a full Modbus reconnect: off the event loop (F-38, 3.83.0)
            summary = await asyncio.to_thread(_import_on_disk)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        _rotated = summary.pop("_identity_rotated", False)
        _restart = len(config.devices) > 1 or _reload_state['primary_changed']
        summary["note"] = ("imported; a restart is recommended so newly-added "
                           "devices start polling") if _restart else "imported"
        if _reload_state['primary_changed']:
            summary["note"] = ("imported; the legacy primary device (primary_device) "
                               "changed — restart the gateway to apply it")
        summary["restart_required"] = _restart
        logger.info(f"config import: {summary}")
        resp = {"status": "ok", **summary, "dangling": _dangling_after_restore("config import")}
        if _rotated:
            return _reissued_response(request, resp)
        return resp

    def _dangling_after_restore(what: str) -> List[Dict]:
        """An import or a restore replaces the configuration as one set, so it
        is not refused for what it leaves out — but the references it makes
        to devices it does not have are said (in the answer and the event
        log), read from the files it just wrote."""
        from .dependents import Sources, dangling
        from .rules_runtime import RuleStore
        try:
            rules = {str(r.get('id') or ''): r for r in RuleStore(config.config_path.parent / 'rules.yaml').load()}
            store = getattr(app.state, 'sequence_store', None)
            out = dangling(Sources(config=config, rules=lambda: rules,
                                   sequences=(store.load if store is not None else list)))
        except Exception:  # noqa: BLE001 — a report, never a failed import
            logger.warning("%s: dangling references not judged", what, exc_info=True)
            return []
        if out:
            names = sorted({f"{d['id']} → {d['missing']}" for d in out})
            event_log.add("warn", "config", f"{what}: {len(names)} reference(s) to devices that are not here: "
                                            + ', '.join(names[:10]) + (' …' if len(names) > 10 else ''))
        return out

    def _identity_fields() -> tuple:
        """The ui.auth fields that make up login identity — compared around a
        disk reload to detect that an import/restore rotated credentials."""
        u = config.ui
        return (u.auth_enabled, u.auth_username, u.auth_password,
                getattr(u, "viewer_username", "") or "",
                getattr(u, "viewer_password", "") or "",
                getattr(u, "operator_username", "") or "",
                getattr(u, "operator_password", "") or "")

    _reload_state: Dict[str, bool] = {'primary_changed': False}

    def _reload_from_disk(apply: bool = True, restore_identity: bool = False) -> bool:
        """Reload config + registries from disk and hot-apply — the shared tail
        of backup import and snapshot restore (restart-lite). Returns True when
        the reload rotated identity: all sessions were revoked, so the caller
        should re-issue its own cookie (like update_ui_security does)."""
        _before = _identity_fields()
        _primary_before = config.primary_enabled
        _gw_before = (config.identity_scope, config.gateway_id)
        config.load()
        if not restore_identity and (config.identity_scope, config.gateway_id) != _gw_before:
            # how this gateway names new devices is its own, not the bundle's
            # (another gateway's backup, last month's snapshot): kept unless
            # the restore asks for it (ADR-0026 §3)
            config.set_identity(scope=_gw_before[0], gateway_id=_gw_before[1])
            try:
                config.save_yaml_config()
            except Exception:  # noqa: BLE001 — kept in memory; the next save writes it
                logger.exception("restore: the live gateway identity could not be written back")
        # device #1 appearing or going (ADR-0024) changes the pollers, the
        # publishers' HA identity and the live store: only a restart builds them
        _reload_state['primary_changed'] = config.primary_enabled != _primary_before
        attributes._invalidate()          # a restored or imported attributes.yaml
        # hot-apply imported login settings so a backup that enables/disables auth
        # or rotates credentials takes effect immediately (the middleware reads
        # auth_state live) — matches update_ui_security(), not a restart-only change
        auth_state.reload(config.ui)
        # Identity rotated by the bundle: pre-existing cookies must not outlive
        # it. update_ui_security() revokes on rotation, but import/restore reach
        # reload() directly and used to skip the revocation — "restored last
        # week's snapshot to kick the attacker" left the attacker's session
        # valid for up to 30 days.
        revoked = False
        if _identity_fields() != _before:
            n = auth_state.revoke_all_sessions()
            revoked = True
            logger.warning("import/restore rotated identity — "
                           f"{n} session(s) revoked")
        # The bundle may carry its own passkeys.json (full-fidelity snapshots
        # do): re-read the live store, else assertions keep validating against
        # the pre-restore credentials and the next sign-count update would
        # overwrite the restored file with the stale list.
        _pk = getattr(getattr(app.state, "ctx", None), "passkey_store", None)
        if _pk is not None:
            _pk.reload()
            # prune credentials whose account no longer exists — mirrors the
            # ui-security rename/remove pruning (F-16); login_finish refuses
            # them too, so this is defense-in-depth plus registry hygiene
            _live = {u for u in (auth_state.admin_user, auth_state.operator_user,
                                 auth_state.viewer_user) if u}
            for _c in _pk.list():
                if _c.get("user") not in _live:
                    _pk.delete(_c["id"])
                    audit_log.append(user="-", ip="-", action="passkey pruned",
                                     target=str(_c.get("user", "")), status="ok",
                                     detail={"reason": "account absent after import/restore",
                                             "label": _c.get("label", "")})
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
        try:
            _publish_attribute_meta()     # what the restore or import changed reaches the broker
        except Exception:  # noqa: BLE001
            logger.exception("attributes meta after reload")
        return revoked

    def _reissued_response(request: Request, resp: Dict) -> Response:
        """JSON response carrying a fresh admin cookie — after an identity
        rotation revoked every session, the admin DOING the import/restore
        must not be logged out by their own action (same pattern as
        update_ui_security). Only for a caller who WAS a logged-in admin:
        an anonymous caller importing a bundle that turns auth ON must log
        in with the imported credentials, not be handed a session."""
        out = JSONResponse(resp)
        if auth_state.enabled and getattr(request.state, "role", None) == "admin":
            token = auth_state.mint_session("admin", auth_state.admin_user)
            secure = bool(config.ui.tls_enabled) or request.url.scheme == "https"
            out.set_cookie(_auth.COOKIE_NAME, token, httponly=True,
                           samesite="lax", secure=secure,
                           max_age=_auth.SESSION_TTL_S, path="/")
        return out

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
        _key_ok = (not auth_state.enabled) and bool(_api_key) and hmac.compare_digest(
            request.headers.get("X-API-Key", ""), _api_key)
        if not (_is_admin or _key_ok):
            raise HTTPException(status_code=403, detail={"errors": [
                "downloading a snapshot requires the admin role"
                + ("" if auth_state.enabled else " or a valid API key")]})
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
    def restore_snapshot(request: Request, sid: str, apply: bool = Query(default=True),
                         restore_identity: bool = Query(default=False)):
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
                from .snapshots import validate_bundle as _validate_bundle
                _verrs = _validate_bundle(zf, allow_nonlan=config.security.allow_nonlan_http_devices)
                if _verrs:
                    raise HTTPException(status_code=422, detail={"errors": _verrs})
                from .device_template import USER_DIR as _UDIR
                summary = _write_bundle_files(
                    zf, cfg_dir=config.config_path.parent,
                    user_tpl_dir=_Path(getattr(template_registry, "user_dir", _UDIR)),
                    registers_path_for=config.device_registers_path,
                    replace_config=True)
        except (ValueError, zipfile.BadZipFile) as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        _rotated = _reload_from_disk(apply, restore_identity=restore_identity)
        event_log.add("warn", "snapshots", f"config rolled back to snapshot {sid}")
        logger.warning(f"config restored from snapshot {sid}: {summary}")
        # _reload_from_disk hot-applies the PRIMARY only; non-primary devices are
        # reconstructed on the next restart, so tell the operator when one is
        # needed (more than one device present) rather than leave it implicit.
        restart_required = len(config.devices) > 1 or _reload_state['primary_changed']
        resp = {"status": "ok", "restored": sid, "restart_required": restart_required,
                **summary, "dangling": _dangling_after_restore(f"snapshot {sid} restore")}
        if _rotated:
            return _reissued_response(request, resp)
        return resp

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
                'device': config.primary_id,   # the snapshot is the primary's
                # snapshot, NOT the live dict (external audit: json.dumps
                # walking the live store while pollers insert raises
                # 'dictionary changed size' and drops the socket — the same
                # bug values_routes.py already documents and fixes)
                'values': dict(current_values),
                'timestamp': last_update['timestamp'],
                'ts': last_update['ts'],
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
    from .routes import (ApiCtx, attributes as attributes_routes, auth_routes, calculated,
                         commissioning, config as config_routes, device_templates,
                         diagnostics, discovery_routes, energy, general_config,
                         languages, metrics, pq, registers_routes, rules as rules_routes,
                         sequences as sequences_routes, opcua as opcua_routes, role_routes,
                         status_routes, system, values_routes, vmeters, bridges as bridges_routes)

    def _restart_devices(device_ids: List[str]) -> List[str]:
        """Rebuild the running client of these devices from the CURRENT config
        (a bridge's new host, a template's new word order)."""
        out = []
        current = {d.id: d for d in config.devices}
        # a device's components read through its connection: they follow it
        device_ids = list(device_ids) + [d.id for d in config.devices
                                         if d.component_of and d.component_of in device_ids
                                         and d.id not in device_ids]
        for dev_cfg, client in list(registry):
            if dev_cfg.id not in device_ids or dev_cfg.primary:
                continue
            fresh = current.get(dev_cfg.id, dev_cfg)
            try:
                if client:
                    client.disconnect()
                new_client = _start_device_client(fresh)
                registry.replace(fresh.id, fresh, client=new_client, add_if_missing=True)
                out.append(fresh.id)
            except Exception as e:  # noqa: BLE001 — one device must not stop the rest
                logger.error("restart of %s failed: %s", fresh.id, e)
        return out

    def _restart_devices_using_template(template_id: str) -> List[str]:
        """A map's protocol (word order, read size) is read when a client is
        made: restart every running device that reads with it, so a saved
        change takes effect now instead of at the next container restart."""
        def uses(tid: str) -> bool:
            # a component's template is `<parent>:<type>`: it changes with it
            return tid == template_id or tid.startswith(template_id + ':')
        ids = [d.id for d, _c in list(registry) if not d.primary and any(uses(t) for t in (
            {getattr(d, 'template', '') or ''}
            | {getattr(sx, 'template', '') or '' for sx in (getattr(d, 'sources', None) or [])}))]
        out = _restart_devices(ids)
        if out:
            logger.info("template %s: protocol changed — restarted %s", template_id, ", ".join(out))
        return out

    def _on_role_change(old: str, new: str) -> None:
        """The gateway became standby or active (PUT /api/role): stop or start
        what it does. Writes are already refused or allowed by then — this only
        winds the pollers and outputs down or up, off the request thread
        (reconnecting devices takes time)."""
        standby = new != 'active'
        if standby:
            # nothing waiting for a bus is written by a standby; it says
            # nothing on cmd/result either (ADR-0018 D10, D12)
            _cmd_dispatch.cancel(reason=f"the gateway became {new} — nothing written")

        def run():
            for _dc, client in registry.pairs():
                fn = getattr(client, 'standby' if standby else 'resume', None)
                if fn is None:
                    continue
                try:
                    fn()
                except Exception as e:  # noqa: BLE001 — one device must not stop the others
                    logger.warning("role %s: device %s — %s", new, getattr(_dc, 'id', '?'), e)
            if standby:
                seqs = getattr(app.state, 'sequences', None)
                for sid in list(getattr(seqs, 'running', {}) or {}):
                    seqs.cancel(sid, who='standby')        # a half-done sequence is never resumed blindly
                for _dc, _c in registry.pairs():
                    _lease_mgr.clear_device(_dc.id)        # the active one owns the devices now
                rest_push_manager.stop_all()
            else:
                if old == 'standby':
                    app.state.role_warm_until = time.monotonic() + TAKEOVER_WARMUP_S
                    _take_over_partner_state()
                try:
                    _publish_attribute_meta()               # the retained attributes are now this one's
                except Exception as e:  # noqa: BLE001
                    logger.warning("role active: attributes meta — %s", e)
                for _dc, _c in registry.pairs():
                    _apply_rest_push(_dc)
                _sync_device_discovery()
                if mqtt_publisher is not None:
                    try:
                        mqtt_publisher.publish_ha_discovery()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("role active: discovery — %s", e)
            ctl = getattr(app.state, 'opcua', None)
            if ctl is not None:
                ctl.apply()
            for fn in list(getattr(app.state, 'role_hooks', [])):   # main.py: virtual meters, PQ recorder
                try:
                    fn(old, new)
                except Exception as e:  # noqa: BLE001
                    logger.warning("role %s: %s — %s", new, getattr(fn, '__name__', fn), e)
            event_log.add('warning', 'gateway', f"role {old} → {new} (epoch {gw_role.epoch})", 'role')
            logger.warning("gateway is %s: pollers and outputs %s", new, 'stopped' if standby else 'started')

        threading.Thread(target=run, name=f"role-{new}", daemon=True).start()

    def _take_over_partner_state() -> None:
        """What the partner held when this gateway took over: its write leases
        go on here with the time they had left (the dead-man keeps watching);
        a sequence it was running is said, not resumed."""
        pm = getattr(app.state, 'peer_mirror', None) or {}
        age = max(0.0, time.time() - float(pm.get('at') or time.time()))
        for m in pm.get('leases') or []:
            try:
                left_ms = int(max(0.0, float(m.get('remaining_s') or 0) - age) * 1000)
                meta = {k: v for k, v in m.items() if k != 'remaining_s'}
                if m.get('command'):
                    rv = _make_command_lease_revert(m['device'], m['command'])
                else:
                    rv = _make_lease_revert(m['device'], m['register_type'], int(m['address']), m['data_type'],
                                            float(m['scale']), m['safe_value'], off=float(m.get('offset', 0.0) or 0.0))
                _lease_mgr.arm(m['device'], m['register_type'], int(m['address']), max(1, left_ms), rv,
                               meta=meta, fire_now=left_ms <= 0)
                logger.warning("take-over: write lease of %s @%s goes on here (%.1f s left)",
                               m['device'], m.get('address'), left_ms / 1000)
            except Exception as e:  # noqa: BLE001
                logger.error("take-over: the partner's lease %r could not be re-armed: %s", m, e)
                alert_mgr.fire('error', 'role:lease', 'gateway', f"a write lease of the partner could not be re-armed: {e}")
        for run in pm.get('sequences') or []:
            msg = (f"sequence {run.get('sequence')} was running on the partner (step {run.get('step')}, "
                   f"by {run.get('by')}) when this gateway took over: it is NOT resumed — check its devices")
            event_log.add('warning', 'gateway', msg, 'role')
            alert_mgr.fire('warning', f"role:sequence:{run.get('sequence')}", 'gateway', msg, transition=True)
        app.state.peer_mirror = {}

    gw_role.on_change(_on_role_change)
    app.state.role_hooks = []

    ctx = ApiCtx(
        restart_devices_using_template=_restart_devices_using_template,
        restart_devices=_restart_devices,
        app=app, config=config, registry=registry, calc_engine=calc_engine,
        event_log=event_log, alert_mgr=alert_mgr,
        auth_state=auth_state, api_key=_api_key, service_tokens=service_tokens,
        template_registry=template_registry,
        current_values=current_values, last_update=last_update,
        modbus_client=modbus_client, ws_manager=ws_manager,
        mqtt_publisher=mqtt_publisher, influxdb_publisher=influxdb_publisher,
        audit_log=audit_log,
        ip_allowed=_ip_allowed,   # WS routes enforce the allowlist themselves
        rule_fields_changed=_fields_cache.clear,
        dependents_of=_dependents,
        mutation_lock=_device_mutation_lock, entry_lock=_entry_lock,
        device_write=_device_write, device_runtime=_device_runtime,
        device_write_query=_device_write_query,
        validate_connection=_dry_run_connection,
        write_primary_modbus=_write_primary_modbus,
    )
    app.state.ctx = ctx
    for _mod in (calculated, commissioning, config_routes,
                 device_templates, diagnostics, discovery_routes, energy,
                 general_config, languages, metrics, pq, registers_routes, rules_routes, sequences_routes, opcua_routes,
                 role_routes,
                 status_routes, system, auth_routes, values_routes, vmeters, bridges_routes,
                 attributes_routes):
        app.include_router(_mod.build(ctx))
    app.include_router(auth_routes.build_passkeys(ctx))
    from .routes import device_types as device_types_routes      # ADR-0027
    app.include_router(device_types_routes.build(ctx))
    from .routes import buses as buses_routes                    # ADR-0031 §5
    app.include_router(buses_routes.build(ctx))
    from .routes import previews as previews_routes              # ADR-0031 §5, L3.8
    app.include_router(previews_routes.build(ctx))
    from .routes import bus_scan as bus_scan_routes              # ADR-0031 §2, L3.7
    app.include_router(bus_scan_routes.build(ctx))

    # One device, as the list shows it. Registered after every static
    # /api/devices/<word> route (restorable, …), which it would otherwise hide.
    @app.get("/api/devices/{device_id}")
    def get_device(device_id: str, request: Request, response: Response):
        """One device's entry (as in GET /api/devices), with its
        ``connection_spec``, and its ``ETag`` (what ``If-Match`` names on
        PUT / DELETE)."""
        _i, dev_cfg, client = _find_device(device_id)
        if dev_cfg is None:
            raise HTTPException(status_code=404, detail="device not found")
        _role = getattr(request.state, "role", None)
        response.headers['ETag'] = _device_etag(dev_cfg)
        return {"device": _device_entry(dev_cfg, client, redact=_role is not None and _role != "admin")}

    # --- Static files ---

    # Mount static files last
    app.mount("/static", StaticFiles(directory="ui"), name="static")

    # stored topic prefixes that share topics: said once, never refused (ADR-0026 §4)
    try:
        from .topic_prefix import report_stored
        report_stored(config)
    except Exception:  # noqa: BLE001 — a report must never stop the gateway
        logger.debug("topic prefix report skipped", exc_info=True)

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

    # the dead-man sweeper last: every revert it may fire (boot recovery's
    # first) finds the whole write path defined (RP-12)
    _lease_mgr.start()

    return app, ws_manager
