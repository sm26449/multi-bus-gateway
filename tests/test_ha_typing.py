# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Explicit per-register Home Assistant entity typing. A register may carry
device_class/state_class/entity_category/enabled_by_default/icon/
suggested_display_precision; each overrides the unit heuristic, `"none"`
suppresses an inferred class, and unset falls back to the old inference."""
from types import SimpleNamespace

from multibus.mqtt_publisher import apply_ha_typing


def _reg(**kw):
    base = dict(unit="", device_class="", state_class="", entity_category="",
                enabled_by_default=None, icon="", suggested_display_precision=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _typed(**kw):
    cfg = {}
    apply_ha_typing(cfg, _reg(**kw))
    return cfg


# ── unchanged unit inference (no explicit fields) ────────────────────────────

def test_inference_power_is_measurement():
    c = _typed(unit="W")
    assert c["unit_of_measurement"] == "W"
    assert c["device_class"] == "power"
    assert c["state_class"] == "measurement"


def test_inference_energy_is_total_increasing():
    assert _typed(unit="kWh")["state_class"] == "total_increasing"
    assert _typed(unit="kWh")["device_class"] == "energy"


def test_kvarh_now_total_increasing_not_measurement():
    # regression: reactive-energy counters used to fall through to "measurement"
    c = _typed(unit="kvarh")
    assert c["state_class"] == "total_increasing"
    assert "device_class" not in c          # reactive energy has no HA device_class


def test_percent_unit_gets_no_device_class_but_keeps_measurement():
    c = _typed(unit="%")
    assert "device_class" not in c          # '%' maps to None
    assert c["state_class"] == "measurement"


def test_unitless_measurement_keeps_measurement_state_class():
    # power factor has no unit but must stay a statistic in HA
    c = _typed(unit="")
    assert "unit_of_measurement" not in c
    assert c["state_class"] == "measurement"


# ── explicit overrides ───────────────────────────────────────────────────────

def test_explicit_device_and_state_class_override_inference():
    c = _typed(unit="W", device_class="power_factor", state_class="total")
    assert c["device_class"] == "power_factor"
    assert c["state_class"] == "total"


def test_none_suppresses_inferred_classes_for_diagnostics():
    # a firmware-revision register: no state_class, grouped as diagnostic
    c = _typed(unit="", state_class="none", device_class="none",
               entity_category="diagnostic")
    assert "state_class" not in c
    assert "device_class" not in c
    assert c["entity_category"] == "diagnostic"


def test_passthrough_of_optional_typing_fields():
    c = _typed(unit="W", enabled_by_default=False, icon="mdi:flash",
               suggested_display_precision=1)
    assert c["enabled_by_default"] is False
    assert c["icon"] == "mdi:flash"
    assert c["suggested_display_precision"] == 1


def test_enabled_by_default_true_is_emitted_explicitly():
    assert _typed(unit="W", enabled_by_default=True)["enabled_by_default"] is True


# ── round-trip through template + config ─────────────────────────────────────

def test_typing_round_trips_through_template():
    from multibus.device_template import parse_template, validate_template
    tpl = {"device_template": {
        "id": "x_typ", "name": "X", "protocol": {},
        "registers": [{
            "address": 0, "name": "firmware_rev", "unit": "",
            "data_type": "uint16", "state_class": "none",
            "entity_category": "diagnostic", "enabled_by_default": False,
            "icon": "mdi:chip", "suggested_display_precision": 0,
        }]}}
    assert validate_template(tpl) == []
    r = parse_template(tpl).registers[0]
    assert r.state_class == "none" and r.entity_category == "diagnostic"
    assert r.enabled_by_default is False and r.icon == "mdi:chip"
    assert r.suggested_display_precision == 0
    d = r.to_dict()
    assert d["state_class"] == "none" and d["entity_category"] == "diagnostic"
    assert d["enabled_by_default"] is False and d["suggested_display_precision"] == 0


def test_typing_absent_by_default_stays_lean():
    from multibus.device_template import parse_template
    r = parse_template({"device_template": {
        "id": "x_plain2", "name": "X", "protocol": {},
        "registers": [{"address": 0, "name": "voltage_l1_n", "unit": "V",
                       "data_type": "uint16"}]}}).registers[0]
    d = r.to_dict()
    for k in ("device_class", "state_class", "entity_category",
              "enabled_by_default", "icon", "suggested_display_precision"):
        assert k not in d
