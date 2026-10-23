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
"""Service tokens: a program signs in with a named token, not a password."""
import json
import os

import pytest

from multibus.service_tokens import ServiceTokens
from tests.test_devices_api import needs_tc
from tests.test_operator_role import _build_clients

T = "/api/auth/service-tokens"


def test_only_the_hash_is_kept_and_the_token_checks(tmp_path):
    p = str(tmp_path / "st.json")
    st = ServiceTokens(p)
    tok = st.create("mbg-esphome", "admin")
    assert tok.startswith("mbgst_") and len(tok) > 40
    assert tok not in open(p).read()
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    assert ServiceTokens(p).check(tok) == ("mbg-esphome", "admin")   # survives a restart
    assert st.check(tok[:-1] + ("A" if tok[-1] != "A" else "B")) is None
    assert st.check("") is None and st.check("Bearer x") is None


def test_names_roles_and_revoke(tmp_path):
    st = ServiceTokens(str(tmp_path / "st.json"))
    for bad in ("", "a", "Has Space", "-x"):
        with pytest.raises(ValueError):
            st.create(bad, "admin")
    with pytest.raises(ValueError):
        st.create("ok-name", "root")
    tok = st.create("ok-name", "viewer")
    with pytest.raises(ValueError):
        st.create("ok-name", "viewer")                 # one per name
    assert [t["name"] for t in st.list()] == ["ok-name"]
    assert "hash" not in st.list()[0]
    assert st.revoke("ok-name") and st.check(tok) is None and not st.revoke("ok-name")


def test_a_corrupt_store_means_no_tokens(tmp_path):
    p = tmp_path / "st.json"
    p.write_text("{not json")
    assert len(ServiceTokens(str(p))) == 0
    p.write_text(json.dumps({"tokens": {"x1": {"hash": "short", "role": "admin"}}}))
    assert len(ServiceTokens(str(p))) == 0


@needs_tc
def test_an_admin_makes_a_token_and_the_program_uses_it(tmp_path):
    c = _build_clients(tmp_path)
    r = c.admin.post(T, json={"name": "mbg-esphome", "role": "admin"})
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    listed = c.admin.get(T).json()["tokens"]
    assert listed[0]["name"] == "mbg-esphome" and "token" not in listed[0]

    from fastapi.testclient import TestClient
    prog = TestClient(c.app, raise_server_exceptions=False)
    assert prog.get("/api/device-templates").status_code == 401                 # no token: nothing
    h = {"Authorization": f"Bearer {tok}"}
    assert prog.get("/api/device-templates", headers=h).status_code == 200
    assert "set-cookie" not in prog.get("/api/device-templates", headers=h).headers
    # it may write (admin), and the audit log names the token
    w = prog.post("/api/device-templates/check", headers=h,
                  json={"device_template": {"id": "x", "name": "x", "registers": []}})
    assert w.status_code != 401 and w.status_code != 403
    # a token never manages tokens
    assert prog.post(T, headers=h, json={"name": "more", "role": "admin"}).status_code == 403
    # revoked: refused at once
    assert c.admin.delete(f"{T}/mbg-esphome").status_code == 200
    assert prog.get("/api/device-templates", headers=h).status_code == 401


@needs_tc
def test_a_viewer_token_reads_only(tmp_path):
    c = _build_clients(tmp_path)
    tok = c.admin.post(T, json={"name": "grafana", "role": "viewer"}).json()["token"]
    from fastapi.testclient import TestClient
    prog = TestClient(c.app, raise_server_exceptions=False)
    h = {"Authorization": f"Bearer {tok}"}
    assert prog.get("/api/device-templates", headers=h).status_code == 200
    assert prog.delete("/api/devices/x", headers=h).status_code == 403


@needs_tc
def test_only_an_admin_manages_tokens(tmp_path):
    c = _build_clients(tmp_path)
    assert c.op.post(T, json={"name": "x1", "role": "admin"}).status_code == 403
    assert c.viewer.get(T).status_code == 403
