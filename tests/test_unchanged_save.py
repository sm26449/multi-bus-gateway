"""A save keeps what it was not told (multibus/save_merge.py).

Every editor shows part of what is stored. Saving it unchanged must change
nothing, and a page opened before an upgrade — sending today's bodies — must
not corrupt anything either. These tests replay the bodies the UI sends.
"""
import copy
import json

import pytest
import yaml

from multibus.save_merge import (BridgeConflict, keep_unsaid, merge_connection, merge_group_route)
from tests.test_devices_api import make_app, needs_tc

BRIDGES = [{"id": "ws", "type": "rtu_transparent", "host": "192.0.2.60", "ports": [{"port": 4196}]}]


# ── the rule, on its own ────────────────────────────────────────────────────

def test_same_protocol_keeps_what_was_not_said_and_none_clears():
    old = {"protocol": "rtu", "serial_port": "/dev/ttyX", "baudrate": 19200, "parity": "E",
           "stopbits": 2, "max_gap": 20}
    new = merge_connection({"protocol": "rtu", "serial_port": "/dev/ttyX", "unit_id": 3}, old)
    assert new == {**old, "unit_id": 3}
    assert "max_gap" not in merge_connection({"protocol": "rtu", "max_gap": None}, old)
    # an empty block is the stored one
    assert merge_connection({}, old) == old and merge_connection(None, old) == old


def test_another_protocol_starts_clean():
    old = {"protocol": "rtu", "serial_port": "/dev/ttyX", "baudrate": 19200, "parity": "E"}
    assert merge_connection({"protocol": "tcp", "host": "192.0.2.5"}, old) == {"protocol": "tcp", "host": "192.0.2.5"}
    # an address without a protocol is TCP, as validation reads it — not the stored rtu
    assert merge_connection({"host": "192.0.2.5", "port": 502}, old) == {"host": "192.0.2.5", "port": 502}


def test_a_bridge_is_never_left_implicitly():
    old = {"bridge": "ws", "bridge_port": 4196, "unit_id": 2, "timeout": 3, "max_gap": 10}
    # an old page echoing the resolved address: the reference stays
    echo = {"protocol": "rtu-tcp", "host": "192.0.2.60", "port": 4196, "unit_id": 2, "timeout": 3}
    assert merge_connection(echo, old, BRIDGES) == old
    # nothing about the address: kept
    assert merge_connection({"unit_id": 5}, old, BRIDGES) == {**old, "unit_id": 5}
    # another address without saying so: refused
    with pytest.raises(BridgeConflict):
        merge_connection({"protocol": "tcp", "host": "192.0.2.9", "port": 502}, old, BRIDGES)
    # said so: off the bridge
    off = merge_connection({"bridge": None, "protocol": "tcp", "host": "192.0.2.9", "port": 502, "unit_id": 2}, old, BRIDGES)
    assert "bridge" not in off and "bridge_port" not in off and off["host"] == "192.0.2.9"


def test_a_group_route_keeps_its_shape():
    pg = {"id": "bank", "connection": {"protocol": "rtu_tap", "serial_port": "/dev/ttyB", "baudrate": 19200,
                                       "stale_after_s": 60}}
    # neither key: the stored route, whole
    assert merge_group_route({"id": "bank"}, pg)["connection"] == pg["connection"]
    # sources: the group is read through sources; its old connection is source "default"
    g = merge_group_route({"id": "bank", "sources": [
        {"id": "default", "protocol": "rtu_tap", "serial_port": "/dev/ttyB"},
        {"id": "net", "protocol": "tcp", "host": "192.0.2.7"}]}, pg)
    assert "connection" not in g and g["sources"][0]["baudrate"] == 19200
    # connection: None = inherit the installation's
    assert "connection" not in merge_group_route({"id": "bank", "connection": None}, pg)


