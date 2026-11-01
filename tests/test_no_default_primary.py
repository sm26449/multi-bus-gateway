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
"""A fresh install starts with no device (ADR-0024).

Until 3.95 the gateway synthesized a Janitza UMG 512-PRO (id ``umg512``) from
the flat ``modbus:``/``mqtt:`` sections on EVERY configuration — a fresh
install, a CAN-only bench — and published it: its ``<prefix>/meta`` (kind
meter), its HA device, its Influx tag. Not everyone has one.

Now the legacy primary exists only where a configuration declares it:
``primary_device: true``, or an older file that holds a ``modbus:`` section
(an install that has it). Every implicit default such an install relied on is
kept, and a save writes it out explicitly.

The production-shaped file is the proof of compatibility: loading it gives the
same devices, topics, meta, Influx tags, HA names and saved file as before the
change (the golden was generated on main before any code moved; regenerate
only on purpose with ``MBG_REGEN_GOLDEN=1``, and only on main).
"""
import hashlib
import json
import os
import pathlib
import tarfile

import pytest
import yaml

from multibus.config import Config

FIX = pathlib.Path(__file__).parent / "fixtures"
# a production configuration directory (3.93.0): config.yaml, every device's
# register selection (calculated registers included), user templates, the
# virtual meters, the attributes — secrets redacted, TEST-NET addresses, a
# moved site location, no backups. The legacy primary comes from the flat
# sections, with no `primary_device:` key.
PROD_DIR = FIX / "prod_shaped_cfg.tar.gz"
# per-section sha256 of what main published from it (generated on main
# c5a0f1d before the change; the sections are big, the hashes are not)
GOLDEN = FIX / "prod_shaped_cfg.golden.json"

try:
    from fastapi.testclient import TestClient  # noqa: F401
    _HAS_TC = True
except Exception:  # noqa: BLE001
    _HAS_TC = False
needs_tc = pytest.mark.skipif(not _HAS_TC, reason="TestClient not installed")

# one primary register, as an install that has the meter selects it
_PRIMARY_SELECTION = {
    "version": "1.0",
    "registers": [{"address": 19000, "name": "_G_ULN[0]", "label": "Voltage L1-N",
                   "unit": "V", "data_type": "float", "poll_group": "realtime",
                   "mqtt": {"enabled": True, "topic": "voltage/l1_n"},
                   "influxdb": {"enabled": True, "measurement": "voltage"}}],
    "poll_groups": {"realtime": {"interval": 1}},
}


class _Pub:
    """Just enough MQTT publisher for create_api: everything it sends lands
    in ``sent``."""
    connected = True
    connects = 1

    def __init__(self):
        self.sent, self.topics, self.discovery_hooks = [], {}, []
        self.client = self

    def publish(self, topic, payload, qos=0, retain=False):
        self.sent.append((topic, payload, retain))

    def _publish(self, topic, payload, retain=None):
        self.sent.append((topic, payload, retain))
        return True

    def subscribe(self, topic):
        pass

    def unsubscribe(self, topic):
        pass

    def register_command(self, topic, fn):
        self.topics[topic] = fn

    def unregister_command(self, topic):
        self.topics.pop(topic, None)

    def unregister_commands(self, prefix):
        self.topics = {t: f for t, f in self.topics.items() if not t.startswith(prefix)}

    def set_command_write_handler(self, fn):
        self.handler = fn

    def __getattr__(self, name):            # the rest of the surface: no-ops
        return lambda *a, **k: None


def _load(tmp_path, text, selection=None):
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
    if selection is not None:
        (tmp_path / "selected_registers.json").write_text(json.dumps(selection), encoding="utf-8")
    return Config(str(tmp_path / "config.yaml"))


def _app(cfg):
    from multibus.api import create_api
    pub = _Pub()
    app, _ = create_api(cfg, None, pub, None, devices=[(d, None) for d in cfg.devices])
    return app, pub


def _meta(cfg, app_pub=None):
    """Every retained meta topic the gateway publishes, without its rev."""
    app, pub = app_pub or _app(cfg)
    pub.sent.clear()
    app.state.publish_attribute_meta(force=True)
    out = {}
    for topic, payload, retain in pub.sent:
        if topic.endswith("/meta") and payload:
            body = json.loads(payload)
            body.pop("rev", None)
            out[topic] = [body, retain]
    return out


