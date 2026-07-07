"""HTTP/JSON output sink (Solar-API style): GET /api/meters[/{id}], per-device
toggle, staleness, persistence, and survival across a device edit."""
from datetime import datetime, timedelta

from multibus.config import Config

from tests.test_devices_api import make_app, needs_tc


def _seed(app, store_attr, device_id, address, name, value, unit='', ts=None):
    store = getattr(app.state, store_attr)
    if store_attr == 'device_values':
        store = store.setdefault(device_id, {})
    store[address] = {
        'value': value, 'name': name, 'label': name, 'unit': unit,
        'poll_group': 'normal',
        'timestamp': (ts or datetime.now()).isoformat(),
    }


@needs_tc
def test_meter_feed_off_by_default(tmp_path):
    _cfg, client = make_app(tmp_path)
    # http output is opt-in: nothing listed, feed 404s even for a real device id
    assert client.get("/api/meters").json()["meters"] == []
    assert client.get("/api/meters/umg512").status_code == 404
    # unknown id 404s the same way (don't leak which ids exist)
    assert client.get("/api/meters/nope").status_code == 404


@needs_tc
def test_toggle_and_feed_primary(tmp_path):
    _cfg, client = make_app(tmp_path)
    _seed(client.app, 'current_values', 'umg512', 19000, '_G_ULN1', 231.2, 'V')

    r = client.post("/api/devices/umg512/http-output", json={"enabled": True})
    assert r.status_code == 200 and r.json()["http_output_enabled"] is True
    assert r.json()["path"] == "/api/meters/umg512"

    meters = client.get("/api/meters").json()["meters"]
    assert [m["device"] for m in meters] == ["umg512"]
    assert meters[0]["count"] == 1

    feed = client.get("/api/meters/umg512").json()
    assert feed["device"] == "umg512"
    assert feed["values"]["_G_ULN1"]["value"] == 231.2
    assert feed["values"]["_G_ULN1"]["unit"] == "V"
    assert feed["stale"] is False

    # persisted to the flat http_output section and reloadable
    reloaded = Config(str(tmp_path / "config.yaml"))
    assert reloaded.http_output_primary_enabled is True
    assert reloaded.get_device("umg512").http_output_enabled is True

    # disable persists too
    client.post("/api/devices/umg512/http-output", json={"enabled": False})
    assert Config(str(tmp_path / "config.yaml")).get_device("umg512").http_output_enabled is False


@needs_tc
def test_stale_flag(tmp_path):
    _cfg, client = make_app(tmp_path)
    old = datetime.now() - timedelta(seconds=120)
    _seed(client.app, 'current_values', 'umg512', 19000, '_G_ULN1', 231.2, 'V', ts=old)
    client.post("/api/devices/umg512/http-output", json={"enabled": True})
    assert client.get("/api/meters/umg512").json()["stale"] is True


@needs_tc
def test_nonprimary_feed_persist_and_sink(tmp_path):
    _cfg, client = make_app(tmp_path)
    payload = {"id": "em24", "name": "EM24", "enabled": False,
               "connection": {"protocol": "tcp", "host": "192.0.2.10",
                              "port": 502, "unit_id": 5},
               "mqtt": {"topic_prefix": "meters/em24"},
               "influxdb": {"bucket": "em24"}}
    assert client.post("/api/devices", json=payload).status_code == 200
    _seed(client.app, 'device_values', 'em24', 100, 'power', 4200.0, 'W')
    client.post("/api/devices/em24/http-output", json={"enabled": True})

    feed = client.get("/api/meters/em24").json()
    assert feed["values"]["power"]["value"] == 4200.0

    # persisted inside the device's raw block (not the primary's flat section)
    reloaded = Config(str(tmp_path / "config.yaml"))
    assert reloaded.get_device("em24").http_output_enabled is True
    assert reloaded.http_output_primary_enabled is False

    # surfaced as a sink in /api/devices
    dev = next(d for d in client.get("/api/devices").json()["devices"]
               if d["id"] == "em24")
    assert dev["sinks"]["http"]["enabled"] is True
    assert dev["sinks"]["http"]["path"] == "/api/meters/em24"


@needs_tc
def test_http_output_survives_device_edit(tmp_path):
    _cfg, client = make_app(tmp_path)
    payload = {"id": "em24", "name": "EM24", "enabled": False,
               "connection": {"protocol": "tcp", "host": "192.0.2.10",
                              "port": 502, "unit_id": 5},
               "mqtt": {"topic_prefix": "meters/em24"},
               "influxdb": {"bucket": "em24"}}
    client.post("/api/devices", json=payload)
    client.post("/api/devices/em24/http-output", json={"enabled": True})

    # a normal edit that omits http_output must NOT wipe the opt-in
    payload["name"] = "EM24 renamed"
    assert client.put("/api/devices/em24", json=payload).status_code == 200
    assert Config(str(tmp_path / "config.yaml")).get_device("em24").http_output_enabled is True


# ── P2: MQTT NaN never published (all mode too) + bounded HTTP read ──────────

def test_mqtt_should_not_publish_nan_in_all_mode():
    from multibus.mqtt_publisher import MQTTPublisher
    from multibus.config import MQTTConfig
    pub = MQTTPublisher(MQTTConfig(enabled=False), [], publish_mode="all")
    assert pub._should_publish("t", float("nan")) is False
    assert pub._should_publish("t", float("inf")) is False
    assert pub._should_publish("t", 230.0) is True     # finite still publishes
