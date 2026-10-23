# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Installations: what a save checks, and only where it changes something
(UI audit 2026-10-21, P5 and P14).

An http group or source needs an http(s) URL, and a group read through a
connection needs a map. Both are judged on what the save adds or changes: an
installation written before these checks is still renamed, and saved
unchanged, as before."""
import copy

import yaml

from tests.test_devices_api import make_app, needs_tc
from tests.test_unchanged_save import PLANT, _ep

LEGACY = PLANT + """
  - id: old
    name: Old
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.41, port: 502}
    groups:
      - id: api
        template: fronius_solar_api_inverter
        connection: {protocol: http, url: 192.0.2.50}
        units: [1]
      - id: bare
        connection: {protocol: tcp, host: 192.0.2.42, port: 502}
        units: [5]
      - id: viaapi
        template: fronius_solar_api_inverter
        units: [6]
        sources:
          - {id: api, protocol: http, url: 192.0.2.51}
"""


def _put(client, pid, body):
    return client.put(f"/api/endpoints/{pid}", json=body)


def _with_group(cfg, pid, group):
    ep = _ep(cfg, pid)
    ep["groups"] = ep["groups"] + [group]
    return ep


@needs_tc
def test_an_installation_written_before_the_checks_still_saves(tmp_path):
    cfg, client = make_app(tmp_path, LEGACY)
    stored = _ep(cfg, "old")
    r = _put(client, "old", stored)
    assert r.status_code == 200 and r.json()["status"] == "unchanged", r.text
    r = _put(client, "old", {**stored, "name": "Old site"})
    assert r.status_code == 200, r.text
    assert _ep(cfg, "old")["groups"] == stored["groups"]
    # the production-shaped installations: unchanged is unchanged
    for pid in ("pv", "bank"):
        assert _put(client, pid, _ep(cfg, pid)).json()["status"] == "unchanged"


@needs_tc
def test_an_http_group_needs_an_http_url(tmp_path):
    cfg, client = make_app(tmp_path, PLANT)
    bad = {"id": "api", "template": "fronius_solar_api_inverter", "units": [7],
           "connection": {"protocol": "http", "url": "192.0.2.50"}}
    r = _put(client, "pv", _with_group(cfg, "pv", bad))
    assert r.status_code == 422 and "groups[2].connection.url: '192.0.2.50' is not an http(s) URL" in r.text
    good = copy.deepcopy(bad)
    good["connection"]["url"] = "http://192.0.2.50/solar_api/v1/GetInverterRealtimeData.cgi?DeviceId=${unit_id}"
    r = _put(client, "pv", _with_group(cfg, "pv", good))
    assert r.status_code == 200, r.text


@needs_tc
def test_an_http_source_needs_an_http_url_unless_it_was_stored_so(tmp_path):
    cfg, client = make_app(tmp_path, LEGACY)
    ep = _ep(cfg, "old")
    ep["groups"][2]["sources"].append({"id": "api2", "protocol": "http", "url": "foo"})
    r = _put(client, "old", ep)
    assert r.status_code == 422 and "sources[1].url: 'foo' is not an http(s) URL" in r.text
    assert "sources[0]" not in r.text                     # the stored one is not judged


@needs_tc
def test_a_group_read_through_a_connection_needs_a_map(tmp_path):
    cfg, client = make_app(tmp_path, PLANT)
    nomap = {"id": "extra", "units": [9], "connection": {"protocol": "tcp", "host": "192.0.2.43", "port": 502}}
    r = _put(client, "pv", _with_group(cfg, "pv", nomap))
    assert r.status_code == 422 and "groups[2].template: required" in r.text
    r = _put(client, "pv", _with_group(cfg, "pv", {**nomap, "template": "fronius_sunspec_meter"}))
    assert r.status_code == 200, r.text
    # the stored group with no map (LEGACY "bare") is not judged until it changes
    (tmp_path / "b").mkdir()
    cfg, client = make_app(tmp_path / "b", LEGACY)
    ep = _ep(cfg, "old")
    ep["groups"][1]["connection"]["port"] = 1502
    r = _put(client, "old", ep)
    assert r.status_code == 422 and "groups[1].template: required" in r.text


# ── virtual meters: Add takes what Edit takes ───────────────────────────────

from tests.test_vmeter_lifecycle import PORT_A, _save_template, app_with_mgr  # noqa: E402,F401


@needs_tc
def test_a_virtual_meter_is_added_with_its_refresh_and_ha_mode(app_with_mgr):  # noqa: F811
    client, mgr = app_with_mgr
    _save_template(client)
    r = client.post("/api/virtual-meters", json={"template": "t_em", "port": PORT_A, "unit_id": 0,
                                                 "update_interval_s": 5, "ha_mode": "balanced"})
    assert r.status_code == 200, r.text
    inst = next(i for i in client.get("/api/virtual-meters").json()["instances"] if i["template"] == "t_em")
    assert inst["update_interval_s"] == 5 and inst["ha_mode"] == "balanced" and inst["unit_id"] == 0
    client.delete("/api/virtual-meters/t_em")
    r = client.post("/api/virtual-meters", json={"template": "t_em", "port": PORT_A, "ha_mode": "x"})
    assert r.status_code == 400 and "ha_mode" in r.text
    r = client.post("/api/virtual-meters", json={"template": "t_em", "port": PORT_A, "unit_id": 300})
    assert r.status_code == 400 and "unit_id" in r.text
    r = client.post("/api/virtual-meters", json={"template": "t_em", "port": PORT_A})
    assert r.status_code == 200, r.text
    raw = yaml.safe_load(open(mgr.config_path)) if hasattr(mgr, "config_path") else None
    inst = next(i for i in client.get("/api/virtual-meters").json()["instances"] if i["template"] == "t_em")
    assert inst["update_interval_s"] == 1.0 and inst["ha_mode"] == "failover"
    if raw is not None:
        stored = next(i for i in raw["instances"] if i["template"] == "t_em")
        assert "update_interval_s" not in stored and "ha_mode" not in stored    # absent: today's defaults


# ── a group that takes the installation's template keeps taking it ─────────

@needs_tc
def test_a_group_view_says_its_own_template_apart_from_the_effective_one(tmp_path):
    """The view's `template` is the effective one (the group's, else the
    installation's); an editor writing that back froze an inherited template
    into the group. `template_declared` is what the group says itself."""
    cfg, client = make_app(tmp_path, PLANT)
    view = client.get("/api/endpoints/bank").json()["groups"]
    g = next(x for x in view if x["id"] == "units")
    assert g["template"] == "seplos_bms_v3_rtu_tap" and g["template_declared"] == ""
    own = [x for x in client.get("/api/endpoints/pv").json()["groups"] if x.get("template_declared")]
    assert own and all(x["template_declared"] == x["template"] for x in own)
    # written back as the editor writes it (the group's own template only):
    # unchanged, and the group still takes the installation's
    stored = _ep(cfg, "bank")
    body = {**stored, "groups": [{k: v for k, v in grp.items() if k != "template"} for grp in stored["groups"]]}
    r = _put(client, "bank", body)
    assert r.status_code == 200 and r.json()["status"] == "unchanged", r.text
    assert "template" not in _ep(cfg, "bank")["groups"][0]
    # adding another group leaves it as it was
    more = {"id": "more", "units": [3],
            "connection": {"protocol": "rtu_tap", "serial_port": "/dev/ttyBANK", "baudrate": 19200}}
    r = _put(client, "bank", {**body, "groups": body["groups"] + [more]})
    assert r.status_code == 200, r.text
    groups = _ep(cfg, "bank")["groups"]
    assert "template" not in groups[0] and "template" not in groups[1]
    assert next(x for x in client.get("/api/endpoints/bank").json()["groups"]
                if x["id"] == "more")["template"] == "seplos_bms_v3_rtu_tap"