def test_keep_unsaid_top_level():
    old = {"id": "d", "write_locked": True, "commands": [{"name": "x"}], "totals": {"a": 1},
           "mqtt": {"topic_prefix": "p", "ha_discovery": False, "publish_mode": "all"}, "pq_recorder": {"enabled": True}}
    new = {"id": "d", "write_locked": False, "mqtt": {"topic_prefix": "p", "enabled": True}}
    out = keep_unsaid(new, old, {"write_locked": False, "mqtt": {"enabled": True}},
                      own=("write_locked", "commands"))
    assert out["write_locked"] is True and out["commands"] == [{"name": "x"}]        # own routes
    assert out["mqtt"] == {"topic_prefix": "p", "enabled": True, "ha_discovery": False, "publish_mode": "all"}
    assert out["pq_recorder"] == {"enabled": True} and out["totals"] == {"a": 1}
    # a key the payload speaks of — empty included — is the payload's
    assert "totals" not in keep_unsaid({"id": "d"}, old, {"totals": {}})


# ── devices, through the API ────────────────────────────────────────────────

DEVICES = """
bridges:
  - {id: ws, type: rtu_transparent, host: 192.0.2.60, ports: [{port: 4196}]}
devices:
  - id: hp
    name: Heat pump
    template: samsung_mim_b19n
    enabled: false
    connection: {bridge: ws, bridge_port: 4196, unit_id: 2, timeout: 3, max_gap: 10}
    mqtt: {topic_prefix: mbg/devices/hp, ha_discovery: false, publish_mode: all}
    influxdb: {bucket: hp, device_tag: hp, enabled: false}
    write_locked: true
    pq_recorder: {enabled: false, poll_s: 30}
  - id: rs
    name: Serial meter
    template: fronius_smart_meter_65a
    enabled: false
    connection: {protocol: rtu, serial_port: /dev/ttyRS, baudrate: 19200, parity: E, stopbits: 2, unit_id: 1,
                 startup_jitter_s: 6.0}
  - id: asc
    name: ASCII meter
    template: fronius_smart_meter_65a
    enabled: false
    connection: {protocol: ascii, serial_port: /dev/ttyAS, baudrate: 9600, unit_id: 1}
  - id: udp
    name: UDP meter
    template: fronius_smart_meter_65a
    enabled: false
    connection: {protocol: udp, host: 192.0.2.30, port: 502, unit_id: 1}
"""


def _stored(cfg, did):
    return copy.deepcopy(next(d for d in yaml.safe_load(cfg.config_path.read_text())["devices"] if d["id"] == did))


def _ui_body(client, did, **conn):
    """What the device page sends today (saveDeviceDetail), from what it GETs."""
    d = next(x for x in client.get("/api/devices").json()["devices"] if x["id"] == did)
    c = d["connection"]
    proto = c["protocol"]
    if proto in ("tcp", "rtu-tcp"):
        connection = {"protocol": proto, "host": c["host"], "port": c["port"], "unit_id": c["unit_id"],
                      "timeout": c["timeout"]}
    else:                                      # the old editor's serial branch
        connection = {"protocol": "rtu_tap" if proto == "rtu_tap" else "rtu", "serial_port": c["serial_port"],
                      "baudrate": c["baudrate"], "parity": c["parity"], "unit_id": c["unit_id"]}
    connection.update(conn)
    return {"id": did, "name": d["name"], "template": d["template"], "enabled": d["enabled"],
            "connection": connection,
            "mqtt": {"topic_prefix": d.get("mqtt_topic_prefix"), "enabled": d.get("mqtt_enabled")},
            "influxdb": {"bucket": d.get("influxdb_bucket"), "device_tag": d.get("influxdb_device_tag"),
                         "enabled": d.get("influxdb_enabled")},
            "ha_discovery_enabled": d.get("ha_discovery_enabled")}


