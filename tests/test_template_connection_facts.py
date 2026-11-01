# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""What a template says about connecting to it, and the matrix's other half
(ADR-0031 §5, L3.2; FINDINGS L3-F6, L3-F14).

- The template summary gains ``speaks``, ``listen_only``, ``transports``,
  ``default_unit_id``, ``setup`` ({count, writes}), ``identify``, ``ports``
  and the optional ``protocol.line``, ``protocol.http.path`` and
  ``protocol.master_hint`` (validated when a template is saved).
- ``/api/protocols`` adds ``invalid`` (the cells a save refuses, each with a
  code and its words) and ``defaults`` (every advanced key with its
  effective default and where it comes from).
- The role words are Master / Listen-only."""
import copy
import glob
import json

import pytest

from tests.test_devices_api import make_app, needs_tc

_NEW_KEYS = {"speaks", "listen_only", "transports", "default_unit_id", "setup", "identify", "ports",
             "line", "http_path", "master_hint"}


def _registry():
    from multibus.device_template import TemplateRegistry
    return TemplateRegistry()


# ── the summary keys, every bundled template ─────────────────────────────
@pytest.mark.parametrize("path", sorted(glob.glob("multibus/device_templates/*.json")))
def test_every_bundled_template_summary_has_the_connection_facts(path):
    with open(path) as f:
        tid = json.load(f)["device_template"]["id"]
    t = _registry().get(tid)
    s = t.summary()
    assert _NEW_KEYS <= set(s), _NEW_KEYS - set(s)
    assert s["speaks"] in ("modbus", "seplos_v2", "can_signals", "json_http", "json_mqtt")
    assert isinstance(s["listen_only"], bool)
    assert isinstance(s["transports"], list)
    assert s["ports"] and set(s["ports"]) <= {"serial", "ethernet", "can", "http", "mqtt"}
    assert set(s["setup"]) == {"count", "writes"} and s["setup"]["count"] == len(s["setup"]["writes"])
    assert set(s["identify"]) == {"fingerprint", "sunspec"}
    assert s["line"] is None or set(s["line"]) == {"baudrate", "parity", "bytesize", "stopbits"}


def test_the_facts_per_template_class():
    reg = _registry()
    deye = reg.get("deye_hybrid_1p").summary()
    assert deye["speaks"] == "modbus" and deye["ports"] == ["serial", "ethernet"]
    assert deye["default_unit_id"] == 1 and deye["identify"]["fingerprint"] is True
    assert deye["line"] == {"baudrate": 9600, "parity": "N", "bytesize": 8, "stopbits": 1}
    tap = reg.get("seplos_bms_v2_tap").summary()
    assert tap["speaks"] == "seplos_v2" and tap["listen_only"] is True and tap["ports"] == ["serial"]
    v3 = reg.get("seplos_bms_v3_rtu_tap").summary()
    assert v3["listen_only"] is True and v3["transports"] == ["rtu_tap"]
    can = reg.get("pylontech_can_inverter").summary()
    assert can["speaks"] == "can_signals" and can["ports"] == ["can"] and can["listen_only"] is True
    sam = reg.get("samsung_mim_b19n").summary()
    assert sam["setup"]["count"] == 2 and all(w["fc"] == 16 for w in sam["setup"]["writes"])
    assert sam["line"] == {"baudrate": 9600, "parity": "E", "bytesize": 8, "stopbits": 1}
    web = reg.get("fronius_solar_api_meter").summary()
    assert web["speaks"] == "json_http" and web["ports"] == ["http"]
    assert web["http_path"] == "/solar_api/v1/GetMeterRealtimeData.cgi?Scope=Device&DeviceId=${unit_id}"
    assert reg.get("fronius_solar_api_site").summary()["http_path"] == \
        "/solar_api/v1/GetPowerFlowRealtimeData.fcgi"
    gx = reg.get("victron_gx_mqtt").summary()
    assert gx["speaks"] == "json_mqtt" and gx["ports"] == ["mqtt"] and gx["line"] is None
    eastron = reg.get("eastron_sdm630").summary()
    assert eastron["master_hint"] and eastron["transports"] == [] and eastron["line"] is None
    assert eastron["ports"] == ["serial", "ethernet"]
    sunspec = reg.get("fronius_sunspec_meter").summary()
    assert sunspec["identify"]["sunspec"] is True                       # the SunSpec marker recognises it


def _tpl(**proto):
    return {"device_template": {"id": "t-x", "name": "X", "protocol": proto,
                                "registers": [{"address": 0, "name": "voltage_l1_n"}]}}


@pytest.mark.parametrize("proto, err", [
    ({"line": {"baudrate": 9600, "parity": "N", "bytesize": 8, "stopbits": 1}}, None),
    ({"line": {"baudrate": 19200}}, None),
    ({"line": "9600 8N1"}, "protocol.line"),
    ({"line": {"baudrate": 12}}, "protocol.line.baudrate"),
    ({"line": {"parity": "X"}}, "protocol.line.parity"),
    ({"line": {"bytesize": 9}}, "protocol.line.bytesize"),
    ({"line": {"stopbits": 3}}, "protocol.line.stopbits"),
    ({"line": {"baud": 9600}}, "protocol.line"),
    ({"line": {}}, "protocol.line"),
    ({"http": {"path": "/solar_api/v1/x.cgi?DeviceId=${unit_id}"}}, None),
    ({"http": {"path": "http://evil/x"}}, "protocol.http.path"),
    ({"http": {"path": "no-slash"}}, "protocol.http.path"),
    ({"http": {"path": "/a b"}}, "protocol.http.path"),
    ({"http": "x"}, "protocol.http"),
    ({"master_hint": "often polled by the inverter as its grid meter"}, None),
    ({"master_hint": ""}, "protocol.master_hint"),
    ({"master_hint": 3}, "protocol.master_hint"),
    ({"master_hint": "x" * 301}, "protocol.master_hint"),
])
def test_the_new_template_keys_are_validated(proto, err):
    from multibus.device_template import validate_template
    errs = validate_template(_tpl(**proto))
    if err is None:
        assert errs == [], errs
    else:
        assert any(e.startswith(err) for e in errs), errs


# ── the matrix's invalid cells and the defaults ──────────────────────────
# Seplos through a bridge record and CAN over a bridge are `not_yet` cells
# of the matrix (with their note), never `invalid` (review m2)
_REASONS = {"listen_ascii", "listen_mbap", "listen_gateway_bridge", "can_poll"}


@needs_tc
def test_api_protocols_serves_invalid_cells_with_reasons(tmp_path):
    _cfg, client = make_app(tmp_path)
    got = client.get("/api/protocols").json()
    inv = got["invalid"]["device"]
    assert {c["code"] for c in inv} == _REASONS
    for c in inv:
        assert {"port", "reach", "protocol", "framing", "udp", "mode", "code", "text"} <= set(c), c
        assert isinstance(c["text"], str) and len(c["text"]) > 20, c
        # never a cell the matrix offers today
        same = [m for m in got["matrix"]["device"]
                if all(m.get(k) == c.get(k) for k in ("port", "reach", "protocol", "framing", "udp", "mode"))]
        assert all(m["status"] != "today" for m in same), (c, same)


def _post(client, conn, i):
    body = {"id": f"inv-{i}", "name": f"Inv {i}", "enabled": False, "connection": conn}
    return client.post("/api/devices", json=body)


@needs_tc
def test_the_invalid_cells_with_a_stored_form_are_refused_by_a_save(tmp_path):
    _cfg, client = make_app(tmp_path, extra_yaml="""
