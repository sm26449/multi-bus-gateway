# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Config self-healing: a good load leaves a snapshot; a later corrupt config
falls back to that snapshot (not bare defaults), preserves the broken file, and
keeps saves disabled + surfaces the condition."""
import yaml

from multibus.config import Config


def _write(path, host):
    path.write_text(yaml.safe_dump({"modbus": {"host": host, "port": 502}}))


def test_good_load_writes_a_snapshot(tmp_path):
    p = tmp_path / "config.yaml"
    _write(p, "10.0.0.5")
    Config(str(p))
    snap = tmp_path / "config.yaml.good"
    assert snap.exists()
    assert yaml.safe_load(snap.read_text())["modbus"]["host"] == "10.0.0.5"


def test_corrupt_config_heals_from_snapshot(tmp_path):
    p = tmp_path / "config.yaml"
    _write(p, "10.0.0.5")
    Config(str(p))                                    # seeds the .good snapshot
    p.write_text("modbus: [this is : not valid yaml ::::")   # corrupt edit
    c = Config(str(p))
    # ran on the snapshot's host, NOT the default
    assert c.modbus.host == "10.0.0.5"
    st = c.config_status()
    assert st["healthy"] is False and st["healed_from_snapshot"] is True
    assert st["saves_disabled"] is True
    assert (tmp_path / "config.yaml.bad").exists()    # broken file preserved


def test_corrupt_config_without_snapshot_falls_back_to_defaults(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("modbus: [broken ::::")
    c = Config(str(p))                                # no prior .good
    assert c.config_status()["healthy"] is False
    assert c.modbus.host == "192.168.1.100"           # bare default, didn't crash


def test_healed_config_refuses_to_save(tmp_path):
    p = tmp_path / "config.yaml"
    _write(p, "10.0.0.5")
    Config(str(p))
    p.write_text("modbus: [unclosed flow ::::")       # genuinely invalid YAML
    c = Config(str(p))
    import pytest
    with pytest.raises(RuntimeError):
        c.save_yaml_config()                          # must not overwrite the real file
