# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""ThresholdEngine: value → band → alert event, with fast-alarm/slow-clear
hysteresis. Pure state machine, exhaustively exercised."""
from multibus.threshold_engine import ThresholdEngine

# a symmetric grid-voltage-style threshold set
T = {"enabled": True, "warningLow": 216, "dangerLow": 207,
     "warningHigh": 245, "dangerHigh": 253}


def _eng(**kw):
    return ThresholdEngine(**kw)


def _feed(e, *vals, key="k", th=T):
    """Feed a sequence, return the list of (band, severity) events emitted."""
    out = []
    for v in vals:
        ev = e.evaluate(key, v, th, source="Meter", label="Grid V", unit="V")
        if ev:
            out.append((ev["band"], ev["severity"]))
    return out


# ── disabled / empty / non-numeric ───────────────────────────────────────────

def test_disabled_thresholds_never_fire():
    e = _eng()
    assert e.evaluate("k", 999, {"enabled": False, "dangerHigh": 1}) is None
    assert e.evaluate("k", 999, None) is None
    assert e.evaluate("k", 999, {"enabled": True}) is None  # no bounds


def test_non_numeric_value_ignored():
    e = _eng()
    assert e.evaluate("k", "n/a", T) is None
    assert e.evaluate("k", None, T) is None


# ── baseline behaviour on first evaluation ───────────────────────────────────

def test_first_eval_normal_is_silent():
    assert _feed(_eng(), 230.0) == []


def test_first_eval_in_alarm_surfaces_it_when_alert_on_start():
    assert _feed(_eng(alert_on_start=True), 260.0) == [("danger_high", "error")]


def test_first_eval_in_alarm_silent_when_alert_on_start_off():
    assert _feed(_eng(alert_on_start=False), 260.0) == []


# ── escalation is immediate ──────────────────────────────────────────────────

def test_normal_to_warning_to_danger_high():
    e = _eng()
    assert _feed(e, 230, 248, 255) == [
        ("warning_high", "warn"), ("danger_high", "error")]


def test_low_side_is_symmetric():
    e = _eng()
    assert _feed(e, 230, 210, 200) == [
        ("warning_low", "warn"), ("danger_low", "error")]


# ── hysteresis: no flapping, slow to clear ───────────────────────────────────

def test_hovering_at_high_boundary_does_not_flap():
    e = _eng(deadband_pct=2.0)          # clear band ≈ 245*0.02 = 4.9 V
    # cross up into warning, then hover just under the raw limit → must NOT clear
    ev = _feed(e, 230, 246, 244, 243, 241.5)
    assert ev == [("warning_high", "warn")]     # single event; no clear yet


def test_clears_only_after_retreating_past_deadband():
    e = _eng(deadband_pct=2.0)          # need to drop below 245 - 4.9 = 240.1
    assert _feed(e, 230, 246) == [("warning_high", "warn")]
    assert _feed(e, 241) == []          # still inside the deadband → sticky
    assert _feed(e, 239) == [("normal", "info")]   # cleared


def test_danger_deescalates_to_warning_then_clears():
    e = _eng(deadband_pct=2.0)
    assert _feed(e, 230, 255) == [("danger_high", "error")]   # into danger
    # drop below dangerHigh-deadband (253-5.06=247.9) but still > warningHigh
    assert _feed(e, 247) == [("warning_high", "warn")]        # de-escalate
    assert _feed(e, 239) == [("normal", "info")]              # clear


def test_zero_deadband_is_pure_boundary():
    e = _eng(deadband_pct=0.0)
    assert _feed(e, 230, 246) == [("warning_high", "warn")]
    assert _feed(e, 244) == [("normal", "info")]   # clears immediately below 245


# ── clear + message content ──────────────────────────────────────────────────

def test_full_round_trip_normal_after_alarm_fires_info():
    e = _eng()
    bands = _feed(e, 230, 260, 230)
    assert bands == [("danger_high", "error"), ("normal", "info")]


def test_message_describes_bound_and_value():
    e = _eng()
    e.evaluate("k", 230, T, label="Grid V", unit="V")
    ev = e.evaluate("k", 255, T, source="Meter", label="Grid V", unit="V")
    assert ev["message"] == "Grid V = 255 V — above danger limit 253 V"
    assert ev["source"] == "Meter" and ev["key"] == "k"


# ── state hygiene ────────────────────────────────────────────────────────────

def test_disabled_after_active_clears_state_without_stuck_alarm():
    e = _eng()
    _feed(e, 230, 260)                              # now in danger_high
    assert e.evaluate("k", 260, {"enabled": False}) is None
    # re-enabled from a clean slate: a normal value is a silent baseline again
    assert _feed(e, 230) == []


def test_retain_prunes_gone_keys():
    e = _eng()
    e.evaluate("a", 260, T)
    e.evaluate("b", 260, T)
    e.retain({"a"})
    assert "a" in e._band and "b" not in e._band


def test_forget_drops_key():
    e = _eng()
    e.evaluate("a", 260, T)
    e.forget("a")
    assert "a" not in e._band


# ── onlyWhen: a threshold judged only while another field is above a value ──
from multibus.threshold_engine import ThresholdEngine as _TE, gate_open  # noqa: E402

_THD = {"enabled": True, "warningHigh": 5, "dangerHigh": 8,
        "onlyWhen": {"field": "current_l1", "above": 2}}


def test_gate_open_follows_the_named_field():
    assert gate_open(_THD, {"current_l1": 10.0}.get)
    assert not gate_open(_THD, {"current_l1": 0.4}.get)
    assert not gate_open(_THD, {}.get)                 # nothing to judge by
    assert gate_open({"enabled": True, "dangerHigh": 8}, {}.get)   # no condition


def test_a_gated_threshold_never_alarms_and_clears_an_active_alarm_loudly():
    eng = _TE(alert_on_start=True)
    assert eng.evaluate("k", 80.0, _THD, gated=True) is None      # near-zero load
    ev = eng.evaluate("k", 9.0, _THD)                               # real load, real THD
    assert ev and ev["band"] == "danger_high"
    ev = eng.evaluate("k", 80.0, _THD, gated=True)                  # load gone
    assert ev and ev["band"] == "normal"
    assert eng.evaluate("k", 80.0, _THD, gated=True) is None        # and stays quiet