@needs_tc
def test_a_bridged_device_saved_by_an_old_page_stays_on_its_bridge(tmp_path):
    cfg, client = make_app(tmp_path, DEVICES)
    before = _stored(cfg, "hp")
    r = client.put("/api/devices/hp", json=_ui_body(client, "hp"))
    assert r.status_code == 200, r.text
    after = _stored(cfg, "hp")
    assert after["connection"] == before["connection"]                   # bridge, port, max_gap
    assert after["write_locked"] is True and after["pq_recorder"] == before["pq_recorder"]
    assert after["mqtt"]["publish_mode"] == "all" and after["mqtt"]["ha_discovery"] is False


@needs_tc
def test_moving_off_a_bridge_must_be_said(tmp_path):
    cfg, client = make_app(tmp_path, DEVICES)
    body = _ui_body(client, "hp", protocol="tcp", host="192.0.2.9", port=502)
    r = client.put("/api/devices/hp", json=body)
    assert r.status_code == 422 and "bridge: null" in r.text
    assert _stored(cfg, "hp")["connection"]["bridge"] == "ws"            # nothing written
    body["connection"]["bridge"] = None
    assert client.put("/api/devices/hp", json=body).status_code == 200
    c = _stored(cfg, "hp")["connection"]
    assert "bridge" not in c and c["host"] == "192.0.2.9"


@needs_tc
def test_a_bridge_that_moved_meanwhile_is_not_a_reason_to_detach(tmp_path):
    cfg, client = make_app(tmp_path, DEVICES)
    stale = _ui_body(client, "hp")                                     # the page, opened now
    client.put("/api/bridges/ws", json={"type": "rtu_transparent", "host": "192.0.2.61",
                                        "ports": [{"port": 4196}]})
    r = client.put("/api/devices/hp", json=stale)
    assert r.status_code == 422
    assert _stored(cfg, "hp")["connection"]["bridge"] == "ws"


@needs_tc
def test_a_serial_line_survives_an_editor_that_shows_part_of_it(tmp_path):
    cfg, client = make_app(tmp_path, DEVICES)
    before = _stored(cfg, "rs")
    assert client.put("/api/devices/rs", json=_ui_body(client, "rs")).status_code == 200
    after = _stored(cfg, "rs")
    for k in ("baudrate", "parity", "stopbits", "startup_jitter_s"):
        assert after["connection"][k] == before["connection"][k], k


@needs_tc
def test_ascii_and_udp_keep_their_protocol(tmp_path):
    cfg, client = make_app(tmp_path, DEVICES)
    for did in ("asc", "udp"):
        d = next(x for x in client.get("/api/devices").json()["devices"] if x["id"] == did)
        body = {"id": did, "name": "Renamed", "template": d["template"], "enabled": False,
                "connection": {"protocol": d["connection"]["protocol"]}}
        r = client.put(f"/api/devices/{did}", json=body)
        assert r.status_code == 200, r.text
        assert _stored(cfg, did)["connection"]["protocol"] == d["connection"]["protocol"]
        assert _stored(cfg, did)["name"] == "Renamed"


@needs_tc
def test_an_unchanged_save_is_a_no_op_and_the_own_routes_win(tmp_path):
    cfg, client = make_app(tmp_path, DEVICES)
    stored = _stored(cfg, "hp")
    r = client.put("/api/devices/hp", json=stored)
    assert r.status_code == 200 and r.json()["status"] == "unchanged"
    # the lock is its own route's: a PUT cannot take it off
    r = client.put("/api/devices/hp", json={**stored, "name": "HP", "write_locked": False})
    assert r.status_code == 200 and _stored(cfg, "hp")["write_locked"] is True
    # a template-assign body (no enable flags) keeps the disabled sink
    r = client.put("/api/devices/hp", json={"id": "hp", "template": stored["template"],
                                            "connection": {}, "mqtt": {}, "influxdb": {}})
    assert r.status_code == 200, r.text
    assert _stored(cfg, "hp")["influxdb"]["enabled"] is False


# ── installations ───────────────────────────────────────────────────────────

