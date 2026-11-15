# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""HTTP middleware + security guards, extracted whole from create_api (P0.8b squeeze).

``Middleware.__init__`` registers, on the ``app`` passed to it, the same guards in the same
order ``create_api`` did: the ``ConfigSaveBlocked`` exception handler, then the allowlist /
write / standby / auth / snapshot-trigger / audit guards, and finally the security-headers
wrapper (registered last → outermost). Bodies are verbatim closures; the per-request policy
constants moved with them to module scope. Collaborators are an explicit keyword constructor;
``req_user`` / ``req_ip`` / ``ip_allowed`` are exposed for ApiCtx and the websocket. Build it
late, after every collaborator binds; CORS + GZip stay in create_api (added earlier, so the
middleware stack order is preserved).
"""
from __future__ import annotations

import hmac
import json
import logging
import re as _re

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

from multibus import auth as _auth
from multibus.api.ui_assets import _render_index_html
from multibus.core.config import ConfigSaveBlocked
from multibus.core.ip_allow import metrics_peer_allowed, parse_networks, peer_in_networks
from multibus.service_tokens import bearer as _bearer

logger = logging.getLogger("multibus.api")  # preserve caplog logger name

# --- per-request policy constants (moved verbatim from create_api) -----------
_CSP = ("default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self' data:; "
        "img-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")

_open_writes = {"/api/query/register", "/api/query/batch",
                # the credential-establishing POSTs: an API key gate in front
                # of them made API_KEY + login unusable together (3.80.0)
                "/api/auth/login", "/api/auth/passkey/login/begin",
                "/api/auth/passkey/login/finish",
                "/api/auth/logout"}  # POST but read-only (any role ends its own session)

_OPERATOR_WRITE_PREFIXES = ("/api/bus-trace", "/api/diagnostics", 
                            "/api/discover", "/api/query",
                            "/api/previews",          # the read preview (ADR-0031 §5)
                            "/api/bus-scan",          # the bus scan / Listen for addresses (L3.7)
                            "/api/auth/logout", "/api/alerts/test",
                            "/api/config/reload-registers",
                            # self-service passkey enrollment/removal
                            "/api/auth/passkey", "/api/auth/passkeys")

_HA_WRITES = {"/api/role", "/api/alerts/fire", "/api/state/mirror", "/api/state/config"}

_auth_open = {"/api/auth/login", "/api/auth/status", "/health", "/metrics", "/favicon.ico",
              "/api/auth/forward",     # checks the session itself (Traefik forwardAuth)
              # passkey assertion happens BEFORE a session exists
              "/api/auth/passkey/login/begin", "/api/auth/passkey/login/finish"}

_auth_open_prefixes = ("/static/",)

_STANDBY_REFUSED = _re.compile(
    r"^/api/(discover/(modbus|sunspec)(/|$)|query/|diagnostics/probe$|meters(/|$)"
    r"|devices/test$|devices/[^/]+/test$|endpoints/[^/]+/test$|devices/[^/]+/rest-push/test$"
    r"|previews$|devices/[^/]+/preview$|bus-scan$"
    r"|bridges/[^/]+/(scan|probe|ports/[^/]+/scan)$|device-templates/identify-test$)")

_SNAP_PREFIXES = ("/api/devices", "/api/config/", "/api/registers/selected",
                  "/api/device-templates", "/api/virtual-meters",
                  "/api/calculated", "/api/energy/fields", "/api/poll-groups",
                  "/api/device-types", "/api/buses")

_SNAP_EXCLUDE = ("/test", "/write", "/payload-sample", "/api/config/import",
                 "/api/device-templates/check", "/identify-test",
                 "/api/config/reload-registers", "/api/config/snapshots",
                 "/components/discover", "/preview")

_BODY_CAP = 8 * 1024 * 1024                 # every JSON route

_BODY_CAP_IMPORT = 25 * 1024 * 1024         # matches the import handler's own cap

_BODY_CAP_LARGE_PATHS = ('/api/config/import',)

_AUDIT_SKIP = ("/api/query", "/api/diagnostics/probe", "/api/discover",
               "/api/device-templates/check", "/identify-test",
               "/api/auth/", "/api/alerts/test", "/api/devices/test",
               "/api/bus-trace")


class Middleware:
    """The HTTP middleware / security-guard stack (ADR-0038)."""

    def __init__(self, *, app, config, auth_state, gw_role, snapshot_store, audit_log, service_tokens, _api_key):
        @app.exception_handler(ConfigSaveBlocked)
        async def _save_blocked(_request, exc):
            return JSONResponse(status_code=409, content={"detail": {"errors": [str(exc)], "config_blocked": True}})

        def _allow_networks():
            sec = getattr(config, 'security', None)
            return parse_networks(sec.allowlist if sec else [], "security.allowlist")

        def _ip_allowed(peer: str) -> bool:
            nets = _allow_networks()
            if not nets:
                return True                       # allowlist empty => open
            return peer_in_networks(peer, nets)

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
                # /metrics also admits security.metrics_allowlist (OBS-PROM-1): a
                # scraper listed there reaches /metrics and nothing else
                if not _ip_allowed(peer) and not (
                        request.url.path == "/metrics" and metrics_peer_allowed(config, peer)):
                    return JSONResponse({"detail": "forbidden (IP not in allowlist)"},
                                        status_code=403)
            return await call_next(request)

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

        def _req_user(request) -> str:
            """Who asked, for an audit line (a call from inside the gateway: '-')."""
            if request is None:
                return '-'
            return getattr(request.state, "user", None) or ("api-key" if _api_key else "anon")

        def _req_ip(request) -> str:
            return (request.client.host if request is not None and request.client else '-')

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

        # register the security-headers wrapper LAST (outermost), as create_api did
        app.middleware("http")(_security_headers)
        # --- public surface (ApiCtx + the websocket) ------------------------
        self.req_user = _req_user
        self.req_ip = _req_ip
        self.ip_allowed = _ip_allowed
