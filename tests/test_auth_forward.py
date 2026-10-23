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
"""/api/auth/forward — the gateway's login in front of a companion service
(Traefik forwardAuth)."""
from tests.test_devices_api import needs_tc
from tests.test_operator_role import _build_clients

F = "/api/auth/forward"


@needs_tc
def test_a_session_answers_with_its_user_and_role(tmp_path):
    c = _build_clients(tmp_path)
    for client, user, role in ((c.admin, "boss", "admin"), (c.op, "ops", "operator"),
                               (c.viewer, "guest", "viewer")):
        r = client.get(F, follow_redirects=False)
        assert r.status_code == 200
        assert r.headers["x-mbg-user"] == user and r.headers["x-mbg-role"] == role


@needs_tc
def test_without_a_session_a_browser_goes_to_login_and_an_api_call_gets_401(tmp_path):
    from fastapi.testclient import TestClient
    anon = TestClient(_build_clients(tmp_path).app, raise_server_exceptions=False)
    page = anon.get(F, headers={"Accept": "text/html,*/*", "X-Forwarded-Uri": "/nodes/"},
                    follow_redirects=False)
    assert page.status_code == 302 and page.headers["location"] == "http://testserver/"
    behind = anon.get(F, headers={"Accept": "text/html", "X-Forwarded-Uri": "/nodes/",
                                  "X-Forwarded-Proto": "https", "X-Forwarded-Host": "mbus.example.org"},
                      follow_redirects=False)
    assert behind.headers["location"] == "https://mbus.example.org/"   # the browser's host, not the proxy's view
    api = anon.get(F, headers={"Accept": "text/html", "X-Forwarded-Uri": "/nodes/api/nodes"},
                   follow_redirects=False)
    assert api.status_code == 401 and "x-mbg-role" not in api.headers
    fetch = anon.get(F, headers={"Accept": "application/json"}, follow_redirects=False)
    assert fetch.status_code == 401


@needs_tc
def test_a_forged_role_header_is_not_a_session(tmp_path):
    from fastapi.testclient import TestClient
    anon = TestClient(_build_clients(tmp_path).app, raise_server_exceptions=False)
    r = anon.get(F, headers={"X-Mbg-Role": "admin", "Accept": "application/json"}, follow_redirects=False)
    assert r.status_code == 401


@needs_tc
def test_auth_disabled_is_admin(tmp_path):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from multibus.api import create_api
    from tests.test_devices import write_config
    cfg = write_config(tmp_path)
    fake = SimpleNamespace(publish_callback=None)
    app, _ = create_api(cfg, fake, None, None, devices=[(d, fake) for d in cfg.devices])
    c = TestClient(app)
    assert c.get("/api/auth/status").json()["enabled"] is False
    r = c.get(F)
    assert r.status_code == 200 and r.headers["x-mbg-role"] == "admin"


@needs_tc
def test_need_refuses_a_lower_role(tmp_path):
    c = _build_clients(tmp_path)
    assert c.admin.get(F + "?need=admin").status_code == 200
    assert c.op.get(F + "?need=admin").status_code == 403
    assert c.viewer.get(F + "?need=admin").status_code == 403
    assert c.op.get(F + "?need=operator").status_code == 200
    assert c.viewer.get(F + "?need=viewer").status_code == 200
    assert c.admin.get(F + "?need=root").status_code == 400
