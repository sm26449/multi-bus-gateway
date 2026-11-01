# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""A section of config.yaml written in the wrong shape (FINDINGS QA-D-11,
review M1/m1/n1 of 2026-10-28).

A gateway-level `mqtt.ha_discovery: true` (the per-device form; the
gateway's is `{enabled, prefix}`) used to fail the whole load. Now:
- `mqtt.ha_discovery: <bool>` is read as `{enabled: <bool>}`, said once;
  `mqtt.ha_discovery: null` and `polling.groups: null` are empty;
- the sections that hold what reaches the plant and its data (modbus, mqtt,
  influxdb: broker, credentials, token) and those whose defaults would open
  the gateway (ui, ui.auth, ui.tls, security — these also when explicitly
  null) refuse the load in another shape: the file is kept as .bad, the last
  good copy runs, saves stay blocked, and the error names the key;
- any other mapping section (opcua, polling, alerts, …) in another shape is
  read as its defaults with one WARNING naming the key — and that load never
  refreshes the last good copy (.good), so the real section survives there."""
import logging

import pytest

from multibus.config import Config

DEVICES = """
devices:
  - id: em1
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.10, port: 502, unit_id: 1}
"""
GOOD = "modbus:\n  host: 192.0.2.5\nmqtt:\n  broker: real-broker\n  password: s3cret\ninfluxdb:\n  token: tok\n"


def _cfg(tmp_path, body: str) -> Config:
    (tmp_path / "config.yaml").write_text(body + DEVICES, encoding="utf-8")
    return Config(str(tmp_path / "config.yaml"))


def _with_good(tmp_path, body: str) -> Config:
    """A gateway that loaded a good file once (its .good written), then the
    file was edited into ``body``."""
    _cfg(tmp_path, GOOD)
    assert "real-broker" in (tmp_path / "config.yaml.good").read_text()
    return _cfg(tmp_path, body)


def _ids(cfg):
    return [d.id for d in cfg.devices]


def test_a_bool_ha_discovery_is_read_as_enabled_and_said(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    cfg = _cfg(tmp_path, """
mqtt:
  enabled: true
  broker: mosquitto
  ha_discovery: false
""")
    assert not getattr(cfg, '_load_failed', False)
    assert "em1" in _ids(cfg)                                  # the rest of the file is there
    assert cfg.mqtt.broker == "mosquitto"
    assert cfg.mqtt.ha_discovery_enabled is False
    assert cfg.mqtt.ha_discovery_prefix == "homeassistant"     # the prefix keeps its default
    warned = [r.getMessage() for r in caplog.records if 'mqtt.ha_discovery' in r.getMessage()]
    assert len(warned) == 1, warned


def test_a_bool_ha_discovery_true_enables_it_and_the_file_is_still_a_good_copy(tmp_path):
    cfg = _cfg(tmp_path, "mqtt:\n  broker: mosquitto\n  ha_discovery: true\n")
    assert not getattr(cfg, '_load_failed', False)
    assert cfg.mqtt.ha_discovery_enabled is True
    assert "ha_discovery: true" in (tmp_path / "config.yaml.good").read_text()   # nothing was lost


@pytest.mark.parametrize("key,body", [
    ("mqtt", "modbus:\n  host: 192.0.2.5\nmqtt: [a, b]\n"),
    ("mqtt", "modbus:\n  host: 192.0.2.5\nmqtt: true\n"),
    ("modbus", "mqtt:\n  broker: mosquitto\nmodbus: 5\n"),
    ("influxdb", "mqtt:\n  broker: mosquitto\ninfluxdb: x\n"),
])
def test_a_data_section_in_another_shape_refuses_the_load(tmp_path, caplog, key, body):
    """Its defaults would lose the broker, the credentials, the token: the
    last good copy runs, .good is untouched, saves stay blocked."""
    caplog.set_level(logging.ERROR)
    cfg = _with_good(tmp_path, body)
    assert cfg._load_failed
    assert cfg.mqtt.broker == "real-broker"                       # the last good copy runs
    good = (tmp_path / "config.yaml.good").read_text()
    assert "real-broker" in good and "s3cret" in good and "tok" in good
    assert (tmp_path / "config.yaml.bad").exists()
    assert any(f"`{key}`" in r.getMessage() for r in caplog.records), [r.getMessage() for r in caplog.records]
    from multibus.config import ConfigSaveBlocked
    with pytest.raises(ConfigSaveBlocked):
        cfg.save_yaml_config()


@pytest.mark.parametrize("section,body", [
    ("opcua", "mqtt:\n  broker: mosquitto\nopcua: true\n"),
    ("polling", "mqtt:\n  broker: mosquitto\npolling: fast\n"),
    ("alerts", "mqtt:\n  broker: mosquitto\nalerts: 5\n"),
    ("http_output", "mqtt:\n  broker: mosquitto\nhttp_output: true\n"),
    ("rest_push", "mqtt:\n  broker: mosquitto\nrest_push: 5\n"),
    ("opcua_output", "mqtt:\n  broker: mosquitto\nopcua_output: true\n"),
    ("pq_recorder", "mqtt:\n  broker: mosquitto\npq_recorder: true\n"),
])
def test_a_cosmetic_section_costs_only_that_section(tmp_path, caplog, section, body):
    caplog.set_level(logging.WARNING)
    cfg = _cfg(tmp_path, body)
    assert not getattr(cfg, '_load_failed', False), section
    assert "em1" in _ids(cfg)
    warned = [r.getMessage() for r in caplog.records if f"`{section}`" in r.getMessage()]
    assert len(warned) == 1, (section, [r.getMessage() for r in caplog.records])


def test_a_degraded_load_never_becomes_the_last_good_copy(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    _cfg(tmp_path, GOOD + "alerts:\n  enabled: true\n  url: http://alerts.example\n")
    before = (tmp_path / "config.yaml.good").read_text()
    cfg = _cfg(tmp_path, GOOD + "alerts: 5\n")
    assert not getattr(cfg, '_load_failed', False)
    assert cfg.alerts == {}                                        # its defaults
    assert (tmp_path / "config.yaml.good").read_text() == before   # NOT refreshed
    said = [r.getMessage() for r in caplog.records if 'last-known-good' in r.getMessage()]
    assert len(said) == 1 and 'alerts' in said[0], said


def test_a_degraded_load_does_not_write_the_heartbeat_back(tmp_path):
    """The one-time heartbeat write is a save: after a degraded load it would
    store the defaults of the degraded section."""
    cfg = _cfg(tmp_path, "mqtt:\n  broker: mosquitto\nalerts: 5\n")
    assert "alerts: 5" in (tmp_path / "config.yaml").read_text()
    assert not getattr(cfg, '_load_failed', False)


def test_a_poll_group_of_the_wrong_shape_is_skipped(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    cfg = _cfg(tmp_path, """
mqtt:
  broker: mosquitto
