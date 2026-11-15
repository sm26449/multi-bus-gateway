# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The running Virtual BMS instances follow the configuration (FINDINGS WK7-LIFE-1): a config
apply reconciles them, and an import/restore disarms every one of them before the new config
is loaded. `armed` is runtime state — never read from the file."""
import copy
import io
import zipfile

import yaml

from multibus.outputs.vbms_manager import VbmsInstance, VbmsManager
from tests.test_vbms_boot import _boot
from tests.test_vbms_manager import CONFIG, FakeTx, _load, _read, _ROWS


def _mgr(cfg=None):
    return VbmsManager(copy.deepcopy(cfg or CONFIG), _read, _load, tx_factory=FakeTx)


def _cfg(**over):
    c = copy.deepcopy(CONFIG)
    c["virtual_bms"][0].update(over)
    return c


# ── armed is never configuration ─────────────────────────────────────────────────────────

def test_an_armed_flag_in_the_file_arms_nothing():
    cfg = copy.deepcopy(CONFIG["virtual_bms"][0])
    cfg["armed"] = True
    assert VbmsInstance(cfg, _ROWS).armed is False


# ── reconcile ────────────────────────────────────────────────────────────────────────────

def test_an_unchanged_instance_is_left_alone_even_while_armed():
    m = _mgr()
    inst = m.instances["deye-vbms"]
    try:
        m.arm("deye-vbms")
        out = m.reconcile(copy.deepcopy(CONFIG))
        assert out == {"removed": [], "changed": [], "added": [], "kept": ["deye-vbms"]}
        assert m.instances["deye-vbms"] is inst and inst.armed and inst.running
    finally:
        m.stop_all()


def test_a_changed_instance_is_stopped_and_rebuilt_disarmed():
    m = _mgr()
    old = m.instances["deye-vbms"]
    try:
        m.arm("deye-vbms")
        out = m.reconcile(_cfg(packs=["seplos-adr1"]))
        new = m.instances["deye-vbms"]
        assert out["changed"] == ["deye-vbms"]
        assert new is not old and new.packs == ["seplos-adr1"]
        assert new.armed is False and old.running is False
    finally:
        m.stop_all()


def test_a_removed_instance_is_stopped_and_dropped_and_a_new_one_is_added_disarmed():
    m = _mgr()
    old = m.instances["deye-vbms"]
    try:
        m.arm("deye-vbms")
        other = copy.deepcopy(CONFIG)
        other["virtual_bms"][0]["id"] = "other"
        out = m.reconcile(other)
        assert out["removed"] == ["deye-vbms"] and out["added"] == ["other"]
        assert list(m.instances) == ["other"] and old.running is False
        assert m.instances["other"].armed is False
    finally:
        m.stop_all()


def test_a_config_that_cannot_be_built_leaves_no_stale_instance():
    def load(name):
        if name == "missing":
            raise ValueError("no such profile")
        return _ROWS
    m = VbmsManager(copy.deepcopy(CONFIG), _read, load, tx_factory=FakeTx)
    out = m.reconcile(_cfg(profile="missing"))
    assert out["removed"] == ["deye-vbms"] and m.instances == {}


# ── through the real app ─────────────────────────────────────────────────────────────────

def _armed(tmp_path):
    app, pub = _boot(tmp_path)
    mgr = app.state.vbms_manager
    mgr._tx_factory = FakeTx
    app.state.ctx.config.security.allow_writes = True
    mgr.arm('vb')
    assert mgr.instances['vb'].running
    return app, pub, mgr


def _bundle(doc):
    b = io.BytesIO()
    with zipfile.ZipFile(b, 'w') as z:
        z.writestr('config.yaml', yaml.safe_dump(doc))
    return b.getvalue()


def test_apply_drops_an_instance_the_config_no_longer_has(tmp_path):
    from fastapi.testclient import TestClient
    app, pub, mgr = _armed(tmp_path)
    old = mgr.instances['vb']
    cfg = app.state.ctx.config
    try:
        cfg.virtual_bms = []
        cfg.save_yaml_config()
        assert TestClient(app).post('/api/config/apply').status_code == 200
        assert mgr.instances == {} and old.running is False
        assert not [t for t in pub.topics if t.startswith("mbg/devices/vb/cmd/")]
    finally:
        mgr.stop_all()


def test_apply_leaves_an_unchanged_armed_instance_transmitting(tmp_path):
    from fastapi.testclient import TestClient
    app, _pub, mgr = _armed(tmp_path)
    inst = mgr.instances['vb']
    try:
        assert TestClient(app).post('/api/config/apply').status_code == 200
        assert mgr.instances['vb'] is inst and inst.armed and inst.running
    finally:
        mgr.stop_all()


def test_an_import_disarms_every_virtual_bms_even_when_its_config_is_unchanged(tmp_path):
    from fastapi.testclient import TestClient
    app, _pub, mgr = _armed(tmp_path)
    cfg = app.state.ctx.config
    try:
        cfg.save_yaml_config()
        doc = yaml.safe_load(cfg.config_path.read_text())
        r = TestClient(app).post('/api/config/import?lock_writes=false', content=_bundle(doc))
        assert r.status_code == 200, r.text
        assert 'vb' in mgr.instances
        assert mgr.instances['vb'].armed is False and mgr.instances['vb'].running is False
    finally:
        mgr.stop_all()


def test_an_import_that_removes_the_instance_removes_it_from_the_manager(tmp_path):
    from fastapi.testclient import TestClient
    app, pub, mgr = _armed(tmp_path)
    cfg = app.state.ctx.config
    old = mgr.instances['vb']
    try:
        cfg.save_yaml_config()
        doc = yaml.safe_load(cfg.config_path.read_text())
        doc['virtual_bms'] = []
        r = TestClient(app).post('/api/config/import?lock_writes=false', content=_bundle(doc))
        assert r.status_code == 200, r.text
        assert mgr.instances == {} and old.running is False
        assert not [t for t in pub.topics if t.startswith("mbg/devices/vb/cmd/")]
    finally:
        mgr.stop_all()


# ── WK7-TELE-1: retained telemetry is taken back when it stops being true ────────────────

class _Broker:
    """A publisher that remembers what is retained and lets it be cleared."""
    def __init__(self):
        self.retained = {}

    def publish_if_changed(self, topic, value, retain=True, heartbeat=None):
        changed = self.retained.get(topic) != value
        self.retained[topic] = value
        return changed

    def clear_topics(self, topics):
        for t in topics:
            self.retained.pop(t, None)
        return len(topics)


def test_a_field_whose_source_went_away_is_withdrawn_from_the_broker():
    from multibus.outputs.vbms_wiring import VbmsStatePublisher
    from tests.test_vbms_manager import STORE
    live = dict(STORE)
    t = [100.0]
    m = VbmsManager(copy.deepcopy(CONFIG), lambda d, f: live.get((d, f)), _load,
                    clock=lambda: t[0])
    broker = _Broker()
    sp = VbmsStatePublisher(m, lambda: broker)
    sp.publish_once()
    assert broker.retained["mbg/devices/deye-vbms/soc"] == 80
    live.clear()
    sp.publish_once()
    assert "mbg/devices/deye-vbms/soc" not in broker.retained
    assert "mbg/devices/deye-vbms/pack_voltage" not in broker.retained
    assert broker.retained["mbg/devices/deye-vbms/status"]["packs_live"] == 0     # the status stays
    assert broker.retained["mbg/devices/deye-vbms/charge_current_limit"] == 0     # so does what is sent
    live.update(STORE)
    sp.publish_once()                                    # dropped packs serve return_after_s first
    assert "mbg/devices/deye-vbms/soc" not in broker.retained                     # (R2D-VBMS-3)
    t[0] += 60
    sp.publish_once()
    assert broker.retained["mbg/devices/deye-vbms/soc"] == 80                     # back with the packs


def test_a_deleted_instance_leaves_nothing_retained():
    from multibus.outputs.vbms_wiring import VbmsStatePublisher
    m = _mgr()
    broker = _Broker()
    sp = VbmsStatePublisher(m, lambda: broker)
    sp.publish_once()
    assert any(t.startswith("mbg/devices/deye-vbms/") for t in broker.retained)
    m.remove("deye-vbms")
    sp.publish_once()
    assert not [t for t in broker.retained if t.startswith("mbg/devices/deye-vbms/")]


# ── WK7-PREVIEW-1 / WK7-UI-2: what the operator is shown ─────────────────────────────────

def test_the_preview_says_which_sent_field_each_pack_value_feeds():
    from tests.test_vbms_manager import STORE
    m = VbmsManager(copy.deepcopy(CONFIG), lambda d, f: STORE.get((d, f)), _load)
    pv = m.preview(m.instances["deye-vbms"])
    pack_keys = {k for p in pv["packs"] for k in p["values"]}
    assert "voltage" in pack_keys and "voltage" not in pv["sends"]         # different names…
    for key in pack_keys:                                                   # …joined by the map
        assert pv["sent_as"][key] in pv["sends"], key
    assert pv["sends"][pv["sent_as"]["voltage"]] == 53.45
    assert set(pv["control"]) == {"charge_current_limit", "discharge_current_limit",
                                  "charge_voltage_limit", "discharge_voltage_limit",
                                  "charge_enable", "discharge_enable"}
    assert pv["control"]["charge_current_limit"] == pv["sends"]["charge_current_limit"] == 20
    assert pv["data_state"] == "ok" and pv["min_packs"] == 1


def test_the_preview_is_one_snapshot_of_the_packs():
    """Every source value is read once: the per-pack columns and the sent sum cannot come
    from two different moments."""
    from tests.test_vbms_manager import STORE
    reads = []

    def read(dev, field):
        reads.append((dev, field))
        return STORE.get((dev, field))
    m = VbmsManager(copy.deepcopy(CONFIG), read, _load)
    reads.clear()
    m.preview(m.instances["deye-vbms"])
    assert len(reads) == len(set(reads))


def test_the_status_says_when_an_armed_instance_is_not_really_sending():
    m = VbmsManager(copy.deepcopy(CONFIG), lambda d, f: None, _load, tx_factory=FakeTx)
    st = m.status()["deye-vbms"]
    assert st["data_state"] in ("hold", "silent") and st["packs_live"] == 0 and st["min_packs"] == 1


def test_the_page_keeps_polling_after_a_failed_refresh_and_says_it_is_stale():
    """Source-level guard: the catch of the list fetch reschedules the
    poll and marks the cards stale instead of returning with a live-looking badge."""
    import pathlib
    import re
    js = (pathlib.Path(__file__).resolve().parent.parent / "ui" / "js" / "app-vbms.js").read_text()
    load = js[js.index("async _loadVbms("):js.index("_vbmsState(s)")]
    catch = load[load.index("} catch (e) {"):load.index("this._vbmsMarkStale(el, false)")]
    assert "this._vbmsSchedulePoll()" in catch and "_vbmsMarkStale(el, true)" in catch
    assert re.search(r"sent_as", js) and "pv.control" in js


# ── R2D-TELE-1: a withdrawal the broker never got is still owed ──────────────────────────

class _FlakyBroker(_Broker):
    connected = True

    def clear_topics(self, topics):
        if not self.connected:
            return 0                              # exactly what MQTTPublisher answers offline
        return super().clear_topics(topics)


def test_a_withdrawal_made_while_the_broker_is_away_is_retried_when_it_is_back():
    from multibus.outputs.vbms_wiring import VbmsStatePublisher
    from tests.test_vbms_manager import STORE
    live = dict(STORE)
    m = VbmsManager(copy.deepcopy(CONFIG), lambda d, f: live.get((d, f)), _load)
    broker = _FlakyBroker()
    sp = VbmsStatePublisher(m, lambda: broker)
    sp.publish_once()
    assert broker.retained["mbg/devices/deye-vbms/soc"] == 80
    broker.connected = False
    live.clear()
    sp.publish_once()
    assert "mbg/devices/deye-vbms/soc" in broker.retained       # could not be cleared yet
    broker.connected = True
    sp.publish_once()
    assert "mbg/devices/deye-vbms/soc" not in broker.retained   # owed, and now paid


# ── R2D-TELE-1 (the open half): the inventory outlives the process ───────────────────────

def _publisher(m, broker, path, **kw):
    from multibus.outputs.vbms_wiring import VbmsStatePublisher
    return VbmsStatePublisher(m, lambda: broker, inventory_path=path, **kw)


def test_an_instance_deleted_while_the_gateway_was_down_is_withdrawn_at_the_next_start(tmp_path):
    """The reviewer's probe, inverted: the first process retained the instance's values, the
    instance was deleted, a new process (empty memory) must still take them back."""
    from tests.test_vbms_manager import STORE
    inv = tmp_path / "vbms_retained.json"
    broker = _Broker()
    m = VbmsManager(copy.deepcopy(CONFIG), lambda d, f: STORE.get((d, f)), _load)
    _publisher(m, broker, inv).publish_once()
    assert "mbg/devices/deye-vbms/soc" in broker.retained and inv.exists()
    m.remove("deye-vbms")                                       # gone while nothing ran
    _publisher(m, broker, inv).publish_once()                   # a new process
    assert not [t for t in broker.retained if t.startswith("mbg/devices/deye-vbms/")], broker.retained
    import json
    assert json.loads(inv.read_text())["retained"] == {}        # and nothing is owed any more


def test_a_withdrawal_still_owed_at_shutdown_is_owed_after_the_restart(tmp_path):
    from tests.test_vbms_manager import STORE
    inv = tmp_path / "vbms_retained.json"
    broker = _FlakyBroker()
    m = VbmsManager(copy.deepcopy(CONFIG), lambda d, f: STORE.get((d, f)), _load)
    _publisher(m, broker, inv).publish_once()
    m.remove("deye-vbms")
    broker.connected = False
    _publisher(m, broker, inv).publish_once()                   # could not clear: owed…
    broker.connected = True
    _publisher(m, broker, inv).publish_once()                   # …by a third process
    assert not [t for t in broker.retained if t.startswith("mbg/devices/deye-vbms/")]


def test_the_inventory_is_on_disk_before_the_topics_are_retained(tmp_path):
    """Write-ahead: a crash right after a retained publish must not leave a topic the next
    process does not know about."""
    import json
    from tests.test_vbms_manager import STORE
    inv = tmp_path / "vbms_retained.json"
    seen = []

    class _Watching(_Broker):
        def publish_if_changed(self, topic, value, **kw):
            seen.append(topic in set(json.loads(inv.read_text())["retained"].get("deye-vbms", [])))
            return super().publish_if_changed(topic, value, **kw)
    m = VbmsManager(copy.deepcopy(CONFIG), lambda d, f: STORE.get((d, f)), _load)
    _publisher(m, _Watching(), inv).publish_once()
    assert seen and all(seen)


def test_a_damaged_or_foreign_inventory_never_clears_a_topic_outside_an_instance(tmp_path):
    """The file is read back defensively: a damaged file is an empty inventory, and only
    topics under a Virtual BMS base (no wildcard), filed under their own instance, are ever
    withdrawn."""
    import json
    inv = tmp_path / "vbms_retained.json"
    broker = _Broker()
    broker.retained = {"homeassistant/sensor/x/config": "keep", "mbg/devices/old/soc": 1,
                       "mbg/devices/old2/x": 2}
    inv.write_text(json.dumps({"retained": {"old": ["mbg/devices/old/soc", "homeassistant/sensor/x/config",
                                                    "mbg/devices/old/#", "mbg/devices/old2/x"],
                                            "Bad Id": ["mbg/devices/Bad Id/x"]}}))
    m = VbmsManager({"virtual_bms": []}, lambda d, f: None, _load)
    _publisher(m, broker, inv).publish_once()
    assert broker.retained == {"homeassistant/sensor/x/config": "keep", "mbg/devices/old2/x": 2}
    inv.write_text("{not json")
    _publisher(m, broker, inv).publish_once()                   # no crash, nothing cleared
    assert "homeassistant/sensor/x/config" in broker.retained


def test_an_oversized_inventory_is_not_read(tmp_path):
    import json

    from multibus.outputs.vbms_wiring import MAX_INVENTORY_BYTES
    inv = tmp_path / "vbms_retained.json"
    inv.write_text(json.dumps({"retained": {"old": ["mbg/devices/old/soc"]}}) + " " * MAX_INVENTORY_BYTES)
    broker = _Broker()
    broker.retained = {"mbg/devices/old/soc": 1}
    _publisher(VbmsManager({"virtual_bms": []}, lambda d, f: None, _load), broker, inv).publish_once()
    assert broker.retained == {"mbg/devices/old/soc": 1}


# ── review M1: a topic of a live device is never withdrawn ───────────────────────────────

def test_an_inventory_id_that_is_a_device_now_is_left_alone(tmp_path):
    """Deleted while the broker was away (owed, on disk), then its id given to a device: the
    next start must not clear the device's retained values."""
    from tests.test_vbms_manager import STORE
    inv = tmp_path / "vbms_retained.json"
    broker = _FlakyBroker()
    m = VbmsManager(copy.deepcopy(CONFIG), lambda d, f: STORE.get((d, f)), _load)
    _publisher(m, broker, inv).publish_once()
    m.remove("deye-vbms")
    broker.connected = False
    _publisher(m, broker, inv).publish_once()                   # owed, persisted
    broker.connected = True
    broker.retained["mbg/devices/deye-vbms/power"] = 5          # now the device's own values
    held = dict(broker.retained)
    _publisher(m, broker, inv, device_ids=lambda: ["deye-vbms"]).publish_once()
    assert broker.retained == held


