# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""L3.11c (ADR-0032 §8): Delete shows a toast with Undo, and Undo is the
existing restore. Undo must bring back exactly what Delete removed, or the
toast must say plainly what it cannot.

What a restore brings back, checked here against the device as it was:
- its configuration entry, key for key — except the two things a restore
  resets on purpose (ADR-0025 ADD-12: a restored device comes back safe):
  the write lock and its command bindings. The DELETE answer says which of
  the two a restore will reset for THIS device (`restore_resets`), and the
  restore answer says which it did (`reset`), so the toast says it plainly;
- its measurement selection, its attributes, its components, its Home
  Assistant node id;
- what ran beside it: its REST push (a restore used to leave it stopped until
  the next restart) and its retained attributes `…/meta` topic.

"Deleted devices: restore…" opens a dialog that lists every deleted device
and explains, before the click, why one cannot come back as it was:
`GET /api/devices/restorable?check=true` asks the same checks a restore makes."""
from types import SimpleNamespace

from fastapi.testclient import TestClient

from multibus.api import create_api
from tests.test_devices import write_config
from tests.test_devices_api import make_app, needs_tc


INV = """
devices:
  - id: inv
    name: Inverter
    enabled: false
    write_locked: false
    template: fronius_sunspec_inverter
    connection: { protocol: tcp, host: 192.0.2.9, port: 502, unit_id: 1 }
    mqtt: { topic_prefix: site/inv, ha_node: my_inverter }
    commands:
      - { name: power_limit, enabled: true }
      - { name: restore, enabled: false }
  - id: em24
    name: Hall EM24
    enabled: false
    template: carlo_gavazzi_em24
    connection: { protocol: tcp, host: 192.0.2.10, port: 1502, unit_id: 2 }
    mqtt: { topic_prefix: meters/hall }
"""

GX = """
devices:
  - id: gx
    name: Victron GX
    template: victron_gx_mqtt
    enabled: false
    connection: {protocol: mqtt, broker: 192.0.2.10, topic: 'N/abc/#', vars: {portal: abc}}
    components:
      - {type: battery, instance: 288, name: SmartShunt}
