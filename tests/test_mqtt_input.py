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
"""MQTT input driver: topic matching, message extraction, device create + persist."""
from types import SimpleNamespace

from multibus import mqtt_input as mi
from multibus.config import Config, SelectedRegister

from tests.test_devices_api import make_app, needs_tc


def test_topic_matches():
    assert mi.topic_matches("a/b/c", "a/b/c")
    assert mi.topic_matches("a/+/c", "a/x/c")
    assert mi.topic_matches("a/#", "a/b/c/d")
    assert mi.topic_matches("sensors/#", "sensors/x")
    assert mi.topic_matches("#", "anything/here")
    assert not mi.topic_matches("a/+/c", "a/x/y")
    assert not mi.topic_matches("a/b", "a/b/c")
    # '#' is only a wildcard as the FINAL segment — a mid-pattern '#' is invalid
    # and must match nothing (not overmatch).
    assert not mi.topic_matches("a/#/b", "a/x/b")
    assert not mi.topic_matches("site/#/meter", "site/1/meter")


def _reg(addr, name, json_path="", topic="", unit=""):
    return SelectedRegister(address=addr, name=name, label=name, unit=unit,
                            data_type="float", poll_group="normal",
                            json_path=json_path, topic=topic)


def test_on_message_json_extraction():
    regs = [_reg(1, "power", "power", "sensors/x", "W"),
            _reg(2, "voltage", "voltage", "sensors/x", "V")]
    cli = mi.MqttInputClient({"topic": "sensors/x"}, regs)
    captured = {}
    cli.publish_callback = lambda pg, data: captured.update({"pg": pg, "data": data})
    msg = SimpleNamespace(topic="sensors/x", payload=b'{"power": 123.5, "voltage": 231.2}')
    cli._on_message(None, None, msg)
    assert captured["pg"] == "mqtt"
    assert captured["data"][1]["value"] == 123.5
    assert captured["data"][2]["value"] == 231.2
    assert cli.updates == 2 and cli.messages == 1


def test_retained_dropped_by_default_and_accepted_on_opt_in():
    """P0: a retained delivery must not be laundered as a fresh value (arrival
    time) — dropped by default, processed only with accept_retained."""
    regs = [_reg(1, "power", "power", "sensors/x", "W")]
    msg = SimpleNamespace(topic="sensors/x", payload=b'{"power": 42.0}', retain=True)

    cli = mi.MqttInputClient({"topic": "sensors/x"}, regs)
    got = {}
    cli.publish_callback = lambda pg, data: got.update(data)
    cli._on_message(None, None, msg)
    assert got == {} and cli.retained_dropped == 1 and cli.messages == 0   # dropped

    cli2 = mi.MqttInputClient({"topic": "sensors/x", "accept_retained": True}, regs)
    got2 = {}
    cli2.publish_callback = lambda pg, data: got2.update(data)
    cli2._on_message(None, None, msg)
    assert got2[1]["value"] == 42.0 and cli2.messages == 1                  # accepted

    # a LIVE (non-retained) message is always processed
    cli.retained_dropped = 0
    cli._on_message(None, None, SimpleNamespace(topic="sensors/x",
                                                payload=b'{"power": 43.0}', retain=False))
    assert got[1]["value"] == 43.0 and cli.retained_dropped == 0


def test_on_message_bare_number_and_per_register_topic():
    # register with its own topic and NO json_path → whole payload is the number
    regs = [_reg(1, "temp", "", "home/temp", "°C")]
    cli = mi.MqttInputClient({"topic": "unused"}, regs)
    captured = {}
    cli.publish_callback = lambda pg, data: captured.update(data)
    cli._on_message(None, None, SimpleNamespace(topic="home/temp", payload=b"21.7"))
    assert captured[1]["value"] == 21.7
    # a message on an unrelated topic is ignored
    captured.clear()
    cli._on_message(None, None, SimpleNamespace(topic="home/other", payload=b"9"))
    assert captured == {}


