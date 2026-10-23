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
"""Devices found over MQTT (multibus/mqtt_discovery.py) and topic variables."""
import json
from types import SimpleNamespace

from multibus import mqtt_discovery as md
from multibus import mqtt_input as mi

from tests.test_devices_api import make_app, needs_tc

TASMOTA = {"ip": "192.168.1.50", "dn": "Boiler plug", "hn": "tasmota-A1B2C3", "mac": "A4CF12A1B2C3", "md": "Sonoff Pow R2",
           "t": "tasmota_A1B2C3", "ft": "%prefix%/%topic%/", "tp": ["cmnd", "stat", "tele"], "rl": [1, 0, 0, 0],
           "onln": "Online", "ofln": "Offline", "sw": "14.2.0"}


def test_a_configured_esp_node_is_found_and_the_basic_image_is_not():
    it = md._mbg_node("mbg/nodes/c049ef256068/announce", {"name": "boiler-room", "firmware": "configured", "ip": "10.0.0.5"})
    assert it["template"] == "esphome_node_c049ef256068" and it["device_id"] == "boiler-room"
    assert it["connection"] == {"protocol": "mqtt", "broker": "@gateway", "port": 1883, "topic": "mbg/nodes/c049ef256068"}
    assert md._mbg_node("mbg/nodes/c049ef256068/announce", {"firmware": "bootstrap"}) is None


def test_a_tasmota_gives_its_topics_as_variables():
    it = md._tasmota("tasmota/discovery/A4CF12A1B2C3/config", TASMOTA)
    c = it["connection"]
    assert c["vars"] == {"stat": "stat/tasmota_A1B2C3", "tele": "tele/tasmota_A1B2C3"} and c["topic"] == "tele/tasmota_A1B2C3"
    assert c["availability"] == {"topic": "{tele}/LWT", "online": "Online", "offline": "Offline"}
    assert it["template"] == "tasmota_generic" and it["info"]["relays"] == 1 and it["name"] == "Boiler plug"
    # a full topic of its own, with %id%
    other = md._tasmota("tasmota/discovery/A4CF12A1B2C3/config", {**TASMOTA, "ft": "home/%topic%/%prefix%/", "t": "plug_%id%"})
    assert other["connection"]["vars"]["stat"] == "home/plug_%id%/stat".replace("%id%", "A1B2C3")
    # a variable can never be a wildcard
    assert md._tasmota("tasmota/discovery/x/config", {**TASMOTA, "t": "a/#"}) is None


def _found(tmp_path):
    return md.MqttDiscovery(str(tmp_path / "mqtt_discovery.json"))


def test_the_list_says_what_each_found_device_is(tmp_path):
    f = _found(tmp_path)
    f.handle("tasmota/discovery/A4CF12A1B2C3/config", json.dumps(TASMOTA).encode(), now=1)
    f.handle("mbg/nodes/c049ef256068/announce", json.dumps({"name": "boiler", "firmware": "configured"}).encode(), now=1)
    f.handle("mbg/nodes/aabbccddeeff/announce", json.dumps({"name": "x", "firmware": "configured"}).encode(), now=1)
    f.handle("mbg/nodes/aabbccddeeff/announce", b"", now=2)                    # cleared: forgotten
    dev = SimpleNamespace(id="boiler", mqtt_in={"topic": "mbg/nodes/c049ef256068"})
    states = {x["key"]: x["state"] for x in f.listing([dev], lambda t: t == "tasmota_generic")}
    assert states == {"tasmota:a4cf12a1b2c3": "new", "mbg_node:c049ef256068": "in_mbg"}
    f.ignore("tasmota:a4cf12a1b2c3")
    assert {x["key"]: x["state"] for x in f.listing([], lambda t: False)} == {
        "tasmota:a4cf12a1b2c3": "ignored", "mbg_node:c049ef256068": "template_missing"}
    assert _found(tmp_path).ignored == ["tasmota:a4cf12a1b2c3"]            # kept