def test_an_owed_clear_is_dropped_when_the_id_becomes_a_device_in_the_same_process(tmp_path):
    from tests.test_vbms_manager import STORE
    broker = _FlakyBroker()
    devices: list = []
    m = VbmsManager(copy.deepcopy(CONFIG), lambda d, f: STORE.get((d, f)), _load)
    sp = _publisher(m, broker, tmp_path / "inv.json", device_ids=lambda: devices)
    sp.publish_once()
    m.remove("deye-vbms")
    broker.connected = False
    sp.publish_once()
    devices.append("deye-vbms")                                 # a device takes the id
    broker.connected = True
    sp.publish_once()
    assert "mbg/devices/deye-vbms/soc" in broker.retained


# ── review M2: no write per field flap, a failing write said once ────────────────────────

def test_a_flapping_field_does_not_rewrite_the_inventory(tmp_path, monkeypatch):
    from multibus.outputs import vbms_wiring
    from tests.test_vbms_manager import STORE
    writes = []
    real = vbms_wiring.os.replace
    monkeypatch.setattr(vbms_wiring.os, "replace", lambda a, b: (writes.append(b), real(a, b)))
    live = dict(STORE)
    m = VbmsManager(copy.deepcopy(CONFIG), lambda d, f: live.get((d, f)), _load)
    sp = _publisher(m, _Broker(), tmp_path / "inv.json")
    sp.publish_once()
    assert len(writes) == 1
    for _ in range(4):                                          # every source comes and goes
        live.clear()
        sp.publish_once()
        live.update(STORE)
        sp.publish_once()
    assert len(writes) == 1


