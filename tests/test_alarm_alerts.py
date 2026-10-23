# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""What a device says is wrong becomes an alert: a template's alarm field
that turns active (and stays so for its for_s) fires once, a new error code
fires again, the clear is announced — and a webhook body can nest the alert
the way its receiver wants it (alertd's /v1/fire)."""
from multibus.alarm_alerts import AlarmTracker
from multibus.alerts import AlertManager

DECL = [{"field": "err", "severity": "danger"}, {"field": "low_flow", "severity": "warning", "for_s": 600}]


def _tracker():
    fired, clock = [], [0.0]
    t = AlarmTracker(lambda *a, **k: fired.append((a, k)), clock=lambda: clock[0])
    return t, fired, clock


def _act(**v):
    return [{"field": f, "label": f.upper(), "severity": "danger" if f == "err" else "warning", "value": x}
            for f, x in v.items()]


def test_an_error_code_fires_once_again_on_a_new_code_and_clears():
    t, fired, clock = _tracker()
    t.update("hp", "Heat pump", DECL, _act(err=101))
    t.update("hp", "Heat pump", DECL, _act(err=101))
    t.update("hp", "Heat pump", DECL, _act(err=201))
    t.update("hp", "Heat pump", DECL, [])
    clock[0] = 61.0                                   # inactive for a minute: the clear goes out
    t.update("hp", "Heat pump", DECL, [])
    sev = [(a[0], a[3]) for a, _k in fired]
    assert sev == [("error", "ERR: 101"), ("error", "ERR: 201"), ("info", "ERR: cleared")]
    assert all(k == {"transition": True} for _a, k in fired)
    assert fired[0][0][1] == "alarm:hp:err" and fired[0][0][2] == "Heat pump"


def test_an_alarm_must_hold_for_its_time_and_a_blip_says_nothing():
    t, fired, clock = _tracker()
    t.update("hp", "HP", DECL, _act(low_flow=1))
    clock[0] = 300.0
    t.update("hp", "HP", DECL, [])                     # gone before 600 s: never announced, no clear
    clock[0] = 400.0
    t.update("hp", "HP", DECL, _act(low_flow=1))
    clock[0] = 999.0
    t.update("hp", "HP", DECL, _act(low_flow=1))
    clock[0] = 1000.0
    t.update("hp", "HP", DECL, _act(low_flow=1))
    assert [(a[0], a[3]) for a, _k in fired] == [("warn", "LOW_FLOW: 1")]


def test_a_device_that_went_away_is_forgotten_a_down_one_is_not():
    t, fired, clock = _tracker()
    t.update("hp", "HP", DECL, _act(err=101))
    t.retain({"hp"})
    assert "alarm:hp:err" in t.state
    t.retain(set())
    assert t.state == {}


def test_a_webhook_body_is_filled_at_any_depth():
    m = AlertManager({"enabled": True, "webhook_body": {
        "type": "mbg_alert", "context": {"severity": "{severity}", "text": "{source}: {message}",
                                         "tags": ["{key}", 3]}}})
    body = m._render_body({"severity": "error", "source": "Heat pump", "message": "ERR: 101",
                           "key": "alarm:hp:err", "ts": 1.0, "host": "h"})
    assert body == {"type": "mbg_alert", "context": {"severity": "error", "text": "Heat pump: ERR: 101",
                                                     "tags": ["alarm:hp:err", 3]}}


def test_a_form_that_does_not_show_a_signal_does_not_switch_it_off(tmp_path):
    import pytest
    tc = pytest.importorskip("fastapi.testclient")
    from multibus.api import create_api
    from tests.test_devices import write_config
    cfg = write_config(tmp_path)
    cfg.alerts = {"enabled": True, "signals": {"threshold": True}}
    app, _ = create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    client = tc.TestClient(app, raise_server_exceptions=False)
    g = client.get("/api/config/alerts").json()["signals"]
    assert g["threshold"] is True and g["alarm"] is True          # defaults filled in, stored kept
    r = client.post("/api/config/alerts", json={"enabled": True, "signals": {"device": False}})
    assert r.status_code == 200
    assert cfg.alerts["signals"] == {"threshold": True, "device": False}
