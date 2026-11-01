# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The L3.11c review (2026-11-01), server side.

- M1: a restore comes back safe (ADR-0025 ADD-12) for its components too:
  each comes back write-locked, and the delete / restore say which were not;
- M2: one malformed kept device does not take the Restore list down (a 500
  for every row); it says why in its own row and can still be forgotten;
- m1: an Undo is tied to the delete it undoes (the delete's `deleted_ts`);
- m2: "already back" is told apart from a failure (409 `code: exists`), and
  a delete whose kept settings could not be written says `restorable: false`;
- m6: what a restore starts again (its REST push, to which host) is said;
- n4: a binding reset is said even when it has no name."""
import json

from tests.test_devices_api import make_app, needs_tc
from tests.test_l311c_undo_restore import GX, INV


def _plant(tmp_path, did, raw, ts=1.0):
    d = tmp_path / "devices" / did
    d.mkdir(parents=True, exist_ok=True)
    (d / "device.json").write_text(json.dumps({"device": raw, "deleted_ts": ts}), encoding="utf-8")


# ── M1 ────────────────────────────────────────────────────────────────────
@needs_tc
def test_a_restored_component_comes_back_write_locked_and_it_is_said(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml=GX)
    cid = client.get("/api/devices/gx/components").json()["components"][0]["id"]
    assert client.post(f"/api/devices/{cid}/write-lock", json={"locked": False}).status_code == 200
    assert cfg.get_device(cid).write_locked is False
    r = client.delete("/api/devices/gx")
    assert r.status_code == 200, r.text
    assert r.json()["restore_resets"].get("components") == [cid], r.json()
    r = client.post("/api/devices/gx/restore")
    assert r.status_code == 200, r.text
    assert r.json()["reset"].get("components") == [cid]
    assert cfg.get_device(cid).write_locked is True, "a component comes back safe too"


# ── M2 ────────────────────────────────────────────────────────────────────
@needs_tc
def test_a_malformed_kept_device_is_said_in_its_row_and_can_be_forgotten(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml=INV)
    assert client.delete("/api/devices/em24").status_code == 200
    _plant(tmp_path, "bad-cmds", {"id": "bad-cmds", "connection": {"protocol": "tcp", "host": "192.0.2.40"}, "commands": 5})
    _plant(tmp_path, "bad-mqtt", {"id": "bad-mqtt", "connection": {"protocol": "tcp", "host": "192.0.2.41"}, "mqtt": "x"})
    r = client.get("/api/devices/restorable?check=true")
    assert r.status_code == 200, r.text
    by = {d["id"]: d for d in r.json()["devices"]}
    assert by["em24"]["problems"] == []
    for bad in ("bad-cmds", "bad-mqtt"):
        assert by[bad]["problems"], by[bad]
        assert isinstance(by[bad]["resets"], dict)
        assert client.post(f"/api/devices/{bad}/restore").status_code in (409, 422), bad
    assert client.delete("/api/devices/restorable/bad-cmds").status_code == 200
    assert "bad-cmds" not in {d["id"] for d in client.get("/api/devices/restorable").json()["devices"]}


# ── m1 / m2 ───────────────────────────────────────────────────────────────
@needs_tc
def test_an_undo_is_tied_to_the_delete_it_undoes(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml=INV)
    first = client.delete("/api/devices/em24").json()
    assert first["restorable"] is True and isinstance(first["deleted_ts"], (int, float)), first
    # another em24 made and deleted meanwhile: the kept settings are its
    body = {"id": "em24", "name": "Another", "template": "carlo_gavazzi_em24", "enabled": False,
            "connection": {"protocol": "tcp", "host": "192.0.2.77", "port": 502, "unit_id": 1}}
    assert client.post("/api/devices", json=body).status_code == 200
    import time
    time.sleep(0.01)
    second = client.delete("/api/devices/em24").json()
    assert second["deleted_ts"] != first["deleted_ts"]
    r = client.post(f"/api/devices/em24/restore?deleted_ts={first['deleted_ts']}")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "tombstone_changed", r.text
    assert client.get("/api/devices/em24").status_code == 404, "nothing restored"
    r = client.post(f"/api/devices/em24/restore?deleted_ts={second['deleted_ts']}")
    assert r.status_code == 200, r.text
    assert cfg.get_device("em24").name == "Another"
    # once more: it is already back — said as such, not as a failure
    r = client.post(f"/api/devices/em24/restore?deleted_ts={second['deleted_ts']}")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "exists", r.text


@needs_tc
def test_a_delete_whose_kept_settings_could_not_be_written_is_not_restorable(tmp_path, monkeypatch):
    cfg, client = make_app(tmp_path, extra_yaml=INV)
    monkeypatch.setattr(cfg._tombstones, "write", lambda did, raw: None)      # the write failed
    r = client.delete("/api/devices/em24")
    assert r.status_code == 200, r.text
    assert r.json()["restorable"] is False and r.json().get("deleted_ts") is None, r.json()


# ── m6 ────────────────────────────────────────────────────────────────────
@needs_tc
def test_what_a_restore_starts_again_is_said(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml=INV)
    mgr = client.app.state.rest_push_manager
    try:
        assert client.post("/api/devices/em24/rest-push", json={
            "enabled": True, "url": "http://user:pw@192.0.2.9:8086/ingest?token=x", "interval_s": 60,
            "format": "flat"}).status_code == 200
        r = client.delete("/api/devices/em24").json()
        assert r["restore_resumes"] == [{"what": "rest_push", "device": "em24", "host": "192.0.2.9:8086"}], r
        listed = {d["id"]: d for d in client.get("/api/devices/restorable?check=true").json()["devices"]}
        assert listed["em24"]["resumes"] == r["restore_resumes"]
        r = client.post("/api/devices/em24/restore").json()
        assert r["resumed"] == [{"what": "rest_push", "device": "em24", "host": "192.0.2.9:8086"}], r
    finally:
        mgr.apply("em24", {"enabled": False}, lambda: {})


# ── n4 ────────────────────────────────────────────────────────────────────
@needs_tc
def test_a_binding_reset_is_said_even_without_a_name(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml=INV)
    _plant(tmp_path, "noname", {"id": "noname", "connection": {"protocol": "tcp", "host": "192.0.2.42"},
                                "write_locked": True, "commands": [{"enabled": True}, {"name": "x", "enabled": False}]})
    by = {d["id"]: d for d in client.get("/api/devices/restorable?check=true").json()["devices"]}
    assert by["noname"]["resets"] == {"commands": ["#1"]}, by["noname"]


# ── second review (R1, R2, R4) ────────────────────────────────────────────
def _plant_text(tmp_path, did, text):
    d = tmp_path / "devices" / did
    d.mkdir(parents=True, exist_ok=True)
    (d / "device.json").write_text(text, encoding="utf-8")


SHAPES = {
    "bad-conn": json.dumps({"device": {"id": "bad-conn", "connection": "x"}, "deleted_ts": 1}),
    "bad-list": json.dumps({"device": [1, 2], "deleted_ts": 1}),
    "bad-top": json.dumps([{"device": {"id": "bad-top"}}]),
}


@needs_tc
def test_r1_a_wrongly_shaped_kept_device_is_listed_said_and_can_be_forgotten(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml=INV)
    assert client.delete("/api/devices/em24").status_code == 200
    for did, text in SHAPES.items():
        _plant_text(tmp_path, did, text)
    r = client.get("/api/devices/restorable")
    assert r.status_code == 200, r.text
    assert set(SHAPES) | {"em24"} <= {d["id"] for d in r.json()["devices"]}
    r = client.get("/api/devices/restorable?check=true")
    assert r.status_code == 200, r.text
    by = {d["id"]: d for d in r.json()["devices"]}
    assert by["em24"]["problems"] == []
    for did in SHAPES:
        assert by[did]["problems"], by[did]
        assert client.post(f"/api/devices/{did}/restore").status_code == 422, did
        assert client.delete(f"/api/devices/restorable/{did}").status_code == 200, did
    assert {d["id"] for d in client.get("/api/devices/restorable").json()["devices"]} == {"em24"}


@needs_tc
def test_r4_an_unreadable_kept_device_is_said_in_plain_words(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml=INV)
    _plant(tmp_path, "bad-mqtt", {"id": "bad-mqtt", "connection": {"protocol": "tcp", "host": "192.0.2.41"}, "mqtt": "x"})
    _plant_text(tmp_path, "bad-conn", SHAPES["bad-conn"])
    for did in ("bad-mqtt", "bad-conn"):
        r = client.post(f"/api/devices/{did}/restore")
        assert r.status_code == 422, r.text
        said = " ".join(r.json()["detail"]["errors"])
        assert "Error" not in said and "attribute" not in said and "object" not in said, said
    by = {d["id"]: d for d in client.get("/api/devices/restorable?check=true").json()["devices"]}
    for did in ("bad-mqtt", "bad-conn"):
        assert not any("Error" in p for p in by[did]["problems"]), by[did]


@needs_tc
def test_r2_a_pushs_host_is_said_as_it_is_written(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml=INV)
    for did, url in (("v6", "http://[fd00::1]:8443/ingest"), ("badport", "http://192.0.2.9:99999/ingest")):
        _plant(tmp_path, did, {"id": did, "write_locked": True, "connection": {"protocol": "tcp", "host": "192.0.2.50"},
                               "rest_push": {"enabled": True, "url": url}})
    by = {d["id"]: d for d in client.get("/api/devices/restorable?check=true").json()["devices"]}
    assert by["v6"]["resumes"][0]["host"] == "[fd00::1]:8443", by["v6"]
    assert by["badport"]["resumes"][0]["host"] == "192.0.2.9", by["badport"]
