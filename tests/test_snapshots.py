# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
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
"""Config snapshots: store, debounce, restore roundtrip, LKG boot seatbelt."""
import io
import json
import time
import zipfile

import pytest
import yaml

from multibus.snapshots import SnapshotStore, boot_seatbelt, write_bundle_files
from tests.test_devices import write_config
from tests.test_devices_api import needs_tc


@pytest.fixture
def store(tmp_path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text("modbus:\n  host: 1.2.3.4\n")
    (cfg_dir / "selected_registers.json").write_text('{"registers": []}')
    (cfg_dir / "virtual_meters.yaml").write_text("meters: []\n")
    tpl = cfg_dir / "device_templates"
    tpl.mkdir()
    (tpl / "my.json").write_text('{"id": "my"}')
    from multibus.config import PRIMARY_DEVICE_ID
    return SnapshotStore(
        cfg_dir, tpl, device_ids=lambda: [PRIMARY_DEVICE_ID, "dev2"],
        registers_path_for=lambda d: (cfg_dir / "selected_registers.json"
                                      if d == PRIMARY_DEVICE_ID
                                      else cfg_dir / "devices" / d / "selected_registers.json"),
        keep=3)


def test_bundle_contains_the_config_set(store):
    zf = zipfile.ZipFile(io.BytesIO(store.build_bundle_bytes()))
    names = set(zf.namelist())
    assert {"config.yaml", "virtual_meters.yaml", "manifest.json",
            "device_templates/my.json",
            "devices/umg512/selected_registers.json"} <= names
    # the rules are runtime state too (3.79.0: they were the one file the
    # bundle did not carry)
    (store.cfg_dir / "rules.yaml").write_text("rules: []\n")
    names = set(zipfile.ZipFile(io.BytesIO(store.build_bundle_bytes())).namelist())
    assert "rules.yaml" in names
    assert json.loads(zf.read("manifest.json"))["snapshot"] is True


def test_create_list_prune_delete(store):
    ids = [store.create(f"t{i}")["id"] for i in range(5)]
    listed = store.list()
    assert len(listed) == 3                       # keep=3, cele mai vechi 2 curățate
    assert [e["id"] for e in listed] == list(reversed(ids[-3:]))
    assert store.get_path(ids[0]) is None         # pruned
    assert store.get_path(ids[-1]) is not None
    assert store.delete(ids[-1]) is True
    assert store.delete(ids[-1]) is False
    assert store.get_path("../evil") is None      # id-uri doar alfanumerice


def test_schedule_coalesces_bursts(store):
    for i in range(10):
        store.schedule(f"POST /api/x{i}", delay_s=0.1)
    time.sleep(0.5)
    entries = store.list()
    assert len(entries) == 1                      # o rafală → UN snapshot
    assert entries[0]["trigger"] == "POST /api/x9"


def test_write_bundle_replace_vs_merge(store, tmp_path):
    cfg = store.cfg_dir / "config.yaml"
    snap = store.create("before")
    cfg.write_text("modbus:\n  host: 9.9.9.9\nnew_section:\n  added: true\n")
    zf = zipfile.ZipFile(store.get_path(snap["id"]))
    # merge: host revine, dar secțiunea adăugată SUPRAVIEȚUIEȘTE
    write_bundle_files(zf, cfg_dir=store.cfg_dir, user_tpl_dir=store.user_tpl_dir,
                       registers_path_for=store._registers_path_for, replace_config=False)
    merged = yaml.safe_load(cfg.read_text())
    assert merged["modbus"]["host"] == "1.2.3.4" and "new_section" in merged
    # replace: fișierul devine verbatim snapshot-ul (rollback adevărat)
    cfg.write_text("modbus:\n  host: 9.9.9.9\nnew_section:\n  added: true\n")
    write_bundle_files(zf, cfg_dir=store.cfg_dir, user_tpl_dir=store.user_tpl_dir,
                       registers_path_for=store._registers_path_for, replace_config=True)
    replaced = yaml.safe_load(cfg.read_text())
    assert replaced["modbus"]["host"] == "1.2.3.4" and "new_section" not in replaced


def test_merge_import_keeps_a_top_level_url_secret(tmp_path):
    """REFAC-URL-1: a sanitized export redacts secrets inside top-level URLs
    (rest_push.url, influxdb.url) to ``token=***``. A merge-import of that bundle
    must KEEP the live secret, not overwrite it with the mask — the same
    keep-what-it-was-not-told contract (ADR-0003) the device-level merge honours.
    Regression for the _deep_merge aliasing bug: merged IS the mutated live dict,
    so the top-level re-inject compared the already-overwritten url to itself."""
    from multibus.core.redact import redact_url
    original = 'http://192.168.1.2/ingest?token=private-value'
    (tmp_path / 'config.yaml').write_text(yaml.safe_dump({'rest_push': {'url': original}}))
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as z:
        z.writestr('config.yaml', yaml.safe_dump({'rest_push': {'url': redact_url(original)}}))
    with zipfile.ZipFile(io.BytesIO(data.getvalue())) as z:
        write_bundle_files(z, cfg_dir=tmp_path, user_tpl_dir=tmp_path / 'tpl',
                           registers_path_for=lambda d: tmp_path / d, replace_config=False)
    kept = yaml.safe_load((tmp_path / 'config.yaml').read_text())['rest_push']['url']
    assert kept == original


def test_apply_bundle_rolls_back_on_a_mid_write_failure(tmp_path, monkeypatch):
    """REFAC-TXN-1: apply_bundle/write_bundle_files is all-or-nothing. If a file's
    swap fails partway, every file already swapped in is rolled back, so a failed
    apply never leaves a half-written bundle. Here config.yaml must stay original
    after attributes.yaml's swap is made to fail."""
    import multibus.lifecycle.snapshots as snapshots
    from unittest.mock import Mock

    (tmp_path / 'config.yaml').write_text('mqtt: {topic_prefix: original}\n')
    (tmp_path / 'attributes.yaml').write_text('original attributes')
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as z:
        z.writestr('config.yaml', 'mqtt: {topic_prefix: changed}\n')
        z.writestr('attributes.yaml', '{}')
    original_replace = snapshots.os.replace

    def fail_on_attributes(src, dst):
        if str(dst).endswith('attributes.yaml'):
            raise OSError('simulated second-file failure')
        return original_replace(src, dst)

    monkeypatch.setattr(snapshots.os, 'replace', fail_on_attributes)
    reload = Mock()
    with zipfile.ZipFile(io.BytesIO(data.getvalue())) as z:
        with pytest.raises(OSError, match='second-file failure'):
            snapshots.apply_bundle(z, cfg_dir=tmp_path, user_tpl_dir=tmp_path / 'tpl',
                                   registers_path_for=lambda d: tmp_path / d, replace_config=True,
                                   allow_nonlan=False, snapshot_store=Mock(), pre_label='pre-restore',
                                   user='t', reload_from_disk=reload, apply=False, restore_identity=False)
    reload.assert_not_called()                                   # no reload after a failed write
    assert 'original' in (tmp_path / 'config.yaml').read_text()  # first file rolled back


def test_bundle_includes_and_restores_tombstones(store):
    """3.4.1: a deleted device's tombstone (devices/<id>/device.json) is in the
    bundle and restores, so export→restore keeps delete→restore recovery."""
    tomb = store.cfg_dir / "devices" / "ghost" / "device.json"
    tomb.parent.mkdir(parents=True, exist_ok=True)
    tomb.write_text('{"id": "ghost", "name": "deleted meter"}')
    snap = store.create("with-tombstone")
    names = set(zipfile.ZipFile(store.get_path(snap["id"])).namelist())
    assert "devices/ghost/device.json" in names
    # wipe it, then restore brings it back
    import shutil
    shutil.rmtree(store.cfg_dir / "devices" / "ghost")
    summary = write_bundle_files(zipfile.ZipFile(store.get_path(snap["id"])),
                                 cfg_dir=store.cfg_dir, user_tpl_dir=store.user_tpl_dir,
                                 registers_path_for=store._registers_path_for,
                                 replace_config=True)
    assert json.loads(tomb.read_text())["name"] == "deleted meter"
    assert summary.get("tombstones", 0) == 1


def test_passkeys_not_restored_on_merge_import(store):
    """3.4.1: passkeys.json is an auth-identity store — a merge-import
    (replace_config=False, the sanitized /api/config/import path) must NOT
    silently swap the passkey registry from a possibly-foreign backup. A full
    restore (replace_config=True, own trusted snapshot) still installs it."""
    # bundle carries an attacker-controlled passkeys.json
    (store.cfg_dir / "passkeys.json").write_text('{"creds": ["ATTACKER"]}')
    snap = store.create("with-passkeys")
    zf_path = store.get_path(snap["id"])
    # local box has its own passkeys
    (store.cfg_dir / "passkeys.json").write_text('{"creds": ["MINE"]}')

    # merge-import must NOT overwrite the local passkeys
    import zipfile as _zip
    summary = write_bundle_files(_zip.ZipFile(zf_path), cfg_dir=store.cfg_dir,
                                 user_tpl_dir=store.user_tpl_dir,
                                 registers_path_for=store._registers_path_for,
                                 replace_config=False)
    assert json.loads((store.cfg_dir / "passkeys.json").read_text())["creds"] == ["MINE"]
    assert any("passkeys" in s for s in summary.get("skipped", []))

    # full restore (own snapshot) DOES install it
    write_bundle_files(_zip.ZipFile(zf_path), cfg_dir=store.cfg_dir,
                       user_tpl_dir=store.user_tpl_dir,
                       registers_path_for=store._registers_path_for,
                       replace_config=True)
    assert json.loads((store.cfg_dir / "passkeys.json").read_text())["creds"] == ["ATTACKER"]


def test_primary_registers_restore_to_legacy_root(store):
    """Registrele primary-ului se restaurează în root-ul legacy, nu într-o
    copie moartă sub devices/ (bug-ul vechiului import)."""
    snap = store.create("s")
    root = store.cfg_dir / "selected_registers.json"
    root.write_text('{"registers": ["changed"]}')
    zf = zipfile.ZipFile(store.get_path(snap["id"]))
    write_bundle_files(zf, cfg_dir=store.cfg_dir, user_tpl_dir=store.user_tpl_dir,
                       registers_path_for=store._registers_path_for, replace_config=True)
    assert json.loads(root.read_text()) == {"registers": []}
    assert not (store.cfg_dir / "devices" / "umg512").exists()


def test_unsafe_paths_refused(store):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../../etc/passwd", "x")
    with pytest.raises(ValueError):
        write_bundle_files(zipfile.ZipFile(io.BytesIO(buf.getvalue())),
                           cfg_dir=store.cfg_dir, user_tpl_dir=store.user_tpl_dir,
                           registers_path_for=store._registers_path_for,
                           replace_config=True)


def test_boot_seatbelt_restores_lkg(store):
    cfg = store.cfg_dir / "config.yaml"
    store.mark_lkg()
    cfg.write_text("modbus: [broken yaml:::")     # config stricat

    def load():
        data = yaml.safe_load(cfg.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("modbus"), dict):
            raise ValueError("bad config")
        return data
    out = boot_seatbelt(str(cfg), load)
    assert out["modbus"]["host"] == "1.2.3.4"     # restaurat din LKG + reîncărcat


def test_boot_seatbelt_without_lkg_reraises(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(":::")
    with pytest.raises(ValueError):
        boot_seatbelt(str(cfg), lambda: (_ for _ in ()).throw(ValueError("bad")))


# ── API roundtrip ────────────────────────────────────────────────────────────

@pytest.fixture
def api(tmp_path):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from multibus.api import create_api
    cfg = write_config(tmp_path)
    fake = SimpleNamespace(publish_callback=None)
    app, _ = create_api(cfg, fake, None, None, devices=[(d, fake) for d in cfg.devices])
    return TestClient(app, raise_server_exceptions=False), cfg


@needs_tc
def test_api_snapshot_restore_roundtrip(api):
    client, cfg = api
    # baseline-ul de la primul boot există deja
    lst = client.get("/api/config/snapshots").json()["snapshots"]
    assert any(e["trigger"] == "baseline" for e in lst)

    # snapshot manual, apoi o schimbare de config prin API
    snap = client.post("/api/config/snapshots", json={"note": "înainte de test"}).json()["snapshot"]
    old_topic = yaml.safe_load(cfg.config_path.read_text())["mqtt"].get("topic_prefix", "")
    r = client.post("/api/config/mqtt", json={"topic_prefix": "changed/by/test"})
    assert r.status_code == 200
    assert yaml.safe_load(cfg.config_path.read_text())["mqtt"]["topic_prefix"] == "changed/by/test"

    # rollback → topic_prefix revine; pre-restore apare în listă
    r = client.post(f"/api/config/snapshots/{snap['id']}/restore?apply=false")
    assert r.status_code == 200, r.text
    assert yaml.safe_load(cfg.config_path.read_text())["mqtt"].get("topic_prefix", "") == old_topic
    lst = client.get("/api/config/snapshots").json()["snapshots"]
    assert any(e["trigger"] == "pre-restore" for e in lst)

    # download + delete + guard-uri
    # download is credential-gated even auth-off (full-fidelity secrets) → 403
    assert client.get(f"/api/config/snapshots/{snap['id']}/download").status_code == 403
    assert client.delete(f"/api/config/snapshots/{snap['id']}").status_code == 200
    assert client.post("/api/config/snapshots/nope/restore").status_code == 404
    assert client.delete("/api/config/snapshots/lkg").status_code == 400


@needs_tc
def test_api_mutation_triggers_auto_snapshot(api):
    client, cfg = api
    before = len(client.get("/api/config/snapshots").json()["snapshots"])
    r = client.post("/api/config/mqtt", json={"topic_prefix": "auto/snap"})
    assert r.status_code == 200
    time.sleep(2.6)                               # debounce-ul de 2s
    after = client.get("/api/config/snapshots").json()["snapshots"]
    assert len(after) == before + 1
    assert after[0]["trigger"].startswith("POST /api/config/mqtt")


# ── semantic diff ────────────────────────────────────────────────────────────

def test_diff_bundles_semantics(store):
    from multibus.snapshots import diff_bundles
    snap_a = store.build_bundle_bytes()
    # schimb: valoare modificată, secret rotit, registru adăugat, fișier șters
    (store.cfg_dir / "config.yaml").write_text(
        "modbus:\n  host: 9.9.9.9\nmqtt:\n  password: nou-secret\n")
    (store.cfg_dir / "selected_registers.json").write_text(
        '{"registers": [{"address": 19026, "name": "freq"}]}')
    (store.cfg_dir / "virtual_meters.yaml").unlink()
    snap_b = store.build_bundle_bytes()
    d = diff_bundles(snap_a, snap_b)
    files = {f["name"]: f for f in d["files"]}

    cfg = files["config.yaml"]["changes"]
    by_path = {c["path"]: c for c in cfg}
    assert by_path["modbus.host"]["kind"] == "changed" and "9.9.9.9" in by_path["modbus.host"]["new"]
    # secțiune nouă = subtree adăugat; secretul din el e DEEP-mascat
    assert by_path["mqtt"]["kind"] == "added"
    assert "nou-secret" not in json.dumps(cfg) and "***" in by_path["mqtt"]["new"]

    regs = files["devices/umg512/selected_registers.json"]["changes"]
    assert any(c["kind"] == "added" and "address=19026" in c["path"] for c in regs)
    assert files["virtual_meters.yaml"]["status"] == "removed"
    assert d["totals"]["files_changed"] == 3


def test_diff_identical_is_empty(store):
    from multibus.snapshots import diff_bundles
    b = store.build_bundle_bytes()
    d = diff_bundles(b, b)
    assert d["totals"]["files_changed"] == 0 and d["totals"]["changes"] == 0


@needs_tc
def test_diff_api(api):
    client, cfg = api
    snap = client.post("/api/config/snapshots", json={}).json()["snapshot"]
    r = client.get(f"/api/config/snapshots/{snap['id']}/diff")
    assert r.status_code == 200
    assert r.json()["totals"]["files_changed"] == 0        # nimic schimbat încă
    client.post("/api/config/mqtt", json={"topic_prefix": "diff/test"})
    d = client.get(f"/api/config/snapshots/{snap['id']}/diff").json()
    assert d["totals"]["files_changed"] == 1
    cfgch = next(f for f in d["files"] if f["name"] == "config.yaml")
    assert any("topic_prefix" in c["path"] and "diff/test" in str(c.get("new", ""))
               for c in cfgch["changes"])
    assert client.get("/api/config/snapshots/nope/diff").status_code == 404
    assert client.get(f"/api/config/snapshots/{snap['id']}/diff?against=nope").status_code == 404


# ── P0: snapshot download gates unconditionally (auth-off must not leak secrets) ──

@needs_tc
def test_snapshot_download_refused_on_open_box(api):
    """On an auth-off box with no API key, a full-fidelity snapshot download
    (contains secrets) must be REFUSED — the old guard vanished when auth was
    off, leaking MQTT/Influx creds to any LAN peer."""
    client, _cfg = api
    snap = client.post("/api/config/snapshots", json={}).json()["snapshot"]
    r = client.get(f"/api/config/snapshots/{snap['id']}/download")
    assert r.status_code == 403                      # no admin, no key → refused


@needs_tc
def test_snapshot_download_allowed_with_api_key(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from multibus.api import create_api
    from tests.test_devices import write_config
    monkeypatch.setenv("API_KEY", "k")
    cfg = write_config(tmp_path)
    fake = SimpleNamespace(publish_callback=None)
    app, _ = create_api(cfg, fake, None, None, devices=[(d, fake) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    snap = client.post("/api/config/snapshots", json={},
                       headers={"X-API-Key": "k"}).json()["snapshot"]
    assert client.get(f"/api/config/snapshots/{snap['id']}/download").status_code == 403
    assert client.get(f"/api/config/snapshots/{snap['id']}/download",
                      headers={"X-API-Key": "k"}).status_code == 200


def test_boot_seatbelt_does_not_roll_back_on_a_non_file_error(store):
    """F-26 (3.83.0): only a config that cannot be parsed or validated is the
    LKG's business — an OSError (read-only volume, full disk) or a bug must
    not restore months-old credentials and settings."""
    cfg = store.cfg_dir / "config.yaml"
    store.mark_lkg()
    cfg.write_text("modbus: {host: 9.9.9.9}")
    with pytest.raises(OSError):
        boot_seatbelt(str(cfg), lambda: (_ for _ in ()).throw(OSError("read-only file system")))
    assert "9.9.9.9" in cfg.read_text()            # untouched


def test_apply_bundle_reload_failure_is_500_not_422(monkeypatch, tmp_path):
    """H1 / REFAC-S6-1: a bundle whose files are WRITTEN but whose reload then
    fails (e.g. a restored register file with a bad value) is a half-applied
    config → BundleAppliedReloadFailed (the routes map it to 500), NOT a
    BundleInvalid/ValueError (→422 'nothing happened'). The pre-op snapshot is
    taken first and its label travels on the exception for rollback."""
    from multibus.lifecycle import snapshots as S
    monkeypatch.setattr(S, "validate_bundle", lambda zf, allow_nonlan=False, **kw: [])
    monkeypatch.setattr(S, "write_bundle_files", lambda zf, **k: {"files": 1})
    created = []

    class _Store:
        def create(self, label, user=""):
            created.append(label)

    def _reload_boom(apply, restore_identity=False):
        raise ValueError("register iu0: interval 'x' is not a number")

    with pytest.raises(S.BundleAppliedReloadFailed) as ei:
        S.apply_bundle(None, cfg_dir=tmp_path, user_tpl_dir=tmp_path,
                       registers_path_for=lambda d: tmp_path, replace_config=True,
                       allow_nonlan=False, snapshot_store=_Store(), pre_label="pre-restore",
                       user="t", reload_from_disk=_reload_boom, apply=True, restore_identity=False)
    assert ei.value.pre_label == "pre-restore"
    assert created == ["pre-restore"]                 # pre-op snapshot taken before the write
    assert not isinstance(ei.value, ValueError)       # routes' `except ValueError` must not 422 it


def test_apply_bundle_validation_failure_writes_nothing(monkeypatch, tmp_path):
    """The other side: a bundle that fails validation raises BundleInvalid BEFORE
    any pre-op snapshot or write — 422, nothing on disk."""
    from multibus.lifecycle import snapshots as S
    monkeypatch.setattr(S, "validate_bundle", lambda zf, allow_nonlan=False, **kw: ["bad thing"])
    wrote = []
    monkeypatch.setattr(S, "write_bundle_files", lambda zf, **k: wrote.append(1))
    created = []

    class _Store:
        def create(self, label, user=""):
            created.append(label)

    with pytest.raises(S.BundleInvalid) as ei:
        S.apply_bundle(None, cfg_dir=tmp_path, user_tpl_dir=tmp_path,
                       registers_path_for=lambda d: tmp_path, replace_config=True,
                       allow_nonlan=False, snapshot_store=_Store(), pre_label="pre-restore",
                       user="t", reload_from_disk=lambda *a, **k: False, apply=True, restore_identity=False)
    assert ei.value.errors == ["bad thing"] and created == [] and wrote == []


def test_apply_bundle_fails_closed_when_preop_snapshot_fails(monkeypatch, tmp_path):
    """C3: if the pre-op (undo) snapshot cannot be taken, the apply ABORTS before
    any write — nothing on disk — raising PreopSnapshotFailed (routes → 500). A
    half-applied bundle with no way back is never acceptable."""
    from multibus.lifecycle import snapshots as S
    monkeypatch.setattr(S, "validate_bundle", lambda zf, allow_nonlan=False, **kw: [])
    wrote = []
    monkeypatch.setattr(S, "write_bundle_files", lambda zf, **k: (wrote.append(1), {"files": 1})[1])

    class _Store:
        def create(self, label, user=""):
            raise OSError("snapshots dir is read-only")

    with pytest.raises(S.PreopSnapshotFailed) as ei:
        S.apply_bundle(None, cfg_dir=tmp_path, user_tpl_dir=tmp_path,
                       registers_path_for=lambda d: tmp_path, replace_config=True,
                       allow_nonlan=False, snapshot_store=_Store(), pre_label="pre-restore",
                       user="t", reload_from_disk=lambda *a, **k: False, apply=True, restore_identity=False)
    assert ei.value.pre_label == "pre-restore"
    assert wrote == []                               # fail-closed: nothing written
    assert not isinstance(ei.value, ValueError)      # routes must not 422 it


# ── F20 (ADR-0041 L2): the write-lock disclosure walker ──────────────────────
from multibus.snapshots import write_lock_disclosure
from types import SimpleNamespace


def _dev(**kw):
    kw.setdefault('write_locked', False); kw.setdefault('component_of', '')
    kw.setdefault('endpoint_id', ''); kw.setdefault('commands', [])
    return kw


def test_disclosure_counts_unlocked_write_paths_by_kind():
    devices = [
        _dev(id='a'),                                   # unlocked top-level device
        _dev(id='b', write_locked=True),                # locked → not counted
        _dev(id='c', component_of='a'),                 # unlocked component
        _dev(id='u1', endpoint_id='plant'),             # unlocked endpoint unit
        _dev(id='u2', endpoint_id='plant', write_locked=True),  # locked unit → not counted
    ]
    d = write_lock_disclosure(devices, is_restore=False)
    assert (d['devices'], d['components'], d['units']) == (1, 1, 1)
    assert d['nothing'] is False


def test_disclosure_counts_enabled_bindings_only_on_unlocked_paths():
    devices = [
        _dev(id='a', commands=[{'name': 'x', 'enabled': True}, {'name': 'y', 'enabled': False}]),
        _dev(id='b', write_locked=True, commands=[{'name': 'z', 'enabled': True}]),  # locked: binding moot
    ]
    d = write_lock_disclosure(devices, is_restore=False)
    assert d['bindings'] == 1            # only the enabled binding on the UNLOCKED device


def test_disclosure_armed_rules_are_restore_only():
    rules = [SimpleNamespace(mode='armed', enabled=True),
             SimpleNamespace(mode='armed', enabled=False),   # disabled → not counted
             SimpleNamespace(mode='shadow', enabled=True)]   # shadow → not counted
    # import: rules.yaml is never in the bundle → R is 0 even if rules are passed
    assert write_lock_disclosure([], rules, is_restore=False)['rules'] == 0
    # restore: one armed+enabled rule
    assert write_lock_disclosure([], rules, is_restore=True)['rules'] == 1


def test_disclosure_nothing_unlocked_is_reassuring():
    devices = [_dev(id='a', write_locked=True),
               _dev(id='b', write_locked=True, commands=[{'name': 'z', 'enabled': True}])]
    d = write_lock_disclosure(devices, is_restore=True)
    assert d == {'devices': 0, 'components': 0, 'units': 0, 'bindings': 0,
                 'rules': 0, 'total': 0, 'nothing': True}


def test_force_lock_all_writes_locks_every_path(tmp_path):
    """F20 L2b: a bundle with unlocked write paths restores LOCKED — the config
    setter forces write_locked on the primary and every devices[] entry, idempotent."""
    from multibus.config import Config
    write_config(tmp_path, extra_yaml="""
security:
  primary_write_locked: false
devices:
  - id: extra
    name: Extra
    protocol: tcp
    connection: {host: 192.168.1.50, port: 502, unit_id: 2}
    write_locked: false
""")
    cfg = Config(str(tmp_path / "config.yaml"))
    assert any(not d.write_locked for d in cfg.devices)        # unlocked before
    n = cfg.force_lock_all_writes()
    assert n >= 1
    assert all(d.write_locked for d in cfg.devices)            # every path locked after
    assert cfg.security.primary_write_locked is True
    assert cfg.force_lock_all_writes() == 0                    # idempotent
    # and it persisted: a fresh load reads locked
    cfg2 = Config(str(tmp_path / "config.yaml"))
    assert all(d.write_locked for d in cfg2.devices)


def _bundle_bytes(config_yaml, rules_yaml=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("config.yaml", config_yaml)
        if rules_yaml is not None:
            z.writestr("rules.yaml", rules_yaml)
    return buf.getvalue()


def test_bundle_write_disclosure_counts_from_the_bundle_without_applying():
    from multibus.snapshots import bundle_write_disclosure
    cfg_yaml = """
modbus: {host: 192.168.1.207, port: 502, unit_id: 1}
mqtt: {enabled: false, broker: m, topic_prefix: a}
influxdb: {enabled: false, url: 'http://x', bucket: b}
security: {primary_write_locked: false}
devices:
  - id: extra
    name: Extra
    protocol: tcp
    connection: {host: 192.168.1.50, port: 502, unit_id: 2}
    write_locked: false
"""
    rules_yaml = "rules:\n  - {id: r1, mode: armed, enabled: true, kind: steps}\n  - {id: r2, mode: shadow, enabled: true, kind: steps}\n"
    # restore: config unlocked paths counted + the one armed rule
    d = bundle_write_disclosure(_bundle_bytes(cfg_yaml, rules_yaml), is_restore=True)
    assert d['devices'] >= 1 and d['rules'] == 1 and d['nothing'] is False
    # import: same config, but rules.yaml is never in an import bundle -> R omitted
    d2 = bundle_write_disclosure(_bundle_bytes(cfg_yaml), is_restore=False)
    assert d2['devices'] >= 1 and d2['rules'] == 0


@needs_tc
def test_f20_restore_forces_the_write_lock_on_the_running_devices(tmp_path):
    """F20 L2 (the blocker): restoring a snapshot whose primary is UNLOCKED brings
    it back LOCKED by default, and the RUNNING registry reads locked at once (no
    restart) — GET /api/devices reflects it. The disclosure says what would unlock.
    lock_writes=false keeps the snapshot's own states; dry_run applies nothing."""
    from fastapi.testclient import TestClient
    from multibus.api import create_api
    cfg = write_config(tmp_path, extra_yaml="security:\n  primary_write_locked: false\n")
    fake = SimpleNamespace(publish_callback=None)
    app, _ = create_api(cfg, fake, None, None, devices=[(d, fake) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)

    def primary_locked():
        # read the RUNNING registry copy (what the write gate reads) — not just config
        _i, c, _cl = app.state.registry.find(cfg.primary_id)
        return c.write_locked

    assert primary_locked() is False                       # unlocked to begin with
    snap = client.post("/api/config/snapshots", json={"note": "unlocked"}).json()["snapshot"]

    # dry-run: discloses what would unlock, changes nothing
    dr = client.post(f"/api/config/snapshots/{snap['id']}/restore?dry_run=true").json()
    assert dr["status"] == "dry-run" and dr["would_unlock"]["devices"] >= 1
    assert primary_locked() is False                       # not applied

    # real restore, default lock_writes=true → comes back LOCKED on the running device
    r = client.post(f"/api/config/snapshots/{snap['id']}/restore?apply=false")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["write_locked_forced"] is True and body["would_unlock"]["devices"] >= 1
    assert primary_locked() is True                        # the running registry reads LOCKED now

    # opt out → the snapshot's own (unlocked) state is restored
    r = client.post(f"/api/config/snapshots/{snap['id']}/restore?apply=false&lock_writes=false")
    assert r.status_code == 200, r.text
    assert r.json()["write_locked_forced"] is False
    assert primary_locked() is False
