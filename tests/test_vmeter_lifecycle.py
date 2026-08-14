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
"""Lifecycle coverage for the 3.21–3.23 vmeter surface (audit 2026-08-14:
virtual_meter_manager 49%, routes/vmeters 27% — the newest code had the least
coverage). Exercises the REST routes end-to-end against a REAL manager:
instance add/edit/toggle/delete with device_fallback, the template editor
routes, and the published state contract (staleness policy + failover routing,
3.23.0)."""
import json
import time

import pytest

from multibus.virtual_meter_manager import VirtualMeterManager

from tests.test_devices_api import make_app, needs_tc

PORT_A = 25502          # test port range — never collides with the live
PORT_B = 25503          # gateway's published 1502-1512 host bindings


def _entry(name, value=1.0, mono=None, interval=None):
    return {"name": name, "value": value, "mono": mono, "interval": interval,
            "label": name, "unit": ""}


@pytest.fixture
def app_with_mgr(tmp_path, monkeypatch):
    monkeypatch.setenv("VMETER_PORT_START", str(PORT_A))
    monkeypatch.setenv("VMETER_PORT_END", str(PORT_A + 8))
    cfg, client = make_app(tmp_path)
    now = time.monotonic()
    current = {1: _entry("power_active_total", -1500.0, mono=now),
               2: _entry("frequency", 50.0, mono=now)}
    twin = {7: _entry("power_active_total", -1490.0, mono=now)}
    mgr = VirtualMeterManager(
        current, config_path=str(tmp_path / "vm.yaml"),
        templates_dir=str(tmp_path / "templates"),
        device_values={"twin": twin}, primary_device_id="janitza")
    client.app.state.vmeter_manager = mgr
    yield client, mgr
    mgr.stop_all()


def _save_template(client, tid="t_em", port=PORT_A):
    return client.put(f"/api/virtual-meters/template/{tid}", json={
        "name": "Test meter", "byte_order": "big", "port": port, "unit_id": 1,
        "registers": [
            {"addr": "0x0000", "type": "int32", "source_kind": "live",
             "source": "power_active_total", "scale": 10},
            {"addr": "0x0002", "type": "uint16", "source_kind": "live",
             "source": "frequency", "scale": 10},
            {"addr": "0x0010", "type": "uint16", "source_kind": "const",
             "source": 1651}]})


@needs_tc
def test_template_editor_routes_roundtrip(app_with_mgr):
    client, mgr = app_with_mgr
    assert _save_template(client).status_code == 200
    got = client.get("/api/virtual-meters/template/t_em").json()
    assert got["id"] == "t_em" and len(got["registers"]) == 3
    # export → import (overwrite) roundtrip through the routes
    exp = client.get("/api/virtual-meters/template/t_em/export").json()
    r = client.post("/api/virtual-meters/templates/import",
                    json={"yaml": exp["yaml"], "overwrite": True})
    assert r.status_code == 200
    # templates list shows it
    tids = [t["id"] for t in client.get("/api/virtual-meters/templates").json()["templates"]]
    assert "t_em" in tids
    # sources endpoint reads the live store
    names = [s["name"] for s in client.get("/api/virtual-meters/sources").json()["sources"]]
    assert "power_active_total" in names


@needs_tc
def test_instance_add_reject_bad_fallback_and_port(app_with_mgr):
    client, _ = app_with_mgr
    _save_template(client)
    r = client.post("/api/virtual-meters", json={
        "template": "t_em", "port": PORT_A, "device_fallback": "nope"})
    assert r.status_code == 400 and "unknown device_fallback" in r.json()["detail"]
    r = client.post("/api/virtual-meters", json={"template": "t_em", "port": 999})
    assert r.status_code == 400 and "outside the published range" in r.json()["detail"]
    r = client.post("/api/virtual-meters", json={
        "template": "t_em", "port": PORT_A, "stale_after_s": -1})
    assert r.status_code == 400


