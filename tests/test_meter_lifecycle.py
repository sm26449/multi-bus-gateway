# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""What a device's meters give it, through a device's life: disabled (it
publishes nothing, as it never did), edited, deleted and created again (no
memory of the deleted one); a report the rate cap held back is judged at the
end of the gap; a clear the broker missed goes out when it is back; the
consumption link keeps the HA identity and the InfluxDB tag it always had;
a rate formula's memory starts again when what it reads through changes."""
import json
import time

from tests.test_devices_api import make_app, needs_tc
from tests.test_linked_meter import _row
from tests.test_meter_derived import _files, _meter_only
from tests.test_meter_links import IN, _devices, _stored


class _Meter:
    publish_callback = None
    connected = True

    def data_health(self):
        return {"status": "ok", "last_success_ts": time.time()}

    def get_stats(self):
        return {"connected": True}

    def disconnect(self):
        pass


PAIR = [{"role": "input", "device": "em-in", "power": "power_active_total"},
        {"role": "output", "device": "em-out", "power": "power_active_total"}]


def _live_app(tmp_path, monkeypatch, extra=""):
    """A gateway whose meters em-in / em-out report through their own poll
    callbacks; `ups` is measured by both, `hp` by em-in (a consumption link)."""
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    import multibus.calc_engine as ce_mod
    from multibus.api import create_api
    from multibus.config import SelectedRegister
    from multibus.meter_only import MeterOnlyClient
    from tests.test_devices import write_config
    for did in ("em-in", "em-out"):
        _files(tmp_path, did, [_row(0, "power_active_total", "W")])
    _files(tmp_path, "hp", [_row(0, "flow_temp", "°C")])
    cfg = write_config(tmp_path, extra_yaml="""
devices:
  - {id: em-in, enabled: true, connection: {protocol: tcp, host: 192.0.2.10}}
  - {id: em-out, enabled: true, connection: {protocol: tcp, host: 192.0.2.11}}
  - id: hp
    enabled: true
    connection: {protocol: tcp, host: 192.0.2.12}
    energy_meter: {device: em-in, power: power_active_total}
  - id: ups
    kind: ups
    connection: {protocol: none}
    meters:
      - {role: input, device: em-in, power: power_active_total}
      - {role: output, device: em-out, power: power_active_total}
""" + extra)
    clients = {"em-in": _Meter(), "em-out": _Meter(), "hp": _Meter(), "tasmota": _Meter()}
    pairs = [(d, clients.get(d.id) or (MeterOnlyClient(cfg, d.id) if d.id == "ups" else None))
             for d in cfg.devices]
    mqtt = MagicMock()
    mqtt.connected = True
    mqtt._build_topic.side_effect = lambda r, prefix: f"{prefix}/{r.name}"
    app, _ = create_api(cfg, None, mqtt, None, devices=pairs)
    client = TestClient(app, raise_server_exceptions=False)
    reg = SelectedRegister(address=0, name="power_active_total", label="P", unit="W",
                           data_type="float", poll_group="normal")
    monkeypatch.setattr(ce_mod, "LINKED_MIN_GAP_S", 0.0)

    def report(pin=1000.0, pout=920.0, times=2):
        for _ in range(times):
            for did, v in (("em-in", pin), ("em-out", pout)):
                clients[did].publish_callback("normal", {0: {"value": v, "register": reg, "ts": time.time(),
                                                             "mono": time.monotonic(), "interval": 5}})
            time.sleep(0.01)

    def published(prefix):
        return [it["register"].name for c in mqtt.publish_register_data.call_args_list
                if c.kwargs.get("topic_prefix") == prefix for it in c.args[1].values()]
    return app, client, report, published, mqtt, cfg


@needs_tc
def test_a_disabled_device_publishes_nothing_its_meters_gave_it(tmp_path, monkeypatch):
    """A regression caught before release: a disabled heat pump (consumption
    link, public since 3.86.1) and a disabled UPS kept publishing."""
    app, client, report, published, mqtt, _cfg = _live_app(tmp_path, monkeypatch)
    report()
    assert "power_electric" in published("mbg/devices/hp")
    assert "efficiency_pct" in published("mbg/devices/ups")
    for did, conn in (("hp", {"protocol": "tcp", "host": "192.0.2.12"}), ("ups", {"protocol": "none"})):
        r = client.put(f"/api/devices/{did}", json={"id": did, "name": did, "enabled": False, "connection": conn})
        assert r.status_code == 200, r.text
    assert "hp" not in app.state.calc_runners and "ups" not in app.state.calc_runners
    mqtt.publish_register_data.reset_mock()
    hp_store = {k: dict(v) for k, v in app.state.registry.ensure_store("hp").items()}
    report(pin=1500.0, pout=1400.0)
    assert published("mbg/devices/hp") == [] and published("mbg/devices/ups") == []
    assert {k: dict(v) for k, v in app.state.registry.ensure_store("hp").items()} == hp_store
    # and the engine refuses even if asked directly
    assert app.state.calc_engine.run_linked("ups", {}, force=True, topic_prefix="x", bucket=None,
                                            device_tag=None, device_id="ups",
                                            mqtt_on=False, influx_on=False) == {}