def test_an_unwritable_inventory_is_said_once(tmp_path, caplog):
    import logging
    from tests.test_vbms_manager import STORE
    (tmp_path / "file").write_text("x")
    m = VbmsManager(copy.deepcopy(CONFIG), lambda d, f: STORE.get((d, f)), _load)
    sp = _publisher(m, _Broker(), tmp_path / "file" / "inv.json")       # its dir is a file
    with caplog.at_level(logging.ERROR, logger="multibus.outputs.vbms_wiring"):
        for _ in range(5):
            sp.publish_once()
    assert len([r for r in caplog.records if "inventory" in r.getMessage()]) == 1


def test_the_inventory_is_bounded(tmp_path):
    import json

    from multibus.outputs.vbms_wiring import MAX_RETAINED_INVENTORY
    inv = tmp_path / "vbms_retained.json"
    many = [f"mbg/devices/old/f{i}" for i in range(MAX_RETAINED_INVENTORY + 50)]
    inv.write_text(json.dumps({"retained": {"old": many}}))
    broker = _Broker()
    broker.retained = {t: 1 for t in many}
    m = VbmsManager({"virtual_bms": []}, lambda d, f: None, _load)
    _publisher(m, broker, inv).publish_once()
    assert len(broker.retained) == 50                           # read up to the bound, no more