@needs_tc
def test_instance_lifecycle_with_fallback_and_state_publish(app_with_mgr):
    client, mgr = app_with_mgr
    _save_template(client)
    # add (disabled) with a VALID fallback twin
    r = client.post("/api/virtual-meters", json={
        "template": "t_em", "port": PORT_A, "unit_id": 1,
        "device_fallback": "twin", "enabled": False})
    assert r.status_code == 200 and r.json()["added"] is True
    ov = client.get("/api/virtual-meters").json()
    inst = next(i for i in ov["instances"] if i["template"] == "t_em")
    assert inst["device_fallback"] == "twin"
    assert ov["port_range"]["start"] == PORT_A

    # PATCH: move port + drop the fallback with "" (documented clear form)
    r = client.patch("/api/virtual-meters/t_em",
                     json={"port": PORT_B, "device_fallback": ""})
    assert r.status_code == 200
    inst = next(i for i in client.get("/api/virtual-meters").json()["instances"]
                if i["template"] == "t_em")
    assert inst["port"] == PORT_B and inst["device_fallback"] == ""

    # PATCH it back on, then toggle the meter ON → _start_one applies the
    # fallback rewrite and a real TCP server comes up on PORT_B
    assert client.patch("/api/virtual-meters/t_em",
                        json={"device_fallback": "twin"}).status_code == 200
    r = client.post("/api/virtual-meters/t_em/toggle?on=true")
    assert r.status_code == 200
    vm = next(m for m in mgr.meters if m.t.id == "t_em")
    by = {reg.addr: reg for reg in vm.t.registers}
    assert by[0].source_kind == "failover"
    assert by[0].source == ["power_active_total", "twin.power_active_total"]
    assert by[0x10].source_kind == "const"              # const untouched

    # 3.23.0 state contract: policy + bounds + quality + failover routing
    class _Pub:
        def __init__(self): self.msgs = {}
        def publish_state(self, sub, payload, retain=True):
            self.msgs[sub] = json.loads(payload)
    pub = _Pub()
    mgr.mqtt_publisher = pub
    vm._rebuild_block()                                  # populate quality/routing
    mgr._publish_states()
    st = pub.msgs["vmeter/t_em/state"]
    assert st["on_stale"] == "legacy" and st["stale_after_s"] == 15.0
    assert "quality" in st and "failover" in st
    routes = {f["addr"]: f for f in st["failover"]}
    assert routes[0]["candidates"] == ["power_active_total", "twin.power_active_total"]
    assert routes[0]["active"] == "power_active_total"   # primary fresh → primary

    # values + stats + decode routes against the live meter (after the
    # fallback rewrite the rows are failover-kind → keyed by address)
    vals = client.get("/api/virtual-meters/t_em/values").json()
    assert vals["values"]["addr_0"]["quality"] == "good"
    assert client.get("/api/virtual-meters/t_em/stats").json()["id"] == "t_em"
    # decode needs the pymodbus block — wait for the supervisor to start the
    # server (first fresh tick), then read through the decode route
    for _ in range(30):
        if getattr(vm, "_block", None):
            break
        time.sleep(0.1)
    dec = client.get("/api/virtual-meters/t_em/decode?addr=0&count=2").json()
    assert dec["registers"][0]["value"] == pytest.approx(-1500.0)

    # toggle off, delete instance, template delete now allowed
    assert client.post("/api/virtual-meters/t_em/toggle?on=false").status_code == 200
    assert client.delete("/api/virtual-meters/t_em").status_code == 200
    assert client.get("/api/virtual-meters").json()["instances"] == []
    assert client.delete("/api/virtual-meters/template/t_em").status_code == 200


@needs_tc
def test_fallover_activates_twin_when_primary_stale(app_with_mgr):
    """End-to-end device_fallback behavior: primary source goes stale → the
    row serves the twin's value and the published routing shows the switch."""
    client, mgr = app_with_mgr
    _save_template(client)
    assert client.post("/api/virtual-meters", json={
        "template": "t_em", "port": PORT_A, "device_fallback": "twin",
        "enabled": True}).status_code == 200
    vm = next(m for m in mgr.meters if m.t.id == "t_em")
    # primary fresh → primary serves
    vm._rebuild_block()
    assert vm.failover_routes()[0]["active"] == "power_active_total"
    # primary stale (frozen mono), twin fresh → twin serves
    mgr.current_values[1]["mono"] = time.monotonic() - 9999
    mgr.device_values["twin"][7]["mono"] = time.monotonic()
    vm._rebuild_block()
    assert vm.failover_routes()[0]["active"] == "twin.power_active_total"
    # and back (recovery)
    mgr.current_values[1]["mono"] = time.monotonic()
    vm._rebuild_block()
    assert vm.failover_routes()[0]["active"] == "power_active_total"


# ── audit 2026-08-14 M3: `enabled` default must be ON on EVERY path ──────────

@needs_tc
def test_missing_enabled_key_survives_edits(app_with_mgr):
    """A hand-edited config row without `enabled` boots ON (start_all default).
    A template re-save and an instance PATCH must apply the SAME default —
    before the fix both read `inst.get("enabled")` and silently stopped (or
    refused to restart) the meter."""
    client, mgr = app_with_mgr
    _save_template(client)
    assert client.post("/api/virtual-meters", json={
        "template": "t_em", "port": PORT_A, "enabled": True}).status_code == 200
    cfg = mgr._load_cfg()                     # simulate the legacy/hand-edited row
    cfg["instances"][0].pop("enabled")
    mgr._save_cfg(cfg)
    assert any(m.t.id == "t_em" for m in mgr.meters)

    # template edit → _reload_instance restarts (does not silently stop)
    assert _save_template(client).status_code == 200
    assert any(m.t.id == "t_em" for m in mgr.meters)

    # instance PATCH → live restart happens
    r = client.patch("/api/virtual-meters/t_em", json={"stale_after_s": 5})
    assert r.status_code == 200 and r.json()["restarted"] is True

    # and the row reads as enabled everywhere it is reported
    ov = client.get("/api/virtual-meters").json()
    inst = next(i for i in ov["instances"] if i["template"] == "t_em")
    assert inst["enabled"] is True
    assert mgr.health()["enabled_meters"] == 1


@needs_tc
def test_toggle_start_failure_reverts_the_flag(app_with_mgr):
    """set_enabled(True) whose start fails must roll the persisted flag back
    and answer 400 — not 500 with `enabled: true` left on disk for the next
    boot to trip over."""
    client, mgr = app_with_mgr
    _save_template(client)
    assert client.post("/api/virtual-meters", json={
        "template": "t_em", "port": PORT_A, "enabled": False}).status_code == 200

    def _boom(inst):
        raise RuntimeError("port already bound")
    orig, mgr._start_one = mgr._start_one, _boom
    r = client.post("/api/virtual-meters/t_em/toggle?on=true")
    assert r.status_code == 400 and "enable reverted" in r.json()["detail"]
    inst = mgr._load_cfg()["instances"][0]
    assert inst["enabled"] is False           # rolled back, boot stays clean

    # with the fault gone the same toggle succeeds
    mgr._start_one = orig
    assert client.post("/api/virtual-meters/t_em/toggle?on=true").status_code == 200
    assert any(m.t.id == "t_em" for m in mgr.meters)
