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
"""The Device Builder moved out (available in MBG Pro): an old
`esphome:` block still loads, and `ui.nodes_url` links to the new service."""
import logging

import yaml

from tests.test_devices import write_config
from tests.test_devices_api import make_app, needs_tc

OLD_BLOCK = """
esphome:
  enabled: true
  url: http://esphome:6052
  password: s3cret
"""


def test_an_old_esphome_block_loads_is_ignored_and_dropped_on_save(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="multibus.config"):
        cfg = write_config(tmp_path, extra_yaml=OLD_BLOCK)
    assert not getattr(cfg, "_load_failed", False)
    assert cfg.devices                                           # the rest of the file loaded
    assert not hasattr(cfg, "esphome")
    assert any("`esphome:` block is ignored" in r.getMessage() for r in caplog.records)
    cfg.save_yaml_config()
    saved = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert "esphome" not in saved
    assert saved["modbus"]["host"] == "192.168.1.207"


def test_esphome_env_vars_no_longer_matter(tmp_path, monkeypatch):
    monkeypatch.setenv("ESPHOME_URL", "http://pv-stack-esphome:6052")
    cfg = write_config(tmp_path)
    cfg.save_yaml_config()
    assert "esphome" not in yaml.safe_load((tmp_path / "config.yaml").read_text())


def test_nodes_url_defaults_to_empty_and_round_trips(tmp_path):
    assert write_config(tmp_path).ui.nodes_url == ""
    cfg = write_config(tmp_path, extra_yaml="ui:\n  nodes_url: /nodes/\n")
    assert cfg.ui.nodes_url == "/nodes/"
    cfg.save_yaml_config()
    assert yaml.safe_load((tmp_path / "config.yaml").read_text())["ui"]["nodes_url"] == "/nodes/"


@needs_tc
def test_nodes_url_set_from_the_security_card_and_shown_in_auth_status(tmp_path):
    cfg, c = make_app(tmp_path)
    assert c.get("/api/auth/status").json()["nodes_url"] == ""
    assert c.post("/api/config/ui-security", json={"nodes_url": "/nodes/"}).status_code == 200
    assert c.get("/api/config/ui-security").json()["nodes_url"] == "/nodes/"
    assert c.get("/api/auth/status").json()["nodes_url"] == "/nodes/"
    for bad in ("javascript:alert(1)", "//evil.example/", "nodes"):
        r = c.post("/api/config/ui-security", json={"nodes_url": bad})
        assert r.status_code == 422, bad
    assert cfg.ui.nodes_url == "/nodes/"                         # a refused value changed nothing
    assert c.post("/api/config/ui-security", json={"nodes_url": ""}).status_code == 200
    assert c.get("/api/auth/status").json()["nodes_url"] == ""


@needs_tc
def test_nodes_url_is_not_shown_before_login(tmp_path):
    from fastapi.testclient import TestClient

    from tests.test_operator_role import _build_clients
    c = _build_clients(tmp_path)
    c.cfg.ui.nodes_url = "/nodes/"
    anon = TestClient(c.app, raise_server_exceptions=False)
    assert anon.get("/api/auth/status").json()["nodes_url"] == ""
    assert c.viewer.get("/api/auth/status").json()["nodes_url"] == "/nodes/"


@needs_tc
def test_the_builder_api_is_gone(tmp_path):
    _cfg, c = make_app(tmp_path)
    for path in ("/api/builder/status", "/api/builder/nodes"):
        assert c.get(path).status_code in (404, 405), path
