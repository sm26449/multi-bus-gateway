# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Review of the can-listen lot (FINDINGS CAN-6..CAN-16): each test failed
before its fix."""
import ast
import json
import struct
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from multibus.can_client import CanClient
from multibus.device_template import TemplateRegistry, parse_template
from tests.test_devices_api import make_app, needs_tc

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "multibus" / "device_templates" / "pylontech_can_inverter.json"
CAN_YAML = """
devices:
  - id: bms-can
    template: pylontech_can_inverter
    connection: {protocol: can, carrier: socketcan, channel: can0, bitrate: 500000}
"""


def template():
    return parse_template(json.loads(TPL.read_text()))


class Car:
    def __init__(self, open_fails=0, open_delay=0.0):
        self.open_fails, self.open_delay, self.is_open = open_fails, open_delay, False

    def open(self):
        if self.open_fails:
            self.open_fails -= 1
            raise OSError(19, "No such device")
        time.sleep(self.open_delay)
        self.is_open = True

    def recv(self):
        time.sleep(0.01)
        return None

    def close(self):
        self.is_open = False


def client(**kw):
    c = CanClient({"channel": "can0"}, template().registers, carrier=kw.pop("carrier", Car()), **kw)
    groups, got = [], {}

    def cb(group, data):
        groups.append(group)
        got.update({e["register"].name: e["value"] for e in data.values()})
    c.publish_callback = cb
    return c, groups, got


# CAN-6 — calculated fields on a CAN device
def test_can_values_arrive_on_the_push_group_calcs_evaluate_on():
    from multibus.calc_engine import CalcEngine
    c, groups, _ = client()
    c.on_frame(0x355, False, False, False, bytes.fromhex("1D006400D90CB40C"))
    assert groups == ["tap"]
    dev = SimpleNamespace(id="bms-can", protocol="can", sources=[SimpleNamespace(protocol="can")])
    cfg = SimpleNamespace(get_device=lambda _id: dev)
    assert CalcEngine(cfg, lambda _d: None, lambda: (None, None))._push_group("bms-can") == "tap"


# CAN-7 — a CAN row saved without its frame gets it back from the template
@needs_tc
def test_a_can_row_saved_without_its_frame_gets_it_from_the_template(tmp_path):
    _cfg, cl = make_app(tmp_path, extra_yaml=CAN_YAML)
    sel = [{"address": 850, "name": "soc", "label": "SOC", "unit": "%", "data_type": "uint16",
            "poll_group": "realtime", "register_type": "input"}]
    assert cl.post("/api/registers/selected?device=bms-can", json=sel).status_code == 200
    got = cl.get("/api/registers/selected?device=bms-can").json()["registers"]
    assert (got[0]["can_id"], got[0]["start_bit"], got[0]["bit_length"]) == ("0x355", 0, 16)


def test_the_register_page_copies_the_can_fields_in_both_add_paths():
    js = (ROOT / "ui" / "js" / "app-registers.js").read_text()
    assert js.count("_canFieldsOf(") >= 3          # the helper + quick-add + the Add modal


# CAN-8 — a genuine Pylontech's shorter frames; vendor rows not seeded
def test_pylontech_length_frames_leave_vendor_rows_absent_not_failed():
    c, _, got = client()
    c.on_frame(0x351, False, False, False, bytes.fromhex("4002B80BDA16"))       # DLC 6
    c.on_frame(0x359, False, False, False, bytes.fromhex("000000000A504E"))     # DLC 7, "PN"
    c.on_frame(0x35C, False, False, False, bytes.fromhex("C000"))               # DLC 2
    st = c.get_stats()
    assert st["can"]["decode_errors"] == 0 and st["failed_reads"] == 0
    assert st["successful_reads"] == 3
    assert got["charge_voltage_limit"] == pytest.approx(57.6) and got["module_count"] == 10
    assert got["charge_enable"] == 1
    assert "discharge_voltage_limit" not in got and "cycles" not in got and "online_packs" not in got
    assert st["can"]["absent_signals"] == 3


def test_a_row_cut_by_the_frame_is_still_an_error():
    c, _, _ = client()
    c.on_frame(0x356, False, False, False, bytes.fromhex("821485"))            # current cut at 1 byte
    assert c.get_stats()["can"]["decode_errors"] == 1


def test_vendor_extension_rows_are_not_seeded(tmp_path):
    from tests.test_devices import write_config
    from multibus.device_seed import seed_devices_at_boot
    cfg = write_config(tmp_path, extra_yaml=CAN_YAML)
    seed_devices_at_boot(cfg, TemplateRegistry(user_dir=tmp_path / "tpl"))
    dev = cfg.get_device("bms-can")
    names = {r.name for r in cfg.load_device_registers(dev, source=dev.sources[0])[0]}
    assert not names & {"total_capacity", "online_packs", "cycles"}
    assert {"soc", "pack_voltage", "charge_voltage_limit", "max_cell_voltage"} <= names


# CAN-9 — the interface's own listen-only mode
def _nl_link(ctrl_flags=None, kind=b"can"):
    def attr(t, data):
        raw = struct.pack("=HH", 4 + len(data), t) + data
        return raw + b"\0" * (-len(raw) % 4)
    info = attr(1, kind + b"\0")
    if ctrl_flags is not None:
        info += attr(2, attr(5, struct.pack("=II", 0xFF, ctrl_flags)))
    body = struct.pack("=BxHiII", 0, 280, 3, 0x41, 0) + attr(3, b"can0\0") + attr(18, info)
    return struct.pack("=IHHII", 16 + len(body), 16, 0, 1, 0) + body


def test_listen_only_is_read_from_the_links_ctrlmode():
    from multibus.can_client import parse_listen_only
    assert parse_listen_only(_nl_link(0x02)) is True
    assert parse_listen_only(_nl_link(0x00)) is False
    assert parse_listen_only(_nl_link(None, b"vcan")) is None
    c, _, _ = client()
    assert "listen_only" in c.get_stats()["can"]


# CAN-10 — overlap warnings survive the canonical checks
def test_overlap_and_canonical_warnings_are_both_kept(tmp_path):
    user = tmp_path / "tpl"
    user.mkdir()
    rows = [{"address": 1, "name": "soc", "data_type": "uint16", "can_id": "0x355",
             "start_bit": 0, "bit_length": 8, "defaults": {"mqtt": {"enabled": True}}},
            {"address": 2, "name": "Weird_Name", "data_type": "uint16", "can_id": "0x355",
             "start_bit": 4, "bit_length": 4, "defaults": {"mqtt": {"enabled": True}}}]
    (user / "t.json").write_text(json.dumps({"device_template": {
        "id": "can_ov", "name": "x", "canonical": True,
        "protocol": {"transports": ["can"]}, "registers": rows}}))
    reg = TemplateRegistry(user_dir=user)
    w = reg.load_warnings.get("t.json", "")
    assert "overlaps" in w and "non-canonical" in w, w


# CAN-11 — an API edit of a config-defined CAN device
@needs_tc
def test_a_can_device_can_be_edited_and_listed_through_the_api(tmp_path):
    _cfg, cl = make_app(tmp_path, extra_yaml=CAN_YAML)
    r = cl.put("/api/devices/bms-can", json={
        "id": "bms-can", "name": "Bank CAN", "enabled": True, "template": "pylontech_can_inverter",
        "connection": {"protocol": "can", "carrier": "socketcan", "channel": "can0"}})
    assert r.status_code == 200, r.text
    from multibus.config import Config
    dev = Config(str(tmp_path / "config.yaml")).get_device("bms-can")
    assert dev.name == "Bank CAN" and dev.sources[0].can["channel"] == "can0"
    assert cl.get("/api/devices?protocol=can").status_code == 200
    bad = cl.put("/api/devices/bms-can", json={
        "id": "bms-can", "template": "pylontech_can_inverter",
        "connection": {"protocol": "can", "carrier": "pigeon", "channel": "can0;rm"}})
    assert bad.status_code == 422


# CAN-12 — "update from template" refreshes the CAN coordinates
def test_template_refresh_brings_the_can_coordinates(tmp_path):
    from multibus.template_refresh import plan_file, refresh_file
    p = tmp_path / "selected_registers.json"
    p.write_text(json.dumps({"registers": [
        {"address": 850, "name": "soc", "label": "SOC", "data_type": "uint16",
         "can_id": "0x355", "start_bit": 8, "bit_length": 8, "byte_order": "little"}]}))
    plan = plan_file(p, template())
    assert any(ch["key"] == "start_bit" and ch["after"] == 0 for ch in plan["changes"]), plan
    assert refresh_file(p, template())["registers"] == 1
    row = json.loads(p.read_text())["registers"][0]
    assert (row["start_bit"], row["bit_length"]) == (0, 16)


# CAN-13 — a disconnect while the reader is opening leaks nothing
def test_disconnect_while_opening_closes_what_was_opened():
    car = Car(open_fails=1, open_delay=0.3)
    c, _, _ = client(carrier=car)
    c.REOPEN_S = 0.01
    c.connect()
    time.sleep(0.1)                     # the reader is inside open() now
    c.disconnect()
    time.sleep(0.4)
    assert not car.is_open
    # this client's reader only (an API test above may have started a device's own)
    assert not any(t.name == "can:can0:" and t.is_alive() for t in threading.enumerate())


# CAN-14 — a Motorola string is named, not "does not fit"
def test_a_big_endian_string_gets_a_clear_message():
    from multibus.device_template import validate_template
    e = validate_template({"device_template": {"id": "can_s", "name": "s",
                           "protocol": {"transports": ["can"]}, "registers": [
                               {"address": 1, "name": "m", "data_type": "string:4", "can_id": "0x35E",
                                "start_bit": 7, "bit_length": 64, "byte_order": "big"}]}})
    assert any("strings are little-endian only" in x for x in e), e


# CAN-15 — reads and failures both count frames
def test_failed_reads_counts_frames_not_rows():
    c, _, _ = client()
    c.on_frame(0x356, False, False, False, bytes.fromhex("8214850000"))   # current ok? no: temp cut
    c.on_frame(0x370, False, False, False, bytes.fromhex("F500E300D90C"))  # min V absent, rest ok
    c.on_frame(0x371, False, False, False, bytes.fromhex("020003"))        # two rows cut / absent
    st = c.get_stats()
    assert st["failed_reads"] == 2 and st["successful_reads"] == 1
    assert st["can"]["decode_errors"] == 2


# CAN-16 — no send path reachable from outside
def test_the_carrier_socket_is_receive_only():
    from multibus import can_client as cc
    w = cc._ReceiveOnlySocket(SimpleNamespace(recv=lambda n: b"", close=lambda: None,
                                              send=lambda b: 1))
    for name in ("send", "sendall", "sendto", "sendmsg", "write", "fileno", "dup", "detach", "_s"):
        assert not hasattr(w, name), name
    tree = ast.parse((ROOT / "multibus" / "can_client.py").read_text())
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    banned = {"send", "sendall", "sendto", "sendmsg", "write", "writelines", "sendfile"}
    assert not (attrs | names) & banned