"""


def _without(raw, *keys):
    return {k: v for k, v in (raw or {}).items() if k not in keys}


# ── the delete says what Undo cannot bring back; the restore says what it reset ──
@needs_tc
def test_the_delete_answer_says_what_a_restore_resets(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml=INV)
    r = client.delete("/api/devices/inv")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "deleted"
    # unlocked before, one binding on: both come back reset, and it is said
    assert body["restore_resets"] == {"write_locked": True, "commands": ["power_limit"]}
    r = client.post("/api/devices/inv/restore")
    assert r.status_code == 200, r.text
    assert r.json()["reset"] == {"write_locked": True, "commands": ["power_limit"]}


@needs_tc
def test_a_device_that_was_safe_already_comes_back_exactly(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml=INV)
    # em24: write-locked by default (a stored device without the key is not
    # migrated: say it explicitly) and no commands
    assert client.post("/api/devices/em24/write-lock", json={"locked": True}).status_code == 200
    r = client.delete("/api/devices/em24")
    assert r.status_code == 200, r.text
    assert r.json()["restore_resets"] == {}
    r = client.post("/api/devices/em24/restore")
    assert r.status_code == 200, r.text
    assert r.json()["reset"] == {}


@needs_tc
def test_undo_brings_back_the_entry_registers_attributes_and_ha_node(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml=INV)
    client.post("/api/registers/selected?device=em24", json=[
        {"address": 0, "name": "voltage_l1_n", "label": "L1", "unit": "V",
         "data_type": "float", "poll_group": "realtime"}])
    assert client.put("/api/attributes/devices/inv/rating", json={"rated_ac_kw": 8}).status_code == 200
    before_inv = cfg.get_raw_device("inv")
    before_em = cfg.get_raw_device("em24")
    attrs_before = client.get("/api/attributes/devices/inv").json()
    regs_before = client.get("/api/registers/selected?device=em24").json()["registers"]
    for did in ("inv", "em24"):
        assert client.delete(f"/api/devices/{did}").status_code == 200
        assert client.post(f"/api/devices/{did}/restore").status_code == 200
    after_inv = cfg.get_raw_device("inv")
    # the entry, key for key, but for the two resets the delete answer named
    assert _without(after_inv, "write_locked", "commands") == _without(before_inv, "write_locked", "commands")
    assert [c["name"] for c in after_inv["commands"]] == [c["name"] for c in before_inv["commands"]]
    assert cfg.get_raw_device("em24") == {**before_em, "write_locked": True}
    # its Home Assistant node id: the same entities come back in HA
    assert cfg.get_device("inv").ha_node == "my_inverter"
    assert client.get("/api/attributes/devices/inv").json() == attrs_before
    assert client.get("/api/registers/selected?device=em24").json()["registers"] == regs_before


@needs_tc
def test_undo_brings_back_its_components(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml=GX)
    comps = client.get("/api/devices/gx/components").json()["components"]
    assert client.delete("/api/devices/gx").status_code == 200
    assert client.post("/api/devices/gx/restore").status_code == 200
    again = client.get("/api/devices/gx/components").json()["components"]
    key = lambda c: (c["id"], c["type"], c["instance"], c["name"], c["enabled"])   # noqa: E731
    assert [key(c) for c in again] == [key(c) for c in comps]


@needs_tc
def test_a_restore_starts_its_rest_push_again(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml=INV)
    mgr = client.app.state.rest_push_manager
    try:
        r = client.post("/api/devices/em24/rest-push", json={
            "enabled": True, "url": "http://192.0.2.9/ingest", "interval_s": 60, "format": "flat"})
        assert r.status_code == 200, r.text
        assert mgr.running("em24")
        assert client.delete("/api/devices/em24").status_code == 200
        assert not mgr.running("em24")
        assert client.post("/api/devices/em24/restore").status_code == 200
        assert mgr.running("em24"), "a restored device pushes again, as it did before the delete"
    finally:
        mgr.apply("em24", {"enabled": False}, lambda: {})


class _Pub:
    connected = True
    connects = 1
    config = SimpleNamespace(enabled=True, topic_prefix="mbg", ha_discovery_enabled=True,
                             ha_discovery_prefix="homeassistant")

    def __init__(self):
        self.sent = []

    def _publish(self, topic, payload, retain=None):
        self.sent.append((topic, payload))
        return True

    def __getattr__(self, n):
        return lambda *a, **k: None


@needs_tc
def test_a_restore_publishes_its_attributes_meta_again(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=INV)
    pub = _Pub()
    app, _ = create_api(cfg, None, pub, None, devices=[(d, None) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    assert client.put("/api/attributes/devices/inv/rating", json={"rated_ac_kw": 8}).status_code == 200
    assert client.delete("/api/devices/inv").status_code == 200
    assert ("site/inv/meta", "") in pub.sent, "the delete clears it"
    pub.sent.clear()
    assert client.post("/api/devices/inv/restore").status_code == 200
    assert any(t == "site/inv/meta" and p for t, p in pub.sent), pub.sent


# ── the Restore dialog: why one cannot come back as it was, before the click ──
@needs_tc
def test_the_restorable_list_explains_a_conflict_before_the_click(tmp_path):
    tty = tmp_path / "ttyS5"
    tty.write_text("")
    cfg, client = make_app(tmp_path, extra_yaml=f"""
devices:
  - {{id: poller, enabled: false, connection: {{protocol: rtu, serial_port: {tty}, unit_id: 1}}}}
  - {{id: other, enabled: false, connection: {{protocol: tcp, host: 192.0.2.30, port: 502, unit_id: 1}}}}
""")
    assert client.delete("/api/devices/poller").status_code == 200
    assert client.delete("/api/devices/other").status_code == 200
    # meanwhile a listener took the bus the poller was on
    r = client.post("/api/devices", json={"id": "tap", "enabled": False,
                                          "connection": {"protocol": "rtu_tap", "serial_port": str(tty), "unit_id": 1}})
    assert r.status_code == 200, r.text
    plain = client.get("/api/devices/restorable").json()["devices"]
    assert all("problems" not in d for d in plain), "the plain listing stays as it was"
    by = {d["id"]: d for d in client.get("/api/devices/restorable?check=true").json()["devices"]}
    assert by["other"]["problems"] == []
    assert by["other"]["resets"] == {"write_locked": True}
    probs = by["poller"]["problems"]
    assert probs and any("tap" in p for p in probs), probs
    # the restore refuses with the same words
    r = client.post("/api/devices/poller/restore")
    assert r.status_code == 422
    assert r.json()["detail"]["errors"] == probs
    # nothing was restored by asking
    assert {d["id"] for d in client.get("/api/devices").json()["devices"]} >= {"tap"}
    assert not any(d["id"] in ("poller", "other") for d in client.get("/api/devices").json()["devices"])
