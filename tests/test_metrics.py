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
"""Prometheus /metrics: exposition format, key series, auth exemption."""
from types import SimpleNamespace

from tests.test_devices import write_config
from tests.test_devices_api import needs_tc


class _Client(SimpleNamespace):
    def get_stats(self):
        return {"connected": True, "poll_rate": 4.22, "successful_reads": 100,
                "failed_reads": 2, "last_latency_ms": 6, "staleness_age_s": 0.2}
    def data_health(self, *a):
        return {"status": "ok"}


def make(tmp_path, auth_yaml=""):
    from fastapi.testclient import TestClient
    from multibus.api import create_api
    cfg = write_config(tmp_path, extra_yaml=auth_yaml)
    fake = _Client(publish_callback=None)
    app, _ = create_api(cfg, fake, None, None,
                        devices=[(d, fake) for d in cfg.devices])
    return TestClient(app, raise_server_exceptions=False)


@needs_tc
def test_metrics_exposition_format_and_series(tmp_path):
    client = make(tmp_path)
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    body = r.text
    assert '# TYPE gateway_device_up gauge' in body
    assert 'gateway_device_up{device="umg512"} 1' in body
    assert 'gateway_device_poll_rate{device="umg512"} 4.22' in body
    assert 'gateway_device_reads_total{device="umg512",result="ok"} 100' in body
    assert 'gateway_device_reads_total{device="umg512",result="error"} 2' in body
    assert 'gateway_device_health{device="umg512"} 1' in body
    assert "gateway_scrape_timestamp_seconds" in body
    # HELP/TYPE emise o singură dată per metrică
    assert body.count("# TYPE gateway_device_up gauge") == 1


@needs_tc
def test_metrics_open_when_auth_enabled(tmp_path):
    # scraperele nu se pot loga — /metrics rămâne accesibil ca /health
    from multibus import auth as _a
    client = make(tmp_path, auth_yaml=f"""
ui:
  auth:
    enabled: true
    username: admin
    password: "{_a.hash_password('pw')}"
""")
    assert client.get("/api/values").status_code in (401, 200) and \
           client.get("/api/values").status_code == 401      # gate activ
    assert client.get("/metrics").status_code == 200          # exempt
    assert client.get("/health").status_code in (200, 503)    # exempt (ca înainte)