PLANT = """
endpoints:
  - id: pv
    name: PV
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.40, port: 502}
    aggregates: false
    mqtt: {ha_discovery: false}
    groups:
      - id: inverters
        role: inverter
        template: fronius_sunspec_inverter
        units: [1, 2]
        sources:
          - id: sunspec
            protocol: tcp
            host: 192.0.2.40
            port: 502
            max_gap: 20
            startup_jitter_s: 6.0
            poll_groups: {realtime: {interval: 5}, static: {interval: 3600}}
      - id: site
        role: meter
        template: fronius_sunspec_meter
        units: [240]
  - id: bank
    name: Bank
    template: seplos_bms_v3_rtu_tap
    enabled: false
    connection: {protocol: rtu_tap, serial_port: /dev/ttyBANK, baudrate: 19200, parity: N, stale_after_s: 60}
    groups:
      - id: units
        connection: {protocol: rtu_tap, serial_port: /dev/ttyBANK, baudrate: 19200, stale_after_s: 60}
        units: [1, 2]
"""


def _ep(cfg, pid):
    return copy.deepcopy(next(e for e in yaml.safe_load(cfg.config_path.read_text())["endpoints"] if e["id"] == pid))


def _old_raw_source(s):
    """Today's _rawSource: rebuilt from the view's address."""
    out = {"id": s["id"], "protocol": s["protocol"], "enabled": s.get("enabled", True)}
    for k in ("template", "timeout", "stale_after_s"):
        if s.get(k):
            out[k] = s[k]
    a = s.get("address", "")
    host, _, port = a.rpartition(":")
    out.update(host=host, port=int(port or 502))
    return out


@needs_tc
def test_a_group_save_from_an_old_page_keeps_the_source_tuning(tmp_path):
    cfg, client = make_app(tmp_path, PLANT)
    before = _ep(cfg, "pv")
    view = client.get("/api/endpoints/pv").json()
    view = view.get("endpoint", view)
    inv = next(g for g in view["groups"] if g["id"] == "inverters")
    groups = [{"id": "inverters", "role": "inverter", "template": "fronius_sunspec_inverter", "enabled": False,
               "units": [{"unit_id": 1, "id": "pv-u1"}, {"unit_id": 2, "id": "pv-u2"}],
               "sources": [_old_raw_source(s) for s in inv["sources"]]},
              {"id": "site", "role": "meter", "template": "fronius_sunspec_meter",
               "units": [{"unit_id": 240, "id": "pv-u240"}]}]
    r = client.put("/api/endpoints/pv", json={"id": "pv", "name": "PV", "enabled": False,
                                              "connection": view["connection"], "groups": groups})
    assert r.status_code == 200, r.text
    src = _ep(cfg, "pv")["groups"][0]["sources"][0]
    assert src["max_gap"] == 20 and src["startup_jitter_s"] == 6.0
    assert src["poll_groups"] == before["groups"][0]["sources"][0]["poll_groups"]
    # the group that inherits still inherits; the installation keeps its own keys
    site = _ep(cfg, "pv")["groups"][1]
    assert "connection" not in site and "sources" not in site
    assert _ep(cfg, "pv")["aggregates"] is False and _ep(cfg, "pv")["mqtt"]["ha_discovery"] is False


@needs_tc
def test_the_settings_dialog_and_a_rename_keep_the_installation(tmp_path):
    cfg, client = make_app(tmp_path, PLANT)
    before = _ep(cfg, "bank")
    r = client.put("/api/endpoints/bank", json={"id": "bank", "name": "Battery bank", "enabled": False,
                                                "connection": {}, "mqtt": {"enabled": True},
                                                "influxdb": {"enabled": True}})
    assert r.status_code == 200, r.text
    after = _ep(cfg, "bank")
    assert after["name"] == "Battery bank"
    assert after["groups"][0]["connection"] == before["groups"][0]["connection"]
    assert after["connection"] == before["connection"]
    assert client.put("/api/endpoints/bank", json=after).json()["status"] == "unchanged"


# ── rules ───────────────────────────────────────────────────────────────────

from tests.test_rules_api import _rule_app        # noqa: E402
from tests.test_commands import _Inverter         # noqa: E402
from tests.test_rules import OV                   # noqa: E402


