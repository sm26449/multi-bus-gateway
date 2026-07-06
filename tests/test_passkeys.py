"""Passkeys: store CRUD, RP-ID rules, challenge cache, endpoint guards."""
import time

import pytest

from multibus.passkeys import ChallengeCache, PasskeyStore, rp_id_for_host
from tests.test_devices import write_config
from tests.test_devices_api import needs_tc


def test_rp_id_rules():
    assert rp_id_for_host("localhost") == "localhost"
    assert rp_id_for_host("gateway.lan") == "gateway.lan"
    assert rp_id_for_host("Gateway.LAN") == "gateway.lan"
    assert rp_id_for_host("192.168.1.207") is None      # IP → passkeys imposibile
    assert rp_id_for_host("127.0.0.1") is None
    assert rp_id_for_host("") is None


def test_store_crud_and_ownership(tmp_path):
    s = PasskeyStore(str(tmp_path / "pk.json"))
    s.add(cred_id=b"cred-1", public_key=b"pub", sign_count=0,
          user="boss", role="admin", rp_id="localhost", label="laptop")
    s.add(cred_id=b"cred-2", public_key=b"pub", sign_count=3,
          user="ops", role="operator", rp_id="gateway.lan")
    assert s.count == 2
    assert [c["user"] for c in s.for_rp("localhost")] == ["boss"]
    # list() nu scurge materialul cheii
    assert all("public_key" not in c for c in s.list())
    assert [c["label"] for c in s.list(user="boss")] == ["laptop"]
    # ștergerea respectă proprietarul
    cid = s.list(user="ops")[0]["id"]
    assert s.delete(cid, user="boss") is False
    assert s.delete(cid, user="ops") is True
    assert s.count == 1
    # sign count persistă peste reload
    cid = s.list()[0]["id"]
    s.update_sign_count(cid, 42)
    s2 = PasskeyStore(str(tmp_path / "pk.json"))
    assert s2.find(cid)["sign_count"] == 42


def test_challenge_cache_single_use_and_ttl():
    c = ChallengeCache(ttl_s=1)
    st = c.put(challenge=b"x", user="u")
    assert c.take(st)["user"] == "u"
    assert c.take(st) is None                            # single use
    st2 = c.put(challenge=b"y", user="u")
    time.sleep(1.1)
    assert c.take(st2) is None                           # expirat


@pytest.fixture
def api(tmp_path):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from multibus.api import create_api
    cfg = write_config(tmp_path)
    fake = SimpleNamespace(publish_callback=None)
    app, _ = create_api(cfg, fake, None, None, devices=[(d, fake) for d in cfg.devices])
    return TestClient(app, raise_server_exceptions=False)


@needs_tc
def test_register_begin_shapes_and_ip_refusal(api):
    # auth OFF → înrolare permisă ca admin implicit; host-ul TestClient e
    # "testserver" (hostname valid) → opțiuni corecte
    r = api.post("/api/auth/passkey/register/begin", json={"label": "laptop"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["state"] and d["options"]["rp"]["id"] == "testserver"
    assert d["options"]["user"]["name"] == "admin"
    # login/begin fără passkeys → 404 doar când auth e ON; cu auth OFF → notă
    r = api.post("/api/auth/passkey/login/begin")
    assert r.status_code == 200 and r.json().get("note") == "auth disabled"


@needs_tc
def test_register_finish_stale_state(api):
    r = api.post("/api/auth/passkey/register/finish",
                 json={"state": "nope", "credential": {}})
    assert r.status_code == 422


@needs_tc
def test_login_guards_with_auth_on(tmp_path):
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
""")
    fake = SimpleNamespace(publish_callback=None)
    app, _ = create_api(cfg, fake, None, None, devices=[(d, fake) for d in cfg.devices])
    c = TestClient(app, raise_server_exceptions=False)
    # fără passkeys înregistrate → 404 (endpoint deschis, nu 401)
    assert c.post("/api/auth/passkey/login/begin").status_code == 404
    # finish cu credential necunoscut → 401 și contorizează lockout-ul
    r = c.post("/api/auth/passkey/login/finish",
               json={"state": "x", "credential": {"id": "unknown"}})
    assert r.status_code == 401
    # înrolarea cere login când auth e ON
    assert c.post("/api/auth/passkey/register/begin", json={}).status_code == 401
    # status expune has_passkeys
    assert c.get("/api/auth/status").json()["has_passkeys"] is False
