# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""`Config.load()` reads into a staging copy and publishes it in one step (ADR-0047 §1 /
design §6; FINDINGS RELOAD-ATOMIC-1). No other thread sees a half-loaded config, a reload
that cannot read its file changes nothing that is live, and the writes a load makes happen
after the publication — never while reading."""
import pytest

from multibus.core.config import Config
from tests.test_commands_api import PLANT_YAML
from tests.test_devices import write_config

TYPES_YAML = """
device_types:
  - id: chiller
    labels: {en: Chiller}
    icon: snow
    behaves_like: heat_pump
"""


def _good(cfg):
    return cfg.config_path.with_suffix('.yaml.good')


# ── nothing half-loaded is ever visible ──────────────────────────────────────────────────

def test_a_reload_never_exposes_cold_defaults(tmp_path, monkeypatch):
    """While the file is being parsed, the live object still answers with the config that was
    running — not with zero devices and security at its defaults."""
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    assert cfg.get_device('pv-u1') is not None and cfg.security.allow_writes is True
    seen = {}
    orig = Config._load_yaml_config

    def spy(self):
        seen.update(devices=len(cfg.devices), allow_writes=cfg.security.allow_writes,
                    pv_u1=cfg.get_device('pv-u1') is not None, same_object=self is cfg)
        return orig(self)
    monkeypatch.setattr(Config, '_load_yaml_config', spy)
    n = len(cfg.devices)
    cfg.load()
    assert seen == {'devices': n, 'allow_writes': True, 'pv_u1': True, 'same_object': False}


def test_a_reload_publishes_the_new_file_on_the_same_object(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    holder = cfg                                    # every component holds this reference
    text = cfg.config_path.read_text().replace('192.168.1.207', '192.168.1.208')
    cfg.config_path.write_text(text)
    assert cfg.load() is True
    assert holder.modbus.host == '192.168.1.208'
    assert holder.get_device('pv-u1') is not None


# ── a reload that cannot read its file changes nothing that is live ──────────────────────

@pytest.mark.parametrize('keep_good', [True, False])
def test_a_failed_reload_keeps_the_running_config(tmp_path, keep_good):
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    devices, host = [d.id for d in cfg.devices], cfg.modbus.host
    if not keep_good:
        _good(cfg).unlink()
    cfg.config_path.write_text("modbus: [unclosed\n")
    assert cfg.load() is False
    assert [d.id for d in cfg.devices] == devices and cfg.modbus.host == host
    assert cfg.security.allow_writes is True
    assert cfg._load_failed is True                 # said, and saves stay blocked
    with pytest.raises(Exception):
        cfg.save_yaml_config()


def test_a_failed_reload_writes_nothing_but_the_bad_copy(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    good_before = _good(cfg).read_text()
    cfg.config_path.write_text("modbus: [unclosed\n")
    cfg.load()
    assert cfg.config_path.read_text() == "modbus: [unclosed\n"
    assert _good(cfg).read_text() == good_before
    assert cfg.config_path.with_suffix('.yaml.bad').exists()


def test_a_repaired_file_loads_again_after_a_failed_reload(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    text = cfg.config_path.read_text()
    cfg.config_path.write_text("modbus: [unclosed\n")
    assert cfg.load() is False
    cfg.config_path.write_text(text.replace('192.168.1.207', '192.168.1.208'))
    assert cfg.load() is True
    assert cfg._load_failed is False and cfg.modbus.host == '192.168.1.208'
    cfg.save_yaml_config()                          # saves work again


def test_a_reload_that_finds_no_file_keeps_the_running_config_and_can_save_it_back(tmp_path):
    """A deleted config.yaml must not publish bare defaults and a new gateway id; the running
    config stays, and — unlike a broken file — a save may write it back."""
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    devices, gateway_id = [d.id for d in cfg.devices], cfg.gateway_id
    cfg.config_path.unlink()
    assert cfg.load() is False
    assert [d.id for d in cfg.devices] == devices and cfg.gateway_id == gateway_id
    assert cfg._load_failed is False
    cfg.save_yaml_config()
    assert Config(str(cfg.config_path)).get_device('pv-u1') is not None


def test_a_refused_reload_says_so_when_a_save_is_blocked(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    cfg.config_path.write_text("modbus: [unclosed\n")
    cfg.load()
    with pytest.raises(Exception, match="could not be reloaded"):
        cfg.save_yaml_config()


def test_a_cold_boot_on_a_broken_file_still_heals_from_the_last_good_copy(tmp_path):
    """Only a RELOAD is refused. A boot has no running config to keep: it heals, as before."""
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    cfg.config_path.write_text("modbus: [unclosed\n")
    booted = Config(str(cfg.config_path))
    assert booted._load_failed is True and booted._healed_from_snapshot is True
    assert booted.get_device('pv-u1') is not None


# ── process-wide state follows the publication, not the staging copy ─────────────────────

def test_the_bridge_guard_recorder_stays_bound_to_the_live_object(tmp_path):
    from multibus.transports.serial import bridge_guard
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    cfg.load()
    assert bridge_guard._recorder.__self__ is cfg


def test_a_failed_reload_leaves_the_user_device_types_in_the_catalog(tmp_path):
    from multibus.catalog.device_types import is_user_type
    cfg = write_config(tmp_path, extra_yaml=TYPES_YAML)
    assert is_user_type('chiller')
    cfg.config_path.write_text("modbus: [unclosed\n")
    assert cfg.load() is False
    assert is_user_type('chiller')


def test_a_reload_that_raises_leaves_the_user_device_types_in_the_catalog(tmp_path):
    """A file that parses but cannot be built (a non-dict device entry) raises out of the
    load: the process-wide catalog must be handed back the live types on the way out."""
    from multibus.catalog.device_types import is_user_type
    cfg = write_config(tmp_path, extra_yaml=TYPES_YAML)
    text = cfg.config_path.read_text().split('device_types:')[0]
    cfg.config_path.write_text(text + "devices: [not-a-device]\n")
    try:
        loaded = cfg.load()
    except Exception:  # noqa: BLE001 — either outcome is fine; the catalog is the point
        loaded = None
    if loaded is not True:
        assert is_user_type('chiller')
        assert cfg.get_device is not None and cfg.device_types


def test_a_reload_that_drops_a_user_type_drops_it_from_the_catalog(tmp_path):
    from multibus.catalog.device_types import is_user_type
    cfg = write_config(tmp_path, extra_yaml=TYPES_YAML)
    cfg.config_path.write_text(cfg.config_path.read_text().split('device_types:')[0])
    assert cfg.load() is True
    assert not is_user_type('chiller')


# ── the writes of a load come after the publication ──────────────────────────────────────

def test_the_good_copy_is_refreshed_after_the_publication(tmp_path, monkeypatch):
    """`.good` is written by the post-load step, on the LIVE object — the read path is pure."""
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    _good(cfg).unlink()
    seen = {}
    orig = Config._build_devices

    def spy(self):
        seen['good_during_read'] = _good(cfg).exists()
        return orig(self)
    monkeypatch.setattr(Config, '_build_devices', spy)
    cfg.load()
    assert seen == {'good_during_read': False}
    assert _good(cfg).exists()