@needs_tc
def test_mqtt_device_create_and_persist(tmp_path):
    _cfg, client = make_app(tmp_path)
    payload = {"id": "shelly-1", "name": "Shelly", "enabled": False,
               "template": "mqtt_json_generic",
               "connection": {"protocol": "mqtt", "broker": "192.168.1.100",
                              "port": 1883, "topic": "sensors/example/state",
                              "username": "u", "password": "secret"}}
    r = client.post("/api/devices", json=payload)
    assert r.status_code == 200, r.text
    reloaded = Config(str(tmp_path / "config.yaml"))
    dev = reloaded.get_device("shelly-1")
    assert dev and dev.protocol == "mqtt"
    assert dev.mqtt_in["broker"] == "192.168.1.100"
    assert dev.mqtt_in["topic"] == "sensors/example/state"
    assert dev.mqtt_in["password"] == "secret"          # persisted
    # the API echoes the password masked, never the secret
    dev_entry = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "shelly-1")
    assert dev_entry["connection"]["password"] == "******"


@needs_tc
def test_mqtt_requires_broker_and_topic(tmp_path):
    _cfg, client = make_app(tmp_path)
    r = client.post("/api/devices", json={"id": "x1", "enabled": False,
                    "template": "mqtt_json_generic",
                    "connection": {"protocol": "mqtt", "broker": "", "topic": ""}})
    assert r.status_code == 422
    errs = " ".join(r.json()["detail"]["errors"])
    assert "broker" in errs and "topic" in errs


@needs_tc
def test_mqtt_rejects_modbus_template(tmp_path):
    _cfg, client = make_app(tmp_path)
    r = client.post("/api/devices", json={"id": "x2", "enabled": False,
                    "template": "eastron_sdm120",
                    "connection": {"protocol": "mqtt", "broker": "b", "topic": "t"}})
    assert r.status_code == 422 and "template" in " ".join(r.json()["detail"]["errors"])


# ── P1: mqtt-in connect uses async + validates CONNACK ───────────────────────

def test_on_connect_rejects_nonzero_connack():
    from types import SimpleNamespace
    cli = mi.MqttInputClient({"topic": "x"}, [_reg(1, "a", "a", "x", "W")])
    # a failure reason code must NOT mark the source connected
    cli._on_connect(None, None, {}, SimpleNamespace(is_failure=True), None)
    assert cli.connected is False
    cli._on_connect(None, None, {}, 5, None)          # int nonzero CONNACK
    assert cli.connected is False
    # success (0 / non-failure) connects
    cli._client = SimpleNamespace(subscribe=lambda t: None)
    cli._on_connect(cli._client, None, {}, SimpleNamespace(is_failure=False), None)
    assert cli.connected is True


# ── P1: scale applied on mqtt-in values ──────────────────────────────────────

def test_mqtt_in_applies_scale():
    r = _reg(1, "power", "power", "sensors/x", "W")
    r.scale = 10.0                                    # raw/10 = engineering
    cli = mi.MqttInputClient({"topic": "sensors/x"}, [r])
    got = {}
    cli.publish_callback = lambda pg, data: got.update(data)
    cli._on_message(None, None, SimpleNamespace(topic="sensors/x",
                                                payload=b'{"power": 2300}', retain=False))
    assert got[1]["value"] == 230.0


def test_hostile_payloads_do_not_cost_the_message_its_other_registers():
    """F-63 (3.83.0): an oversized payload is dropped before parsing, a
    100k-deep document does not raise past the parser, and a 400-digit
    integer in one field no longer aborts the whole message."""
    regs = [_reg(1, "power", "power", "sensors/x", "W"),
            _reg(2, "voltage", "voltage", "sensors/x", "V")]
    cli = mi.MqttInputClient({"topic": "sensors/x"}, regs)
    captured = {}
    cli.publish_callback = lambda pg, data: captured.update({"pg": pg, "data": data})
    big = b'{"power": ' + b'9' * 400 + b', "voltage": 231.2}'
    cli._on_message(None, None, SimpleNamespace(topic="sensors/x", payload=big))
    assert captured["data"][2]["value"] == 231.2 and 1 not in captured["data"]
    captured.clear()
    deep = b'{"power": ' + b'[' * 30000 + b']' * 30000 + b'}'      # under the size cap, over the recursion limit
    cli._on_message(None, None, SimpleNamespace(topic="sensors/x", payload=deep))
    assert cli.messages == 1 + 1                      # counted, nothing published, no raise
    oversized = b'{"voltage": 1}' + b' ' * mi.MAX_PAYLOAD_BYTES
    cli._on_message(None, None, SimpleNamespace(topic="sensors/x", payload=oversized))
    assert cli.oversized_dropped == 1 and not captured
