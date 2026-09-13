# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""`POST /api/endpoints/{id}/test` probes every unit over EVERY source.

Before sources existed it probed the endpoint's top-level Modbus connection
alone — which a grouped installation does not have — so an installation
reading perfectly over HTTP answered "blocked: host required" for every unit
(UI audit, finding 1.2).
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tests.test_endpoints import make_app, needs_tc


class _SolarApi(BaseHTTPRequestHandler):
    """Answers like a Fronius: unit 1 exists, unit 2 does not."""

    def do_GET(self):
        if 'DeviceId=1' in self.path:
            body = {"Body": {"Data": {"PAC": {"Value": 4200, "Unit": "W"},
                                      "FAC": {"Value": 50.0, "Unit": "Hz"}}}}
            code = 200
        else:
            body, code = {"Head": {"Status": {"Code": 255}}}, 404
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):  # quiet
        pass


@pytest.fixture
def solar_api():
    srv = HTTPServer(('127.0.0.1', 0), _SolarApi)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


# the fixture server lives on loopback, which the HTTP fetch guard rejects by
# default (SSRF into local services) — the operator opt-out lets a test reach it
NONLAN = "security:\n  allow_nonlan_http_devices: true\n"


def _plant(base):
    return {
        "id": "pv", "name": "PV", "enabled": False,
        "groups": [{
            "id": "inverters", "role": "inverter", "units": [1, 2],
            "sources": [
                {"id": "solar_api", "protocol": "http",
                 "url": base + "/solar_api/v1/x.cgi?DeviceId=${unit_id}",
                 "template": "fronius_solar_api_inverter",
                 "poll_groups": {"realtime": {"interval": 2}}},
                {"id": "pushed", "protocol": "mqtt", "topic": "inv/${unit_id}",
                 "template": "fronius_solar_api_inverter"}]}],
    }


@needs_tc
def test_every_unit_is_probed_over_every_source(tmp_path, solar_api):
    cfg, client = make_app(tmp_path, extra_yaml=NONLAN)
    assert client.post("/api/endpoints", json=_plant(solar_api)).status_code == 200
    r = client.post("/api/endpoints/pv/test")
    assert r.status_code == 200, r.text
    d = r.json()
    by = {(u['source'], u['unit_id']): u for u in d['units']}
    assert set(by) == {('solar_api', 1), ('solar_api', 2), ('pushed', 1), ('pushed', 2)}
    # the unit the datalogger knows answers, the one it does not fails —
    # over the source that reads them, not over a Modbus host nobody declared
    assert by[('solar_api', 1)]['ok'] is True, by[('solar_api', 1)]
    assert 'paths resolved' in by[('solar_api', 1)]['message']
    assert by[('solar_api', 2)]['ok'] is False
    assert 'blocked' not in by[('solar_api', 2)]['message']
    # a pushed input cannot be asked — said plainly, never counted as failed
    assert by[('pushed', 1)]['ok'] is None
    assert 'not probed' in by[('pushed', 1)]['message']
    # per-source census for the page's headline line
    srcs = {s['id']: s for s in d['sources']}
    assert srcs['solar_api'] == {**srcs['solar_api'], 'protocol': 'http',
                                 'answered': 1, 'probed': 2, 'total': 2, 'ok': False}
    assert srcs['pushed']['probed'] == 0 and srcs['pushed']['ok'] is None
    assert d['ok'] is False


@needs_tc
def test_the_url_is_redacted_and_substituted_per_unit(tmp_path, solar_api):
    cfg, client = make_app(tmp_path, extra_yaml=NONLAN)
    assert client.post("/api/endpoints", json=_plant(solar_api)).status_code == 200
    d = client.post("/api/endpoints/pv/test").json()
    wheres = {u['unit_id']: u['where'] for u in d['units'] if u['source'] == 'solar_api'}
    assert 'DeviceId=1' in wheres[1] and 'DeviceId=2' in wheres[2]
