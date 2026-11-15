# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The config tree written as one transaction (ADR-0047 P3c, `core/config/tree_store.py`):
after a crash at ANY point the tree is one whole generation — the old one before the commit
record, the new one after it — and config.yaml is never involved. Behind MBG_CONFIG_TREE the
gateway writes the tree after each save and still reads config.yaml only."""
import os
import stat

import pytest
import yaml

from multibus.core.config import Config
from multibus.core.config.assemble import AssembleError, path_for
from multibus.core.config.tree_store import TreeStore, rev_of
from tests.test_commands_api import PLANT_YAML
from tests.test_devices import write_config

OLD = {"modbus": {"host": "1.1.1.1"}, "mqtt": {"enabled": True},
       "devices": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}, {"id": "gone"}],
       "bridges": [{"id": "br", "host": "10.0.0.1"}]}
NEW = {"modbus": {"host": "2.2.2.2"}, "mqtt": {"enabled": True},
       "devices": [{"id": "b", "name": "B"}, {"id": "a", "name": "A — renamed"}, {"id": "fresh"}],
       "bridges": [{"id": "br", "host": "10.0.0.1"}]}


class Crash(Exception):
    pass


def _crash_at(point, nth=1):
    seen = {"n": 0}

    def fault(p):
        if p == point:
            seen["n"] += 1
            if seen["n"] == nth:
                raise Crash(point)
    return fault


def _name(kind, eid):
    return path_for(kind, eid)[len("config.d/"):]


# ── a plain write ────────────────────────────────────────────────────────────────────────

def test_write_then_read_back_is_the_document(tmp_path):
    store = TreeStore(tmp_path)
    stats = store.write(OLD, "rev-old")
    assert stats == {"files": 4, "written": 4, "linked": 0}
    assert store.document() == OLD
    assert store.state("rev-old") == "current" and store.state("other") == "stale"
    assert not store.base_new.exists() and not store.d_new.exists() and not store.d_old.exists()


def test_a_tree_never_written_is_absent(tmp_path):
    assert TreeStore(tmp_path).state("x") == "absent"


def test_files_are_private(tmp_path):
    store = TreeStore(tmp_path)
    store.write(OLD, "r")
    for p in [store.base, *store.d.iterdir()]:
        assert stat.S_IMODE(p.stat().st_mode) == 0o600, p
    assert stat.S_IMODE(store.d.stat().st_mode) == 0o700


def test_only_changed_entities_are_written_and_a_removed_one_loses_its_file(tmp_path):
    store = TreeStore(tmp_path)
    store.write(OLD, "rev-old")
    untouched = (store.d / _name("device", "b")).stat().st_ino
    stats = store.write(NEW, "rev-new")
    assert stats == {"files": 4, "written": 2, "linked": 2}       # a (renamed) + fresh
    assert (store.d / _name("device", "b")).stat().st_ino == untouched   # hard-linked, not rewritten
    assert not (store.d / _name("device", "gone")).exists()
    assert store.document() == NEW


def test_a_document_that_cannot_be_mapped_touches_nothing(tmp_path):
    store = TreeStore(tmp_path)
    store.write(OLD, "rev-old")
    with pytest.raises(AssembleError):
        store.write({**NEW, "devices": [{"id": "x" * 300}]}, "rev-new")
    assert store.document() == OLD and store.state("rev-old") == "current"


# ── a crash at every point of the transaction ────────────────────────────────────────────

@pytest.mark.parametrize("point,nth,expect", [
    ("staged_file", 1, OLD), ("staged_file", 2, OLD), ("staged", 1, OLD),
    ("commit_written", 1, OLD), ("committed", 1, NEW), ("old_moved", 1, NEW), ("new_moved", 1, NEW), ("base_replaced", 1, NEW),
])
def test_a_crash_leaves_one_whole_generation(tmp_path, point, nth, expect):
    TreeStore(tmp_path).write(OLD, "rev-old")
    with pytest.raises(Crash):
        TreeStore(tmp_path, fault=_crash_at(point, nth)).write(NEW, "rev-new")
    after = TreeStore(tmp_path)                        # "the process restarted"
    if point != "base_replaced":
        assert after.state("rev-new") == "torn"        # said, until recovered
    after.recover()
    assert after.document() == expect
    assert after.state("rev-new" if expect is NEW else "rev-old") == "current"
    assert not after.base_new.exists() and not after.d_new.exists() and not after.d_old.exists()


@pytest.mark.parametrize("point", ["staged_file", "staged", "commit_written", "committed",
                                   "old_moved", "new_moved", "base_replaced"])
def test_the_next_write_heals_an_interrupted_one(tmp_path, point):
    TreeStore(tmp_path).write(OLD, "rev-old")
    with pytest.raises(Crash):
        TreeStore(tmp_path, fault=_crash_at(point)).write(NEW, "rev-new")
    store = TreeStore(tmp_path)
    third = {**NEW, "modbus": {"host": "3.3.3.3"}}
    store.write(third, "rev-3")
    assert store.document() == third and store.state("rev-3") == "current"


def test_a_crash_on_the_very_first_write_leaves_no_tree_or_the_whole_one(tmp_path):
    with pytest.raises(Crash):
        TreeStore(tmp_path, fault=_crash_at("staged")).write(OLD, "r")
    store = TreeStore(tmp_path)
    assert store.recover() == "discarded" and store.state("r") == "absent"
    with pytest.raises(Crash):
        TreeStore(tmp_path, fault=_crash_at("committed")).write(OLD, "r")
    store = TreeStore(tmp_path)
    assert store.recover() == "rolled_forward" and store.document() == OLD


def test_a_file_missing_from_the_live_tree_reads_as_torn(tmp_path):
    store = TreeStore(tmp_path)
    store.write(OLD, "r")
    (store.d / _name("device", "a")).unlink()
    assert store.state("r") == "torn"
    (store.d / "device.dropped-in.yaml").write_text("id: dropped-in\n")
    assert store.state("r") == "torn"


# ── the commit record is whole or absent; nothing in the way wedges the store ────────────

def test_a_half_written_commit_record_is_never_rolled_forward(tmp_path):
    """The record gets its name by a rename. A crash (or a full disk) while it is being
    written leaves `base.yaml.new.tmp`, which commits nothing."""
    store = TreeStore(tmp_path)
    store.write(OLD, "rev-old")
    os.mkdir(store.d_new)
    (store.d_new / _name("device", "half")).write_text("id: half\n")
    store.base_tmp.write_text("modbus:\n  ho")       # torn mid-write
    assert store.state("rev-old") == "torn"
    assert store.recover() == "discarded"
    assert store.document() == OLD and store.state("rev-old") == "current"
    assert not store.base_tmp.exists()


@pytest.mark.parametrize("wrong", ["d_new", "d_old", "base_new", "base_tmp"])
def test_a_leftover_of_the_wrong_kind_does_not_wedge_the_store(tmp_path, wrong):
    store = TreeStore(tmp_path)
    store.write(OLD, "rev-old")
    path = getattr(store, wrong)
    if wrong.startswith("d_"):
        path.write_text("not a directory")
    else:
        path.mkdir()
    store.write(NEW, "rev-new")
    assert store.document() == NEW and store.state("rev-new") == "current"
    assert not path.exists()


def test_a_symlink_left_as_staging_is_removed_not_followed(tmp_path):
    store = TreeStore(tmp_path)
    store.write(OLD, "rev-old")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    os.symlink(outside, store.d_new)
    store.write(NEW, "rev-new")
    assert store.document() == NEW
    assert (outside / "keep.txt").read_text() == "keep"


def test_a_tree_that_cannot_be_read_is_torn_not_an_exception(tmp_path):
    store = TreeStore(tmp_path)
    store.write(OLD, "r")
    store.base.write_text("- a list\n- not a mapping\n")
    assert store.state("r") == "torn" and store.source_rev() == ""
    store.write(OLD, "r")
    (store.d / _name("device", "a")).write_bytes(b"\xff\xfe not utf-8")
    assert store.state("r") == "torn"


def test_an_editor_swap_file_beside_a_drop_in_is_not_a_drop_in(tmp_path):
    store = TreeStore(tmp_path)
    store.write(OLD, "r")
    (store.d / ".device.a.yaml.swp").write_bytes(b"\x00\xff\xfe")
    assert store.state("r") == "current" and store.document() == OLD


def test_a_second_process_cannot_write_while_one_is_writing(tmp_path):
    """The store holds an flock for the whole transaction: another process's recover() must
    not clear this one's staging."""
    import fcntl
    seen = {}

    def fault(point):
        if point == "staged":
            fd = os.open(str(tmp_path / ".config-tree.lock"), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen["locked_out"] = False
            except BlockingIOError:
                seen["locked_out"] = True
            finally:
                os.close(fd)
    TreeStore(tmp_path, fault=fault).write(OLD, "r")
    assert seen == {"locked_out": True}


def test_a_linked_file_does_not_carry_a_loosened_mode(tmp_path):
    store = TreeStore(tmp_path)
    store.write(OLD, "r")
    f = store.d / _name("device", "b")
    os.chmod(f, 0o644)
    store.write(NEW, "r2")
    assert stat.S_IMODE((store.d / _name("device", "b")).stat().st_mode) == 0o600


# ── wired behind the flag: write both, read config.yaml ──────────────────────────────────

def _tree(cfg):
    return TreeStore(cfg.config_path.parent)


def test_without_the_flag_no_tree_is_written(tmp_path, monkeypatch):
    monkeypatch.delenv("MBG_CONFIG_TREE", raising=False)
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    cfg.save_yaml_config()
    assert not _tree(cfg).base.exists() and not _tree(cfg).d.exists()
    assert "tree" not in cfg.config_status()


def test_with_the_flag_a_save_writes_a_tree_that_assembles_to_config_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("MBG_CONFIG_TREE", "1")
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    cfg.save_yaml_config()
    text = cfg.config_path.read_text()
    tree = _tree(cfg)
    assert tree.document() == yaml.safe_load(text)
    assert tree.state(rev_of(text)) == "current"
    assert cfg.config_status()["tree"]["state"] == "current"


def test_the_tree_follows_an_edit_and_a_removal(tmp_path, monkeypatch):
    monkeypatch.setenv("MBG_CONFIG_TREE", "1")
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    cfg.save_yaml_config()
    before = sorted(p.name for p in _tree(cfg).d.iterdir())
    cfg._raw_endpoints = []
    cfg._build_devices()
    cfg.save_yaml_config()
    after = sorted(p.name for p in _tree(cfg).d.iterdir()) if _tree(cfg).d.exists() else []
    assert before and len(after) < len(before)
    assert _tree(cfg).document() == yaml.safe_load(cfg.config_path.read_text())


def test_the_gateway_still_reads_config_yaml_not_the_tree(tmp_path, monkeypatch):
    monkeypatch.setenv("MBG_CONFIG_TREE", "1")
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    cfg.save_yaml_config()
    tree = _tree(cfg)
    base = yaml.safe_load(tree.base.read_text())
    base["modbus"]["host"] = "203.0.113.7"             # a hand edit of the tree
    tree.base.write_text(yaml.safe_dump(base))
    assert Config(str(cfg.config_path)).modbus.host == "192.168.1.207"


def test_a_tree_that_cannot_be_written_does_not_fail_the_save(tmp_path, monkeypatch):
    monkeypatch.setenv("MBG_CONFIG_TREE", "1")
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)

    def boom(self, data, source_rev=""):
        raise OSError("disk full")
    monkeypatch.setattr(TreeStore, "write", boom)
    cfg.modbus.host = "192.168.1.208"
    cfg.save_yaml_config()                              # does not raise
    assert yaml.safe_load(cfg.config_path.read_text())["modbus"]["host"] == "192.168.1.208"
    status = cfg.config_status()["tree"]
    assert status["state"] == "failed" and "disk full" in status["error"]


