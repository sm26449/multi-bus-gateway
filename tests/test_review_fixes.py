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
"""Regression tests for the code-review findings (security + correctness)."""
import pytest

from multibus import auth
from multibus.device_template import validate_template
from multibus.modbus_client import ModbusClient
from multibus.register_parser import RegisterParser

try:
    from fastapi.testclient import TestClient
    _HAS_TC = True
except Exception:  # noqa: BLE001
    _HAS_TC = False

needs_tc = pytest.mark.skipif(not _HAS_TC, reason="TestClient not installed")


# ── auth.is_hashed / admin-admin guard ───────────────────────────────────────

def test_is_hashed():
    assert auth.is_hashed(auth.hash_password("x")) is True
    assert auth.is_hashed("admin") is False          # legacy plaintext
    assert auth.is_hashed("") is False


def _make_app(tmp_path, **ui_over):
    from tests.test_devices import write_config
    from multibus.api import create_api
    cfg = write_config(tmp_path)
    for k, v in ui_over.items():
        setattr(cfg.ui, k, v)
    app, _ = create_api(cfg, None, None, None,
                        devices=[(d, None) for d in cfg.devices])
    return cfg, TestClient(app, raise_server_exceptions=False)


@needs_tc
def test_enabling_auth_with_default_password_is_refused(tmp_path):
    # auth disabled, password still the default plaintext "admin"
    cfg, client = _make_app(tmp_path, auth_enabled=False, auth_password="admin")
    r = client.post("/api/config/ui-security", json={"auth_enabled": True})
    assert r.status_code == 422                        # cannot enable with admin/admin
    # providing a real new password is accepted (and hashed)
    ok = client.post("/api/config/ui-security",
                     json={"auth_enabled": True, "auth_password": "n3w-secret"})
    assert ok.status_code == 200
    assert auth.is_hashed(cfg.ui.auth_password)


# ── WebSocket auth ────────────────────────────────────────────────────────────

@needs_tc
def test_ws_requires_auth_when_enabled(tmp_path):
    _cfg, client = _make_app(tmp_path, auth_enabled=True, auth_username="admin",
                             auth_password=auth.hash_password("pw"))
    # unauthenticated websocket is closed before streaming
    with pytest.raises(Exception):
        with client.websocket_connect("/ws"):
            pass
    # after login the cookie is set on the client → ws connects and streams init
    assert client.post("/api/auth/login",
                       json={"username": "admin", "password": "pw"}).status_code == 200
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "init"


@needs_tc
def test_ws_open_when_auth_disabled(tmp_path):
    _cfg, client = _make_app(tmp_path)
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "init"


@needs_tc
def test_ws_enforces_ip_allowlist(tmp_path):
    # the allowlist middleware is HTTP-only; /ws must enforce it too
    cfg, client = _make_app(tmp_path)
    cfg.security.allowlist = ["192.168.50.0/24"]          # TestClient peer is not in it
    with pytest.raises(Exception):
        with client.websocket_connect("/ws"):
            pass
    cfg.security.allowlist = []                            # empty = open again
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "init"


def test_redirect_ssrf_guard_blocks_public_target():
    import urllib.request
    import urllib.error
    from multibus.http_client import _GuardedRedirect
    h = _GuardedRedirect(allow_nonlan=False)
    req = urllib.request.Request("http://192.168.1.5/a")
    # a LAN host trying to 302 us to a public/metadata URL must be blocked
    with pytest.raises(urllib.error.HTTPError):
        h.redirect_request(req, None, 302, "Found", {}, "http://169.254.169.254/latest/")
    with pytest.raises(urllib.error.HTTPError):
        h.redirect_request(req, None, 302, "Found", {}, "http://8.8.8.8/x")
    # opt-out disables the guard
    assert _GuardedRedirect(allow_nonlan=True).allow_nonlan is True


# ── template validation: duplicate address + string type ─────────────────────

def _tpl(regs):
    return {"device_template": {"schema_version": 1, "id": "t_test", "name": "T",
                                "protocol": {}, "categories": {}, "registers": regs}}


def test_validate_rejects_duplicate_address():
    errs = validate_template(_tpl([
        {"address": 100, "name": "a", "data_type": "uint16"},
        {"address": 100, "name": "b", "data_type": "uint16"},   # same addr
    ]))
    assert any("duplicate address" in e for e in errs)


def test_validate_string_type_needs_length():
    # 'string' now has a real length-aware decoder — but a BARE 'string' (no
    # register count) is still rejected; it must carry its length as 'string:N'.
    errs = validate_template(_tpl([
        {"address": 0, "name": "serial", "data_type": "string"},
    ]))
    assert any("string" in e and "length" in e for e in errs)
    # the 'string:N' form is accepted (N = register count, ASCII decode).
    errs_ok = validate_template(_tpl([
        {"address": 0, "name": "serial", "data_type": "string:7"},
    ]))
    assert not any("string" in e for e in errs_ok)


def test_validate_write_guards_optional_but_coherent():
    # F3a: guards are OPT-IN — a writable register without bounds is legal
    # (the write dialog requires an explicit confirmation instead). Declared
    # guards still have to be coherent.
    ok = validate_template(_tpl([
        {"address": 10, "name": "w", "data_type": "uint16", "writable": True},
    ]))
    assert not any("write" in e for e in ok)
    errs = validate_template(_tpl([
        {"address": 10, "name": "w", "data_type": "uint16", "writable": True,
         "write_min": 100, "write_max": 0},
    ]))
    assert any("write_min" in e for e in errs)


