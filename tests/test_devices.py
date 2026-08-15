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
"""Tier 2 Phase A: device templates, device config synthesis, and the
byte-identical migration guarantees for device #1 (UMG512).

The contract under test (docs/design/tier2-device-profiles.md §4): after the
multi-device refactor, device #1's MQTT topics and InfluxDB line protocol are
IDENTICAL to the legacy single-device behavior — data collection must not
change by one byte.
"""
import json
import time

import pytest

from multibus.config import Config, MQTTConfig, PRIMARY_DEVICE_ID
from multibus.device_template import (
    BUILTIN_DIR, TemplateRegistry, load_template, parse_template, validate_template,
)
from multibus.mqtt_publisher import MQTTPublisher
from tests.test_reliability import make_publisher, make_register


# ── device templates ─────────────────────────────────────────────────────────

def minimal_template(tid="test_meter", **over):
    t = {
        "device_template": {
            "schema_version": 1,
            "id": tid,
            "name": "Test Meter",
            "poll_groups": {"normal": {"interval": 5}},
            "categories": {"basic": {"label": "Basic", "order": 1}},
            "registers": [
                {"address": 100, "name": "_V1", "unit": "V",
                 "data_type": "float", "category": "basic", "poll_group": "normal"},
            ],
        }
    }
    t["device_template"].update(over)
    return t


def test_builtin_umg512_template_loads():
    t = load_template(str(BUILTIN_DIR / "janitza_umg512_pro.json"), builtin=True)
    assert t.id == "janitza_umg512_pro"
    assert len(t.registers) > 4000
    assert t.protocol["max_registers_per_read"] == 125
    # the curated defaults from selected_registers made it in
    curated = [r for r in t.registers if r.defaults]
    assert len(curated) >= 50
    # data types normalized to the parser vocabulary (int/uint would have
    # silently decoded as float at runtime)
    assert not [r for r in t.registers if r.data_type in ("int", "uint")]


def test_template_validation_reports_row_level_errors():
    bad = minimal_template()
    bad["device_template"]["registers"].append(
        {"address": 70000, "name": "_BAD", "data_type": "floatx", "category": "nope"})
    errors = validate_template(bad)
    joined = "\n".join(errors)
    assert "address must be an integer 0..65535" in joined
    assert "data_type 'floatx' not supported" in joined
    assert "category 'nope' not declared" in joined
    with pytest.raises(ValueError):
        parse_template(bad)


def test_registry_user_templates_and_builtin_protection(tmp_path):
    reg = TemplateRegistry(builtin_dir=BUILTIN_DIR, user_dir=tmp_path)
    n_builtin = len(reg.list())
    saved = reg.save_user(minimal_template())
    assert (tmp_path / "test_meter.json").exists()
    assert len(reg.list()) == n_builtin + 1
    assert not reg.get("test_meter").builtin
    # built-in ids are shielded
    with pytest.raises(ValueError):
        reg.save_user(minimal_template(tid="janitza_umg512_pro"))
    with pytest.raises(ValueError):
        reg.delete_user("janitza_umg512_pro")
    reg.delete_user("test_meter")
    assert reg.get("test_meter") is None
    # exported form round-trips
    again = parse_template(saved.to_dict())
    assert again.id == "test_meter" and len(again.registers) == 1


# ── device config synthesis ──────────────────────────────────────────────────

def write_config(tmp_path, extra_yaml=""):
    (tmp_path / "config.yaml").write_text(f"""
modbus:
  host: 192.168.1.207
  port: 502
  unit_id: 1
mqtt:
  enabled: true
  broker: mosquitto
  topic_prefix: janitza/umg512
influxdb:
  enabled: true
  url: http://influxdb:8086
  bucket: janitza
{extra_yaml}""", encoding="utf-8")
    (tmp_path / "selected_registers.json").write_text(json.dumps({
        "version": "1.0",
        "registers": [{"address": 19000, "name": "_G_ULN[0]", "label": "L1",
                       "unit": "V", "data_type": "float", "poll_group": "realtime",
                       "mqtt": {"enabled": True, "topic": "voltage/l1_n"}}],
        "poll_groups": {"realtime": {"interval": 1}},
    }), encoding="utf-8")
    return Config(str(tmp_path / "config.yaml"))