@needs_tc
def test_a_rule_asks_only_for_what_its_command_accepts(tmp_path):
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    for bad, word in ((dict(OV, normal={'value': 'off'}), 'normal'),
                      (dict(OV, normal={'value': None}), 'normal'),
                      (dict(OV, normal={'value': 100, 'speed': 3}), 'unknown parameter'),
                      (dict(OV, steps=[{'at': 250, 'value': 400}]), 'steps[0]')):
        r = client.put("/api/rules/ov-u1", json=bad)
        assert r.status_code == 422 and word in r.text, (bad, r.text)
    # no opinion stays legal
    r = client.put("/api/rules/ov-u1", json=dict(OV, normal=None))
    assert r.status_code == 200, r.text


@needs_tc
def test_an_armed_rule_keeps_its_target_and_can_always_be_disarmed(tmp_path):
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2), 'pv-u2': _Inverter(sf=-2)},
                                            mode='armed')
    r = client.put("/api/rules/ov-u1", json=dict(OV, mode='armed',
                                                 target={'device': 'pv-u2', 'command': 'power_limit'}))
    assert r.status_code == 422 and 'shadow' in r.text
    # a stored rule the command would now refuse can still be put in shadow
    rt.raw['ov-u1'] = dict(rt.raw['ov-u1'], normal={'value': 400})
    assert rt.set_mode('ov-u1', 'shadow', who='t') == []


@needs_tc
def test_an_unchanged_rule_save_keeps_its_state(tmp_path):
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    rt.states[('ov-u1', 'pv-u1')].state = 'Moderate'
    stored = json.loads(json.dumps(rt.raw['ov-u1']))
    assert client.put("/api/rules/ov-u1", json=stored).status_code == 200
    assert rt.states[('ov-u1', 'pv-u1')].state == 'Moderate'


# ── templates ───────────────────────────────────────────────────────────────

@needs_tc
def test_a_new_template_never_replaces_one_silently(tmp_path):
    cfg, client = make_app(tmp_path)
    t = {"device_template": {"id": "mine", "name": "Mine", "protocol": {"transports": ["tcp"]},
                             "registers": [{"address": 1, "name": "v", "data_type": "uint16"}]}}
    assert client.post("/api/device-templates", json={**t, "create": True}).status_code == 200
    r = client.post("/api/device-templates", json={**t, "create": True})
    assert r.status_code == 409 and r.json()["detail"]["conflict"] == "mine"
    assert r.json()["detail"]["builtin"] is False
    assert client.post("/api/device-templates", json=t).status_code == 200      # an edit saves
    b = client.post("/api/device-templates", json={"device_template": {
        **t["device_template"], "id": "janitza_umg512_pro"}, "create": True})
    assert b.status_code == 409 and b.json()["detail"]["builtin"] is True


# ── one list of protocols ───────────────────────────────────────────────────

def test_the_editors_lists_match_the_api():
    """The device page's protocol families are the config's tuples; the
    installation editors read theirs from /api/protocols."""
    import re
    from pathlib import Path
    from multibus.config import MODBUS_PROTOCOLS, NETWORK_PROTOCOLS, SERIAL_PROTOCOLS
    js = Path(__file__).resolve().parents[1].joinpath("ui/js/app-devices.js").read_text(encoding="utf-8")

    def arr(name):
        return re.findall(r"'([^']+)'", re.search(rf"{name}: \[([^\]]*)\]", js).group(1))
    assert set(arr("_DEVICE_PROTOCOLS")) == set(MODBUS_PROTOCOLS + ("http", "mqtt"))
    assert set(arr("_NET_PROTOCOLS")) == set(NETWORK_PROTOCOLS)
    assert set(arr("_SERIAL_PROTOCOLS")) == set(SERIAL_PROTOCOLS + ("rtu_tap",))