# ── batch read respects the Modbus 125-register limit ─────────────────────────

def test_lan_url_error():
    from multibus.http_client import lan_url_error
    assert lan_url_error("http://192.168.1.5/data") is None      # private LAN
    assert lan_url_error("http://10.0.0.9/x") is None            # private LAN
    assert lan_url_error("http://127.0.0.1/x") is not None       # loopback
    assert lan_url_error("http://169.254.169.254/latest") is not None  # metadata
    assert lan_url_error("http://8.8.8.8/x") is not None         # public
    # IPv4-mapped IPv6 must not smuggle a metadata/loopback target past the guard
    assert lan_url_error("http://[::ffff:169.254.169.254]/latest") is not None
    assert lan_url_error("http://[::ffff:127.0.0.1]/x") is not None
    assert lan_url_error("http://0.0.0.0/x") is not None         # unspecified → loopback


def test_http_fetch_ssrf_guard_blocks_metadata():
    from multibus.http_client import HttpClient
    c = HttpClient({"url": "http://169.254.169.254/latest/meta-data/"}, [], {})
    with pytest.raises(RuntimeError, match="SSRF"):
        c._fetch()
    # opt-out flag skips the guard (would then attempt a real fetch — not asserted)
    assert HttpClient({"url": "http://169.254.169.254/"}, [], {},
                      allow_nonlan=True).allow_nonlan is True


def test_resolve_lan_ip_pins_and_rejects():
    from multibus.http_client import resolve_lan_ip
    ip, host, err = resolve_lan_ip("http://192.168.1.7:8080/x")
    assert err is None and ip == "192.168.1.7" and host == "192.168.1.7"
    ip, host, err = resolve_lan_ip("http://169.254.169.254/latest")
    assert err is not None and ip is None
    ip, host, err = resolve_lan_ip("http://[::ffff:169.254.169.254]/x")
    assert err is not None                                  # mapped metadata rejected


def test_pinned_fetch_connects_to_validated_ip_defeating_rebind(monkeypatch):
    """The connection must target the IP validated at check time, even if the
    name would resolve to a metadata/public IP on a second lookup (rebinding)."""
    import socket as _s
    import multibus.http_client as H
    from multibus.http_client import HttpClient
    LAN, EVIL = "192.168.50.10", "169.254.169.254"
    calls = {"resolve": 0, "connected_to": None}

    def fake_getaddrinfo(host, *a, **k):
        calls["resolve"] += 1
        ip = LAN if calls["resolve"] == 1 else EVIL   # first=validate, later=rebind
        return [(_s.AF_INET, _s.SOCK_STREAM, 6, "", (ip, 0))]

    def fake_create_connection(addr, *a, **k):
        calls["connected_to"] = addr[0]
        raise RuntimeError("stop-before-real-connect")

    monkeypatch.setattr(H.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(H.socket, "create_connection", fake_create_connection)
    c = HttpClient({"url": "http://meter.local/data"}, [], {})
    with pytest.raises(Exception):
        c._fetch()
    assert calls["connected_to"] == LAN          # pinned to the validated IP
    assert calls["resolve"] == 1                 # resolved exactly once — no re-lookup


def test_same_host_redirect_blocks_cross_origin():
    import urllib.request
    import urllib.error
    from multibus.http_client import _SameHostRedirect
    h = _SameHostRedirect(allow_nonlan=False)
    req = urllib.request.Request("http://meter.local/a")
    with pytest.raises(urllib.error.HTTPError):
        h.redirect_request(req, None, 302, "Found", {}, "http://other.evil/b")  # cross-host
    with pytest.raises(urllib.error.HTTPError):
        h.redirect_request(req, None, 302, "Found", {}, "http://meter.local:22/")  # port pivot
    reqs = urllib.request.Request("https://meter.local/a")
    with pytest.raises(urllib.error.HTTPError):
        h.redirect_request(reqs, None, 302, "Found", {}, "http://meter.local/a")  # scheme downgrade


def test_http_client_honors_verify_tls_flag():
    from multibus.http_client import HttpClient
    assert HttpClient({"url": "https://x/"}, [], {}).verify_tls is True     # default: verify
    assert HttpClient({"url": "https://x/", "verify_tls": False}, [], {}).verify_tls is False


@needs_tc
def test_device_create_rejects_public_http_url(tmp_path):
    _cfg, client = _make_app(tmp_path)
    r = client.post("/api/devices", json={
        "id": "ssrftest", "name": "x",
        "connection": {"protocol": "http", "url": "http://8.8.8.8/data"}})
    assert r.status_code == 422
    body = str(r.json())
    assert "LAN" in body or "private" in body


def test_read_registers_batch_respects_125_limit():
    mc = ModbusClient.__new__(ModbusClient)             # skip network __init__
    mc.parser = RegisterParser("big")

    class _Rec:
        def __init__(self): self.spans = []
        def read_registers(self, start, count, rtype):
            self.spans.append(count); return [0] * count
    mc.connection = _Rec()

    # 200 contiguous uint16 registers must be split into spans <= 125
    regs = [{"address": a, "data_type": "uint16", "register_type": "holding"}
            for a in range(200)]
    mc.read_registers_batch(regs)
    assert mc.connection.spans, "no read issued"
    assert all(c <= 125 for c in mc.connection.spans), mc.connection.spans
    assert len(mc.connection.spans) >= 2               # actually split
