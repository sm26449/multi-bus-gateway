# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Config version stamp (audit MEDIUM-2): every save records which gateway
wrote the file; loading a file stamped by a NEWER gateway (a downgrade) warns
loudly and surfaces in config_status() — a save from the older version would
silently drop the settings the newer one introduced."""
import yaml

from multibus import __version__
from multibus.config import Config, _version_tuple


def _write_min_config(path, extra=""):
    path.write_text("modbus:\n  host: 1.2.3.4\n" + extra)


def test_save_stamps_the_running_version(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    _write_min_config(cfg_file)
    cfg = Config(str(cfg_file))
    cfg.save_yaml_config()
    data = yaml.safe_load(cfg_file.read_text())
    assert data["config_version"] == __version__
    # first key on purpose — visible at the top of a hand-opened file
    assert next(iter(data)) == "config_version"


def test_missing_or_older_stamp_is_silent(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    _write_min_config(cfg_file)                       # no stamp (pre-3.24 file)
    cfg = Config(str(cfg_file))
    assert cfg.config_written_by_newer is False
    assert cfg.config_status()["written_by"] is None

    _write_min_config(cfg_file, "config_version: 1.0.0\n")
    cfg = Config(str(cfg_file))                       # older stamp → fine
    assert cfg.config_written_by_newer is False
    assert cfg.config_status()["written_by"] == "1.0.0"


def test_newer_stamp_warns_and_surfaces(tmp_path, caplog):
    cfg_file = tmp_path / "config.yaml"
    _write_min_config(cfg_file, "config_version: 999.0.0\n")
    import logging
    with caplog.at_level(logging.WARNING, logger="multibus.config"):
        cfg = Config(str(cfg_file))
    assert cfg.config_written_by_newer is True
    st = cfg.config_status()
    assert st["written_by"] == "999.0.0" and st["written_by_newer"] is True
    assert any("DROPPED by the next save" in r.message for r in caplog.records)
    # loading still works — the guard warns, it never blocks
    assert cfg.modbus.host == "1.2.3.4"


def test_version_tuple_is_fail_open():
    assert _version_tuple("3.24.1") == (3, 24, 1)
    assert _version_tuple("3.24.1-rc1") == (3, 24, 1)
    assert _version_tuple("garbage") == (0,)
    # a malformed stamp never reads as "newer" than a real version
    assert not _version_tuple("garbage") > _version_tuple(__version__)