@needs_tc
def test_the_api_serves_each_editor_its_protocols(tmp_path):
    _cfg, client = make_app(tmp_path)
    p = client.get("/api/protocols").json()
    assert "rtu_tap" not in p["source"] and "rtu_tap" in p["group"] and "http" in p["group"]
    assert "udp" in p["endpoint"] and "ascii" in p["device"]


# ── after the code review ───────────────────────────────────────────────────

BANK_SHAPE = """
endpoints:
  - id: bank
    name: Bank
    template: seplos_bms_v3_rtu_tap
    enabled: false
    connection: {protocol: rtu_tap, serial_port: /dev/ttyBANK, baudrate: 19200, stale_after_s: 60}
    groups:
      - id: units
        connection: {protocol: rtu_tap, serial_port: /dev/ttyBANK, baudrate: 19200, stale_after_s: 60}
        units: [1, 2]
devices:
  - id: hp
    name: Heat pump
    template: samsung_mim_b19n
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.70, port: 502, unit_id: 2}
"""


@needs_tc
def test_explicit_defaults_restart_nothing(tmp_path, caplog):
    """A form that writes down a default (parity N, timeout 3, mqtt.enabled)
    changes the file, not what anything runs on: nothing restarts."""
    import logging
    cfg, client = make_app(tmp_path, BANK_SHAPE)
    caplog.set_level(logging.INFO, logger="multibus.api")
    r = client.put("/api/endpoints/bank", json={"id": "bank", "groups": [
        {"id": "units", "units": [{"unit_id": 1, "id": "bank-u1"}, {"unit_id": 2, "id": "bank-u2"}],
         "connection": {"protocol": "rtu_tap", "serial_port": "/dev/ttyBANK", "baudrate": 19200,
                        "parity": "N", "stopbits": 1, "bytesize": 8, "stale_after_s": 60}}]})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "unchanged" or "pollers kept running" in caplog.text
    r = client.put("/api/devices/hp", json={"id": "hp", "name": "Heat pump", "template": "samsung_mim_b19n",
                                            "enabled": False, "connection": {"protocol": "tcp", "host": "192.0.2.70",
                                            "port": 502, "unit_id": 2, "timeout": 3},
                                            "mqtt": {"enabled": True}, "influxdb": {"enabled": True}})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "unchanged" or r.json().get("restarted") is False, r.json()


@needs_tc
def test_a_group_source_moved_to_another_port_restarts_its_units(tmp_path):
    """Before, the restart decision did not look at groups: a source moved
    to another port kept polling the old one until the next boot."""
    cfg, client = make_app(tmp_path, PLANT)
    ep = _ep(cfg, "pv")
    ep["groups"][0]["sources"][0]["port"] = 1502
    r = client.put("/api/endpoints/pv", json=ep)
    assert r.status_code == 200, r.text
    assert cfg.get_device("pv-u1").sources[0].connection.port == 1502


def test_a_stale_page_is_told_to_reload():
    old = {"bridge": "ws", "bridge_port": 4196, "unit_id": 2}
    moved = [{"id": "ws", "type": "rtu_transparent", "host": "192.0.2.61", "ports": [{"port": 4196}]}]
    with pytest.raises(BridgeConflict) as e:
        merge_connection({"protocol": "rtu-tcp", "host": "192.0.2.60", "port": 4196}, old, moved)
    assert "reload" in str(e.value) and "192.0.2.61" in str(e.value)


@needs_tc
def test_a_rule_in_error_heals_on_an_unchanged_save(tmp_path):
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    rt.errors['ov-u1'] = "target: command 'power_limit' is not enabled on 'pv-u1'"   # as at a boot before the fix
    stored = json.loads(json.dumps(rt.raw['ov-u1']))
    assert client.put("/api/rules/ov-u1", json=stored).status_code == 200
    assert 'ov-u1' not in rt.errors
    assert rt.set_mode('ov-u1', 'armed', who='t') == []