def _ha(cfg, vmeters=None):
    """What Home Assistant is told: the primary's device and sensors, every
    other device with its own selection, the virtual meters."""
    from multibus.mqtt_publisher import MQTTPublisher
    mcfg = cfg.mqtt
    pub = MQTTPublisher.__new__(MQTTPublisher)
    MQTTPublisher.__init__(pub, type(mcfg)(**{**mcfg.__dict__, "enabled": False}),
                           cfg.selected_registers)
    pub.config = mcfg
    sent = []
    pub.connected = True
    pub._publish = lambda t, p, retain=None: (sent.append((t, p, retain)), True)[1]
    if hasattr(pub, "has_primary"):
        pub.has_primary = cfg.primary_device is not None     # what main.py hands it
    pub.publish_ha_discovery()
    for d in cfg.devices:
        if d.primary:
            continue
        try:
            regs = cfg.load_device_registers(d)[0]
        except Exception:  # noqa: BLE001 — a device without a file announces none
            regs = []
        pub.publish_device_discovery(d.id, d.name, d.mqtt_topic_prefix, regs or [])
    pub.publish_vmeter_discovery(vmeters or [{"id": "em24", "name": "EM24"}])
    out = {}
    for t, p, r in sent:
        body = json.loads(p) if p else None
        if isinstance(body, dict) and isinstance(body.get("device"), dict):
            body["device"].pop("sw_version", None)
        out[t] = [body, r]
    return out


