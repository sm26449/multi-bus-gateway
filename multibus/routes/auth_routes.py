"""Login/logout/status — session cookie auth (optional, off by default).

Moved verbatim from create_api(). The enforcement middleware stays in api.py
(middlewares are app-level, not router-level); these are just the endpoints.
"""
from __future__ import annotations

from typing import Dict

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import auth as _auth


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["auth"])
    config, auth_state = ctx.config, ctx.auth_state
    audit = ctx.audit_log

    @r.get("/api/auth/status")
    async def auth_status(request: Request):
        """Whether auth is on, and the caller's current role (if any)."""
        role = None
        if auth_state.enabled:
            role = auth_state.role_for(request.cookies.get(_auth.COOKIE_NAME, ""))
        pk = getattr(ctx, "passkey_store", None)
        return {"enabled": auth_state.enabled, "role": role,
                "has_viewer": bool(auth_state.viewer_user),
                "has_operator": bool(auth_state.operator_user),
                "has_passkeys": bool(pk and pk.count)}

    @r.post("/api/auth/login")
    async def auth_login(request: Request, payload: Dict = Body(...)):
        """Log in; sets an HttpOnly session cookie. Rate-limited per IP."""
        if not auth_state.enabled:
            return {"status": "ok", "role": "admin", "note": "auth disabled"}
        ip = request.client.host if request.client else "?"
        try:
            token, role = auth_state.login(ip, str(payload.get("username", "")),
                                           str(payload.get("password", "")))
        except PermissionError as locked:
            audit.append(user=str(payload.get("username", ""))[:40], ip=ip,
                         action="login", status="locked out")
            raise HTTPException(status_code=429, detail={
                "error": "locked_out", "retry_after_s": int(str(locked))})
        if not token:
            audit.append(user=str(payload.get("username", ""))[:40], ip=ip,
                         action="login", status="invalid credentials")
            raise HTTPException(status_code=401, detail={"error": "invalid_credentials"})
        audit.append(user=str(payload.get("username", ""))[:40], ip=ip,
                     action="login", status="ok", detail={"role": role})
        # Secure flag when the UI is served over HTTPS — either by uvicorn
        # itself (tls_enabled) or by a trusted proxy (scheme rewritten from
        # X-Forwarded-Proto only when ui.trusted_proxies matches the peer)
        secure = bool(config.ui.tls_enabled) or request.url.scheme == "https"
        resp = JSONResponse({"status": "ok", "role": role})
        resp.set_cookie(_auth.COOKIE_NAME, token, httponly=True, samesite="lax",
                        secure=secure, max_age=_auth.SESSION_TTL_S, path="/")
        return resp

    @r.post("/api/auth/logout")
    async def auth_logout(request: Request):
        tok = request.cookies.get(_auth.COOKIE_NAME, "")
        ident = auth_state.identity_for(tok)
        if ident:
            audit.append(user=ident[1], ip=request.client.host if request.client else "-",
                         action="logout", status="ok")
        auth_state.logout(tok)
        resp = JSONResponse({"status": "ok"})
        resp.delete_cookie(_auth.COOKIE_NAME, path="/")
        return resp

    return r