@needs_tc
def test_a_deleted_device_leaves_nothing_and_its_id_starts_fresh(tmp_path, monkeypatch):
    app, client, report, published, mqtt, _cfg = _live_app(tmp_path, monkeypatch)
    report()
    ce = app.state.calc_engine
    assert "ups" in ce.pair_verdicts and "ups" in app.state.calc_runners
    assert client.delete("/api/devices/ups").status_code == 200
    assert "ups" not in app.state.calc_runners and "ups" not in ce.store
    assert "ups" not in ce.pair_verdicts and "ups" not in ce._linked_state
    assert "ups" not in ce.dependents("em-in")
    mqtt.publish_register_data.reset_mock()
    report()
    assert published("mbg/devices/ups") == []
    # created again: no memory of the deleted one — its first reading is not "steady"
    r = client.post("/api/devices", json={"id": "ups", "name": "ups", "kind": "ups",
                                          "connection": {"protocol": "none"}, "meters": PAIR})
    assert r.status_code in (200, 201), r.text
    report(times=1)
    assert ce.pair_verdicts["ups"]["efficiency_pct"]["reason"] == "unsteady"


@needs_tc
def test_an_edited_meter_only_device_keeps_being_measured(tmp_path, monkeypatch):
    app, client, report, published, mqtt, _cfg = _live_app(tmp_path, monkeypatch)
    # what the view shows is what a save sends back
    view = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "ups")
    assert view["connection"] == {"protocol": "none"} and view["measured_by"] == ["em-in", "em-out"]
    r = client.put("/api/devices/ups", json={"id": "ups", "name": "UPS hall", "connection": view["connection"]})
    assert r.status_code == 200, r.text
    st = _stored(tmp_path, "ups")
    assert st["kind"] == "ups" and len(st["meters"]) == 2 and st["name"] == "UPS hall"
    mqtt.publish_register_data.reset_mock()
    report()
    assert "efficiency_pct" in published("mbg/devices/ups")
    t = client.post("/api/devices/ups/test").json()
    assert "measured by em-in, em-out" in t["message"]


@needs_tc
def test_how_a_device_is_read_is_fixed_after_creation(tmp_path, monkeypatch):
    app, client, report, published, mqtt, _cfg = _live_app(tmp_path, monkeypatch)
    r = client.put("/api/devices/em-out", json={"id": "em-out", "name": "em-out", "connection": {"protocol": "none"}})
    assert r.status_code == 422 and "connection.protocol" in r.json()["detail"]["fields"], r.text
    r = client.put("/api/devices/ups", json={"id": "ups", "name": "ups",
                                             "connection": {"protocol": "tcp", "host": "192.0.2.20"}})
    assert r.status_code == 422 and "connection.protocol" in r.json()["detail"]["fields"], r.text


@needs_tc
def test_a_meter_only_post_keeps_options_for_a_pair(tmp_path):
    _cfg, client = make_app(tmp_path)
    r = _meter_only(tmp_path, client, meters=[IN], options={"min_load_w": 5})
    assert r.status_code == 422 and "options" in r.json()["detail"]["fields"], r.text


@needs_tc
def test_a_report_held_back_by_the_gap_is_judged_at_its_end(tmp_path, monkeypatch):
    import multibus.calc_engine as ce_mod
    app, client, report, published, mqtt, _cfg = _live_app(tmp_path, monkeypatch)
    report()                                            # a steady pair, published
    monkeypatch.setattr(ce_mod, "LINKED_MIN_GAP_S", 0.3)
    ce = app.state.calc_engine
    ce._linked_last["ups"] = time.monotonic()           # an evaluation just ran
    report(pin=1000.0, pout=400.0, times=1)             # held back by the gap
    assert ce.pair_verdicts["ups"]["efficiency_pct"] == {"state": "published"}
    store = app.state.registry.ensure_store("ups")
    assert next(v["value"] for v in store.values() if v.get("name") == "efficiency_pct") == 92.0
    time.sleep(0.7)                                     # the trailing evaluation
    assert next(v["value"] for v in store.values() if v.get("name") == "efficiency_pct") == 40.0


@needs_tc
def test_an_absent_value_is_cleared_once_the_broker_is_back(tmp_path, monkeypatch):
    app, client, report, published, mqtt, _cfg = _live_app(tmp_path, monkeypatch)
    report()
    mqtt.connected = False
    report(pin=50.0, pout=40.0)                         # absent while the broker is away
    assert not mqtt.clear_topics.called
    mqtt.connected = True
    report(pin=50.0, pout=40.0, times=1)
    cleared = set().union(*[set(c.args[0]) for c in mqtt.clear_topics.call_args_list])
    assert "mbg/devices/ups/efficiency_pct" in cleared


