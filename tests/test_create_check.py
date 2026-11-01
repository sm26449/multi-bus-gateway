# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""L3.9-F3: Add Device's create says how its Check ended — `check: ok | failed
| skipped` (and the preview job it read, `check_job`) — and the create's
audit line records it, so "Next — without a check" is in the trail. The
words are the create's only: nothing of them is stored on the device; a
dry run takes and ignores them; anything else is a 422 at the field."""
import json

from tests.test_devices_api import make_app, needs_tc

BODY = {"id": "chk1", "name": "Checked", "template": "eastron_sdm120", "enabled": False,
        "connection": {"protocol": "tcp", "host": "127.0.0.1", "port": 15990, "unit_id": 1}}


def _created(client):
    lines = client.app.state.ctx.audit_log.recent(50)
    return [e for e in lines if e.get("action") == "device created"]


@needs_tc
def test_the_create_records_how_the_check_ended(tmp_path):
    cfg, client = make_app(tmp_path)
    r = client.post("/api/devices", json=dict(BODY, check="skipped", check_job="abc123"))
    assert r.status_code == 200, r.text
    got = _created(client)
    assert len(got) == 1 and got[0]["target"] == "chk1", got
    det = json.loads(got[0]["detail"]) if isinstance(got[0]["detail"], str) else got[0]["detail"]
    assert det["check"] == "skipped" and det["check_job"] == "abc123", det
    raw = cfg.get_raw_device("chk1")
    assert "check" not in raw and "check_job" not in raw


@needs_tc
def test_a_create_without_the_word_is_recorded_as_not_said(tmp_path):
    cfg, client = make_app(tmp_path)
    assert client.post("/api/devices", json=dict(BODY, id="chk2")).status_code == 200
    det = _created(client)[0]["detail"]
    det = json.loads(det) if isinstance(det, str) else det
    assert det["check"] == "not said"


@needs_tc
def test_a_wrong_word_is_refused_at_its_field_and_a_dry_run_ignores_it(tmp_path):
    cfg, client = make_app(tmp_path)
    r = client.post("/api/devices", json=dict(BODY, id="chk3", check="maybe"))
    assert r.status_code == 422 and "check" in r.json()["detail"]["fields"], r.text
    r = client.post("/api/devices?dry_run=true", json=dict(BODY, id="chk4", check="ok", check_job="j"))
    assert r.status_code == 200, r.text
    assert cfg.get_raw_device("chk4") is None
