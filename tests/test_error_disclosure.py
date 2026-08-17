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
"""str(e) review (go-public audit): unexpected server-side exceptions must not
echo their raw text to API clients — an OSError carries filesystem paths, a
library error its internals. The contract:
 * broad `except Exception` handlers return a GENERIC 500 detail (exception
   class name only) and log the full traceback server-side;
 * deliberate validation errors (our own ValueError messages) still pass
   through verbatim — they are the operator's fix-it text;
 * internal service URLs in error strings go through redact_url (userinfo
   stripped) before leaving the process."""
import pytest

from tests.test_devices import write_config
from tests.test_devices_api import needs_tc


def _app(tmp_path):
    from types import SimpleNamespace
    from multibus.api import create_api
    cfg = write_config(tmp_path)
    fake = SimpleNamespace(publish_callback=None)
    app, _ = create_api(cfg, fake, None, None, devices=[(d, fake) for d in cfg.devices])
    return app, cfg


@needs_tc
def test_config_update_500_hides_raw_exception(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    app, cfg = _app(tmp_path)

    def boom(**kw):
        raise OSError(13, "Permission denied", "/very/secret/host/path/config.yaml")
    monkeypatch.setattr(cfg, "update_modbus", boom)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/api/config/modbus", json={"host": "192.168.1.9", "port": 502,
                                           "unit_id": 1})
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert "/very/secret/host/path" not in detail          # no path disclosure
    assert "Permission denied" not in detail               # no raw message
    assert "PermissionError" in detail                     # class name is enough


@needs_tc
def test_validation_error_text_still_passes_through(tmp_path):
    """The counterpart guarantee: our own ValueError messages (422 validation)
    keep reaching the operator verbatim — sanitizing those would destroy the
    fix-it guidance the UI shows."""
    from fastapi.testclient import TestClient
    app, _ = _app(tmp_path)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/api/devices", json={"id": "!bad id!", "name": "x",
                                     "template": "carlo_gavazzi_em24",
                                     "protocol": "tcp",
                                     "connection": {"host": "192.168.1.9", "port": 502,
                                                    "unit_id": 1}})
    assert r.status_code == 422
    assert "a-z 0-9" in str(r.json()["detail"])            # the exact rule text


def test_esphome_error_redacts_userinfo_in_url():
    from multibus.esphome_client import EsphomeDashboard, EsphomeError
    with pytest.raises(EsphomeError) as ei:
        EsphomeDashboard("ftp://user:hunter2@esphome.lan:6052")
    assert "hunter2" not in str(ei.value)                  # password never echoed
    assert "esphome.lan" in str(ei.value)                  # host stays diagnostic


def test_bridge_unreachable_error_is_redacted(monkeypatch):
    import multibus.routes.commissioning as comm
    # If the operator ever points SERIAL_BRIDGE_URL at http://user:pw@host,
    # the "unreachable" diagnostic must not echo the credentials.
    from multibus.redact import redact_url
    assert "pw" not in redact_url("http://user:pw@bridge:7000")
    assert "bridge:7000" in redact_url("http://user:pw@bridge:7000")
    # and the route builds its message through redact_url (source contract)
    import inspect
    src = inspect.getsource(comm)
    assert "redact_url(_BRIDGE_URL)" in src
