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


# ── availability (birth / last will) and per-source staleness (3.92.0) ─────
def _node(**cfg):
    regs = [_reg(1, "relay_1", topic="~/switch/relay_1/state"),
            _reg(2, "flow", topic="~/sensor/flow/state")]
    regs[0].enum = {"0": "OFF", "1": "ON"}
    cli = mi.MqttInputClient({"topic": "mbg/nodes/aa", **cfg}, regs)
    got = {}
    cli.publish_callback = lambda pg, data: got.update(data)
    cli.connected = True
    return cli, got


def _msg(topic, payload, retain=False):
    return SimpleNamespace(topic=topic, payload=payload, retain=retain)


AV = {"availability": {"topic": "~/status", "online": "online", "offline": "offline"}}


def test_availability_topic_is_subscribed_once():
    cli, _ = _node(**AV)
    assert "mbg/nodes/aa/status" in cli._subscriptions()
    assert cli._subscriptions().count("mbg/nodes/aa/status") == 1
    wide, _ = _node(topic="mbg/nodes/aa/#", **AV)
    wide.registers, wide._by_topic = [], {}
    assert wide._subscriptions() == ["mbg/nodes/aa/#"]


def test_online_source_is_healthy_however_old_its_last_value():
    cli, got = _node(**AV)
    cli._on_message(None, None, _msg("mbg/nodes/aa/status", b"online", retain=True))
    cli._on_message(None, None, _msg("mbg/nodes/aa/switch/relay_1/state", b"ON"))
    assert got[1]["value"] == "ON"                 # the enum, decoded
    cli.last_msg_mono -= 3600                      # an hour with no change
    h = cli.data_health()
    assert h["status"] == "ok" and h["stale"] is False and h["available"] is True


def test_offline_source_is_down_at_once():
    cli, _ = _node(**AV)
    cli._on_message(None, None, _msg("mbg/nodes/aa/switch/relay_1/state", b"ON"))
    cli._on_message(None, None, _msg("mbg/nodes/aa/status", b"offline", retain=True))
    h = cli.data_health()
    assert h["status"] == "down" and h["available"] is False


def test_retained_values_wait_for_the_source_to_say_online():
    cli, got = _node(**AV)
    cli._on_message(None, None, _msg("mbg/nodes/aa/sensor/flow/state", b"12.5", retain=True))
    assert got == {}                                # nobody knows yet: held
    cli._on_message(None, None, _msg("mbg/nodes/aa/status", b"online", retain=True))
    assert got[2]["value"] == 12.5                  # replayed once online


def test_retained_values_of_an_offline_source_are_dropped():
    cli, got = _node(**AV)
    cli._on_message(None, None, _msg("mbg/nodes/aa/sensor/flow/state", b"12.5", retain=True))
    cli._on_message(None, None, _msg("mbg/nodes/aa/status", b"offline", retain=True))
    cli._on_message(None, None, _msg("mbg/nodes/aa/sensor/flow/state", b"13", retain=True))
    assert got == {} and cli.retained_dropped == 1


def test_cleared_availability_falls_back_to_message_age():
    cli, _ = _node(stale_after_s=120, **AV)
    cli._on_message(None, None, _msg("mbg/nodes/aa/status", b"online"))
    cli._on_message(None, None, _msg("mbg/nodes/aa/status", b""))
    cli._on_message(None, None, _msg("mbg/nodes/aa/sensor/flow/state", b"1"))
    cli.last_msg_mono -= 60
    assert cli.data_health()["status"] == "ok"      # within its own 120 s
    cli.last_msg_mono -= 120
    assert cli.data_health()["status"] == "down"


def test_without_availability_nothing_changes():
    cli, got = _node()
    cli._on_message(None, None, _msg("mbg/nodes/aa/sensor/flow/state", b"1", retain=True))
    assert got == {} and cli.retained_dropped == 1  # retained still dropped
    cli._on_message(None, None, _msg("mbg/nodes/aa/sensor/flow/state", b"1"))
    cli.last_msg_mono -= 31
    assert cli.data_health()["status"] == "down"    # the old 30 s
    assert "available" not in cli.get_stats()


def test_template_availability_reaches_the_driver():
    from multibus.device_runtime import driver_for
    tpl = SimpleNamespace(protocol={"transports": ["mqtt"], **AV})
    reg = SimpleNamespace(get=lambda tid: tpl if tid == "esp_node" else None)
    src = SimpleNamespace(protocol="mqtt", template="esp_node", mqtt_in={"topic": "mbg/nodes/aa"})
    dev = SimpleNamespace(template="", id="n1")
    drv = driver_for(None, reg, dev, src, [_reg(1, "x", topic="~/x")], {}, False)
    assert drv.availability_topic == "~/status"
    src.mqtt_in = {"topic": "mbg/nodes/aa", "availability": {"topic": "~/alive"}}
    assert driver_for(None, reg, dev, src, [], {}, False).availability_topic == "~/alive"


def test_template_availability_is_checked():
    from multibus.device_template import _availability_errors
    assert _availability_errors({"availability": {"topic": "~/status"}}) == []
    assert _availability_errors({"availability": {"topic": "a/+/status"}})
    assert _availability_errors({"availability": {"topic": ""}})
    assert _availability_errors({"availability": {"topic": "~/s", "online": "x", "offline": "x"}})


@needs_tc
def test_an_edit_keeps_what_the_editor_does_not_show(tmp_path):
    cfg, client = make_app(tmp_path)
    conn = {"protocol": "mqtt", "broker": "@gateway", "port": 1883, "topic": "mbg/nodes/aa",
            "accept_retained": True, "stale_after_s": 300,
            "availability": {"topic": "~/status"}}
    payload = {"id": "node-1", "name": "Node", "enabled": False,
               "template": "mqtt_json_generic", "connection": conn}
    assert client.post("/api/devices", json=payload).status_code == 200
    # the device editor sends only what it shows
    edit = {**payload, "name": "Pump node",
            "connection": {"protocol": "mqtt", "broker": "@gateway", "port": 1883,
                           "topic": "mbg/nodes/aa", "username": "", "password": "", "tls": False}}
    assert client.put("/api/devices/node-1", json=edit).status_code == 200
    dev = Config(str(tmp_path / "config.yaml")).get_device("node-1")
    assert dev.mqtt_in["accept_retained"] is True
    assert dev.mqtt_in["stale_after_s"] == 300
    assert dev.mqtt_in["availability"] == {"topic": "~/status"}


def test_gateway_broker_takes_the_gateways_own_connection():
    from multibus.device_runtime import gateway_broker
    g = SimpleNamespace(mqtt=SimpleNamespace(broker="mosquitto", port=1884, username="gw",
                                             password="pw", tls_enabled=False))
    out = gateway_broker(g, {"broker": "@gateway", "topic": "mbg/nodes/aa"})
    assert (out["broker"], out["port"], out["username"], out["password"]) == ("mosquitto", 1884, "gw", "pw")
    assert out["topic"] == "mbg/nodes/aa"
    own = {"broker": "10.0.0.5", "username": "x"}
    assert gateway_broker(g, own) is own
