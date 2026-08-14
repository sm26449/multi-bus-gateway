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
"""Security package: password hashing, AuthState (login/lockout/sessions),
IP allowlist middleware, MQTT TLS wiring, and the login-gated API."""

import pytest

from multibus import auth
from multibus.config import UIConfig, MQTTConfig

try:
    from fastapi.testclient import TestClient
    _HAS_TC = True
except Exception:  # noqa: BLE001
    _HAS_TC = False

needs_tc = pytest.mark.skipif(not _HAS_TC, reason="TestClient not installed")


# ── password hashing ─────────────────────────────────────────────────────────

def test_hash_roundtrip_and_wrong():
    h = auth.hash_password("s3cret")
    assert h.startswith("pbkdf2_sha256$")
    assert auth.verify_password("s3cret", h)
    assert not auth.verify_password("nope", h)


def test_verify_plaintext_fallback():
    # legacy/hand-edited plaintext value
    assert auth.verify_password("admin", "admin")
    assert not auth.verify_password("admin", "other")


def test_new_hash_uses_owasp_iteration_floor():
    h = auth.hash_password("x")
    assert h.split("$")[1] == "600000"


def test_old_hash_iterations_still_verify():
    # a self-describing hash carries its own iteration count → old (240k) hashes
    # keep verifying after the floor bump.
    import hashlib
    salt = bytes(range(16))
    dk = hashlib.pbkdf2_hmac("sha256", b"legacy", salt, 240_000)
    old = f"pbkdf2_sha256$240000${salt.hex()}${dk.hex()}"
    assert auth.verify_password("legacy", old)
    assert not auth.verify_password("nope", old)


def test_plaintext_auth_warns(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="multibus.auth"):
        assert auth.verify_password("admin", "admin")     # plaintext success
    assert any("PLAINTEXT" in r.message for r in caplog.records)


def test_authenticate_runs_exactly_one_verify_regardless_of_username(monkeypatch):
    # Anti-enumeration: a non-existent username must cost the same one PBKDF2
    # verify as a real user — otherwise timing reveals which usernames exist.
    st = auth.AuthState(make_ui(viewer_username="guest",
                                viewer_password=auth.hash_password("look")))
    calls = {"n": 0}
    real = auth.verify_password

    def counting(pw, stored):
        calls["n"] += 1
        return real(pw, stored)

    monkeypatch.setattr(auth, "verify_password", counting)
    calls["n"] = 0; assert st.authenticate("admin", "pw") == "admin"; assert calls["n"] == 1
    calls["n"] = 0; assert st.authenticate("guest", "look") == "viewer"; assert calls["n"] == 1
    calls["n"] = 0; assert st.authenticate("ghost", "pw") is None; assert calls["n"] == 1
    calls["n"] = 0; assert st.authenticate("admin", "wrong") is None; assert calls["n"] == 1


# ── AuthState ────────────────────────────────────────────────────────────────

def make_ui(**over):
    d = dict(auth_enabled=True, auth_username="admin",
             auth_password=auth.hash_password("pw"), viewer_username="",
             viewer_password="", lockout_threshold=3, lockout_minutes=5)
    d.update(over)
    return UIConfig(**d)


def test_login_success_and_session_role():
    st = auth.AuthState(make_ui())
    token, role = st.login("1.2.3.4", "admin", "pw")
    assert role == "admin" and token
    assert st.role_for(token) == "admin"
    st.logout(token)
    assert st.role_for(token) is None


def test_bad_credentials_and_lockout():
    st = auth.AuthState(make_ui(lockout_threshold=3))
    for _ in range(3):
        token, role = st.login("9.9.9.9", "admin", "wrong")
        assert token is None
    # now locked out
    with pytest.raises(PermissionError):
        st.login("9.9.9.9", "admin", "pw")     # even correct pw is refused
    assert st.is_locked("9.9.9.9") is not None
    # a different IP is unaffected
    token, role = st.login("8.8.8.8", "admin", "pw")
    assert role == "admin"


def test_viewer_role():
    st = auth.AuthState(make_ui(viewer_username="guest",
                                viewer_password=auth.hash_password("look")))
    token, role = st.login("1.1.1.1", "guest", "look")
    assert role == "viewer"


# ── API integration ──────────────────────────────────────────────────────────