def test_a_declared_rule(tmp_path):
    p = tmp_path / "mqtt_discovery.json"
    p.write_text(json.dumps({"rules": [
        {"id": "zb", "label": "Zigbee sensor", "topic": "zigbee2mqtt/+/info", "id_segment": 1, "name": "friendly",
         "match": {"type": "sensor"}, "template": "zigbee2mqtt_sensor", "base_topic": "zigbee2mqtt/{id}"},
        {"id": "tasmota", "topic": "x"}]}))
    f = md.MqttDiscovery(str(p))
    assert "tasmota" in f.rule_problems
    f.handle("zigbee2mqtt/kitchen/info", json.dumps({"type": "sensor", "friendly": "Kitchen"}).encode(), now=1)
    f.handle("zigbee2mqtt/door/info", json.dumps({"type": "switch"}).encode(), now=1)
    items = f.listing([], lambda t: True)
    assert [(x["name"], x["connection"]["topic"]) for x in items] == [("Kitchen", "zigbee2mqtt/kitchen")]


def test_topic_variables_fill_a_templates_topics():
    regs = [SimpleNamespace(address=1, name="power", topic="{stat}/POWER", json_path="", data_type="uint16",
                            enum={"0": "OFF", "1": "ON"}, scale_from="")]
    cli = mi.MqttInputClient({"topic": "tele/plug", "vars": {"stat": "stat/plug", "tele": "tele/plug"},
                              "availability": {"topic": "{tele}/LWT", "online": "Online", "offline": "Offline"}}, regs)
    assert "stat/plug/POWER" in cli._subscriptions() and "tele/plug/LWT" in cli._subscriptions()
    cli.connected = True
    cli._on_message(None, None, SimpleNamespace(topic="tele/plug/LWT", payload=b"Online", retain=True))
    assert cli.available is True


@needs_tc
def test_adopt_makes_the_device_off_with_its_variables(tmp_path):
    cfg, client = make_app(tmp_path)
    disc = client.app.state.mqtt_discovery
    disc.handle("tasmota/discovery/A4CF12A1B2C3/config", json.dumps(TASMOTA).encode())
    lst = client.get("/api/mqtt-discovery").json()
    assert [x["state"] for x in lst["items"]] == ["new"]
    r = client.post("/api/mqtt-discovery/tasmota:a4cf12a1b2c3/adopt", json={})
    assert r.status_code == 200, r.text
    did = r.json()["device_id"]
    assert did == "boiler-plug"
    from multibus.config import Config
    dev = Config(str(tmp_path / "config.yaml")).get_device(did)
    assert dev.enabled is False and dev.template == "tasmota_generic"
    assert dev.mqtt_in["vars"] == {"stat": "stat/tasmota_A1B2C3", "tele": "tele/tasmota_A1B2C3"}
    assert client.get("/api/mqtt-discovery").json()["items"][0]["state"] == "in_mbg"
    assert client.post("/api/mqtt-discovery/nope:1/adopt", json={}).status_code == 404
    # a template that is not here: refused, with what to do
    disc.handle("mbg/nodes/c049ef256068/announce", json.dumps({"name": "boiler", "firmware": "configured"}).encode())
    r = client.post("/api/mqtt-discovery/mbg_node:c049ef256068/adopt", json={})
    assert r.status_code == 409 and "ESP nodes service" in r.json()["detail"]
    assert client.post("/api/mqtt-discovery/mbg_node:c049ef256068/ignore", json={}).json()["ignored"] is True


@needs_tc
def test_a_device_with_bad_variables_is_refused(tmp_path):
    _cfg, client = make_app(tmp_path)
    r = client.post("/api/devices", json={"id": "p1", "name": "P", "enabled": False, "template": "tasmota_generic",
                                          "connection": {"protocol": "mqtt", "broker": "@gateway", "port": 1883,
                                                         "topic": "tele/p", "vars": {"stat": "stat/#"}}})
    assert r.status_code == 422 and "connection.vars.stat" in r.text


@needs_tc
def test_only_an_admin_adopts(tmp_path):
    from tests.test_operator_role import _build_clients
    c = _build_clients(tmp_path)
    assert c.op.post("/api/mqtt-discovery/x:1/adopt", json={}).status_code == 403
    assert c.viewer.get("/api/mqtt-discovery").status_code == 200