def test_primary_device_synthesized_from_legacy_config(tmp_path):
    cfg = write_config(tmp_path)
    assert len(cfg.devices) == 1
    d = cfg.primary_device
    assert d.id == PRIMARY_DEVICE_ID and d.primary
    assert d.template == "janitza_umg512_pro"
    assert d.connection is cfg.modbus            # same object → UI edits stay live
    assert d.mqtt_topic_prefix == "janitza/umg512"
    assert d.influxdb_bucket == "janitza"
    assert d.influxdb_device_tag == "janitza_umg512"


def test_extra_devices_parsed_with_defaults_and_substitution(tmp_path):
    cfg = write_config(tmp_path, extra_yaml="""
devices:
  - id: em24-hala
    name: Warehouse EM24
    template: cg_em24
    connection: { protocol: tcp, host: 192.168.1.42, port: 1502, unit_id: 5 }
    influxdb: { bucket: warehouse }
  - id: rtu-meter
    connection: { protocol: rtu, serial_port: /dev/ttyUSB0, baudrate: 9600 }
  - id: umg512          # duplicate of primary -> skipped
    connection: { host: 1.2.3.4 }
""")
    ids = [d.id for d in cfg.devices]
    assert ids == [PRIMARY_DEVICE_ID, "em24-hala", "rtu-meter"]
    em24 = cfg.devices[1]
    assert em24.mqtt_topic_prefix == "meters/em24-hala"      # ${device_id} default
    assert em24.influxdb_bucket == "warehouse"
    assert em24.influxdb_device_tag == "em24-hala"
    assert em24.connection.port == 1502 and em24.connection.unit_id == 5
    rtu = cfg.devices[2]
    assert rtu.protocol == "rtu" and rtu.serial["serial_port"] == "/dev/ttyUSB0"
    # per-device registers path: primary keeps the legacy file
    assert cfg.device_registers_path(PRIMARY_DEVICE_ID) == cfg.registers_path
    assert "devices/em24-hala" in str(cfg.device_registers_path("em24-hala"))
    regs, groups = cfg.load_device_registers(em24)           # no file yet
    assert regs == [] and "realtime" in groups


# ── byte-identical migration guarantees (device #1) ──────────────────────────

def test_primary_mqtt_topics_byte_identical():
    pub = MQTTPublisher(MQTTConfig(enabled=False, topic_prefix="janitza/umg512"),
                        [], publish_mode="changed")
    reg = make_register()                        # _G_ULN[0], no explicit topic
    legacy = f"janitza/umg512/{'_G_ULN[0]'.lower().replace('[','_').replace(']','').replace('_g_','')}"
    # primary passes topic_prefix=None → falls back to config = legacy behavior
    assert pub._build_topic(reg, None) == legacy == "janitza/umg512/uln_0"
    reg.mqtt_topic = "voltage/l1_n"
    assert pub._build_topic(reg, None) == "janitza/umg512/voltage/l1_n"
    # a second device routes elsewhere without touching device #1's topics
    assert pub._build_topic(reg, "meters/em24") == "meters/em24/voltage/l1_n"


def test_primary_influx_line_protocol_byte_identical():
    pub = make_publisher()
    ts = 1_700_000_000.0
    reg = make_register()
    # primary path: device_tag=None → historical 'janitza_umg512' tag
    line = pub._build_point(reg, 231.7, ts, poll_group="realtime",
                            device_tag=None).to_line_protocol()
    expected = ('voltage,address=19000,device=janitza_umg512,name=_G_ULN[0],'
                'poll_group=realtime uln_0=231.7,value=231.7 '
                f'{int(ts * 1e9)}')
    assert line == expected
    # second device: only the device tag differs, structure identical
    line2 = pub._build_point(reg, 231.7, ts, poll_group="realtime",
                             device_tag="em24-hala").to_line_protocol()
    assert 'device=em24-hala' in line2


def test_multi_device_cache_and_bucket_isolation():
    pub = make_publisher()
    reg = make_register()
    ts = time.time()
    # same address+value on two devices → both write (no cross-suppression)
    pub.write_register_data("realtime", {19000: {"value": 1.0, "register": reg, "ts": ts}},
                            device_id="")                      # primary
    pub.write_register_data("realtime", {19000: {"value": 1.0, "register": reg, "ts": ts}},
                            bucket="warehouse", device_tag="em24", device_id="em24")
    assert len(pub._buffer) == 2
    buckets = [b for _, b, _ in pub._buffer]
    assert buckets == ["b", "warehouse"]                       # per-device routing
    # and the change-detection caches stay per-device
    assert ("", 19000) in pub.last_values and ("em24", 19000) in pub.last_values


