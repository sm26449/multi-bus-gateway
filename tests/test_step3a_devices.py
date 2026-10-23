# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Devices, server side of the 2026-10-21 UI audit (lot a): a save asked
first (dry run) never touches anything, a refusal says which field, poll
groups keep what a save did not send, a component names its kind, a
channel says who holds it."""
import json


from tests.test_devices_api import make_app, needs_tc
from tests.test_unchanged_save import DEVICES, _stored


def _raw_text(cfg):
    return cfg.config_path.read_text()


@needs_tc
def test_a_dry_run_create_checks_everything_and_makes_nothing(tmp_path):
    cfg, client = make_app(tmp_path, DEVICES)
    before = _raw_text(cfg)
    body = {"id": "em-new", "name": "New", "template": "eastron_sdm630",
            "connection": {"protocol": "tcp", "host": "192.0.2.77", "port": 502, "unit_id": 1}}
    r = client.post("/api/devices?dry_run=true", json=body)
    assert r.status_code == 200 and r.json() == {"status": "valid", "would": "create"}, r.text
    assert _raw_text(cfg) == before and cfg.get_device("em-new") is None
    assert not any(d["id"] == "em-new" for d in client.get("/api/devices").json()["devices"])
    # a refusal names its field, next to the whole list as always
    r = client.post("/api/devices?dry_run=true", json={**body, "connection": {"protocol": "tcp", "port": 502}})
    d = r.json()["detail"]
    assert r.status_code == 422 and "connection.host" in d["fields"]
    assert isinstance(d["errors"], list) and all(isinstance(e, str) for e in d["errors"])
    # a taken id is refused as the save would
    r = client.post("/api/devices?dry_run=true", json={**body, "id": "udp"})
    assert r.status_code == 422 and "id" in r.json()["detail"]["fields"]


@needs_tc
def test_a_dry_run_edit_says_whether_it_would_restart_and_writes_nothing(tmp_path):
    cfg, client = make_app(tmp_path, DEVICES)
    stored = _stored(cfg, "udp")
    before = _raw_text(cfg)
    ask = lambda body: client.put("/api/devices/udp?dry_run=true", json=body)  # noqa: E731
    assert ask(stored).json() == {"status": "valid", "would": "unchanged"}
    assert ask({**stored, "name": "UDP meter 2"}).json()["would"] == "update"
    moved = {**stored, "connection": {**stored["connection"], "port": 1502}}
    assert ask(moved).json()["would"] == "update_restart"
    assert _raw_text(cfg) == before
    # the real save's refusal carries the fields too
    r = client.put("/api/devices/udp", json={**stored, "connection": {**stored["connection"], "port": "x"}})
    assert r.status_code == 422 and "connection.port" in r.json()["detail"]["fields"]


@needs_tc
def test_a_dry_run_on_the_primary_saves_nothing_and_reconnects_nothing(tmp_path):
    cfg, client = make_app(tmp_path)
    before, timeout = _raw_text(cfg), cfg.modbus.timeout
    m = cfg.modbus
    r = client.put("/api/devices/umg512?dry_run=true", json={"connection": {
        "protocol": "tcp", "host": m.host, "port": m.port, "unit_id": m.unit_id, "timeout": float(m.timeout) + 2}})
    assert r.status_code == 200 and r.json() == {"status": "valid", "would": "update_restart"}, r.text
    assert _raw_text(cfg) == before and cfg.modbus.timeout == timeout
    # the values it already reads with are no save at all (FINDINGS PROD-1):
    # nothing is rewritten, nothing reconnected
    r = client.put("/api/devices/umg512?dry_run=true", json={"connection": {
        "protocol": "tcp", "host": m.host, "port": m.port, "unit_id": m.unit_id, "timeout": m.timeout}})
    assert r.json()["would"] == "unchanged"


@needs_tc
def test_poll_groups_keep_what_a_save_did_not_send(tmp_path):
    cfg, client = make_app(tmp_path, DEVICES)
    path = cfg.device_registers_path("udp")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": "1.0", "registers": [], "poll_groups": {
        "normal": {"interval": 5, "description": "the usual"},
        "static": {"interval": 3600, "description": "nameplate"}}}))
    r = client.post("/api/devices/udp/poll-groups", json={"poll_groups": {"normal": {"interval": 7}}})
    assert r.status_code == 200, r.text
    got = json.loads(path.read_text())["poll_groups"]
    assert got == {"normal": {"interval": 7.0, "description": "the usual"},
                   "static": {"interval": 3600, "description": "nameplate"}}
    client.post("/api/devices/udp/poll-groups", json={"poll_groups": {"normal": {"interval": 7, "description": ""}}})
    assert json.loads(path.read_text())["poll_groups"]["normal"]["description"] == ""
    client.post("/api/devices/udp/poll-groups", json={"poll_groups": {"static": None}})
    assert "static" not in json.loads(path.read_text())["poll_groups"]


@needs_tc
def test_a_component_names_its_kind(tmp_path):
    from tests.test_device_components import _app
    cfg, client = _app(tmp_path)
    comps = client.get("/api/devices/gx/components").json()["components"]
    assert {c["id"]: c["type_name"] for c in comps}["gx-battery-288"] == "Battery"
    entry = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "gx-battery-288")
    assert entry["component_type_name"] == "Battery"
    gx = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "gx")
    assert "component_type_name" not in gx


@needs_tc
def test_a_channel_says_who_holds_it_and_for_how_long_a_pause_runs(tmp_path):
    from tests.test_commands import _Inverter
    from tests.test_rules_api import _rule_app
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    ch = client.get("/api/channels/pv-u1").json()["channels"][0]
    assert ch["owner"] is None and ch["paused_until_s"] is None and ch["max_override_s"] == 24 * 3600
    assert client.post("/api/rules/ov-u1/mode", json={"mode": "armed"}).status_code == 200
    ch = client.get("/api/channels/pv-u1").json()["channels"][0]
    assert ch["owner"] == "ov-u1"
    assert client.post("/api/rules/ov-u1/override", json={"seconds": 600}).status_code == 200
    ch = client.get("/api/channels/pv-u1").json()["channels"][0]
    assert ch["owner"] is None and 590 <= ch["paused_until_s"] <= 600
