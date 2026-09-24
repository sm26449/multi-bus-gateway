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


# ── valid-but-truncated files (external audit) ───────────────────────────────
# YAML/JSON that PARSES can still be a husk (empty file, bare scalar, cut
# before the sections every save writes). It must heal like a parse error —
# and must NEVER overwrite the .good snapshot with the husk.

def test_truncated_valid_yaml_heals_and_keeps_good_snapshot(tmp_path):
    p = tmp_path / "config.yaml"
    _write(p, "10.0.0.5")
    Config(str(p))                                    # seeds .good
    for husk in ("", "null", "just a string", "unrelated_key: 1\n"):
        p.write_text(husk)
        c = Config(str(p))
        assert c.modbus.host == "10.0.0.5"            # healed from snapshot
        assert c._load_failed                         # saves stay blocked
        assert (tmp_path / "config.yaml.bad").exists()
        # the snapshot survived — NOT clobbered by the husk
        assert yaml.safe_load((tmp_path / "config.yaml.good").read_text())["modbus"]["host"] == "10.0.0.5"


def _regs_payload(n):
    return {"version": "1.0",
            "registers": [{"address": i, "name": f"r{i}", "label": f"r{i}",
                           "unit": "V", "data_type": "float",
                           "poll_group": "normal"} for i in range(n)]}


def _cfg_with_regs(tmp_path, payload) -> Config:
    import json
    _write(tmp_path / "config.yaml", "10.0.0.5")
    (tmp_path / "selected_registers.json").write_text(json.dumps(payload))
    return Config(str(tmp_path / "config.yaml"))


def test_selected_registers_good_snapshot_and_heal(tmp_path):
    import json
    c = _cfg_with_regs(tmp_path, _regs_payload(3))
    assert len(c.selected_registers) == 3
    good = tmp_path / "selected_registers.json.good"
    assert good.exists()                              # snapshot seeded on load

    # corrupt JSON → .bad kept + healed from .good (selection NOT emptied)
    (tmp_path / "selected_registers.json").write_text('{"version": "1.0", "registers": [{"addr')
    c2 = Config(str(tmp_path / "config.yaml"))
    assert len(c2.selected_registers) == 3
    assert (tmp_path / "selected_registers.json.bad").exists()

    # truncated-valid JSON (parses, but the always-written key is gone)
    (tmp_path / "selected_registers.json").write_text('{"version": "1.0"}')
    c3 = Config(str(tmp_path / "config.yaml"))
    assert len(c3.selected_registers) == 3
    # .good survived both incidents
    assert len(json.loads(good.read_text())["registers"]) == 3


def test_selected_registers_legitimate_empty_selection_is_accepted(tmp_path):
    import json
    c = _cfg_with_regs(tmp_path, _regs_payload(3))
    assert len(c.selected_registers) == 3
    # deselect-all is a REAL save shape (registers key present, empty) — it
    # must load as empty and refresh the snapshot, not trigger the heal
    (tmp_path / "selected_registers.json").write_text(json.dumps(_regs_payload(0)))
    c2 = Config(str(tmp_path / "config.yaml"))
    assert c2.selected_registers == []
    assert json.loads((tmp_path / "selected_registers.json.good").read_text())["registers"] == []


def test_device_registers_heal_from_snapshot(tmp_path):
    import json
    from multibus.config import DeviceConfig
    c = _cfg_with_regs(tmp_path, _regs_payload(1))
    dev_dir = tmp_path / "devices" / "acme"
    dev_dir.mkdir(parents=True)
    (dev_dir / "selected_registers.json").write_text(json.dumps(_regs_payload(2)))
    dev = DeviceConfig(id="acme", primary=False)
    regs, _ = c.load_device_registers(dev)
    assert len(regs) == 2
    # truncation → healed from the per-device .good, not an empty selection
    (dev_dir / "selected_registers.json").write_text("[]")
    regs2, _ = c.load_device_registers(dev)
    assert len(regs2) == 2


def _write_with_devices(path, host, n_devices):
    doc = {"modbus": {"host": host, "port": 502},
           "mqtt": {"enabled": False},
           "devices": [{"id": f"dev{i}",
                        "connection": {"protocol": "tcp", "host": "10.0.0.9",
                                       "port": 1502, "unit_id": i + 2}}
                       for i in range(n_devices)]}
    path.write_text(yaml.safe_dump(doc))


def test_good_not_clobbered_when_devices_vanish(tmp_path):
    # the truncation signature: a cut file keeps the early sections (still
    # plausible) but LOSES the devices tail — it may load, but it must not
    # replace the only recovery copy
    p = tmp_path / "config.yaml"
    _write_with_devices(p, "10.0.0.5", 2)
    Config(str(p))                                    # seeds .good (2 devices)
    _write(p, "10.0.0.5")                             # plausible, devices GONE
    Config(str(p))
    good = yaml.safe_load((tmp_path / "config.yaml.good").read_text())
    assert len(good.get("devices") or []) == 2        # snapshot survived


def test_intentional_device_removal_refreshes_good_via_save(tmp_path):
    # a legitimate shrink goes through save_config, which refreshes .good
    # directly — so the conservative load gate never wedges the snapshot
    p = tmp_path / "config.yaml"
    _write_with_devices(p, "10.0.0.5", 2)
    c = Config(str(p))
    c._raw_devices = c._raw_devices[:1]               # operator deletes one
    c.save_yaml_config()
    good = yaml.safe_load((tmp_path / "config.yaml.good").read_text())
    assert len(good.get("devices") or []) == 1        # .good follows the save


def test_first_run_provisions_hashed_admin_password(tmp_path, capsys):
    """B5 (external audit): a fresh install must not ship ~120 open routes.
    No config.yaml → generate an admin password, store it HASHED with auth
    on, print it once; an existing config is never touched."""
    import main as m
    app = m.GatewayApp.__new__(m.GatewayApp)
    app.config_path = str(tmp_path / "config.yaml")
    app._first_run_provision()
    doc = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert doc["ui"]["auth"]["enabled"] is True
    assert doc["ui"]["auth"]["password"].startswith("pbkdf2_sha256$")
    printed = capsys.readouterr().out
    assert "FIRST RUN" in printed and "password:" in printed
    # the password appears ONLY in stdout, never in the file
    assert printed.split("password: ")[1].split()[0] not in (tmp_path / "config.yaml").read_text()
    # second boot: config exists → untouched, nothing printed
    before = (tmp_path / "config.yaml").read_text()
    app._first_run_provision()
    assert (tmp_path / "config.yaml").read_text() == before
    assert "FIRST RUN" not in capsys.readouterr().out


def test_json_self_heal_copies_are_private(tmp_path):
    """F-39 (3.83.0): the .good/.bad copies of the register selection are
    written 0600 and atomically, like every other file in the config dir."""
    import json
    import os
    import stat
    p = tmp_path / "config.yaml"
    p.write_text("modbus:\n  host: 1.2.3.4\n")
    c = Config(str(p))
    sel = tmp_path / "selected_registers.json"
    sel.write_text(json.dumps({"registers": [{"address": 1, "name": "a", "data_type": "uint16"}]}))
    c._load_json_with_heal(sel, required_key="registers", label="selection")
    good = tmp_path / "selected_registers.json.good"
    assert good.exists()
    assert stat.S_IMODE(os.stat(good).st_mode) == 0o600
    sel.write_text("{not json")
    c._load_json_with_heal(sel, required_key="registers", label="selection")
    bad = tmp_path / "selected_registers.json.bad"
    assert bad.exists() and stat.S_IMODE(os.stat(bad).st_mode) == 0o600