def test_drain_writes_each_bucket_separately():
    from unittest.mock import MagicMock
    pub = make_publisher()
    now = time.time()
    pub._buffer_line("a 1", now, bucket="b")
    pub._buffer_line("w 1", now, bucket="warehouse")
    pub._buffer_line("b 2", now, bucket="b")
    wapi = MagicMock()
    client = MagicMock()
    client.write_api.return_value = wapi
    pub.client = client
    pub.connected = True
    pub._drain_buffer()
    calls = [(c.kwargs["bucket"], c.kwargs["record"]) for c in wapi.write.call_args_list]
    # audit DP-26: entries are GROUPED per bucket (one write per bucket, order
    # preserved within it) instead of run-length chunks that degenerated to
    # tiny writes under interleaved multi-device traffic
    assert sorted(calls) == sorted([("b", "a 1\nb 2"), ("warehouse", "w 1")])
    assert pub.points_replayed == 3


# ── Modbus RTU transport ─────────────────────────────────────────────────────

def test_rtu_connection_builds_serial_client(monkeypatch):
    from multibus import modbus_client as mc
    from multibus.config import ModbusConfig
    captured = {}

    class FakeSerial:
        def __init__(self, **kw): captured.update(kw)
        def connect(self): return True
        def close(self): pass
    monkeypatch.setattr(mc, "ModbusSerialClient", FakeSerial)

    cfg = ModbusConfig(protocol="rtu", serial_port="/dev/ttyUSB0", baudrate=19200,
                       parity="E", stopbits=2, bytesize=8, unit_id=5, timeout=2)
    conn = mc.ModbusConnection(cfg)
    assert conn.connect() is True
    assert captured["port"] == "/dev/ttyUSB0"
    assert captured["baudrate"] == 19200
    assert captured["parity"] == "E"
    assert captured["stopbits"] == 2
    assert mc._endpoint(cfg) == "/dev/ttyUSB0@19200 unit 5"


def test_tcp_connection_still_builds_tcp_client(monkeypatch):
    from multibus import modbus_client as mc
    from multibus.config import ModbusConfig
    captured = {}

    class FakeTcp:
        def __init__(self, **kw): captured.update(kw)
        def connect(self): return True
        def close(self): pass
    monkeypatch.setattr(mc, "ModbusTcpClient", FakeTcp)

    cfg = ModbusConfig(protocol="tcp", host="10.0.0.5", port=1502, timeout=3)
    conn = mc.ModbusConnection(cfg)
    assert conn.connect() is True
    assert captured["host"] == "10.0.0.5" and captured["port"] == 1502
    assert mc._endpoint(cfg) == "10.0.0.5:1502"


def test_rtu_device_config_carries_serial(tmp_path):
    cfg = Config.__new__(Config)  # avoid full init; use write_config path instead
    # simpler: go through the normal loader
    from tests.test_devices import write_config
    c = write_config(tmp_path, extra_yaml="""
devices:
  - id: rtu1
    template: janitza_umg512_pro
    connection: { protocol: rtu, serial_port: /dev/ttyUSB0, baudrate: 38400, parity: E, stopbits: 1 }
""")
    d = c.get_device("rtu1")
    assert d.protocol == "rtu"
    assert d.connection.protocol == "rtu"
    assert d.connection.serial_port == "/dev/ttyUSB0"
    assert d.connection.baudrate == 38400
    assert d.connection.parity == "E"


def test_modbus_stale_and_max_gap_survive_save(tmp_path):
    # regression: save_yaml_config() dropped modbus.stale_after_s (and max_gap),
    # so any UI-triggered save silently reverted the health threshold to 30.
    cfg = write_config(tmp_path)
    cfg.modbus.stale_after_s = 99
    cfg.modbus.max_gap = 0
    cfg.save_yaml_config()
    reloaded = Config(str(tmp_path / "config.yaml"))
    assert reloaded.modbus.stale_after_s == 99      # not silently reverted to 30
    assert reloaded.modbus.max_gap == 0


def test_saved_config_is_chmod_0600(tmp_path):
    # config.yaml holds secrets (password hashes, MQTT/Influx tokens) → owner-only.
    import os
    import stat
    cfg = write_config(tmp_path)
    cfg.save_yaml_config()
    mode = stat.S_IMODE(os.stat(tmp_path / "config.yaml").st_mode)
    assert mode == 0o600, oct(mode)


