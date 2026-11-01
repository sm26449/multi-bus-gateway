# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""ADR-0029 / INFLUX-HB-1: in `changed` mode InfluxDB rewrites an unchanged
value once it has not been written for `heartbeat_interval` seconds, so a
steady value always has a recent point. The value is a visible setting: a
file without it gets the starting value written into it, Settings and the
device's Outputs show what is stored."""
import logging
import threading
import time
from types import SimpleNamespace

import yaml

from multibus.config import Config, InfluxDBConfig
from multibus.influxdb_publisher import InfluxDBPublisher

from tests.test_devices import write_config
from tests.test_devices_api import make_app, needs_tc

KEY = ("d", 7)
REG = SimpleNamespace(name="valve", influxdb_enabled=True)


def _pub(hb=600, wi=5, mode="changed"):
    cfg = InfluxDBConfig(enabled=False, write_interval=wi, heartbeat_interval=hb, buffer_persist=False)
    pub = InfluxDBPublisher(cfg, [], publish_mode=mode)
    pub.config.enabled = True                     # no client, no monitor thread
    out = []
    pub._build_point = lambda reg, v, ts, **k: (v, ts)
    pub._deliver = lambda point, ts, bucket=None: out.append(point)
    return pub, out


def _read(pub, value=1.0, ts=1000.0, **policy):
    pub.write_register_data("g", {7: {"value": value, "register": REG, "ts": ts}}, device_id="d", **policy)


def _age(pub, seconds):
    """The last point of the value was written `seconds` ago."""
    pub.last_write_time[KEY] = time.monotonic() - seconds


# ── the publisher ────────────────────────────────────────────────────────────

def test_an_unchanged_value_is_rewritten_after_the_interval_and_not_before():
    pub, out = _pub(hb=600)
    _read(pub)
    assert len(out) == 1
    _age(pub, 100)
    _read(pub)
    assert len(out) == 1                                      # 100 s: still quiet
    _age(pub, 599)
    _read(pub)
    assert len(out) == 1
    _age(pub, 601)
    _read(pub, ts=5000.0)
    assert out[-1] == (1.0, 5000.0) and len(out) == 2         # the reading's point, its timestamp
    _read(pub, ts=5001.0)
    assert len(out) == 2                                      # and the clock starts again


def test_zero_turns_the_heartbeat_off():
    pub, out = _pub(hb=0)
    _read(pub)
    _age(pub, 100_000)
    _read(pub)
    assert len(out) == 1


def test_the_devices_own_heartbeat_comes_before_the_gateways():
    pub, out = _pub(hb=600)
    _read(pub)
    _age(pub, 61)
    _read(pub, heartbeat=60)
    assert len(out) == 2                                      # its own 60 s
    _age(pub, 700)
    _read(pub, heartbeat=0)
    assert len(out) == 2                                      # 0 = off, its choice
    pub.config.heartbeat_interval = 0
    _age(pub, 61)
    _read(pub, heartbeat=60)
    assert len(out) == 3                                      # on although the gateway's is off


def test_the_write_interval_stays_the_minimum_gap():
    pub, out = _pub(hb=1, wi=5)
    _read(pub)
    _age(pub, 3)
    _read(pub)
    assert len(out) == 1
    _age(pub, 6)
    _read(pub)
    assert len(out) == 2


def test_every_reading_mode_behaves_as_before():
    pub, out = _pub(hb=600, mode="all")
    _read(pub)
    _age(pub, 2)
    _read(pub)
    assert len(out) == 1                                      # the write interval holds
    _age(pub, 6)
    _read(pub)
    assert len(out) == 2                                      # unchanged, written anyway


def test_a_device_that_stops_reading_writes_nothing():
    threads = set(threading.enumerate())
    pub, out = _pub(hb=1, wi=0)
    _read(pub)
    _age(pub, 10_000)
    pub.flush()
    time.sleep(0.05)
    assert len(out) == 1                                      # no reading, no point
    assert set(threading.enumerate()) <= threads              # and no timer of its own
    _read(pub, ts=99.0)
    assert out[-1] == (1.0, 99.0)                             # the next reading, its own time


def test_the_quiet_time_is_measured_on_the_monotonic_clock():
    pub, _out = _pub()
    _read(pub)
    assert abs(pub.last_write_time[KEY] - time.monotonic()) < 5


# ── the configuration ────────────────────────────────────────────────────────

