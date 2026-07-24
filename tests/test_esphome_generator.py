# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""ESPHome node generator — the template→firmware transpiler.

The generator is a CONTRACT: the firmware topics and the paired gateway
template rows must match byte-for-byte, values arrive in engineering units
(scale folded into a multiply filter), and every gateway data type maps to
the right modbus_controller value_type. These tests pin that contract."""
import yaml as pyyaml
import pytest

from multibus.config import PollGroup
from multibus.device_template import parse_template
from multibus.esphome_generator import generate_node, merge_secrets, safe_topic_name

POLL_GROUPS = {"realtime": PollGroup(interval=1),
               "normal": PollGroup(interval=5),
               "slow": PollGroup(interval=60)}
MQTT_DEFAULTS = {"broker": "mosquitto", "port": 1883,
                 "username": "mb", "password": "s3cret"}


def _tpl(registers, byte_order="big", tpl_id="test_meter"):
    return parse_template({"device_template": {
        "id": tpl_id, "name": "Test meter", "vendor": "T", "model": "M",
        "protocol": {"byte_order": byte_order, "default_register_type": "input"},
        "registers": registers}})


BASIC_REGS = [
    {"address": 0, "name": "V_L1", "label": "Voltage L1", "unit": "V",
     "data_type": "float", "register_type": "input", "poll_group": "realtime"},
    {"address": 72, "name": "Import_kWh", "label": "Import energy", "unit": "kWh",
     "data_type": "float", "register_type": "input", "poll_group": "slow"},
    {"address": 100, "name": "Status", "label": "Status word", "unit": "",
     "data_type": "uint16", "register_type": "holding", "poll_group": "normal"},
]

BASIC_PAYLOAD = {
    "node": {"name": "hall-meter", "friendly_name": "Hall meter"},
    "uart": {"tx_pin": "GPIO17", "rx_pin": "GPIO16", "baud_rate": 9600},
    "modbus": {"unit_id": 3},
    "mqtt": {},
    "registers": [],
}


def gen(payload=None, regs=BASIC_REGS, **tpl_kw):
    return generate_node(payload or dict(BASIC_PAYLOAD), _tpl(regs, **tpl_kw),
                         POLL_GROUPS, dict(MQTT_DEFAULTS))


# ---------------------------------------------------------------------------
# firmware YAML shape
# ---------------------------------------------------------------------------

def test_yaml_parses_with_secret_tag():
    """The generated document must be structurally valid YAML (ESPHome's
    !secret tag registered as a passthrough constructor)."""
    class L(pyyaml.SafeLoader):
        pass
    L.add_constructor("!secret", lambda loader, node: f"!secret {node.value}")
    doc = pyyaml.load(gen()["yaml"], Loader=L)
    assert doc["esphome"]["name"] == "hall-meter"
    assert doc["modbus_controller"][0]["address"] == 3
    assert len(doc["sensor"]) == 3
    assert doc["wifi"]["ssid"] == "!secret wifi_ssid"


def test_yaml_core_content():
    out = gen()
    y = out["yaml"]
    assert "name: hall-meter" in y
    assert "board: esp32dev" in y
    assert 'broker: "mosquitto"' in y            # inherited from the gateway
    assert "password: !secret mqtt_password" in y  # broker has a password
    assert 'topic_prefix: "esphome/hall-meter"' in y
    assert "address: 3" in y                     # unit id
    assert "update_interval: 1s" in y            # min(realtime=1)
    assert "register_type: read" in y            # input → read
    assert "value_type: FP32" in y
    assert "value_type: U_WORD" in y             # uint16 holding
    assert "register_type: holding" in y
    assert out["node_yaml_name"] == "hall-meter.yaml"


def test_topic_contract_matches_paired_template():
    out = gen()
    y = out["yaml"]
    rows = out["device_template"]["device_template"]["registers"]
    assert [r["topic"] for r in rows] == out["topics"]
    for t in out["topics"]:
        assert f'state_topic: "{t}"' in y
        assert t.startswith("esphome/hall-meter/")
    # paired rows consume whole-payload scalars in engineering units
    assert all(r["scale"] == 1.0 and "json_path" not in r for r in rows)
    dp = out["device_payload"]
    assert dp["connection"]["protocol"] == "mqtt"
    assert dp["connection"]["topic"] == "esphome/hall-meter/#"
    assert dp["connection"]["broker"] == "mosquitto"
    assert dp["template"] == "esphome_hall_meter"
    assert dp["id"] == "hall-meter"
    # staleness: 3× slowest group (60s) → 180
    assert dp["connection"]["stale_after_s"] == 180


def test_skip_updates_follow_poll_groups():
    out = gen()
    y = out["yaml"]
    # slow (60s) with base 1s → skip_updates 59; normal (5s) → 4
    assert "skip_updates: 59" in y
    assert "skip_updates: 4" in y


def test_scale_becomes_multiply_filter_and_little_endian_variants():
    regs = [{"address": 10, "name": "P", "label": "Power", "unit": "W",
             "data_type": "int32", "register_type": "holding",
             "poll_group": "normal", "scale": 10.0}]
    out = gen(regs=regs, byte_order="little")
    y = out["yaml"]
    assert "value_type: S_DWORD_R" in y
    assert "- multiply: 0.1" in y
    # engineering units on the wire → paired row scale is 1
    assert out["device_template"]["device_template"]["registers"][0]["scale"] == 1.0


def test_register_subset_and_unknown_names():
    p = dict(BASIC_PAYLOAD, registers=["V_L1"])
    out = gen(p)
    assert len(out["topics"]) == 1 and "V_L1" in out["topics"][0]
    with pytest.raises(ValueError, match="unknown register"):
        gen(dict(BASIC_PAYLOAD, registers=["Nope"]))


def test_unmappable_types_are_skipped_with_warning():
    regs = BASIC_REGS + [{"address": 200, "name": "Serial", "label": "S/N",
                          "unit": "", "data_type": "double",
                          "register_type": "holding", "poll_group": "slow"}]
    out = gen(regs=regs)
    assert any("Serial" in w for w in out["warnings"])
    assert all("Serial" not in t for t in out["topics"])


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("patch,msg", [
    ({"node": {"name": "Bad Name"}}, "mDNS-safe"),
    ({"node": {"name": "x" * 30}}, "mDNS-safe"),
    ({"node": {"name": "ok-node", "platform": "avr"}}, "platform"),
    ({"uart": {"tx_pin": "D4"}}, "tx_pin"),
    ({"uart": {"baud_rate": 1234}}, "standard rate"),
    ({"uart": {"parity": "MARK"}}, "parity"),
    ({"modbus": {"unit_id": 0}}, "1..247"),
    ({"mqtt": {"topic_prefix": "bad topic!"}}, "topic_prefix"),
])
def test_input_validation(patch, msg):
    p = {**BASIC_PAYLOAD, **patch}
    if "node" in patch and "name" not in patch["node"]:
        p["node"] = {**BASIC_PAYLOAD["node"], **patch["node"]}
    elif "node" not in patch:
        p["node"] = dict(BASIC_PAYLOAD["node"])
    with pytest.raises(ValueError, match=msg):
        gen(p)


def test_no_broker_anywhere_is_an_error():
    with pytest.raises(ValueError, match="broker"):
        generate_node(dict(BASIC_PAYLOAD), _tpl(BASIC_REGS), POLL_GROUPS,
                      {"broker": "", "port": 1883, "username": "", "password": ""})


def test_safe_topic_name():
    assert safe_topic_name("_G_ULN[0]") == "G_ULN_0"
    assert safe_topic_name("Import kWh (T1)") == "Import_kWh_T1"
    assert safe_topic_name("") == "reg"


# ---------------------------------------------------------------------------
# secrets merge
# ---------------------------------------------------------------------------

def test_merge_secrets_appends_missing_only():
    existing = 'wifi_ssid: "MyNet"\nwifi_password: "hunter2"\n'
    merged, added = merge_secrets(existing, {
        "wifi_ssid": None, "wifi_password": None,
        "ota_password": None, "mqtt_password": "s3cret"})
    assert added == ["mqtt_password", "ota_password"]
    assert 'wifi_ssid: "MyNet"' in merged            # untouched
    assert 'mqtt_password: "s3cret"' in merged       # real value
    assert 'ota_password: "CHANGE_ME"' in merged     # placeholder flagged
    assert "TODO" in merged
    m2, a2 = merge_secrets(merged, {"mqtt_password": "other"})
    assert a2 == [] and m2 == merged                 # idempotent


def test_merge_secrets_from_empty():
    merged, added = merge_secrets("", {"wifi_ssid": None})
    assert added == ["wifi_ssid"] and merged.startswith("wifi_ssid:")
