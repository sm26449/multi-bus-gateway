# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Unit tests for the soft-delete lifecycle collaborator (tombstone_store.py)."""
import json

from multibus.tombstone_store import TombstoneStore


def _store(tmp_path):
    # identity safe_id is fine for the test ids used here
    return TombstoneStore(tmp_path / "devices", lambda x: x)


def test_write_load_list_forget_roundtrip(tmp_path):
    s = _store(tmp_path)
    raw = {"id": "em24", "name": "Warehouse EM24",
           "template": "cg_em24", "connection": {"protocol": "tcp"}}
    s.write("em24", raw)

    # file exists, 0600, holds the definition + a deleted_ts
    tomb = s.path("em24")
    assert tomb.is_file()
    assert (tomb.stat().st_mode & 0o777) == 0o600
    payload = json.loads(tomb.read_text())
    assert payload["device"]["id"] == "em24" and payload["deleted_ts"] > 0

    # load returns the raw device dict
    assert s.load("em24") == raw

    # list: a summary while the id is NOT active; hidden once active
    (tomb.parent / "selected_registers.json").write_text(json.dumps(
        {"registers": [{"address": 0}, {"address": 2}]}))
    rows = s.list(active_ids=set())
    assert len(rows) == 1
    assert rows[0]["id"] == "em24" and rows[0]["registers"] == 2
    assert rows[0]["protocol"] == "tcp" and rows[0]["template"] == "cg_em24"
    assert s.list(active_ids={"em24"}) == []          # active → not restorable

    # forget removes the whole kept dir
    assert s.forget("em24") is True
    assert not tomb.parent.exists()
    assert s.forget("em24") is False                   # already gone
    assert s.load("em24") is None


def test_load_and_list_tolerate_missing_or_corrupt(tmp_path):
    s = _store(tmp_path)
    assert s.load("ghost") is None                     # no dir at all
    assert s.list(active_ids=set()) == []              # base dir doesn't exist yet
    # a corrupt tombstone is skipped, not fatal
    d = tmp_path / "devices" / "bad"
    d.mkdir(parents=True)
    (d / "device.json").write_text("{not json")
    assert s.list(active_ids=set()) == []
    assert s.load("bad") is None
