# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The review of L3.11b (m3, m5):

- an output an installation's unit sets for every unit of its installation
  (HTTP / JSON, REST push) or of its group (OPC UA, the publishing rhythm)
  is said by every one of them at once — their entries in the registry are
  drawn again, not only the unit written;
- the PQ recorder's interval is a number of seconds, at least 30: a word, a
  boolean or less is refused, nothing stored."""
import pathlib

import pytest

from tests.test_devices_api import make_app, needs_tc

SETUP = """
primary_device: false
endpoints:
  - id: plant
    name: Plant
    enabled: false
    groups:
      - id: inverters
        role: inverter
        template: eastron_sdm120
        connection: {protocol: tcp, host: 192.0.2.20, port: 502}
        units: [1, 2]
      - id: grid
        role: meter
        template: eastron_sdm120
        connection: {protocol: tcp, host: 192.0.2.21, port: 502}
        units: [{unit_id: 240, id: plant-grid}]
"""


def _app(tmp_path):
    return make_app(tmp_path, extra_yaml=SETUP)


def _row(client, did):
    return next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == did)


def _units(client):
    return sorted(d["id"] for d in client.get("/api/devices").json()["devices"] if d.get("endpoint_id") == "plant")


@needs_tc
def test_an_installation_wide_http_output_is_said_by_every_unit(tmp_path):
    _cfg, client = _app(tmp_path)
    units = _units(client)
    assert len(units) == 3, units
    r = client.post(f"/api/devices/{units[0]}/http-output", json={"enabled": True})
    assert r.status_code == 200, r.text
    got = [(u, _row(client, u)["http_output_enabled"]) for u in units]
    assert all(v is True for _u, v in got), got


@needs_tc
def test_an_installation_wide_rest_push_is_said_by_every_unit(tmp_path):
    _cfg, client = _app(tmp_path)
    units = _units(client)
    r = client.post(f"/api/devices/{units[0]}/rest-push",
                    json={"enabled": False, "url": "https://192.0.2.9/in", "interval_s": 60, "format": "native",
                          "verify_tls": True, "headers": {}})
    assert r.status_code == 200, r.text
    got = [(u, (_row(client, u).get("rest_push") or {}).get("url")) for u in units]
    assert all(v == "https://192.0.2.9/in" for _u, v in got), got


@needs_tc
def test_a_group_wide_opcua_output_is_said_by_every_unit_of_the_group_only(tmp_path):
    _cfg, client = _app(tmp_path)
    inv = [u for u in _units(client) if _row(client, u).get("group_id") == "inverters"]
    assert len(inv) == 2, inv
    before = _row(client, "plant-grid")["opcua_enabled"]
    want = not _row(client, inv[0])["opcua_enabled"]
    r = client.post(f"/api/devices/{inv[0]}/opcua-output", json={"enabled": want})
    assert r.status_code == 200, r.text
    got = [(u, _row(client, u)["opcua_enabled"]) for u in inv]
    assert all(v is want for _u, v in got), got
    assert _row(client, "plant-grid")["opcua_enabled"] is before


@needs_tc
def test_a_group_wide_publishing_rhythm_is_said_by_every_unit_of_the_group(tmp_path):
    _cfg, client = _app(tmp_path)
    inv = [u for u in _units(client) if _row(client, u).get("group_id") == "inverters"]
    r = client.post(f"/api/devices/{inv[0]}/publishing", json={"influxdb": {"write_interval": 30}})
    assert r.status_code == 200, r.text
    got = [(u, _row(client, u)["publishing"]["own"]["influxdb"]["write_interval"]) for u in inv]
    assert all(v == 30 for _u, v in got), got


PQ_SETUP = """
devices:
  - id: jz
    name: Janitza
    template: janitza_umg512_pro
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.30, port: 502, unit_id: 1}
"""


@needs_tc
@pytest.mark.parametrize("poll", ["abc", True, 10, 0, None, [300]])
def test_the_pq_interval_is_a_number_of_seconds_at_least_30(tmp_path, poll):
    cfg, client = make_app(tmp_path, extra_yaml=PQ_SETUP)
    path = pathlib.Path(cfg.config_path)
    before = path.read_bytes()
    r = client.post("/api/pq/config?device=jz", json={"enabled": False, "poll_s": poll})
    assert r.status_code == 422, r.text
    assert "poll_s" in r.text
    assert path.read_bytes() == before


@needs_tc
def test_a_pq_interval_of_seconds_is_taken(tmp_path):
    _cfg, client = make_app(tmp_path, extra_yaml=PQ_SETUP)
    r = client.post("/api/pq/config?device=jz", json={"enabled": False, "poll_s": 600})
    assert r.status_code == 200, r.text
