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
"""S4 (security audit): value-borne secrets must not reach the audit log or
the viewer-readable snapshot diff.
 1. Cookie/session headers on an HTTP-input device are secrets — redact_obj
    (the audit-middleware path) must mask them.
 2. A SCALAR string at a url leaf in the snapshot diff (device connection url,
    influxdb.url, webhook_url …) can embed userinfo — _mask must strip it."""
from multibus.audit import redact_obj
from multibus.snapshots import _mask


def test_cookie_header_masked_in_audit():
    obj = {"connection": {"headers": {"Cookie": "sid=SECRET123",
                                      "X-Session-Id": "abc",
                                      "Accept": "application/json"}}}
    out = redact_obj(obj)
    h = out["connection"]["headers"]
    assert h["Cookie"] == "***"
    assert h["X-Session-Id"] == "***"
    assert h["Accept"] == "application/json"      # benign header untouched


def test_diff_scalar_url_userinfo_masked():
    v = _mask("devices[0].connection.url", "https://user:tok3n@host:8443/api?x=1")
    assert "tok3n" not in v and "user" not in v
    assert "host:8443" in v                       # host stays useful for diffing


def test_diff_scalar_url_query_secret_masked():
    v = _mask("alerts.webhook_url", "https://host/ingest?api_key=SECRET&b=1")
    assert "SECRET" not in v and "b=1" in v


def test_diff_non_url_scalar_untouched():
    assert _mask("mqtt.broker", "mosquitto") == "mosquitto"
    assert _mask("polling.interval", 5) == 5