polling:
  groups:
    fast: {interval: 2}
    broken: 7
""")
    assert not getattr(cfg, '_load_failed', False)
    assert cfg.poll_groups["fast"].interval == 2
    assert "broken" not in cfg.poll_groups
    assert any("polling.groups.broken" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("body", [
    "mqtt:\n  broker: mosquitto\npolling:\n  groups:\n",
    "mqtt:\n  broker: mosquitto\n  ha_discovery:\n",
])
def test_null_groups_and_null_ha_discovery_are_empty(tmp_path, caplog, body):
    caplog.set_level(logging.WARNING)
    cfg = _cfg(tmp_path, body)
    assert not getattr(cfg, '_load_failed', False)
    assert not [r for r in caplog.records if 'must be a section' in r.getMessage()]
    assert cfg.mqtt.ha_discovery_enabled is True


@pytest.mark.parametrize("key,body", [
    ("ui", "mqtt:\n  broker: mosquitto\nui: true\n"),
    ("ui.auth", "mqtt:\n  broker: mosquitto\nui:\n  auth: true\n"),
    ("ui.tls", "mqtt:\n  broker: mosquitto\nui:\n  tls: on\n"),
    ("security", "mqtt:\n  broker: mosquitto\nsecurity: strict\n"),
    ("security", "mqtt:\n  broker: mosquitto\nsecurity:\n"),
    ("ui", "mqtt:\n  broker: mosquitto\nui: null\n"),
    ("ui.auth", "mqtt:\n  broker: mosquitto\nui:\n  auth:\n"),
    ("ui.tls", "mqtt:\n  broker: mosquitto\nui:\n  tls: null\n"),
])
def test_access_sections_refuse_the_load_also_when_null(tmp_path, caplog, key, body):
    """Their defaults would open the gateway (no login, no TLS, no
    allowlist): the file is not used, the last good copy is, saves stay
    blocked — an explicit null included."""
    caplog.set_level(logging.ERROR)
    cfg = _cfg(tmp_path, body)
    assert cfg._load_failed
    assert any(f"`{key}`" in r.getMessage() for r in caplog.records), [r.getMessage() for r in caplog.records]


def test_an_empty_section_is_its_defaults_without_a_warning(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    cfg = _cfg(tmp_path, "mqtt:\n  broker: mosquitto\nopcua:\ninfluxdb:\n")
    assert not getattr(cfg, '_load_failed', False)
    assert not [r for r in caplog.records if 'must be a section' in r.getMessage()]


def test_a_save_while_saves_are_blocked_is_refused_in_words_not_500(tmp_path):
    """The load failed (here: an access section in the wrong shape): Add
    Device answers 409 with what to repair and a marker the UI reads, never
    an Internal Server Error."""
    from fastapi.testclient import TestClient
    from multibus.api import create_api
    cfg = _cfg(tmp_path, "mqtt:\n  broker: mosquitto\nsecurity: strict\n")
    assert cfg._load_failed
    app, _ = create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/devices", json={"id": "em9", "name": "x", "enabled": False,
                                          "connection": {"protocol": "tcp", "host": "192.0.2.11",
                                                         "port": 502, "unit_id": 1}})
    assert r.status_code == 409, (r.status_code, r.text[:300])
    det = r.json()["detail"]
    assert "config.yaml" in " ".join(det["errors"])
    assert det.get("config_blocked") is True
