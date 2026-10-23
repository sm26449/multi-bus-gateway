# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""A template ships the rules a device of its kind is commonly run with, as
examples: the rule editor offers them for each device made from it, with
{device} replaced, always in shadow. A save checks them as the rule editor
would, and their command and fields must be the template's own."""
from multibus.device_template import parse_template, rule_preset_for, validate_template
from tests.test_devices_api import needs_tc

REGS = [{"address": 0, "name": "water_in", "data_type": "int16"},
        {"address": 1, "name": "onoff", "data_type": "uint16", "writable": True, "write_allowed": [0, 1]}]
CMDS = {"power": {"params": {"value": {"allowed": [0, 1]}},
                  "writes": [{"register": "onoff", "value": "${value}"}]}}
BUFFER = {"id": "buffer", "label": "Buffer", "kind": "condition",
          "target": {"device": "{device}", "command": "power"},
          "when": "{device}.water_in < 25", "release": "{device}.water_in > 35",
          "then": {"params": {"value": 1}}, "else": {"params": {"value": 0}}}


def _tpl(rules):
    return {"device_template": {"schema_version": 1, "id": "rp_t", "name": "RP",
                                "protocol": {"transports": ["tcp"]}, "poll_groups": {"normal": {"interval": 5}},
                                "registers": REGS, "commands": CMDS, "rules": rules}}


def test_a_preset_is_made_concrete_for_a_device_and_starts_in_shadow():
    assert validate_template(_tpl([BUFFER])) == []
    t = parse_template(_tpl([BUFFER]))
    r = rule_preset_for(t.rules[0], "hp-1")
    assert r["id"] == "hp-1-buffer" and r["mode"] == "shadow"
    assert r["target"] == {"device": "hp-1", "command": "power"}
    assert r["when"] == "hp-1.water_in < 25" and r["release"] == "hp-1.water_in > 35"
    assert parse_template(t.to_dict()).rules == t.rules
    assert t.summary()["rules"] == ["buffer"]


def test_a_preset_is_checked_where_it_is_saved():
    bad = [dict(BUFFER, id="Bad Id"),
           dict(BUFFER, id="other-dev", target={"device": "pv-u1", "command": "power"}),
           dict(BUFFER, id="no-cmd", target={"device": "{device}", "command": "limit"}),
           dict(BUFFER, id="no-field", when="{device}.water_out < 25"),
           dict(BUFFER, id="no-outcome", then=None, **{"else": None})]
    text = " | ".join(validate_template(_tpl(bad)))
    for frag in ("rules[0]: id invalid", "rules[1]: target.device must be", "rules[2]: target.command 'limit'",
                 "rules[3]: when reads 'water_out'", "rules[4]: then/else"):
        assert frag in text, frag


def test_the_shipped_heat_pump_presets_are_valid():
    import json
    d = json.load(open("multibus/device_templates/samsung_mim_b19n.json"))
    assert validate_template(d) == []
    assert {r["id"] for r in d["device_template"]["rules"]} >= {"buffer", "discharge-guard"}


@needs_tc
def test_the_editor_is_offered_the_presets_of_each_device(tmp_path):
    from tests.test_devices_api import make_app
    _cfg, client = make_app(tmp_path)
    assert client.post("/api/devices", json={"id": "hp", "enabled": False, "template": "samsung_mim_b19n",
                                             "connection": {"protocol": "tcp", "host": "192.168.1.9"}}).status_code in (200, 201)
    got = [p for p in client.get("/api/rule-presets").json()["presets"] if p["device"] == "hp"]
    assert {p["preset"] for p in got} >= {"buffer", "discharge-guard"}
    rule = next(p for p in got if p["preset"] == "buffer")["rule"]
    assert rule["id"] == "hp-buffer" and rule["target"]["device"] == "hp" and rule["mode"] == "shadow"