def _unpack(tmp_path, legacy_defaults=False):
    with tarfile.open(PROD_DIR) as tf:
        tf.extractall(tmp_path)  # noqa: S202 — our own fixture
    if legacy_defaults:
        # an older file that never named them: the old defaults must hold
        raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
        raw["mqtt"].pop("topic_prefix")
        raw["mqtt"]["ha_discovery"].pop("device_name")
        (tmp_path / "config.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    return Config(str(tmp_path / "config.yaml"))


def _fingerprint(tmp_path, legacy_defaults=False):
    from fastapi.testclient import TestClient
    from multibus.influxdb_publisher import get_tags
    from multibus.mqtt_publisher import MQTTPublisher
    cfg = _unpack(tmp_path, legacy_defaults)
    keys = ("id", "name", "template", "primary", "enabled", "mqtt_topic_prefix", "mqtt_enabled",
            "ha_discovery_enabled", "influxdb_bucket", "influxdb_enabled", "influxdb_device_tag",
            "endpoint_id", "component_of")
    devices = [{k: getattr(d, k, None) for k in keys} for d in cfg.devices]
    tag_of = {d.id: (None if d.primary else d.influxdb_device_tag) for d in cfg.devices}
    sample = cfg.selected_registers[0]
    out = {
        "devices": devices,
        "influx_tags": {did: get_tags(sample, t) for did, t in tag_of.items()},
        "mqtt": {"topic_prefix": cfg.mqtt.topic_prefix, "ha_device_name": cfg.mqtt.ha_device_name,
                 "ha_discovery_prefix": cfg.mqtt.ha_discovery_prefix},
        "primary_topics": [MQTTPublisher._build_topic(type("P", (), {"config": cfg.mqtt})(), r)
                           for r in cfg.selected_registers],
        "primary_tags": [get_tags(r, None) for r in cfg.selected_registers],
    }
    vm = yaml.safe_load((tmp_path / "virtual_meters.yaml").read_text())
    out["ha"] = _ha(cfg, [{"id": i["template"], "name": i["template"]} for i in vm["instances"]])
    app_pub = _app(cfg)
    out["meta"] = _meta(cfg, app_pub)
    # ADR-0027: a device's meta gains its type and the type's words (additive)
    for body, _retain in out["meta"].values():
        if "type" in body:
            assert body.pop("type") and body.pop("type_label")
    # the register lists the UI and the dashboards read, calculated rows
    # included — the primary's by its default route, every device by id
    cl = TestClient(app_pub[0], raise_server_exceptions=False)
    out["registers_api"] = {did: cl.get("/api/registers/selected",
                                        params={"device": did} if did else {}).json()
                            for did in [""] + [d.id for d in cfg.devices]}
    out["calculated"] = {d.id: cfg.load_calculated(d.id) for d in cfg.devices}
    before = (tmp_path / "selected_registers.json").read_bytes()
    cfg.save_yaml_config()
    saved = yaml.safe_load((tmp_path / "config.yaml").read_text())
    saved.pop("config_version", None)
    out["saved_primary_device"] = saved.pop("primary_device", "ABSENT")
    # ADR-0026 §3: a save writes the scope out (an existing install: legacy)
    out["saved_identity_scope"] = saved.pop("identity_scope", "ABSENT")
    # ADR-0029: the InfluxDB heartbeat is written into a file without it
    out["saved_influx_heartbeat"] = (saved.get("influxdb") or {}).pop("heartbeat_interval", "ABSENT")
    # ADR-0031 §5: and the read preview's budget, in its own section
    out["saved_preview_budget"] = saved.pop("preview", "ABSENT")
    out["saved"] = saved
    out["selection_untouched"] = (tmp_path / "selected_registers.json").read_bytes() == before
    return json.loads(json.dumps(out, default=str, sort_keys=True))


def _digest(section) -> str:
    return hashlib.sha256(json.dumps(section, sort_keys=True).encode()).hexdigest()


@needs_tc
@pytest.mark.skipif(not PROD_DIR.exists(), reason="the production-shaped fixture is private (not in the public export)")
@pytest.mark.parametrize("variant", ["as_saved", "legacy_defaults"])
def test_prod_config_publishes_exactly_what_it_did(tmp_path, variant):
    got = _fingerprint(tmp_path, legacy_defaults=(variant == "legacy_defaults"))
    # the two things a save adds: the primary and the identity scope,
    # written out explicitly
    marker = got.pop("saved_primary_device")
    scope = got.pop("saved_identity_scope")
    # ADR-0029: and the InfluxDB heartbeat's starting value, written in
    assert got.pop("saved_influx_heartbeat") == 600
    assert got.pop("saved_preview_budget") == {"max_requests": 4, "listen_s": 10}
    digests = {k: _digest(v) for k, v in got.items()}
    counts = {"devices": len(got["devices"]), "ha": len(got["ha"]), "meta": len(got["meta"]),
              "primary_topics": len(got["primary_topics"]),
              "calculated": sum(len(v) for v in got["calculated"].values())}
    golden = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
    if os.environ.get("MBG_REGEN_GOLDEN"):
        golden[variant] = {"digests": digests, "counts": counts}
        GOLDEN.write_text(json.dumps(golden, indent=1, sort_keys=True) + "\n")
    want = golden[variant]
    assert counts == want["counts"]
    changed = sorted(k for k in set(want["digests"]) | set(digests)
                     if want["digests"].get(k) != digests.get(k))
    if changed:   # leave the evidence beside the test's tmp dir
        (tmp_path / "fingerprint.json").write_text(json.dumps(got, indent=1, sort_keys=True))
    assert changed == [], f"differs from main: {changed} (see {tmp_path}/fingerprint.json)"
    assert marker is True
    assert scope == "legacy" and "gateway" not in got["saved"]
    if variant == "legacy_defaults":
        assert got["saved"]["mqtt"]["topic_prefix"] == "multibus/umg512"
        assert got["saved"]["mqtt"]["ha_discovery"]["device_name"] == "Janitza UMG 512-PRO"


# ── a fresh install, a CAN-only bench: no device #1, nothing Janitza ──────────

CAN_ONLY = """
mqtt:
  broker: mosquitto
devices:
  - id: bms-can
    template: pylontech_can_inverter
    connection: {protocol: can, carrier: socketcan, channel: can0, bitrate: 500000}
"""


def _no_janitza(where, blob):
    text = blob if isinstance(blob, str) else json.dumps(blob, default=str)
    text = text.lower()
    assert "umg512" not in text and "janitza" not in text, (where, text[:400])


@needs_tc
def test_a_fresh_install_has_no_device_and_names_no_janitza(tmp_path):
    cfg = Config(str(tmp_path / "config.yaml"))            # no file: first boot
    assert cfg.devices == [] and cfg.primary_device is None and cfg.primary_id == ""
    assert cfg.selected_registers == []
    _no_janitza("mqtt", {"prefix": cfg.mqtt.topic_prefix, "ha": cfg.mqtt.ha_device_name})
    meta = _meta(cfg)
    assert list(meta) == [f"{cfg.mqtt.topic_prefix}/site/meta"]   # the site, no meter
    _no_janitza("meta", meta)
    _no_janitza("ha", _ha(cfg))
    cfg.save_yaml_config()
    text = (tmp_path / "config.yaml").read_text()
    assert yaml.safe_load(text)["primary_device"] is False
    _no_janitza("saved config.yaml", text)
    again = Config(str(tmp_path / "config.yaml"))           # a saved fresh file stays fresh
    assert again.devices == [] and again.mqtt.topic_prefix == cfg.mqtt.topic_prefix


@needs_tc
def test_a_can_only_config_publishes_no_phantom_meter(tmp_path):
    cfg = _load(tmp_path, CAN_ONLY)
    assert [d.id for d in cfg.devices] == ["bms-can"] and cfg.primary_device is None
    meta = _meta(cfg)
    assert sorted(meta) == sorted([f"{cfg.mqtt.topic_prefix}/site/meta",
                                   f"{cfg.get_device('bms-can').mqtt_topic_prefix}/meta"])
    assert f"{cfg.mqtt.topic_prefix}/meta" not in meta      # the phantom meter's topic
    _no_janitza("meta", meta)
    ha = _ha(cfg)
    _no_janitza("ha", ha)
    assert ha and all("via_device" not in ((b or {}).get("device") or {}) for b, _r in ha.values())


def test_a_selection_file_without_a_primary_is_not_announced(tmp_path):
    """A leftover selected_registers.json on a gateway with no device #1 is
    kept on disk but never read into a meter that is not there."""
    cfg = _load(tmp_path, CAN_ONLY, selection=_PRIMARY_SELECTION)
    assert cfg.selected_registers == []
    assert (tmp_path / "selected_registers.json").exists()


def test_an_older_install_keeps_its_primary_and_its_old_names(tmp_path):
    """No `primary_device:` key, a `modbus:` section, no prefix or HA name:
    exactly what the old defaults gave — and a save writes them out."""
    cfg = _load(tmp_path, "modbus:\n  host: 192.0.2.7\nmqtt:\n  broker: mosquitto\n")
    prim = cfg.primary_device
    assert prim is not None and prim.id == "umg512" and prim.template == "janitza_umg512_pro"
    assert prim.influxdb_device_tag == "janitza_umg512"
    assert cfg.mqtt.topic_prefix == "multibus/umg512" == prim.mqtt_topic_prefix
    assert cfg.mqtt.ha_device_name == "Janitza UMG 512-PRO"
    cfg.save_yaml_config()
    saved = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert saved["primary_device"] is True
    assert saved["mqtt"]["topic_prefix"] == "multibus/umg512"
    assert saved["mqtt"]["ha_discovery"]["device_name"] == "Janitza UMG 512-PRO"


def test_primary_device_false_removes_device_1_from_a_saved_bench_file(tmp_path):
    """A bench file an older gateway saved carries `modbus:` with the
    placeholder host: `primary_device: false` is how it says there is no meter."""
    cfg = _load(tmp_path, "primary_device: false\nmodbus:\n  host: 192.168.1.100\n" + CAN_ONLY)
    assert [d.id for d in cfg.devices] == ["bms-can"]
    cfg.save_yaml_config()
    assert yaml.safe_load((tmp_path / "config.yaml").read_text())["primary_device"] is False


def test_primary_device_true_keeps_device_1_without_a_modbus_section(tmp_path):
    cfg = _load(tmp_path, "primary_device: true\nmqtt:\n  topic_prefix: meters/umg512\n")
    assert cfg.primary_device is not None
    assert cfg.primary_device.mqtt_topic_prefix == "meters/umg512"


def test_modbus_host_in_the_environment_still_means_device_1(tmp_path, monkeypatch):
    """NDJ-3: the environment alone declares device #1 — with its old names,
    as before (a file silent on them, or no file at all)."""
    monkeypatch.setenv("MODBUS_HOST", "192.0.2.9")
    cfg = Config(str(tmp_path / "config.yaml"))
    assert cfg.primary_device is not None and cfg.modbus.host == "192.0.2.9"
    assert cfg.mqtt.topic_prefix == "multibus/umg512" == cfg.primary_device.mqtt_topic_prefix
    assert cfg.mqtt.ha_device_name == "Janitza UMG 512-PRO"
    on_file = _load(tmp_path, "mqtt:\n  broker: mosquitto\n")
    assert on_file.primary_device is not None and on_file.mqtt.topic_prefix == "multibus/umg512"
    monkeypatch.setenv("MQTT_PREFIX", "site/meter")              # the environment's prefix wins
    assert Config(str(tmp_path / "config.yaml")).mqtt.topic_prefix == "site/meter"


def test_without_a_primary_no_history_query_runs_unfiltered(tmp_path):
    """NDJ-4: no device named and no device #1 — never the default bucket
    with no device filter (every device's series mixed)."""
    from multibus.routes._shared import device_influx
    cfg = _load(tmp_path, CAN_ONLY)
    with pytest.raises(ValueError, match="no primary"):
        device_influx(cfg, "")
    dev = cfg.get_device("bms-can")
    assert device_influx(cfg, "bms-can") == (dev.influxdb_bucket, dev.influxdb_device_tag)


def test_a_vmeter_without_a_source_says_so_when_there_is_no_primary(caplog):
    """NDJ-6: a virtual meter row with no `device` on a gateway with no
    device #1 serves nothing — and says so, once."""
    import logging
    from multibus.virtual_meter_manager import VirtualMeterManager
    mgr = VirtualMeterManager.__new__(VirtualMeterManager)
    mgr.current_values, mgr.device_values, mgr.primary_device_id = {}, {"bms-can": {1: {}}}, ""
    mgr._no_source_warned = set()
    with caplog.at_level(logging.WARNING, logger="multibus.virtual_meter_manager"):
        assert mgr._store_for({"template": "em24_av53"}) == {}
        mgr._store_for({"template": "em24_av53"})
    said = [r for r in caplog.records if "no primary device" in r.getMessage()]
    assert len(said) == 1


@needs_tc
def test_an_import_that_changes_device_1_asks_for_a_restart(tmp_path):
    """NDJ-5: an import (or a restore) that turns device #1 on or off cannot
    be hot-applied — the answer says a restart is needed."""
    import io
    import zipfile
    from fastapi.testclient import TestClient
    from multibus.api import create_api
    cfg = _load(tmp_path, CAN_ONLY)
    cfg.save_yaml_config()
    app, _ = create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    cl = TestClient(app, raise_server_exceptions=False)
    src = zipfile.ZipFile(io.BytesIO(cl.get("/api/config/export").content))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "config.yaml":
                doc = yaml.safe_load(data)
                doc["primary_device"] = True
                doc["modbus"]["host"] = "192.0.2.7"
                data = yaml.safe_dump(doc).encode()
            dst.writestr(item, data)
    r = cl.post("/api/config/import", content=buf.getvalue(),
                headers={"Content-Type": "application/zip"})
    assert r.status_code == 200, r.text
    assert cfg.primary_device is not None
    assert r.json()["restart_required"] is True and "restart" in r.json()["note"]


def test_an_unreadable_file_keeps_device_1_and_blocks_saves(tmp_path):
    (tmp_path / "config.yaml").write_text("modbus: [unclosed\n")
    cfg = Config(str(tmp_path / "config.yaml"))
    assert cfg.primary_device is not None
    assert cfg.mqtt.topic_prefix == "multibus/umg512"
    with pytest.raises(RuntimeError):
        cfg.save_yaml_config()


def test_the_legacy_id_stays_reserved_without_a_primary(tmp_path):
    cfg = _load(tmp_path, CAN_ONLY)
    with pytest.raises(ValueError, match="reserved"):
        cfg.upsert_raw_device({"id": "umg512", "template": "janitza_umg512_pro",
                               "connection": {"protocol": "tcp", "host": "192.0.2.7"}})


@needs_tc
def test_the_api_serves_a_gateway_without_a_primary(tmp_path):
    from fastapi.testclient import TestClient
    from multibus.api import create_api
    cfg = _load(tmp_path, CAN_ONLY)
    app, _ = create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    cl = TestClient(app, raise_server_exceptions=False)
    devs = cl.get("/api/devices").json()
    ids = [d["id"] for d in (devs.get("devices") if isinstance(devs, dict) else devs)]
    assert ids == ["bms-can"]
    assert cl.get("/api/status").status_code == 200
    v = cl.get("/api/values").json()
    assert v["values"] == {} and v["device"] == ""
    assert cl.get("/api/registers/selected").status_code == 200
    assert cl.get("/api/values?device=bms-can").status_code == 200
    # NDJ-2: the Energy tab with no device named answers, it does not fail
    ef = cl.get("/api/energy/fields")
    assert ef.status_code == 200, ef.text
    assert ef.json()["selected"] == [] and ef.json()["candidates"] == []
    assert cl.get("/api/energy/fields?device=bms-can").status_code == 200
    assert cl.post("/api/energy/fields", json={"fields": []}).status_code == 404
    # a selection for a device #1 that is not there is refused, not written
    r = cl.post("/api/registers/selected", json=[])
    assert r.status_code == 404, r.text
    assert not (tmp_path / "selected_registers.json").exists()
    with cl.websocket_connect("/ws") as ws:
        assert ws.receive_json()["device"] == ""