def test_a_file_without_the_key_gets_the_starting_value_written_once(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="multibus.config")
    cfg = write_config(tmp_path)
    assert cfg.influxdb.heartbeat_interval == 600
    doc = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert doc["influxdb"]["heartbeat_interval"] == 600
    assert doc["influxdb"]["bucket"] == "janitza"             # the rest as it was
    lines = [r for r in caplog.records if "heartbeat_interval" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    caplog.clear()
    before = (tmp_path / "config.yaml").read_bytes()
    again = Config(str(tmp_path / "config.yaml"))
    assert again.influxdb.heartbeat_interval == 600
    assert (tmp_path / "config.yaml").read_bytes() == before  # written once
    assert not [r for r in caplog.records if "heartbeat_interval" in r.getMessage()]


def test_a_file_without_an_influxdb_section_gets_it_too(tmp_path):
    (tmp_path / "config.yaml").write_text("mqtt:\n  enabled: false\n", encoding="utf-8")
    cfg = Config(str(tmp_path / "config.yaml"))
    assert cfg.influxdb.heartbeat_interval == 600
    assert yaml.safe_load((tmp_path / "config.yaml").read_text())["influxdb"] == {"heartbeat_interval": 600}


def test_the_stored_value_is_what_runs_never_a_substitute(tmp_path):
    for stored in (120, 0):
        # (ADR-0031 §5: the read preview's budget is written out the same
        # way — a file that has it too is left as it is)
        (tmp_path / "config.yaml").write_text(
            f"preview:\n  max_requests: 4\n  listen_s: 10\n"
            f"mqtt:\n  enabled: false\ninfluxdb:\n  heartbeat_interval: {stored}\n", encoding="utf-8")
        before = (tmp_path / "config.yaml").read_bytes()
        cfg = Config(str(tmp_path / "config.yaml"))
        assert cfg.influxdb.heartbeat_interval == stored
        assert (tmp_path / "config.yaml").read_bytes() == before
        cfg.save_yaml_config()
        assert yaml.safe_load((tmp_path / "config.yaml").read_text())["influxdb"]["heartbeat_interval"] == stored
        assert cfg.to_dict()["influxdb"]["heartbeat_interval"] == stored


def test_a_broken_file_is_never_written_into(tmp_path):
    (tmp_path / "config.yaml").write_text("influxdb: [unclosed\n", encoding="utf-8")
    Config(str(tmp_path / "config.yaml"))
    assert (tmp_path / "config.yaml").read_text() == "influxdb: [unclosed\n"


YAML = """
devices:
  - id: hp
    connection: { protocol: tcp, host: 192.0.2.9 }
    influxdb: { heartbeat_interval: 60 }
  - id: plain
    connection: { protocol: tcp, host: 192.0.2.10 }
  - id: wrong
    connection: { protocol: tcp, host: 192.0.2.11 }
    influxdb: { heartbeat_interval: -3 }
endpoints:
  - id: pv
    connection: { protocol: tcp, host: 192.0.2.9, port: 502 }
    groups:
      - id: inverters
        units: [1, 2]
        influxdb: { heartbeat_interval: 300 }
"""


def test_a_device_or_a_group_carries_its_own_heartbeat_or_none(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=YAML)
    assert cfg.get_device("hp").influxdb_heartbeat_s == 60
    assert cfg.get_device("plain").influxdb_heartbeat_s is None              # the gateway's
    assert cfg.get_device("wrong").influxdb_heartbeat_s is None              # nonsense = the gateway's
    assert cfg.get_device("pv-u2").influxdb_heartbeat_s == 300               # its group's
    cfg.set_device_publishing("plain", influxdb={"heartbeat_interval": 30})
    cfg.set_device_publishing("hp", influxdb={"heartbeat_interval": None})
    cfg.set_device_publishing("pv-u1", influxdb={"heartbeat_interval": 0})
    re = Config(str(tmp_path / "config.yaml"))
    assert re.get_device("plain").influxdb_heartbeat_s == 30
    assert re.get_device("hp").influxdb_heartbeat_s is None
    assert re.get_device("pv-u2").influxdb_heartbeat_s == 0
    assert re.get_raw_endpoint("pv")["groups"][0]["influxdb"] == {"heartbeat_interval": 0}


# ── the API ──────────────────────────────────────────────────────────────────

@needs_tc
def test_settings_show_the_stored_value_and_take_a_new_one(tmp_path):
    cfg, client = make_app(tmp_path)
    assert client.get("/api/config/influxdb").json()["heartbeat_interval"] == 600   # never a blank
    r = client.post("/api/config/influxdb", json={"heartbeat_interval": 120})
    assert r.status_code == 200, r.text
    assert client.get("/api/config/influxdb").json()["heartbeat_interval"] == 120
    assert yaml.safe_load(cfg.config_path.read_text())["influxdb"]["heartbeat_interval"] == 120
    for bad in (-1, 86401, "60", True, 1.5):
        assert client.post("/api/config/influxdb", json={"heartbeat_interval": bad}).status_code == 422, bad
    assert cfg.influxdb.heartbeat_interval == 120


@needs_tc
def test_the_device_says_its_heartbeat_and_wrong_values_are_refused(tmp_path):
    _cfg, client = make_app(tmp_path, extra_yaml=YAML)
    pub = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "hp")["publishing"]
    assert pub["own"]["influxdb"]["heartbeat_interval"] == 60
    assert pub["settings"]["influxdb"]["heartbeat_interval"] == 600
    assert pub["effective"]["influxdb"]["heartbeat_interval"] == 60
    for bad in (-1, 86401, "5", True):
        r = client.post("/api/devices/plain/publishing", json={"influxdb": {"heartbeat_interval": bad}})
        assert r.status_code == 422, bad
    r = client.post("/api/devices/plain/publishing", json={"influxdb": {"heartbeat_interval": 45}})
    assert r.status_code == 200, r.text
    p = r.json()["publishing"]
    assert p["own"]["influxdb"]["heartbeat_interval"] == 45 and p["effective"]["influxdb"]["heartbeat_interval"] == 45
    r = client.post("/api/devices/plain/publishing", json={"influxdb": {"heartbeat_interval": None}})
    p = r.json()["publishing"]
    assert p["own"]["influxdb"]["heartbeat_interval"] is None and p["effective"]["influxdb"]["heartbeat_interval"] == 600