def build_passkeys(ctx) -> APIRouter:
    """WebAuthn endpoints. Login begin/finish are auth-OPEN (a passkey holder
    is not logged in yet) and share the password path's per-IP lockout."""
    import json as _json
    from urllib.parse import urlsplit

    import webauthn
    from webauthn.helpers.structs import (AuthenticatorSelectionCriteria,
                                          PublicKeyCredentialDescriptor,
                                          ResidentKeyRequirement,
                                          UserVerificationRequirement)

    from ..passkeys import (ChallengeCache, PasskeyStore, _from_b64u,
                            rp_id_for_host)

    r = APIRouter(tags=["auth"])
    config, auth_state, audit = ctx.config, ctx.auth_state, ctx.audit_log
    store = PasskeyStore(str(ctx.config.config_path.parent / "passkeys.json"))
    ctx.passkey_store = store
    challenges = ChallengeCache()

    def _rp(request: Request) -> str:
        rp_id = rp_id_for_host(request.url.hostname)
        if rp_id is None:
            raise HTTPException(status_code=422, detail={"errors": [
                "passkeys need a hostname, not an IP — open the UI via "
                "localhost (on the box) or a LAN name like gateway.lan"]})
        return rp_id

    def _origin(request: Request, rp_id: str) -> str:
        """The browser's Origin, validated against the RP ID before it is
        trusted as the expected_origin of the ceremony."""
        origin = request.headers.get("origin", "")
        host = urlsplit(origin).hostname or ""
        if not origin or not (host == rp_id or host.endswith("." + rp_id)):
            raise HTTPException(status_code=422,
                                detail={"errors": ["origin/RP mismatch"]})
        return origin

    def _identity(request: Request):
        """(user, role) allowed to manage passkeys: the logged-in account, or
        the implicit admin while auth is still off (enroll first, enable later)."""
        if not auth_state.enabled:
            return (config.ui.auth_username or "admin"), "admin"
        role = getattr(request.state, "role", None)
        user = getattr(request.state, "user", None)
        if not role:
            raise HTTPException(status_code=401, detail="login required")
        return user or role, role

    # ── enrollment (logged-in / implicit admin) ──────────────────────────────

    @r.post("/api/auth/passkey/register/begin")
    async def register_begin(request: Request, payload: Dict = Body(default={})):
        user, role = _identity(request)
        rp_id = _rp(request)
        opts = webauthn.generate_registration_options(
            rp_id=rp_id, rp_name="Multi-Bus Gateway",
            user_name=user, user_display_name=f"{user} ({role})",
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.PREFERRED),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=_from_b64u(c["id"]))
                for c in store.for_rp(rp_id) if c["user"] == user])
        state = challenges.put(challenge=opts.challenge, user=user, role=role,
                               rp_id=rp_id, label=str(payload.get("label", ""))[:60])
        return {"state": state, "options": _json.loads(webauthn.options_to_json(opts))}

    @r.post("/api/auth/passkey/register/finish")
    async def register_finish(request: Request, payload: Dict = Body(...)):
        user, role = _identity(request)
        meta = challenges.take(str(payload.get("state", "")))
        if not meta or meta["user"] != user:
            raise HTTPException(status_code=422, detail={"errors": ["stale or unknown ceremony — retry"]})
        rp_id = meta["rp_id"]
        try:
            ver = webauthn.verify_registration_response(
                credential=payload.get("credential"),
                expected_challenge=meta["challenge"],
                expected_rp_id=rp_id,
                expected_origin=_origin(request, rp_id))
        except Exception as e:  # noqa: BLE001 — library raises many subtypes
            raise HTTPException(status_code=422, detail={"errors": [f"registration failed: {e}"]})
        entry = store.add(cred_id=ver.credential_id, public_key=ver.credential_public_key,
                          sign_count=ver.sign_count, user=user, role=meta["role"],
                          rp_id=rp_id, label=meta["label"])
        ip = request.client.host if request.client else "-"
        audit.append(user=user, ip=ip, action="passkey registered",
                     target=entry["label"], status="ok", detail={"rp_id": rp_id})
        return {"status": "ok", "passkey": entry}

    @r.get("/api/auth/passkeys")
    async def list_passkeys(request: Request):
        user, role = _identity(request)
        return {"passkeys": store.list(user=None if role == "admin" else user),
                "user": user}

    @r.delete("/api/auth/passkeys/{cred_id}")
    async def delete_passkey(request: Request, cred_id: str):
        user, role = _identity(request)
        ok = store.delete(cred_id, user=None if role == "admin" else user)
        if not ok:
            raise HTTPException(status_code=404, detail="unknown passkey")
        audit.append(user=user, ip=request.client.host if request.client else "-",
                     action="passkey deleted", target=cred_id[:16], status="ok")
        return {"status": "deleted"}

    # ── login (auth-open) ────────────────────────────────────────────────────

    @r.post("/api/auth/passkey/login/begin")
    async def login_begin(request: Request):
        if not auth_state.enabled:
            return {"status": "ok", "note": "auth disabled"}
        ip = request.client.host if request.client else "?"
        locked = auth_state.is_locked(ip)
        if locked:
            raise HTTPException(status_code=429, detail={
                "error": "locked_out", "retry_after_s": locked})
        rp_id = _rp(request)
        creds = store.for_rp(rp_id)
        if not creds:
            raise HTTPException(status_code=404, detail={"errors": [
                f"no passkey is registered for {rp_id!r}"]})
        opts = webauthn.generate_authentication_options(
            rp_id=rp_id,
            allow_credentials=[PublicKeyCredentialDescriptor(id=_from_b64u(c["id"]))
                               for c in creds],
            user_verification=UserVerificationRequirement.PREFERRED)
        state = challenges.put(challenge=opts.challenge, rp_id=rp_id,
                               user="", role="")
        return {"state": state, "options": _json.loads(webauthn.options_to_json(opts))}

    @r.post("/api/auth/passkey/login/finish")
    async def login_finish(request: Request, payload: Dict = Body(...)):
        if not auth_state.enabled:
            return {"status": "ok", "role": "admin", "note": "auth disabled"}
        ip = request.client.host if request.client else "?"
        meta = challenges.take(str(payload.get("state", "")))
        cred = payload.get("credential") or {}
        entry = store.find(str(cred.get("id", "")))
        if not meta or entry is None or entry["rp_id"] != meta["rp_id"]:
            auth_state._record_failure(ip)
            audit.append(user="-", ip=ip, action="login (passkey)",
                         status="unknown credential")
            raise HTTPException(status_code=401, detail={"error": "invalid_passkey"})
        try:
            ver = webauthn.verify_authentication_response(
                credential=cred,
                expected_challenge=meta["challenge"],
                expected_rp_id=meta["rp_id"],
                expected_origin=_origin(request, meta["rp_id"]),
                credential_public_key=_from_b64u(entry["public_key"]),
                credential_current_sign_count=int(entry["sign_count"]))
        except Exception:  # noqa: BLE001
            auth_state._record_failure(ip)
            audit.append(user=entry["user"], ip=ip, action="login (passkey)",
                         status="verification failed")
            raise HTTPException(status_code=401, detail={"error": "invalid_passkey"})
        auth_state._clear_failures(ip)
        store.update_sign_count(entry["id"], ver.new_sign_count)
        token = auth_state.mint_session(entry["role"], entry["user"])
        audit.append(user=entry["user"], ip=ip, action="login (passkey)",
                     status="ok", detail={"role": entry["role"], "label": entry["label"]})
        secure = bool(config.ui.tls_enabled) or request.url.scheme == "https"
        resp = JSONResponse({"status": "ok", "role": entry["role"]})
        resp.set_cookie(_auth.COOKIE_NAME, token, httponly=True, samesite="lax",
                        secure=secure, max_age=_auth.SESSION_TTL_S, path="/")
        return resp

    return r
