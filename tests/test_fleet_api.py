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
"""/api/fleet — the fleet-overview feed: one row per device with health,
explicit-threshold alarm counts and up to three hero metrics."""
import json

import pytest

from multibus.api import create_api
from multibus.config import Config

try:
    from fastapi.testclient import TestClient
    _HAS_TC = True
except Exception:  # noqa: BLE001
    _HAS_TC = False

needs_tc = pytest.mark.skipif(not _HAS_TC, reason="TestClient not installed")


def make_app(tmp_path, extra_yaml="", thresholds=None):
    (tmp_path / "config.yaml").write_text(f"""
modbus:
  host: 192.168.1.207
  port: 502
  unit_id: 1
mqtt:
  enabled: false
influxdb:
  enabled: false
{extra_yaml}""", encoding="utf-8")
    reg = {"address": 19000, "name": "voltage_l1_n", "label": "L1 voltage",
           "unit": "V", "data_type": "float", "poll_group": "realtime"}
    if thresholds is not None:
        reg["thresholds"] = thresholds
    (tmp_path / "selected_registers.json").write_text(json.dumps({
        "version": "1.0",
        "registers": [reg],
        "poll_groups": {"realtime": {"interval": 1}},
    }), encoding="utf-8")
    cfg = Config(str(tmp_path / "config.yaml"))
    devices = [(d, None) for d in cfg.devices]
    app, _ = create_api(cfg, None, None, None, devices=devices)
    return cfg, app, TestClient(app, raise_server_exceptions=False)


@needs_tc
def test_fleet_shape_and_hero(tmp_path):
    """One row per device; hero = first dashboard registers with address,
    label, unit; no live client -> health idle, zero alarms."""
    _cfg, _app, client = make_app(tmp_path)
    data = client.get("/api/fleet").json()
    assert isinstance(data["devices"], list) and len(data["devices"]) == 1
    d = data["devices"][0]
    assert d["health"] == "idle"
    assert d["alarms"] == {"danger": 0, "warning": 0}
    assert len(d["hero"]) == 1
    h = d["hero"][0]
    assert h["address"] == 19000 and h["label"] == "L1 voltage"
    assert h["unit"] == "V" and h["value"] is None
    assert data["endpoints"] == []


@needs_tc
def test_fleet_lists_every_device(tmp_path):
    """Tier-2 devices appear as rows of their own, endpoint_id carried."""
    _cfg, _app, client = make_app(tmp_path, extra_yaml="""
devices:
  - id: em24-hala
    name: EM24 Hala
    template: generic
    connection: {host: 10.0.0.9, port: 502, unit_id: 2}
""")
    rows = client.get("/api/fleet").json()["devices"]
    assert [r["id"] for r in rows].count("em24-hala") == 1
    em = next(r for r in rows if r["id"] == "em24-hala")
    assert em["name"] == "EM24 Hala" and em["endpoint_id"] == ""


@needs_tc
def test_fleet_alarms_explicit_thresholds_only(tmp_path):
    """A value beyond a CONFIGURED limit counts; band picked correctly and a
    disabled threshold block counts nothing."""
    thr = {"enabled": True, "warningHigh": 240, "dangerHigh": 250}
    _cfg, app, client = make_app(tmp_path, thresholds=thr)
    store = app.state.current_values
    store[19000] = {"value": 245.0, "unit": "V", "name": "voltage_l1_n"}
    d = client.get("/api/fleet").json()["devices"][0]
    assert d["alarms"] == {"danger": 0, "warning": 1}
    assert d["hero"][0]["value"] == 245.0

    store[19000]["value"] = 260.0
    d = client.get("/api/fleet").json()["devices"][0]
    assert d["alarms"] == {"danger": 1, "warning": 0}

    # disabled block -> no alarm even far beyond the numbers
    thr_off = dict(thr, enabled=False)
    _cfg2, app2, client2 = make_app(tmp_path, thresholds=thr_off)
    app2.state.current_values[19000] = {"value": 500.0, "unit": "V"}
    d = client2.get("/api/fleet").json()["devices"][0]
    assert d["alarms"] == {"danger": 0, "warning": 0}


@needs_tc
def test_fleet_hero_ignores_text_values_gracefully(tmp_path):
    """A text value (decoded enum/string register) passes through verbatim —
    the band check must not crash on it."""
    thr = {"enabled": True, "dangerHigh": 10}
    _cfg, app, client = make_app(tmp_path, thresholds=thr)
    app.state.current_values[19000] = {"value": "Standby", "unit": ""}
    d = client.get("/api/fleet").json()["devices"][0]
    assert d["alarms"] == {"danger": 0, "warning": 0}
    assert d["hero"][0]["value"] == "Standby"
