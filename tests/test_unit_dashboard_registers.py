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
"""An endpoint unit's registers live in per-source files; the dashboard asks
without a source. The GET must answer with the union (first source owns a
name), the POST must route edits back to the owning source file, and the
fleet hero metrics must come from the same union — the pv units showed an
empty dashboard while their pollers read dozens of registers."""
import json

import pytest

from multibus.api import create_api
from tests.test_commands_api import PLANT_YAML
from tests.test_devices import write_config
from tests.test_unit_registers import _sel

try:
    from fastapi.testclient import TestClient
    _HAS_TC = True
except Exception:  # noqa: BLE001
    _HAS_TC = False

needs_tc = pytest.mark.skipif(not _HAS_TC, reason="TestClient not installed")


def make_plant_app(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    _sel(cfg.source_registers_path('pv-u1', 'solar_api'),
         [(1, 'power_active_total', 'power/active/total'),
          (2, 'energy_active_generated', 'energy/active/generated')])
    _sel(cfg.source_registers_path('pv-u1', 'sunspec'),
         [(40083, 'power_active_total', 'power/active/total'),
          (40092, 'voltage_l1_n', 'voltage/l1_n')])
    devices = [(d, None) for d in cfg.devices]
    app, _ = create_api(cfg, None, None, None, devices=devices)
    return cfg, app, TestClient(app, raise_server_exceptions=False)


@needs_tc
def test_unit_get_without_source_answers_the_union(tmp_path):
    _cfg, _app, client = make_plant_app(tmp_path)
    d = client.get("/api/registers/selected?device=pv-u1").json()
    names = [r["name"] for r in d["registers"]]
    assert names == ['power_active_total', 'energy_active_generated', 'voltage_l1_n']
    # first source that declares a name owns it — solar_api's address wins
    assert d["registers"][0]["address"] == 1
    assert [s["id"] for s in d["sources"]] == ['solar_api', 'sunspec']


@needs_tc
def test_unit_post_without_source_routes_to_owning_source_file(tmp_path):
    cfg, _app, client = make_plant_app(tmp_path)
    rows = client.get("/api/registers/selected?device=pv-u1").json()["registers"]
    for r in rows:
        r["ui_show_on_dashboard"] = r["name"] == "voltage_l1_n"
    resp = client.post("/api/registers/selected?device=pv-u1", json=rows)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 3 and set(body["sources"]) == {'solar_api', 'sunspec'}
    # the edit landed in the SOURCE files; the device-root file stayed out of it
    sun = json.loads(cfg.source_registers_path('pv-u1', 'sunspec').read_text())
    sol = json.loads(cfg.source_registers_path('pv-u1', 'solar_api').read_text())
    sun_by = {r["name"]: r for r in sun["registers"]}
    sol_by = {r["name"]: r for r in sol["registers"]}
    assert sun_by["voltage_l1_n"]["ui"]["show_on_dashboard"] is True
    assert sol_by["power_active_total"]["ui"]["show_on_dashboard"] is False
    # the union's power row belongs to solar_api; sunspec's own copy is
    # untouched (it was not the owner of that name)
    assert "ui" not in sun_by["power_active_total"] \
        or sun_by["power_active_total"].get("ui", {}).get("show_on_dashboard", True) is True
    assert not cfg.device_registers_path('pv-u1').exists() \
        or json.loads(cfg.device_registers_path('pv-u1').read_text()).get("registers") in ([], None)
    # and the GET reflects the save (round-trip through the union)
    d2 = client.get("/api/registers/selected?device=pv-u1").json()
    flags = {r["name"]: r["ui_show_on_dashboard"] for r in d2["registers"]}
    assert flags == {'power_active_total': False,
                     'energy_active_generated': False,
                     'voltage_l1_n': True}


@needs_tc
def test_fleet_hero_uses_the_union_for_units(tmp_path):
    _cfg, _app, client = make_plant_app(tmp_path)
    data = client.get("/api/fleet").json()
    u1 = next(r for r in data["devices"] if r["id"] == "pv-u1")
    assert [h["name"] for h in u1["hero"]][:2] == ['power_active_total',
                                                   'energy_active_generated']
    assert u1["endpoint_id"]
    # the site strip's endpoint card: live aggregate fields ride along
    ep = next(p for p in data["endpoints"] if p["id"] == u1["endpoint_id"])
    assert {"name", "status", "units_online", "units_total",
            "power_active_total"} <= set(ep)