def test_ui_timezone_default_and_survives_save(tmp_path):
    # #14: monthly-energy month boundaries must be configurable, not hardcoded to
    # Europe/Bucharest. Default preserves historical behaviour; a set value survives.
    cfg = write_config(tmp_path)
    assert cfg.ui.timezone == "Europe/Bucharest"     # default keeps current behaviour
    cfg.ui.timezone = "America/New_York"
    cfg.save_yaml_config()
    reloaded = Config(str(tmp_path / "config.yaml"))
    assert reloaded.ui.timezone == "America/New_York"


def test_malformed_device_is_skipped_not_boot_crash(tmp_path):
    # A single malformed devices[] entry (port not an int) must be skipped, not
    # crash _build_devices — otherwise the primary's polling dies too (#9).
    cfg = write_config(tmp_path, extra_yaml="""
devices:
  - id: bad
    connection: {protocol: tcp, host: h, port: "abc"}
  - id: good
    connection: {protocol: tcp, host: h, port: 1502}
""")
    ids = [d.id for d in cfg.devices]
    assert "umg512" in ids and "good" in ids   # primary + valid device survive
    assert "bad" not in ids                     # malformed one skipped


def test_env_sourced_secret_not_persisted_to_yaml(tmp_path, monkeypatch):
    import yaml as _yaml
    p = tmp_path / "config.yaml"
    p.write_text(_yaml.dump({
        "modbus": {"host": "h", "port": 502},
        "mqtt": {"enabled": True, "broker": "b", "password": "STORED_PW"},
        "influxdb": {"enabled": False, "token": "STORED_TOKEN"},
    }))
    monkeypatch.setenv("MQTT_PASSWORD", "ENV_SECRET")
    monkeypatch.setenv("INFLUXDB_TOKEN", "ENV_TOKEN")
    from multibus.config import Config
    cfg = Config(str(p))
    # live object uses the env values (runtime source of truth)
    assert cfg.mqtt.password == "ENV_SECRET" and cfg.influxdb.token == "ENV_TOKEN"
    cfg.save_yaml_config()
    on_disk = _yaml.safe_load(p.read_text())
    # but the persisted file keeps the STORED values — the env secret never lands
    assert on_disk["mqtt"]["password"] == "STORED_PW"
    assert on_disk["influxdb"]["token"] == "STORED_TOKEN"


def test_general_config_timezone_roundtrip(tmp_path):
    # /api/config/general: GET exposes the tz + IANA list; POST validates and
    # persists; a bogus zone is refused (a typo would shift report boundaries).
    from tests.test_devices_api import make_app, _HAS_TC
    import pytest as _pytest
    if not _HAS_TC:
        _pytest.skip("TestClient not installed")
    cfg, client = make_app(tmp_path)
    g = client.get("/api/config/general").json()
    assert g["timezone"] == "Europe/Bucharest"
    assert "Europe/Bucharest" in g["timezones"]          # picker list served
    assert client.post("/api/config/general",
                       json={"timezone": "Not/AZone"}).status_code == 422
    r = client.post("/api/config/general", json={"timezone": "America/New_York"})
    assert r.status_code == 200 and r.json()["applies"] == "live"
    assert Config(str(tmp_path / "config.yaml")).ui.timezone == "America/New_York"


def test_general_config_default_colors_roundtrip(tmp_path):
    from tests.test_devices_api import make_app, _HAS_TC
    import pytest as _pytest
    if not _HAS_TC:
        _pytest.skip("TestClient not installed")
    cfg, client = make_app(tmp_path)
    # invalid convention / bad hex refused
    assert client.post("/api/config/general", json={
        "timezone": "Europe/Bucharest",
        "default_colors": {"phase_convention": "neon"}}).status_code == 422
    assert client.post("/api/config/general", json={
        "timezone": "Europe/Bucharest",
        "default_colors": {"phase_convention": "custom",
                           "categories": {"temperature": "red"}}}).status_code == 422
    # valid saves + persists + GET returns it
    r = client.post("/api/config/general", json={
        "timezone": "Europe/Bucharest",
        "default_colors": {"phase_convention": "iec",
                           "categories": {"temperature": "#f97316"}}})
    assert r.status_code == 200
    g = client.get("/api/config/general").json()
    assert g["default_colors"]["phase_convention"] == "iec"
    assert Config(str(tmp_path / "config.yaml")).ui.default_colors["phase_convention"] == "iec"
