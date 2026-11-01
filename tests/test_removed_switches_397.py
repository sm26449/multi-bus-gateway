# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The kill switches `rules.recheck` (ADR-0021 D9) and `commands.raw_lanes`
(ADR-0022 D7) were removed in 3.97: their ON behaviour is the only one. A
config.yaml that still holds them loads, says so once, and the next save
drops them; the environment variables change nothing."""
import logging

import yaml

from tests.test_devices import write_config

OLD = "rules: { recheck: off }\ncommands: { raw_lanes: off }\n"


def _removed_lines(caplog):
    return [r for r in caplog.records if 'removed in 3.97' in r.getMessage()]


def test_a_file_with_the_old_keys_loads_and_says_so_once(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv('MBG_RULES_RECHECK', raising=False)
    monkeypatch.delenv('MBG_RAW_LANES', raising=False)
    with caplog.at_level(logging.INFO, logger='multibus.config'):
        cfg = write_config(tmp_path, extra_yaml=OLD)
    assert not getattr(cfg, '_load_failed', False)
    lines = _removed_lines(caplog)
    assert len(lines) == 1 and lines[0].levelno == logging.INFO
    assert 'rules.recheck' in lines[0].getMessage() and 'commands.raw_lanes' in lines[0].getMessage()
    assert not hasattr(cfg, 'rules') and not hasattr(cfg, 'commands')


def test_one_old_key_alone_is_said_alone(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv('MBG_RULES_RECHECK', raising=False)
    monkeypatch.delenv('MBG_RAW_LANES', raising=False)
    with caplog.at_level(logging.INFO, logger='multibus.config'):
        write_config(tmp_path, extra_yaml="commands: { raw_lanes: on }\n")
    lines = _removed_lines(caplog)
    assert len(lines) == 1 and 'commands.raw_lanes' in lines[0].getMessage()
    assert 'rules.recheck' not in lines[0].getMessage()


def test_the_next_save_drops_them(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv('MBG_RULES_RECHECK', raising=False)
    monkeypatch.delenv('MBG_RAW_LANES', raising=False)
    cfg = write_config(tmp_path, extra_yaml=OLD)
    cfg.save_yaml_config()
    data = yaml.safe_load((tmp_path / 'config.yaml').read_text())
    assert 'rules' not in data and 'commands' not in data
    caplog.clear()
    with caplog.at_level(logging.INFO, logger='multibus.config'):
        cfg.load()                                       # a clean file: nothing left to say
    assert not _removed_lines(caplog)


def test_a_file_without_them_says_nothing(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv('MBG_RULES_RECHECK', raising=False)
    monkeypatch.delenv('MBG_RAW_LANES', raising=False)
    with caplog.at_level(logging.INFO, logger='multibus.config'):
        write_config(tmp_path)
    assert not _removed_lines(caplog)


def test_the_environment_is_said_and_changes_nothing(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv('MBG_RULES_RECHECK', 'off')
    monkeypatch.setenv('MBG_RAW_LANES', 'off')
    with caplog.at_level(logging.INFO, logger='multibus.config'):
        cfg = write_config(tmp_path)
    lines = _removed_lines(caplog)
    assert len(lines) == 1 and 'MBG_RULES_RECHECK' in lines[0].getMessage() \
        and 'MBG_RAW_LANES' in lines[0].getMessage()
    cfg.save_yaml_config()
    data = yaml.safe_load((tmp_path / 'config.yaml').read_text())
    assert 'rules' not in data and 'commands' not in data
