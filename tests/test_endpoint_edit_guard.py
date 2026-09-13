# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""An edit that does not mention groups or sources keeps them.

The Edit dialog of an installation sends name, enabled and the sink toggles
plus the flat shape it was born with (`units`, `connection`). Taken literally
that flattened a plant to one Modbus group, dropped every source and renamed
its site unit — on a save meant to change the name (UI audit, finding 1.1).
"""
import pytest

from tests.test_endpoints import make_app, needs_tc

PLANT = {
    "id": "pv", "name": "PV installation", "enabled": False,
    "groups": [
        {"id": "inverters", "role": "inverter", "units": [1, 2],
         "sources": [
             {"id": "solar_api", "protocol": "http",
              "url": "http://192.0.2.9/solar_api/v1/x.cgi?DeviceId=${unit_id}",
              "template": "fronius_solar_api_inverter",
              "poll_groups": {"realtime": {"interval": 2}}},
             {"id": "sunspec", "protocol": "tcp",
              "host": "192.0.2.9", "port": 502,
              "template": "fronius_sunspec_inverter",
              "poll_groups": {"normal": {"interval": 20}}}]},
        {"id": "site", "role": "site",
         "units": [{"unit_id": 0, "id": "pv-site", "name": "Site totals"}],
         "sources": [
             {"id": "solar_api", "protocol": "http",
              "url": "http://192.0.2.9/solar_api/v1/GetPowerFlowRealtimeData.fcgi",
              "template": "fronius_solar_api_site",
              "poll_groups": {"realtime": {"interval": 2}}}]},
    ],
}

# exactly what the Edit dialog sends: nothing about groups or sources
UI_EDIT = {
    "id": "pv", "name": "Roof PV", "enabled": False,
    "connection": {"protocol": "tcp", "host": "", "port": 502, "max_connections": 1},
    "units": [1, 2, 0], "aggregates": True,
    "mqtt": {"enabled": True, "ha_discovery": False},
    "influxdb": {"enabled": True}, "template": "",
}


def _ids(cfg):
    return [d.id for d in cfg.endpoint_devices("pv")]


@needs_tc
def test_a_settings_edit_keeps_the_groups_and_their_sources(tmp_path):
    cfg, client = make_app(tmp_path)
    assert client.post("/api/endpoints", json=PLANT).status_code == 200, "setup"
    before = _ids(cfg)
    r = client.put("/api/endpoints/pv", json=UI_EDIT)
    assert r.status_code == 200, r.text
    ep = r.json()["endpoint"]
    assert [g["id"] for g in ep["groups"]] == ["inverters", "site"]
    assert [s["id"] for s in ep["groups"][0]["sources"]] == ["solar_api", "sunspec"]
    assert ep["name"] == "Roof PV"
    # the units are still the groups' units — no pv-u0 born from the flat list
    assert _ids(cfg) == before == ["pv-u1", "pv-u2", "pv-site"]
    raw = cfg.get_raw_endpoint("pv")
    assert [g["id"] for g in raw["groups"]] == ["inverters", "site"]
    assert "units" not in raw or not raw["units"]


@needs_tc
def test_a_settings_edit_with_an_empty_host_is_not_refused(tmp_path):
    """The grouped installation has no top-level host; the dialog sends an
    empty one. That must not be an error — it was the ONLY thing standing
    between the operator and the flattening above."""
    cfg, client = make_app(tmp_path)
    assert client.post("/api/endpoints", json=PLANT).status_code == 200
    r = client.put("/api/endpoints/pv", json=dict(UI_EDIT, name="Renamed"))
    assert r.status_code == 200, r.text
    assert cfg.get_raw_endpoint("pv")["name"] == "Renamed"


@needs_tc
def test_an_explicit_groups_list_still_replaces(tmp_path):
    """Keeping omitted groups must not freeze them: the group editor sends the
    full list and that list wins."""
    cfg, client = make_app(tmp_path)
    assert client.post("/api/endpoints", json=PLANT).status_code == 200
    body = dict(PLANT, groups=[PLANT["groups"][0]])
    r = client.put("/api/endpoints/pv", json=body)
    assert r.status_code == 200, r.text
    assert [g["id"] for g in r.json()["endpoint"]["groups"]] == ["inverters"]
    assert _ids(cfg) == ["pv-u1", "pv-u2"]


FLAT_TWO_SOURCES = {
    "id": "inv", "name": "Inverters", "enabled": False,
    "connection": {"protocol": "tcp", "host": "192.0.2.9", "port": 502},
    "units": [1, 2],
    "sources": [
        {"id": "sunspec", "protocol": "tcp", "host": "192.0.2.9", "port": 502,
         "template": "fronius_sunspec_inverter"},
        {"id": "solar_api", "protocol": "http",
         "url": "http://192.0.2.9/x.cgi?DeviceId=${unit_id}",
         "template": "fronius_solar_api_inverter",
         "poll_groups": {"realtime": {"interval": 5}}}],
}


@needs_tc
def test_a_flat_endpoint_keeps_its_sources_when_the_form_omits_them(tmp_path):
    cfg, client = make_app(tmp_path)
    assert client.post("/api/endpoints", json=FLAT_TWO_SOURCES).status_code == 200
    body = {k: v for k, v in FLAT_TWO_SOURCES.items() if k != "sources"}
    r = client.put("/api/endpoints/inv", json=dict(body, name="Renamed"))
    assert r.status_code == 200, r.text
    assert [s["id"] for s in r.json()["endpoint"]["sources"]] == ["sunspec", "solar_api"]
    assert [s.id for s in cfg.get_device("inv-u1").sources] == ["sunspec", "solar_api"]


@needs_tc
def test_a_flat_rename_lands_on_the_unit_inside_its_group(tmp_path):
    """The unit page and the installation page rename a unit by re-sending the
    flat unit list with a name. On a grouped installation that list is not
    membership — but the NAME is the operator's, and must land."""
    cfg, client = make_app(tmp_path)
    assert client.post("/api/endpoints", json=PLANT).status_code == 200
    body = dict(UI_EDIT, name="PV installation",
                units=[{"unit_id": 1, "id": "pv-u1", "name": "East inverter"},
                       {"unit_id": 2, "id": "pv-u2"}, {"unit_id": 0, "id": "pv-site"}])
    r = client.put("/api/endpoints/pv", json=body)
    assert r.status_code == 200, r.text
    ep = r.json()["endpoint"]
    names = {u["device_id"]: u["name"] for u in ep["units"]}
    assert names["pv-u1"] == "East inverter"
    assert names["pv-site"] == "Site totals"              # the hand-written name survives
    assert [g["id"] for g in ep["groups"]] == ["inverters", "site"]
    assert [s["id"] for s in ep["groups"][0]["sources"]] == ["solar_api", "sunspec"]
    assert _ids(cfg) == ["pv-u1", "pv-u2", "pv-site"]