def test_snapshots_do_not_carry_the_tree(tmp_path, monkeypatch):
    """Backup/restore keep speaking the ONE document (design §9): the tree is not in a bundle."""
    import io
    import zipfile

    from multibus.lifecycle.snapshots import SnapshotStore
    monkeypatch.setenv("MBG_CONFIG_TREE", "1")
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    cfg.save_yaml_config()
    assert _tree(cfg).base.exists()
    store = SnapshotStore(cfg.config_path.parent, tmp_path / "user_tpl",
                          device_ids=lambda: [d.id for d in cfg.devices])
    names = zipfile.ZipFile(io.BytesIO(store.build_bundle_bytes())).namelist()
    assert "config.yaml" in names
    assert not any(n.startswith("config.d") or n.startswith("base.yaml") for n in names)
    assert os.path.isdir(_tree(cfg).d)


def test_a_boot_measures_the_tree_and_brings_a_stale_one_up_to_date(tmp_path, monkeypatch):
    """The status is what is on disk, not what the last save believed: a tree written before
    config.yaml changed underneath it (an edit while stopped, a restore) is rewritten at load."""
    monkeypatch.setenv("MBG_CONFIG_TREE", "1")
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    cfg.save_yaml_config()
    text = cfg.config_path.read_text().replace("192.168.1.207", "192.168.1.208")
    cfg.config_path.write_text(text)                    # changed with the gateway stopped
    assert _tree(cfg).state(rev_of(text)) == "stale"
    booted = Config(str(cfg.config_path))
    now = booted.config_path.read_text()
    assert _tree(booted).state(rev_of(now)) == "current"
    assert _tree(booted).document() == yaml.safe_load(now)
    assert booted.config_status()["tree"]["state"] == "current"


def test_a_boot_completes_a_write_that_was_interrupted(tmp_path, monkeypatch):
    monkeypatch.setenv("MBG_CONFIG_TREE", "1")
    cfg = write_config(tmp_path, extra_yaml=PLANT_YAML)
    cfg.save_yaml_config()
    os.mkdir(_tree(cfg).d_new)                           # staging left by a crash
    booted = Config(str(cfg.config_path))
    tree = _tree(booted)
    assert not tree.d_new.exists()
    assert tree.state(rev_of(booted.config_path.read_text())) == "current"


def test_a_broken_base_or_index_is_an_assemble_error():
    from multibus.core.config.assemble import BASE, assemble, check
    with pytest.raises(AssembleError):
        assemble({BASE: ["not", "a", "mapping"]})
    with pytest.raises(AssembleError):
        check({BASE: {"_layout_order": {"devices": "abc"}}})
