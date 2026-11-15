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


def make(tmp_path, auth_yaml="", client_host="127.0.0.1"):
    from fastapi.testclient import TestClient
    from multibus.api import create_api
    cfg = write_config(tmp_path, extra_yaml=auth_yaml)
    fake = _Client(publish_callback=None)
    app, _ = create_api(cfg, fake, None, None,
                        devices=[(d, fake) for d in cfg.devices])
    # /metrics is served to loopback by default (A-MED-5); a real peer must be
    # allowlisted. The tests set the client address explicitly.
    return TestClient(app, raise_server_exceptions=False, client=(client_host, 50000))


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


@needs_tc
def test_metrics_refused_from_a_lan_peer_without_an_allowlist(tmp_path):
    """A-MED-5: /metrics exposes operational inventory, so with NO allowlist it is
    served only to loopback — a plain LAN peer is refused (not left anonymous)."""
    client = make(tmp_path, client_host="192.168.1.50")
    r = client.get("/metrics")
    assert r.status_code == 403
    assert "allowlist" in r.text.lower() or "localhost" in r.text.lower()


@needs_tc
def test_metrics_served_to_an_allowlisted_lan_scraper(tmp_path):
    """A deliberate scraper is let in by configuring security.allowlist with its
    subnet — the normal allowlist path, not anonymous exposure."""
    client = make(tmp_path, auth_yaml='security:\n  allowlist: ["192.168.1.0/24"]\n',
                  client_host="192.168.1.50")
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "gateway_device_up" in r.text


# ── OBS-PROM-1: security.metrics_allowlist (scrape without gating the whole app) ──

_MAL = 'security:\n  metrics_allowlist: ["172.20.0.0/16"]\n'


@needs_tc
def test_metrics_allowlist_admits_the_docker_scraper(tmp_path):
    client = make(tmp_path, auth_yaml=_MAL, client_host="172.20.0.34")
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "gateway_device_up" in r.text


@needs_tc
def test_metrics_allowlist_refuses_a_peer_outside_it(tmp_path):
    client = make(tmp_path, auth_yaml=_MAL, client_host="10.9.8.5")
    assert client.get("/metrics").status_code == 403


@needs_tc
def test_metrics_allowlist_keeps_loopback_and_leaves_the_app_open(tmp_path):
    client = make(tmp_path, auth_yaml=_MAL, client_host="127.0.0.1")
    assert client.get("/metrics").status_code == 200
    # the key is for /metrics only: with no security.allowlist the app stays open
    other = make(tmp_path, auth_yaml=_MAL, client_host="10.9.8.5")
    assert other.get("/api/values").status_code == 200


@needs_tc
def test_metrics_allowlist_with_an_app_allowlist(tmp_path):
    """With security.allowlist set too, the scraper reaches /metrics and nothing
    else; an app-allowlisted peer keeps reaching /metrics as before."""
    yml = ('security:\n  allowlist: ["192.168.1.0/24"]\n'
           '  metrics_allowlist: ["172.20.0.0/16"]\n')
    scraper = make(tmp_path, auth_yaml=yml, client_host="172.20.0.34")
    assert scraper.get("/metrics").status_code == 200
    assert scraper.get("/api/values").status_code == 403
    lan = make(tmp_path, auth_yaml=yml, client_host="192.168.1.50")
    assert lan.get("/metrics").status_code == 200
    assert make(tmp_path, auth_yaml=yml, client_host="10.9.8.5").get("/metrics").status_code == 403


@needs_tc
def test_metrics_allowlist_ipv4_mapped_peer(tmp_path):
    client = make(tmp_path, auth_yaml=_MAL, client_host="::ffff:172.20.0.34")
    assert client.get("/metrics").status_code == 200


def test_metrics_allowlist_parsed_and_saved(tmp_path):
    import yaml
    cfg = write_config(tmp_path, extra_yaml=_MAL)
    assert cfg.security.metrics_allowlist == ["172.20.0.0/16"]
    cfg.save_yaml_config()
    data = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert data["security"]["metrics_allowlist"] == ["172.20.0.0/16"]
    assert write_config(tmp_path, extra_yaml="").security.metrics_allowlist == []


def _bundle_errors(sec):
    import io
    import zipfile

    import yaml
    from multibus.lifecycle.snapshots import validate_bundle
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("config.yaml", yaml.safe_dump({"security": sec}))
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        return validate_bundle(zf)


def test_metrics_allowlist_validated_like_the_allowlist():
    assert any("security.metrics_allowlist" in e
               for e in _bundle_errors({"metrics_allowlist": ["nope"]}))
    assert any("security.metrics_allowlist" in e
               for e in _bundle_errors({"metrics_allowlist": "172.20.0.0/16"}))
    assert not any("metrics_allowlist" in e
                   for e in _bundle_errors({"metrics_allowlist": ["172.20.0.0/16"]}))


@needs_tc
def test_metrics_allowlist_via_security_api(tmp_path):
    client = make(tmp_path)
    r = client.post("/api/config/security", json={"metrics_allowlist": ["nope"]})
    assert r.status_code == 422
    r = client.post("/api/config/security", json={"metrics_allowlist": ["172.20.0.0/16", " "]})
    assert r.status_code == 200
    assert r.json()["metrics_allowlist"] == ["172.20.0.0/16"]
    assert client.get("/api/config/security").json()["metrics_allowlist"] == ["172.20.0.0/16"]
