# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Templates ship DERIVED measurements (P3).

A vendor's decoded status, an alarm flag or a derived total belongs WITH the
device map, not retyped per unit — otherwise the fifth inverter of an endpoint
speaks differently from the first four. A template's ``calculated`` block is
seeded into every device made from it, the calc engine honours an explicit
topic so a derived value lands on the branch it belongs to, and an ``enum``
turns a computed code into text through the same decoder real registers use.
"""
import json

import pytest

from multibus.calc_engine import CalcEngine
from multibus.device_template import parse_template, validate_template


def _stub(calcs, regs=None):
    return {"device_template": {
        "id": "x_derived", "name": "X",
        "protocol": {"byte_order": "big"},
        "poll_groups": {"normal": {"interval": 5}},
        "registers": regs or [
            {"address": 40107, "name": "operating_state", "label": "St",
             "unit": "", "data_type": "uint16", "register_type": "holding",
             "poll_group": "normal"},
        ],
        "calculated": calcs,
    }}


_GOOD = {"name": "status_text", "label": "Operating state",
         "expr": "operating_state", "poll_group": "normal",
         "topic": "status/text", "influxdb": False,
         "enum": {"4": "Tracking power point", "7": "Fault"}}


# ── validation ───────────────────────────────────────────────────────────────

def test_a_well_formed_derived_measurement_validates_and_round_trips():
    data = _stub([_GOOD])
    assert validate_template(data) == []
    t = parse_template(data)
    assert [c.name for c in t.calculated] == ["status_text"]
    c = t.calculated[0]
    assert c.topic == "status/text" and c.influxdb is False and c.enum
    # export round-trips, and unset fields stay absent rather than "" noise
    out = t.to_dict()["device_template"]["calculated"][0]
    assert out["topic"] == "status/text" and out["influxdb"] is False
    assert "measurement" not in out
    assert validate_template(t.to_dict()) == []


def test_a_template_without_derived_measurements_exports_none():
    t = parse_template(_stub([]))
    assert t.calculated == []
    assert "calculated" not in t.to_dict()["device_template"]


def test_name_may_not_shadow_a_register():
    # expressions resolve by NAME out of one per-device store, so a shadowing
    # derived name would make the formula reference itself
    errs = validate_template(_stub([dict(_GOOD, name="operating_state")]))
    assert any("collides with a register name" in e for e in errs)


def test_duplicate_derived_names_are_rejected():
    errs = validate_template(_stub([_GOOD, dict(_GOOD)]))
    assert any("duplicate name" in e for e in errs)


@pytest.mark.parametrize("bad", ["", "__import__('os')", "lambda: 1"])
def test_invalid_expression_is_a_validation_error(bad):
    errs = validate_template(_stub([dict(_GOOD, expr=bad)]))
    assert errs and any("status_text" in e for e in errs)


@pytest.mark.parametrize("topic", ["/status/text", "status/text/", "a/+/b", "a/#"])
def test_topic_must_be_a_relative_leaf_without_wildcards(topic):
    errs = validate_template(_stub([dict(_GOOD, topic=topic)]))
    assert any("relative leaf" in e for e in errs)


def test_unknown_poll_group_is_rejected():
    errs = validate_template(_stub([dict(_GOOD, poll_group="hourly")]))
    assert any("not declared by this template" in e for e in errs)


@pytest.mark.parametrize("enum", [{}, {"x": "a"}, {"1": 2}, "nope"])
def test_malformed_enum_is_rejected(enum):
    assert validate_template(_stub([dict(_GOOD, enum=enum)])) != []


def test_bad_name_and_decimals_are_rejected():
    assert any("letters, digits" in e
               for e in validate_template(_stub([dict(_GOOD, name="has space")])))
    assert any("decimals" in e
               for e in validate_template(_stub([dict(_GOOD, decimals="two")])))


# ── seeding ──────────────────────────────────────────────────────────────────

def test_seeding_a_device_brings_the_templates_derived_measurements(tmp_path):
    from multibus.device_seed import autoselect_template_registers
    from tests.test_devices import write_config

    class _Registry:
        def __init__(self, t): self._t = t
        def get(self, tid): return self._t if tid == self._t.id else None

    tpl = parse_template(_stub([_GOOD]))
    cfg = write_config(tmp_path, extra_yaml="""