bridges:
  - {id: gw, name: Gateway, type: modbus_gateway, host: 192.0.2.70, ports: [{port: 502}]}
  - {id: ws, name: Panel, type: rtu_transparent, host: 192.0.2.60, ports: [{port: 4196}]}
""")
    assert _post(client, {"protocol": "rtu_tap", "bridge": "gw", "bridge_port": 502, "unit_id": 1}, 1)\
        .status_code == 422
    assert _post(client, {"protocol": "seplos_v2-tcp", "bridge": "ws", "bridge_port": 4196, "unit_id": 1,
                          "mode": "listen"}, 2).status_code == 422
    assert _post(client, {"protocol": "can", "carrier": "bridge", "channel": "can0"}, 3).status_code == 422


@needs_tc
def test_api_protocols_serves_the_defaults_with_their_sources(tmp_path):
    cfg, client = make_app(tmp_path)
    d = client.get("/api/protocols").json()["defaults"]
    assert set(d) == {"modbus", "seplos_v2", "can_signals", "json_http", "json_mqtt"}
    mb = d["modbus"]
    built = cfg._modbus_from_conn({})
    for key in ("timeout", "retry_attempts", "retry_delay", "stale_after_s", "max_gap", "serialize_endpoint",
                "share_transport", "max_connections", "endpoint_wait_s", "endpoint_min_gap_s", "baudrate",
                "parity", "stopbits", "bytesize", "port", "unit_id", "drop_all_zero", "illegal_registers"):
        assert mb[key]["value"] == getattr(built, key), key
        assert mb[key]["source"] == "code", key
    assert mb["startup_jitter_s"]["value"] == cfg.modbus.startup_jitter_s
    assert mb["startup_jitter_s"]["source"] == "gateway"
    assert mb["connect_settle_s"]["source"] == "template" and mb["connect_settle_s"]["value"] is None
    assert d["seplos_v2"]["mode"]["value"] is None and d["seplos_v2"]["mode"]["source"] == "none"
    assert d["json_http"]["timeout"]["value"] == 8 and d["json_mqtt"]["port"]["value"] == 1883
    assert d["can_signals"]["stale_after_s"]["value"] == 30


@needs_tc
def test_the_role_words_are_master_and_listen_only(tmp_path):
    from multibus.connection_spec import LABELS
    assert LABELS["mode"] == {"poll": "Master", "listen": "Listen-only"}
    _cfg, client = make_app(tmp_path)
    got = client.get("/api/protocols").json()
    assert got["labels"]["mode"] == {"poll": "Master", "listen": "Listen-only"}
    # the keys it answered with before stay
    for k in ("device", "source", "endpoint", "group", "meter_only", "serial", "network", "matrix"):
        assert k in got, k
    assert copy.deepcopy(got["matrix"]) == got["matrix"]



# ── review of l3.2a (m2, m3, NIT) ────────────────────────────────────────
def test_invalid_is_disjoint_from_the_matrix():
    from multibus.connection_matrix import INVALID
    from multibus.connection_spec import MATRIX
    keys = ("port", "reach", "protocol", "framing", "udp", "mode")
    offered = {tuple(c.get(k) for k in keys) for c in MATRIX["device"]}
    for c in INVALID["device"]:
        assert tuple(c.get(k) for k in keys) not in offered, c


class _Reg:
    def __init__(self, address):
        self.address = address


class _Tpl:
    def __init__(self, tid="x", name="X", identify=None, protocol=None, registers=()):
        self.id, self.name, self.identify = tid, name, identify or {}
        self.protocol, self.registers = protocol or {}, list(registers)


def test_identify_says_fingerprint_and_sunspec_from_the_map():
    from multibus.template_facts import identifies
    reg = _registry()
    assert reg.get("deye_hybrid_1p").summary()["identify"] == {"fingerprint": True, "sunspec": False}
    assert reg.get("fronius_sunspec_meter").summary()["identify"] == {"fingerprint": False, "sunspec": True}
    assert reg.get("eastron_sdm630").summary()["identify"] == {"fingerprint": False, "sunspec": False}
    # a name that says SunSpec is not a SunSpec map; a map that reads the marker is
    assert identifies(_Tpl("sunspec_lookalike", "SunSpec-ish")) == {"fingerprint": False, "sunspec": False}
    assert identifies(_Tpl(registers=[_Reg(40000)]))["sunspec"] is True
    assert identifies(_Tpl(protocol={"sunspec": True}))["sunspec"] is True


def test_a_line_from_disk_is_normalised_and_a_bad_one_dropped():
    from multibus.template_facts import line_of
    assert line_of(_Tpl(protocol={"line": {"baudrate": 9600, "parity": "e"}})) == {
        "baudrate": 9600, "parity": "E", "bytesize": 8, "stopbits": 1}
    assert line_of(_Tpl(protocol={"line": {"baudrate": "fast"}})) is None
    assert line_of(_Tpl(protocol={"line": {"parity": "X"}})) is None
