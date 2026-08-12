# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Threshold engine ⇄ AlertManager integration: config parsing, the signal
toggle, and a crossing firing through the existing alert path."""
from multibus.alerts import AlertManager
from multibus.threshold_engine import ThresholdEngine

T = {"enabled": True, "warningHigh": 245, "dangerHigh": 253}


def test_alertmanager_parses_threshold_config():
    a = AlertManager({"enabled": True, "signals": {"threshold": True},
                      "threshold_deadband_pct": 3.5,
                      "threshold_alert_on_start": False})
    assert a.sig_threshold is True
    assert a.threshold_deadband_pct == 3.5
    assert a.threshold_alert_on_start is False
    st = a.status()
    assert st["signals"]["threshold"] is True
    assert st["threshold_deadband_pct"] == 3.5


def test_threshold_signal_defaults_off():
    a = AlertManager({"enabled": True})
    assert a.sig_threshold is False        # value alerting is opt-in
    assert a.threshold_deadband_pct == 2.0


def test_crossing_fires_through_alert_manager():
    # enabled manager, no MQTT/webhook → alerts land in the in-memory recent()
    a = AlertManager({"enabled": True, "min_interval_s": 0})
    eng = ThresholdEngine()

    def pump(value):
        ev = eng.evaluate("thr:dev:1", value, T, source="Meter",
                          label="Grid V", unit="V")
        if ev:
            a.fire(ev["severity"], ev["key"], ev["source"], ev["message"])

    pump(230)          # baseline normal — silent
    assert a.recent() == []
    pump(255)          # into danger_high → fires
    pump(230)          # back to normal → clear fires

    fired = a.recent()
    assert {x["severity"] for x in fired} == {"error", "info"}   # alarm + clear
    assert "danger" in [x for x in fired if x["severity"] == "error"][0]["message"]
    assert all(x["key"] == "thr:dev:1" for x in fired)


def test_rate_limit_still_applies_as_backstop():
    # even if the engine somehow emitted twice, the per-key min_interval guards
    a = AlertManager({"enabled": True, "min_interval_s": 9999})
    a.fire("error", "thr:dev:1", "Meter", "first")
    a.fire("error", "thr:dev:1", "Meter", "second (suppressed)")
    assert len(a.recent()) == 1