@needs_tc
def test_each_reading_goes_out_with_the_devices_heartbeat(tmp_path):
    from fastapi.testclient import TestClient
    from multibus.api import create_api
    from tests.test_device_publishing import _Cap, _poll
    cfg = write_config(tmp_path, extra_yaml=YAML)
    cap = _Cap()
    clients = {d.id: SimpleNamespace(publish_callback=None) for d in cfg.devices}
    app, _ = create_api(cfg, None, cap, cap, devices=[(d, clients[d.id]) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    _poll(clients, "hp")
    assert cap.influx[-1] == {"heartbeat": 60}
    _poll(clients, "plain")
    assert cap.influx[-1] == {}                                              # the gateway's
    assert client.post("/api/devices/plain/publishing",
                       json={"influxdb": {"heartbeat_interval": 0}}).status_code == 200
    _poll(clients, "plain")
    assert cap.influx[-1] == {"heartbeat": 0}


# ── review: NaN, one validator, the example file ─────────────────────────────

@needs_tc
def test_nan_and_infinity_are_refused_for_every_publishing_number(tmp_path):
    _cfg, client = make_app(tmp_path, extra_yaml=YAML)
    for key in ("heartbeat_interval", "write_interval"):
        for bad in ("NaN", "Infinity", "-Infinity"):
            r = client.post("/api/devices/plain/publishing", content=f'{{"influxdb": {{"{key}": {bad}}}}}',
                            headers={"Content-Type": "application/json"})
            assert r.status_code == 422, (key, bad, r.text)
    own = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "plain")["publishing"]["own"]
    assert own["influxdb"]["write_interval"] is None and own["influxdb"]["heartbeat_interval"] is None


def test_one_validator_for_the_gateway_the_device_and_the_api():
    from multibus.config import heartbeat_seconds
    assert [heartbeat_seconds(v) for v in (0, 600, 86400, 30.0)] == [0, 600, 86400, 30]
    assert all(heartbeat_seconds(v) is None
               for v in (-1, 86401, 1.5, True, "60", None, float("nan"), float("inf")))


def test_a_device_value_that_is_not_whole_seconds_falls_back_to_the_gateways(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="multibus.config")
    cfg = write_config(tmp_path, extra_yaml="""
devices:
  - id: frac
    connection: { protocol: tcp, host: 192.0.2.12 }
    influxdb: { heartbeat_interval: 1.5 }
  - id: text
    connection: { protocol: tcp, host: 192.0.2.13 }
    influxdb: { heartbeat_interval: "60" }
""")
    assert cfg.get_device("frac").influxdb_heartbeat_s is None
    assert cfg.get_device("text").influxdb_heartbeat_s is None
    assert any("frac" in r.getMessage() for r in caplog.records)


def test_the_example_file_has_the_key_so_a_fresh_copy_is_never_rewritten(tmp_path):
    import pathlib
    import shutil
    src = pathlib.Path(__file__).resolve().parent.parent / "config" / "config.example.yaml"
    assert yaml.safe_load(src.read_text())["influxdb"]["heartbeat_interval"] == 600
    shutil.copy(src, tmp_path / "config.yaml")
    before = (tmp_path / "config.yaml").read_bytes()
    Config(str(tmp_path / "config.yaml"))
    assert (tmp_path / "config.yaml").read_bytes() == before


@needs_tc
def test_settings_refuse_nan_and_a_fraction(tmp_path):
    cfg, client = make_app(tmp_path)
    for bad in ("NaN", "Infinity", "1.5"):
        r = client.post("/api/config/influxdb", content=f'{{"heartbeat_interval": {bad}}}',
                        headers={"Content-Type": "application/json"})
        assert r.status_code == 422, (bad, r.text)
    assert client.post("/api/config/influxdb", json={"heartbeat_interval": 30.0}).status_code == 200
    assert cfg.influxdb.heartbeat_interval == 30