@needs_tc
def test_the_consumption_link_keeps_its_ha_identity_and_its_push_tag(tmp_path, monkeypatch):
    """Its HA entity keeps the address-based id it always had; a push-fed
    device's linked value keeps the `poll_group` tag its own report gave it
    (the InfluxDB series does not split). Only the pair's new values are
    known by name, and they expire in HA when absent."""
    from multibus.mqtt_publisher import MQTTPublisher
    app, client, report, published, mqtt, cfg = _live_app(tmp_path, monkeypatch, extra="""
  - {id: tasmota, enabled: true, connection: {protocol: mqtt, broker: 192.0.2.30, topic: tele/plug/SENSOR},
     energy_meter: {device: em-in, power: power_active_total}}
""")
    ce = app.state.calc_engine
    hp = {e["_reg"].name: e["_reg"] for e in ce.store["hp"]}
    ups = {e["_reg"].name: e["_reg"] for e in ce.store["ups"]}
    assert not getattr(hp["power_electric"], "discovery_key", "")
    assert ups["efficiency_pct"].discovery_key == "linked_efficiency_pct"
    assert ups["efficiency_pct"].ha_expire_after >= 60

    sent = []
    pub = MQTTPublisher(cfg.mqtt, [])
    pub.connected = True
    pub._publish = lambda topic, payload, retain=False, **kw: sent.append((topic, payload)) or True
    pub.publish_device_discovery("hp", "hp", "mbg/devices/hp", [hp["power_electric"]])
    cfg_hp = json.loads(next(p for t, p in sent if "power_electric" in t and p))
    assert cfg_hp["unique_id"] == f"mbg_dev_hp_{hp['power_electric'].address}_power_electric"
    assert "expire_after" not in cfg_hp
    sent.clear()
    pub.publish_device_discovery("ups", "ups", "mbg/devices/ups", [ups["efficiency_pct"]])
    cfg_ups = json.loads(next(p for t, p in sent if "efficiency_pct" in t and p))
    assert cfg_ups["unique_id"] == "mbg_dev_ups_linked_efficiency_pct"
    assert cfg_ups["expire_after"] == ups["efficiency_pct"].ha_expire_after

    mqtt.publish_register_data.reset_mock()
    report()
    groups = lambda prefix: {c.args[0] for c in mqtt.publish_register_data.call_args_list  # noqa: E731
                             if c.kwargs.get("topic_prefix") == prefix}
    assert groups("mbg/devices/tasmota") == {"mqtt"}
    assert groups("mbg/devices/hp") == {"normal"}
    # a pair's values are re-sent while present (HA's expire_after only
    # fires when they are absent); the consumption link keeps its own rhythm
    beats = {c.kwargs.get("heartbeat") for c in mqtt.publish_register_data.call_args_list
             if c.kwargs.get("topic_prefix") == "mbg/devices/ups"}
    assert beats and all(b and b <= ups["efficiency_pct"].ha_expire_after / 3 for b in beats)
    assert {c.kwargs.get("heartbeat") for c in mqtt.publish_register_data.call_args_list
            if c.kwargs.get("topic_prefix") == "mbg/devices/hp"} == {None}


@needs_tc
def test_a_rate_starts_again_when_what_it_reads_changes(tmp_path):
    """prev() and dt start again when the device's map or units change;
    ema() and hysteresis() keep theirs; an unchanged device keeps both."""
    _cfg, client = make_app(tmp_path)
    _devices(tmp_path, client, ids=("em",))
    ce = client.app.state.calc_engine
    r = client.post("/api/devices/em/calculated", json={"calculated": [
        {"name": "rate", "expr": "(energy_active_import - prev(energy_active_import)) / dt", "unit": "W"},
        {"name": "smooth", "expr": "ema(power_active_total, 60)", "unit": "W"}]})
    assert r.status_code in (200, 201), r.text

    def mark():
        for e in ce.store["em"]:
            e["_state"]["prev"] = {"energy_active_import": 1.0}
            e["_state"]["ts_mono"] = 1.0
            e["_state"]["memo"] = {"ema@1:0": (5.0, 1.0)}
    mark()
    ce.load("em")                                       # nothing changed: all kept
    assert all(e["_state"]["prev"] and e["_state"]["ts_mono"] == 1.0 for e in ce.store["em"])
    _files(tmp_path, "em", [_row(0, "power_active_total", "W"), _row(2, "energy_active_import", "Wh")])
    ce.load("em")                                       # kWh -> Wh: the rate starts again
    for e in ce.store["em"]:
        assert e["_state"]["prev"] == {} and e["_state"]["ts_mono"] is None
        assert e["_state"]["memo"] == {"ema@1:0": (5.0, 1.0)}
