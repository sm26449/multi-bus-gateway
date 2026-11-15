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
"""AUD-LOGIN-1 (passkey half): the passkey login shares the password login's
per-IP lockout — every failed ``login/finish`` counts, a locked IP is refused
at begin AND finish (429 ``locked_out``, even with a valid assertion), and a
success resets the streak. Refusals are coalesced in the audit like the
password path's."""
import json
from types import SimpleNamespace

import pytest

from multibus import auth as _a
from multibus.passkeys import PasskeyStore
from tests.test_devices import write_config
from tests.test_devices_api import needs_tc

ORIGIN = {"origin": "http://testserver"}
THRESHOLD = 3


def _make(tmp_path, monkeypatch, *, verify_ok=True):
    """An API with auth ON (lockout after THRESHOLD failures), one passkey
    for ``boss`` on rp ``testserver``, and the WebAuthn verifier stubbed."""
    from fastapi.testclient import TestClient
    import webauthn
    from multibus.api import create_api
    cfg = write_config(tmp_path, extra_yaml=f"""
ui:
  auth:
    enabled: true
    username: boss
    password: "{_a.hash_password('pw')}"
    lockout_threshold: {THRESHOLD}
    lockout_minutes: 5
""")
    store = PasskeyStore(str(cfg.config_path.parent / "passkeys.json"))
    entry = store.add(cred_id=b"cred-boss", public_key=b"pub", sign_count=0,
                      user="boss", role="admin", rp_id="testserver", label="laptop")
    state = {"ok": verify_ok}

    def _verify(**_kw):
        if not state["ok"]:
            raise ValueError("bad signature")
        return SimpleNamespace(new_sign_count=1)

    monkeypatch.setattr(webauthn, "verify_authentication_response", _verify)
    fake = SimpleNamespace(publish_callback=None)
    app, _ = create_api(cfg, fake, None, None, devices=[(d, fake) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    return client, entry["id"], state, app


def _begin(c):
    return c.post("/api/auth/passkey/login/begin", headers=ORIGIN)


def _finish(c, cred_id, state=None):
    if state is None:
        r = _begin(c)
        assert r.status_code == 200, r.text
        state = r.json()["state"]
    return c.post("/api/auth/passkey/login/finish", headers=ORIGIN,
                  json={"state": state, "credential": {"id": cred_id}})


@needs_tc
def test_the_stub_lets_a_valid_passkey_in(tmp_path, monkeypatch):
    c, cid, _st, _app = _make(tmp_path, monkeypatch)
    r = _finish(c, cid)
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "admin"


@needs_tc
def test_failed_assertions_lock_the_ip_and_a_valid_one_is_then_refused(tmp_path, monkeypatch):
    c, cid, st, _app = _make(tmp_path, monkeypatch, verify_ok=False)
    for _ in range(THRESHOLD):
        assert _finish(c, cid).status_code == 401
    # now locked: begin refuses with the password path's shape
    r = _begin(c)
    assert r.status_code == 429
    d = r.json()["detail"]
    assert d["error"] == "locked_out" and d["retry_after_s"] > 0
    # finish refuses too — even a VALID assertion with a state obtained earlier
    st["ok"] = True
    r = c.post("/api/auth/passkey/login/finish", headers=ORIGIN,
               json={"state": "whatever", "credential": {"id": cid}})
    assert r.status_code == 429
    assert r.json()["detail"]["error"] == "locked_out"


@needs_tc
def test_a_valid_assertion_while_locked_does_not_log_in(tmp_path, monkeypatch):
    c, cid, st, _app = _make(tmp_path, monkeypatch, verify_ok=False)
    r = _begin(c)
    good_state = r.json()["state"]            # ceremony started before the lock
    for _ in range(THRESHOLD):
        _finish(c, cid)
    st["ok"] = True
    r = _finish(c, cid, state=good_state)
    assert r.status_code == 429
    assert "set-cookie" not in {k.lower() for k in r.headers}


@needs_tc
def test_the_lockout_is_shared_with_the_password_login(tmp_path, monkeypatch):
    c, cid, _st, _app = _make(tmp_path, monkeypatch, verify_ok=False)
    # one bad password + (THRESHOLD-1) bad passkeys → locked
    assert c.post("/api/auth/login", json={"username": "boss", "password": "no"}).status_code == 401
    for _ in range(THRESHOLD - 1):
        assert _finish(c, cid).status_code == 401
    r = c.post("/api/auth/login", json={"username": "boss", "password": "pw"})
    assert r.status_code == 429
    assert r.json()["detail"]["error"] == "locked_out"


@needs_tc
def test_unknown_credential_missing_state_and_junk_all_count(tmp_path, monkeypatch):
    c, cid, _st, _app = _make(tmp_path, monkeypatch)
    # unknown credential, expired/missing challenge, malformed credential
    assert _finish(c, "unknown").status_code == 401
    assert c.post("/api/auth/passkey/login/finish", headers=ORIGIN,
                  json={"state": "stale", "credential": {"id": cid}}).status_code == 401
    assert c.post("/api/auth/passkey/login/finish", headers=ORIGIN,
                  json={"credential": "not-a-dict"}).status_code == 401
    assert _begin(c).status_code == 429


@needs_tc
def test_a_success_resets_the_failure_streak(tmp_path, monkeypatch):
    c, cid, st, _app = _make(tmp_path, monkeypatch, verify_ok=False)
    for _ in range(THRESHOLD - 1):
        assert _finish(c, cid).status_code == 401
    st["ok"] = True
    assert _finish(c, cid).status_code == 200
    st["ok"] = False
    for _ in range(THRESHOLD - 1):            # a fresh streak, still below the threshold
        assert _finish(c, cid).status_code == 401
    assert _begin(c).status_code == 200


@needs_tc
def test_begin_does_not_depend_on_a_username(tmp_path, monkeypatch):
    """begin takes no account: whatever username a client sends, the answer
    is the same (no account enumeration)."""
    c, _cid, _st, _app = _make(tmp_path, monkeypatch)
    a = c.post("/api/auth/passkey/login/begin", headers=ORIGIN, json={"username": "boss"})
    b = c.post("/api/auth/passkey/login/begin", headers=ORIGIN, json={"username": "nobody"})
    assert a.status_code == b.status_code == 200
    oa, ob = a.json()["options"], b.json()["options"]
    assert oa.get("allowCredentials") == ob.get("allowCredentials")


@needs_tc
def test_refusals_while_locked_are_coalesced_in_the_audit(tmp_path, monkeypatch):
    c, cid, _st, app = _make(tmp_path, monkeypatch, verify_ok=False)
    for _ in range(THRESHOLD):
        _finish(c, cid)
    for _ in range(20):
        assert _begin(c).status_code == 429
        assert c.post("/api/auth/passkey/login/finish", headers=ORIGIN,
                      json={"state": "x", "credential": {"id": cid}}).status_code == 429
    entries = app.state.audit_log.recent(500, q="login (passkey)")
    locked = [e for e in entries if e.get("status") == "locked out"]
    # the first refusal of the streak is written, the 39 repeats are only counted
    assert len(locked) == 1, entries
    assert json.loads(locked[0]["detail"])["coalesce"] == "start"