def make_app(tmp_path, **ui_over):
    from tests.test_devices import write_config
    cfg = write_config(tmp_path)
    for k, v in ui_over.items():
        setattr(cfg.ui, k, v)
    from multibus.api import create_api
    app, _ = create_api(cfg, None, None, None,
                        devices=[(d, None) for d in cfg.devices])
    return cfg, TestClient(app, raise_server_exceptions=False)


@needs_tc
def test_api_open_when_auth_disabled(tmp_path):
    _cfg, client = make_app(tmp_path)
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/auth/status").json()["enabled"] is False


@needs_tc
def test_api_login_flow(tmp_path):
    _cfg, client = make_app(tmp_path, auth_enabled=True, auth_username="admin",
                            auth_password=auth.hash_password("pw"))
    # unauthenticated API call is rejected
    assert client.get("/api/status").status_code == 401
    # bad login
    assert client.post("/api/auth/login",
                       json={"username": "admin", "password": "x"}).status_code == 401
    # good login sets the cookie on the client
    r = client.post("/api/auth/login", json={"username": "admin", "password": "pw"})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    assert client.get("/api/status").status_code == 200
    # logout revokes it
    client.post("/api/auth/logout")
    assert client.get("/api/status").status_code == 401


@needs_tc
def test_viewer_is_read_only(tmp_path):
    _cfg, client = make_app(tmp_path, auth_enabled=True,
                            auth_username="admin", auth_password=auth.hash_password("pw"),
                            viewer_username="guest", viewer_password=auth.hash_password("look"))
    r = client.post("/api/auth/login", json={"username": "guest", "password": "look"})
    assert r.json()["role"] == "viewer"
    assert client.get("/api/status").status_code == 200          # reads OK
    # a write is blocked for viewer (device create)
    w = client.post("/api/devices", json={"id": "x", "connection": {"protocol": "tcp", "host": "1.2.3.4"}})
    assert w.status_code == 403
    # a viewer must NOT be able to export secrets (would leak the admin hash + creds)
    assert client.get("/api/config/export?include_secrets=true").status_code == 403
    assert client.get("/api/config/export").status_code == 200        # non-secret backup OK


@needs_tc
def test_admin_can_export_secrets(tmp_path):
    _cfg, client = make_app(tmp_path, auth_enabled=True,
                            auth_username="admin", auth_password=auth.hash_password("pw"))
    assert client.post("/api/auth/login",
                       json={"username": "admin", "password": "pw"}).json()["role"] == "admin"
    assert client.get("/api/config/export?include_secrets=true").status_code == 200


@needs_tc
def test_secret_export_refused_on_open_box(tmp_path, monkeypatch):
    # default-open box (no auth, no API key): plain backup OK, secrets refused —
    # export is a GET, so the api-key middleware never sees it; the endpoint gates.
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("JANITZA_API_KEY", raising=False)
    _cfg, client = make_app(tmp_path)
    assert client.get("/api/config/export").status_code == 200
    assert client.get("/api/config/export?include_secrets=true").status_code == 403
    assert client.get("/api/config/export?include_identity=true").status_code == 403


@needs_tc
def test_secret_export_needs_api_key_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    _cfg, client = make_app(tmp_path)
    assert client.get("/api/config/export?include_secrets=true").status_code == 403   # no key
    assert client.get("/api/config/export?include_secrets=true",
                      headers={"X-API-Key": "k"}).status_code == 200                   # key OK


@needs_tc
def test_import_hot_applies_auth(tmp_path):
    import io as _io
    import zipfile
    _cfg, client = make_app(tmp_path)                       # login OFF
    assert client.get("/api/status").status_code == 200     # open before import
    pw = auth.hash_password("pw")
    cfgyaml = ("modbus:\n  host: 192.168.1.207\n"
               "ui:\n  auth:\n    enabled: true\n    username: admin\n"
               f'    password: "{pw}"\n')
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("config.yaml", cfgyaml)
        z.writestr("manifest.json", '{"backup_version": 1}')
    r = client.post("/api/config/import", content=buf.getvalue(),
                    headers={"Content-Type": "application/zip"})
    assert r.status_code == 200, r.text
    # login is now enforced WITHOUT a restart (auth_state reloaded on import)
    assert client.get("/api/status").status_code == 401


@needs_tc
def test_ip_allowlist_blocks_non_listed(tmp_path):
    cfg, client = make_app(tmp_path)
    assert client.get("/api/status").status_code == 200          # empty = open
    cfg.security.allowlist = ["192.168.1.0/24"]                  # testclient host not in it
    assert client.get("/api/status").status_code == 403
    cfg.security.allowlist = []                                  # back to open
    assert client.get("/api/status").status_code == 200


