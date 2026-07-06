"""Audit trail: redaction, rotation, capture middleware, admin gating."""
import json
import time

import pytest

from multibus.audit import AuditLog, redact_obj
from tests.test_devices import write_config
from tests.test_devices_api import needs_tc


def test_redact_obj_masks_secret_keys():
    out = redact_obj({"broker": "10.0.0.1", "password": "hunter2",
                      "nested": {"api_token": "abc", "port": 1883},
                      "list": [{"secret_key": "x"}, 5]})
    assert out["password"] == "***"
    assert out["nested"]["api_token"] == "***"
    assert out["nested"]["port"] == 1883
    assert out["list"][0]["secret_key"] == "***" and out["list"][1] == 5
    assert out["broker"] == "10.0.0.1"


def test_append_recent_filter(tmp_path):
    a = AuditLog(str(tmp_path / "audit.jsonl"))
    a.append(user="admin", ip="1.2.3.4", action="POST /api/config/mqtt",
             detail={"password": "s3cret", "broker": "x"})
    a.append(user="viewer", ip="1.2.3.5", action="login", status="invalid credentials")
    ents = a.recent(10)
    assert len(ents) == 2 and ents[0]["action"] == "login"          # newest first
    assert "s3cret" not in json.dumps(ents)                          # redactat PE DISC
    disk = (tmp_path / "audit.jsonl").read_text()
    assert "***" in disk and "s3cret" not in disk
    assert [e["user"] for e in a.recent(10, user="admin")] == ["admin"]
    assert a.recent(10, q="mqtt")[0]["action"].endswith("mqtt")
    assert a.recent(10, q="nope") == []


def test_rotation_keeps_bounded_files(tmp_path):
    a = AuditLog(str(tmp_path / "audit.jsonl"), rotate_bytes=500, keep=2)
    for i in range(60):
        a.append(user="u", ip="-", action=f"action {i}", detail={"i": i})
    files = sorted(p.name for p in tmp_path.iterdir())
    assert "audit.jsonl" in files and len(files) <= 3               # main + max 2 rotite
    # cele mai noi intrări rămân citibile, cap-coadă peste fișiere
    ents = a.recent(200)
    assert ents[0]["action"] == "action 59"
    assert len(ents) > 10


@pytest.fixture
def api(tmp_path):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from multibus.api import create_api
    cfg = write_config(tmp_path)
    fake = SimpleNamespace(publish_callback=None)
    app, _ = create_api(cfg, fake, None, None, devices=[(d, fake) for d in cfg.devices])
    return TestClient(app, raise_server_exceptions=False), cfg


@needs_tc
def test_mutation_lands_in_audit_with_redaction(api):
    client, cfg = api
    r = client.post("/api/config/mqtt", json={"topic_prefix": "audited/x",
                                              "password": "supersecret"})
    assert r.status_code == 200
    ents = client.get("/api/audit?limit=20").json()["entries"]
    e = next(x for x in ents if x["action"] == "POST /api/config/mqtt")
    assert e["status"] == "ok" and "audited/x" in e.get("detail", "")
    assert "supersecret" not in json.dumps(ents)


@needs_tc
def test_live_actions_not_audited(api):
    client, _cfg = api
    client.post("/api/bus-trace/config", json={"enabled": False})
    client.post("/api/query/register", json={"address": 1, "data_type": "uint16"})
    ents = client.get("/api/audit?limit=50").json()["entries"]
    acts = [e["action"] for e in ents]
    assert not any("bus-trace" in a or "query" in a for a in acts)


@needs_tc
def test_audit_admin_gate_and_login_events(tmp_path):
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
    viewer_username: guest
    viewer_password: "{_a.hash_password('vw')}"
""")
    fake = SimpleNamespace(publish_callback=None)
    app, _ = create_api(cfg, fake, None, None, devices=[(d, fake) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)

    # login eșuat + reușit → ambele în audit
    client.post("/api/auth/login", json={"username": "boss", "password": "wrong"})
    r = client.post("/api/auth/login", json={"username": "boss", "password": "pw"})
    assert r.status_code == 200

    ents = client.get("/api/audit?limit=50").json()["entries"]
    logins = [e for e in ents if e["action"] == "login"]
    assert {e["status"] for e in logins} == {"ok", "invalid credentials"}
    assert all(e["user"] == "boss" for e in logins)

    # viewer: mutațiile refuzate apar ca denied; audit-ul însuși e interzis
    vc = TestClient(app, raise_server_exceptions=False)
    assert vc.post("/api/auth/login", json={"username": "guest", "password": "vw"}).status_code == 200
    assert vc.post("/api/config/mqtt", json={"topic_prefix": "x"}).status_code == 403
    assert vc.get("/api/audit").status_code == 403

    denied = [e for e in client.get("/api/audit?limit=50").json()["entries"]
              if e["status"].startswith("denied")]
    assert any(e["action"] == "POST /api/config/mqtt" and e["user"] == "guest" for e in denied)

    # utilizatorul apare cu numele lui (nu doar rolul) pe o mutație reușită
    client.post("/api/config/mqtt", json={"topic_prefix": "by-boss"})
    ents = client.get("/api/audit?limit=10").json()["entries"]
    e = next(x for x in ents if "by-boss" in x.get("detail", ""))
    assert e["user"] == "boss"

    # CSV export merge pentru admin
    assert client.get("/api/audit/export.csv").status_code == 200
    assert vc.get("/api/audit/export.csv").status_code == 403
