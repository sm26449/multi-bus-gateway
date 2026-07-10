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
"""Alert webhook: body templating + the test-fire endpoint."""
import json

import pytest

import multibus.alerts as A
from multibus.alerts import AlertManager

try:
    from fastapi.testclient import TestClient
    _HAS_TC = True
except Exception:  # noqa: BLE001
    _HAS_TC = False
needs_tc = pytest.mark.skipif(not _HAS_TC, reason="TestClient not installed")


class _Resp:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_webhook_body_renders_placeholders():
    m = AlertManager({"enabled": True, "webhook_url": "http://x",
                      "webhook_body": {"message": "{severity} {source}: {message}"}})
    body = m._render_body({"severity": "error", "source": "dev", "message": "down",
                           "key": "k", "host": "h", "ts": 1})
    assert body == {"message": "error dev: down"}


def test_test_delivers_webhook_and_captures(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=8):
        captured['url'] = req.full_url
        captured['data'] = req.data
        captured['headers'] = {k.lower(): v for k, v in req.header_items()}
        return _Resp()

    monkeypatch.setattr(A._NO_REDIRECT_OPENER, "open", fake_urlopen)
    m = AlertManager({"enabled": False, "mqtt": False, "webhook_url": "http://sms:5080/send",
                      "webhook_headers": {"X-API-Key": "secret"},
                      "webhook_body": {"message": "{source}: {message}"}})
    res = m.test("hello")
    assert res["delivered"] and "sent" in res["channels"]["webhook"]
    assert json.loads(captured['data']) == {"message": "test: hello"}
    assert captured['headers'].get('x-api-key') == "secret"


def test_test_reports_failure(monkeypatch):
    def boom(req, timeout=8):
        raise OSError("connection refused")
    monkeypatch.setattr(A._NO_REDIRECT_OPENER, "open", boom)
    m = AlertManager({"mqtt": False, "webhook_url": "http://down/x"})
    res = m.test()
    assert res["delivered"] and "failed" in res["channels"]["webhook"]


@needs_tc
def test_alerts_config_masks_and_applies_live(tmp_path, monkeypatch):
    monkeypatch.setattr(A._NO_REDIRECT_OPENER, "open", lambda req, timeout=8: _Resp())
    from tests.test_devices import write_config
    from multibus.api import create_api
    cfg = write_config(tmp_path)
    cfg.alerts = {"enabled": True, "webhook_url": "http://x/y",
                  "webhook_headers": {"X-API-Key": "secret"}}
    app, _ = create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    # GET masks the header VALUE
    g = client.get("/api/config/alerts").json()
    assert g["webhook_headers"]["X-API-Key"] == "••••••"
    assert g["webhook_url"] == "http://x/y"
    # POST with the masked header preserves the secret; changes the url + body
    r = client.post("/api/config/alerts", json={
        "enabled": True, "webhook_url": "http://new/z",
        "webhook_headers": {"X-API-Key": "••••••"},
        "webhook_body": {"message": "{severity}: {message}"}})
    assert r.status_code == 200
    assert cfg.alerts["webhook_headers"]["X-API-Key"] == "secret"      # preserved
    assert cfg.alerts["webhook_url"] == "http://new/z"
    assert cfg.alerts["webhook_body"] == {"message": "{severity}: {message}"}


@needs_tc
def test_security_allow_writes_toggle(tmp_path):
    from tests.test_devices import write_config
    from multibus.api import create_api
    cfg = write_config(tmp_path)
    app, _ = create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/api/config/security").json()["allow_writes"] is False
    r = client.post("/api/config/security", json={"allow_writes": True})
    assert r.status_code == 200 and r.json()["allow_writes"] is True
    assert cfg.security.allow_writes is True


@needs_tc
def test_alerts_test_requires_credential(tmp_path, monkeypatch):
    """On a default LAN-open deployment (no auth, no API key) the test-fire is
    refused so it can't be spammed into real webhook/SMS traffic."""
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("JANITZA_API_KEY", raising=False)
    monkeypatch.setattr(A._NO_REDIRECT_OPENER, "open", lambda req, timeout=8: _Resp())
    from tests.test_devices import write_config
    from multibus.api import create_api
    cfg = write_config(tmp_path)
    cfg.alerts = {"enabled": False, "mqtt": False, "webhook_url": "http://x/y"}
    app, _ = create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post("/api/alerts/test", json={"message": "hi"}).status_code == 403


@needs_tc
def test_alerts_test_endpoint_credentialed_and_throttled(tmp_path, monkeypatch):
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setattr(A._NO_REDIRECT_OPENER, "open", lambda req, timeout=8: _Resp())
    from tests.test_devices import write_config
    from multibus.api import create_api
    cfg = write_config(tmp_path)
    cfg.alerts = {"enabled": False, "mqtt": False, "webhook_url": "http://x/y"}
    app, _ = create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    hdr = {"X-API-Key": "k"}
    r = client.post("/api/alerts/test", json={"message": "hi"}, headers=hdr)
    assert r.status_code == 200
    body = r.json()
    assert body["delivered"] and "webhook" in body["channels"]
    # a second immediate fire is throttled
    assert client.post("/api/alerts/test", json={}, headers=hdr).status_code == 429


@needs_tc
def test_export_strips_webhook_headers(tmp_path):
    import io
    import zipfile
    import yaml as y
    from tests.test_devices import write_config
    from multibus.api import create_api
    cfg = write_config(tmp_path, extra_yaml=(
        "alerts:\n  webhook_url: http://x\n  webhook_headers:\n    X-API-Key: secret\n"))
    app, _ = create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/api/config/export")          # non-secret backup
    assert r.status_code == 200
    exported = y.safe_load(zipfile.ZipFile(io.BytesIO(r.content)).read("config.yaml"))
    assert "webhook_headers" not in exported.get("alerts", {})     # secret stripped
    assert exported.get("alerts", {}).get("webhook_url") == "http://x"   # non-secret kept


@needs_tc
def test_webhook_redirect_is_blocked(monkeypatch):
    """A 3xx from the webhook host must not be followed (credential-leak guard)."""
    import urllib.error
    def redirecter(fullurl, data=None, timeout=8):
        raise urllib.error.HTTPError(
            "http://sms/x", 302, "redirect blocked → http://evil/steal", {}, None)
    monkeypatch.setattr(A._NO_REDIRECT_OPENER, "open", redirecter)
    m = AlertManager({"mqtt": False, "webhook_url": "http://sms/x",
                      "webhook_headers": {"X-API-Key": "secret"}})
    res = m.test()
    assert "failed" in res["channels"]["webhook"]
