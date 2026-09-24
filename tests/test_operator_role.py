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
"""Operator role: live actions allowed, configuration denied, audit named."""
import pytest

from tests.test_devices import write_config
from tests.test_devices_api import needs_tc


def _build_clients(tmp_path):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from multibus import auth as _a
    from multibus.api import create_api
    cfg = write_config(tmp_path, extra_yaml=f"""
ui:
  auth:
    enabled: true
    username: boss
    password: "{_a.hash_password('pw')}"
    operator_username: ops
    operator_password: "{_a.hash_password('op')}"
    viewer_username: guest
    viewer_password: "{_a.hash_password('vw')}"
""")
    fake = SimpleNamespace(publish_callback=None)
    app, _ = create_api(cfg, fake, None, None, devices=[(d, fake) for d in cfg.devices])

    def login(user, pw):
        c = TestClient(app, raise_server_exceptions=False)
        r = c.post("/api/auth/login", json={"username": user, "password": pw})
        assert r.status_code == 200, r.text
        return c
    return SimpleNamespace(admin=login("boss", "pw"), op=login("ops", "op"),
                           viewer=login("guest", "vw"), cfg=cfg, app=app)


@pytest.fixture
def clients(tmp_path):
    return _build_clients(tmp_path)


@needs_tc
def test_operator_live_actions_allowed(clients):
    op = clients.op
    # unelte de comisionare
    assert op.post("/api/bus-trace/config", json={"enabled": False}).status_code == 200
    assert op.get("/api/bus-trace").status_code == 200
    # probe pe device fake → 400 (nu e Modbus), NU 403 — politica a lăsat-o să treacă
    assert op.post("/api/diagnostics/probe",
                   json={"device": "umg512", "address": 0}).status_code == 400
    # query one-shot (clientul fake nu e disponibil → 503, nu 403)
    r = op.post("/api/query/register", json={"address": 1, "data_type": "uint16"})
    assert r.status_code in (500, 503) and r.status_code != 403
    # logout permis
    assert op.post("/api/auth/logout").status_code == 200


@needs_tc
def test_operator_config_denied_and_audited(clients):
    op, admin = clients.op, clients.admin
    denied_paths = [
        ("/api/config/mqtt", {"topic_prefix": "x"}),
        ("/api/registers/selected", {"registers": []}),
        ("/api/config/snapshots", {}),
        ("/api/devices", {"id": "x", "name": "x"}),
    ]
    for path, body in denied_paths:
        r = op.post(path, json=body)
        assert r.status_code == 403, f"{path} → {r.status_code}"
    assert op.delete("/api/config/snapshots/nope").status_code == 403
    # audit-ul (admin) vede refuzurile pe numele operatorului
    ents = admin.get("/api/audit?limit=50").json()["entries"]
    denied = [e for e in ents if e["status"].startswith("denied") and e["user"] == "ops"]
    assert any(e["action"] == "POST /api/config/mqtt" for e in denied)
    # operatorul nu citește audit-ul și nu exportă secrete
    assert op.get("/api/audit").status_code == 403
    assert op.get("/api/config/export?include_secrets=true").status_code == 403


@needs_tc
def test_status_and_hierarchy(clients):
    st = clients.op.get("/api/auth/status").json()
    assert st["role"] == "operator" and st["has_operator"] is True
    # viewer-ul rămâne read-only inclusiv pe acțiunile live permise operatorului
    assert clients.viewer.post("/api/bus-trace/config",
                               json={"enabled": False}).status_code == 403
    # adminul poate tot
    assert clients.admin.post("/api/bus-trace/config",
                              json={"enabled": False}).status_code == 200


@needs_tc
def test_operator_password_stripped_from_sanitized_export(clients):
    admin = clients.admin
    r = admin.get("/api/config/export")          # fără secrete
    assert r.status_code == 200
    import io
    import zipfile
    import yaml
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    data = yaml.safe_load(zf.read("config.yaml"))
    auth = (data.get("ui") or {}).get("auth") or {}
    assert "operator_password" not in auth


@needs_tc
def test_operator_and_viewer_get_redacted_urls_admin_raw(tmp_path):
    """3.4.1: only admin sees raw URL-embedded credentials; operator AND viewer
    get redacted URLs from /api/config and /api/config/influxdb."""
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from multibus import auth as _a
    from multibus.api import create_api
    cfg = write_config(tmp_path, extra_yaml=f"""
influxdb:
  enabled: true
  url: "http://influx:8086?token=SUPERSECRET"
ui:
  auth:
    enabled: true
    username: boss
    password: "{_a.hash_password('pw')}"
    operator_username: ops
    operator_password: "{_a.hash_password('op')}"
    viewer_username: guest
    viewer_password: "{_a.hash_password('vw')}"
""")
    fake = SimpleNamespace(publish_callback=None)
    app, _ = create_api(cfg, fake, None, None, devices=[(d, fake) for d in cfg.devices])

    def login(u, p):
        c = TestClient(app, raise_server_exceptions=False)
        assert c.post("/api/auth/login", json={"username": u, "password": p}).status_code == 200
        return c
    admin, op, viewer = login("boss", "pw"), login("ops", "op"), login("guest", "vw")

    assert "SUPERSECRET" in admin.get("/api/config/influxdb").json()["url"]
    for c in (op, viewer):
        u = c.get("/api/config/influxdb").json()["url"]
        # redact_url masks the token to *** (URL-encoded %2A%2A%2A in the query)
        assert "SUPERSECRET" not in u and ("***" in u or "%2A" in u)
        u2 = (c.get("/api/config").json().get("influxdb") or {}).get("url", "")
        assert "SUPERSECRET" not in u2


# ── P1: operator write-matcher is segment-anchored ───────────────────────────

@needs_tc
def test_operator_write_matcher_segment_anchored(clients):
    # BEHAVIORAL (was an inspect.getsource assert — external audit): the
    # operator write matcher must be segment-anchored. 403 = the ROLE gate
    # refused; any other status means the request passed the gate (and then
    # hit routing/validation, which is fine for the allowed cases).
    op = clients.op
    # allowed prefix, exact + with a sub-path
    assert op.post("/api/query/register",
                   json={"address": 1, "data_type": "uint16"}).status_code != 403
    # prefix WITHOUT a segment boundary must NOT inherit the allowance
    assert op.post("/api/query-evil").status_code == 403
    assert op.post("/api/bus-traceX/config").status_code == 403
    # device live-action: exactly /api/devices/<id>/<action>
    assert op.post("/api/devices/em24/test", json={}).status_code != 403
    assert op.post("/api/devices/em24/test/extra", json={}).status_code == 403
    assert op.post("/api/devices/em24/rename", json={}).status_code == 403


@needs_tc
def test_operator_test_actions_one_level_deeper(clients):
    """The documented contract (API.md role column) grants the operator the
    fire-once TEST actions: ad-hoc device probe (4 segments) and the
    rest-push/calculated tests (6 segments). The segment matcher used to
    miss all three shapes — 403 where the docs said operator."""
    op = clients.op
    # 403 = the ROLE gate refused; anything else means it passed the gate
    assert op.post("/api/devices/test", json={}).status_code != 403
    assert op.post("/api/devices/em24/rest-push/test", json={}).status_code != 403
    assert op.post("/api/devices/em24/calculated/test", json={}).status_code != 403
    # ...without widening anything else at that depth
    assert op.post("/api/devices/em24/rest-push/enable", json={}).status_code == 403
    assert op.post("/api/devices/restorable/rest-push/test", json={}).status_code == 403