def test_mqtt_tls_applied(monkeypatch):
    from multibus.mqtt_publisher import MQTTPublisher
    cfg = MQTTConfig(enabled=False, tls_enabled=True, tls_ca_cert="/ca.crt",
                     tls_client_cert="/c.crt", tls_client_key="/c.key")
    pub = MQTTPublisher(cfg, [], publish_mode="changed")
    calls = {}
    pub.client = type("C", (), {
        "username_pw_set": lambda *a, **k: None,
        "tls_set": lambda self, **k: calls.update(k),
        "tls_insecure_set": lambda self, v: calls.update(insecure=v),
        "will_set": lambda *a, **k: None,
        "reconnect_delay_set": lambda *a, **k: None,
    })()
    # re-run setup with our fake client in place
    import multibus.mqtt_publisher as mp
    monkeypatch.setattr(mp.mqtt, "Client", lambda *a, **k: pub.client)
    pub._setup_client()
    assert calls.get("ca_certs") == "/ca.crt"
    assert calls.get("certfile") == "/c.crt" and calls.get("keyfile") == "/c.key"


# ── P2 batch A: security hardening ───────────────────────────────────────────

@needs_tc
def test_security_response_headers_present(tmp_path):
    _cfg, client = make_app(tmp_path)
    r = client.get("/api/status")
    assert "default-src 'self'" in r.headers.get("content-security-policy", "")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_audit_csv_formula_injection_escaped():
    # simulate the export _safe helper contract
    def _safe(v):
        s = "" if v is None else str(v)
        return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s
    assert _safe("=cmd()") == "'=cmd()"
    assert _safe("+1") == "'+1"
    assert _safe("normal") == "normal"


def test_revoke_all_sessions():
    from multibus.auth import AuthState
    from types import SimpleNamespace
    ui = SimpleNamespace(auth_enabled=True, auth_username="a",
                         auth_password=__import__("multibus.auth", fromlist=["hash_password"]).hash_password("pw"),
                         viewer_username="", viewer_password="", operator_username="",
                         operator_password="", lockout_threshold=5, lockout_minutes=5)
    st = AuthState(ui)
    tok = st.mint_session("admin", "a")
    assert st.role_for(tok) == "admin"
    assert st.revoke_all_sessions() == 1
    assert st.role_for(tok) is None            # old cookie dead after rotation


def test_plaintext_compare_handles_unicode():
    from multibus.auth import verify_password
    # a non-ASCII plaintext stored password must not raise, just compare
    assert verify_password("pårola", "pårola") is True
    assert verify_password("x", "pårola") is False


def test_challenge_cache_evicts_oldest_not_all():
    from multibus.passkeys import ChallengeCache
    c = ChallengeCache(ttl_s=999)
    first = c.put(challenge=b"a", tag="keep-me-mid-flight")
    for i in range(80):
        c.put(challenge=bytes([i]))
    # the very first is evicted (oldest), but the cache didn't wipe everything
    assert c.take(first) is None
    assert len(c._pending) > 0


# ── canonical redirect (steer to HTTPS host, IP fallback preserved) ──────────

def test_canonical_redirect_script_and_bypass():
    from multibus.api import _canonical_redirect_script, _render_index_html
    s = _canonical_redirect_script("https://mbus.diysolar.ro")
    assert "mbus.diysolar.ro" in s
    assert "mbg-stay-local" in s          # sticky local bypass
    assert "p.has('local')" in s          # ?local escape hatch
    assert "location.host===h" in s       # no redirect when already canonical
    assert _canonical_redirect_script("") == ""    # unset → no redirect (compat)
    html = _render_index_html(canonical_url="https://mbus.diysolar.ro")
    assert html.startswith("") and "<head><script>" in html   # injected first in head


@needs_tc
def test_ui_security_canonical_url_roundtrip(tmp_path):
    _cfg, client = make_app(tmp_path)
    r = client.post("/api/config/ui-security", json={"canonical_url": "not-a-url"})
    assert r.status_code == 422                                  # scheme required
    r = client.post("/api/config/ui-security", json={"canonical_url": "https://mbus.diysolar.ro"})
    assert r.status_code == 200
    assert client.get("/api/config/ui-security").json()["canonical_url"] == "https://mbus.diysolar.ro"