devices:
  - id: unit9
    template: x_derived
    connection: { protocol: tcp, host: 192.0.2.9 }
""")
    dev = next(d for d in cfg.devices if d.id == "unit9")
    autoselect_template_registers(cfg, _Registry(tpl), dev)

    saved = cfg.load_calculated("unit9")
    assert [c["name"] for c in saved] == ["status_text"]
    assert saved[0]["topic"] == "status/text"
    assert saved[0]["enum"]["4"] == "Tracking power point"
    # and it lives in the same file as the registers, not a second one
    on_disk = json.loads(cfg.device_registers_path("unit9").read_text())
    assert on_disk["calculated"] == saved and on_disk["registers"]


# ── the calc engine honours topic / enum ─────────────────────────────────────

class _Cfg:
    def __init__(self, calcs): self._c = calcs
    def load_calculated(self, device_id): return self._c


def _engine(calcs):
    return CalcEngine(_Cfg(calcs), lambda did: {}, lambda: (None, None))


def test_explicit_topic_and_measurement_reach_the_register():
    built = _engine([{"name": "status_text", "expr": "operating_state",
                      "topic": "status/text", "measurement": "status",
                      "influxdb": False}]).load("d1")
    reg = built[0]["_reg"]
    assert reg.mqtt_topic == "status/text"
    assert reg.influxdb_measurement == "status"
    assert reg.influxdb_enabled is False and reg.mqtt_enabled is True


def test_a_calc_without_a_topic_keeps_publishing_to_its_flat_name():
    """Routing identity must not shift under a user who only upgraded: every
    calc register that exists today has no `topic` and lands on its name."""
    reg = _engine([{"name": "power_active_total",
                    "expr": "a + b"}]).load("d1")[0]["_reg"]
    assert reg.mqtt_topic == ""          # NOT the canonical power/active/total


def test_a_computed_code_is_decoded_to_text():
    engine = _engine([{"name": "status_text", "expr": "operating_state",
                       "enum": {"4": "Tracking power point", "7": "Fault"}}])
    engine.load("d1")
    store = {1: {"name": "operating_state", "value": 4,
                 "timestamp": "2026-09-12T08:00:00", "ts": 1, "interval": 5}}
    engine.run("d1", "normal", store, topic_prefix="mbg/devices/d1",
               bucket=None, device_tag=None, device_id="d1",
               mqtt_on=False, influx_on=False)
    calc = next(v for v in store.values() if v.get("calculated"))
    assert calc["value"] == "Tracking power point"

    store[1]["value"] = 99               # unmapped → named, never a bare number
    engine.run("d1", "normal", store, topic_prefix="mbg/devices/d1",
               bucket=None, device_tag=None, device_id="d1",
               mqtt_on=False, influx_on=False)
    calc = next(v for v in store.values() if v.get("calculated"))
    assert calc["value"] == "unknown (99)"


def test_a_calc_without_an_enum_stays_numeric():
    engine = _engine([{"name": "status_alarm",
                       "expr": "1 if (operating_state == 7) else 0"}])
    engine.load("d1")
    store = {1: {"name": "operating_state", "value": 7,
                 "timestamp": "2026-09-12T08:00:00", "ts": 1, "interval": 5}}
    engine.run("d1", "normal", store, topic_prefix="p", bucket=None,
               device_tag=None, device_id="d1", mqtt_on=False, influx_on=False)
    assert next(v for v in store.values() if v.get("calculated"))["value"] == 1