@needs_tc
def test_asking_for_the_mode_a_rule_has_is_nothing(tmp_path):
    cfg, app, client, clock, rt = _rule_app(tmp_path, {'pv-u1': _Inverter(sf=-2)}, mode='armed')
    audits = []
    rt._audit = lambda *a, **k: audits.append(a)
    assert rt.set_mode('ov-u1', 'armed', who='t') == [] and audits == []


def _record_stops(client):
    """The ids of the units a save takes out of the running registry."""
    reg, stopped = client.app.state.registry, []
    remove = reg.remove
    reg.remove = lambda did, *a, **k: (stopped.append(did), remove(did, *a, **k))[1]
    return stopped


@needs_tc
def test_a_new_rhythm_on_one_group_restarts_only_its_units(tmp_path, caplog):
    """In prod a 2 → 5 s change on the site meter's source restarted the four
    inverters beside it: the decision was taken for the whole installation."""
    import logging
    cfg, client = make_app(tmp_path, PLANT)
    caplog.set_level(logging.INFO, logger="multibus.api")
    stopped = _record_stops(client)
    ep = _ep(cfg, "pv")
    ep["groups"][0]["sources"][0]["poll_groups"]["realtime"]["interval"] = 2
    r = client.put("/api/endpoints/pv", json=ep)
    assert r.status_code == 200, r.text
    assert sorted(stopped) == ["pv-u1", "pv-u2"], stopped
    assert r.json()["restarted"] == ["pv-u1", "pv-u2"], r.json()["restarted"]
    assert cfg.get_device("pv-u1").sources[0].poll_groups["realtime"].interval == 2
    assert "the others kept running" in caplog.text


@needs_tc
def test_a_page_that_echoes_names_and_enabled_restarts_nothing(tmp_path):
    """The source editor sends the whole installation back: each group and
    source `enabled: true`, every unit's default name, the flat units beside
    the groups. None of it changes what a unit reads, and none of it is
    written: a frozen default name stops following the installation's."""
    cfg, client = make_app(tmp_path, PLANT)
    before = _ep(cfg, "pv")
    stopped = _record_stops(client)
    r = client.put("/api/endpoints/pv", json=_echoed(before))
    assert r.status_code == 200, r.text
    assert stopped == [] and r.json()["status"] == "unchanged", (stopped, r.json())
    assert _ep(cfg, "pv") == before


@needs_tc
def test_a_rhythm_change_sent_with_echoes_writes_only_the_rhythm(tmp_path):
    """The prod save of 2026-10-22 21:33, in shape."""
    cfg, client = make_app(tmp_path, PLANT)
    before = _ep(cfg, "pv")
    ep = _echoed(before)
    ep["groups"][0]["sources"][0]["poll_groups"]["realtime"]["interval"] = 2
    assert client.put("/api/endpoints/pv", json=ep).status_code == 200
    before["groups"][0]["sources"][0]["poll_groups"]["realtime"]["interval"] = 2
    before.update(template="", units=[])          # what every real save spells out
    assert _ep(cfg, "pv") == before
    # renamed later, the units follow: their names were never frozen
    assert client.put("/api/endpoints/pv", json={"name": "Roof"}).status_code == 200
    assert cfg.get_device("pv-u1").name == "Roof unit 1"


@needs_tc
def test_a_name_given_by_hand_is_kept(tmp_path):
    cfg, client = make_app(tmp_path, PLANT)
    ep = _echoed(_ep(cfg, "pv"))
    ep["groups"][0]["units"][1]["name"] = "East roof"
    assert client.put("/api/endpoints/pv", json=ep).status_code == 200
    assert cfg.get_device("pv-u2").name == "East roof" and cfg.get_device("pv-u1").name == "PV unit 1"


def _echoed(ep):
    """An installation as an old page sends it back."""
    ep = copy.deepcopy(ep)
    for g in ep["groups"]:
        g["enabled"] = True
        g["units"] = [{"unit_id": u, "id": f"pv-u{u}", "name": f"PV unit {u}"} for u in g["units"]]
        for s in g.get("sources") or []:
            s["enabled"] = True
    ep["units"] = ep["groups"][0]["units"]
    return ep
