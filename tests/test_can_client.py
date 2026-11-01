# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The CAN driver (ADR-0020): event-driven, listen-only, counters in the
device status; a CAN device from config.yaml to its client; the
pylontech_can_inverter template on the Deye ↔ Seplos V2 bench capture."""
import ast
import json
import threading
import time
from pathlib import Path

import pytest

from multibus.can_client import CanClient, SocketCanCarrier
from multibus.can_signal import parse_candump_line
from multibus.config import SelectedRegister, store_key
from multibus.device_template import TemplateRegistry, parse_template

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "can" / "deye_seplos_v2_20261026.candump"
TPL = ROOT / "multibus" / "device_templates" / "pylontech_can_inverter.json"


class FakeCarrier:
    """A carrier the test drives: frames to hand out, failures to raise."""

    def __init__(self, open_fails=0):
        self.open_fails = open_fails
        self.opens = 0
        self.closed = 0
        self.frames = []
        self.lost = False

    def open(self):
        self.opens += 1
        if self.open_fails:
            self.open_fails -= 1
            raise OSError(19, "No such device")

    def recv(self):
        if self.lost:
            self.lost = False
            raise OSError(100, "Network is down")
        if self.frames:
            return self.frames.pop(0)
        time.sleep(0.01)
        return None

    def close(self):
        self.closed += 1


def template():
    return parse_template(json.loads(TPL.read_text()))


def client(regs=None, carrier=None, **kw):
    c = CanClient({"carrier": "socketcan", "channel": "vcan0", "bitrate": 500000},
                  template().registers if regs is None else regs,
                  carrier=carrier or FakeCarrier(), device_id="bms", **kw)
    got = {}
    c.publish_callback = lambda group, data: got.update(
        {e["register"].name: (k, e["value"]) for k, e in data.items()})
    return c, got


def replay(c, lines):
    for line in lines:
        _ts, _if, cid, ext, data = parse_candump_line(line)
        c.on_frame(cid, ext, False, False, data)


# ── the bench capture through the bundled template ──────────────────────────

EXPECTED = {   # by hand from the frames, against the protocol documents
    "inverter_keepalive": 0,
    "charge_voltage_limit": 57.6, "charge_current_limit": 300.0,
    "discharge_current_limit": 585.0, "discharge_voltage_limit": 46.4,
    "soc": 29, "soh": 100,
    "pack_voltage": 52.5, "battery_current": 4.5, "battery_temperature": 24.5,
    "protection_flags": "", "alarm_flags": "", "module_count": 3, "total_capacity": 840,
    "online_packs": "pack 0, pack 1, pack 2",
    "full_charge_request": 0, "force_charge_1": 0, "force_charge_2": 0,
    "discharge_enable": 1, "charge_enable": 1, "cycles": 310,
    "manufacturer": "SHEnergy",
    "max_cell_temp": 24.5, "min_cell_temp": 22.7,
    "max_cell_voltage": 3.289, "min_cell_voltage": 3.252,
    "max_cell_temp_id": 2, "min_cell_temp_id": 3, "max_cell_voltage_id": 3, "min_cell_voltage_id": 3,
}


def test_the_first_second_of_the_bench_capture_decodes_every_row():
    c, got = client()
    replay(c, FIX.read_text().splitlines()[:9])        # one full frame set + 0x305
    vals = {k: v for k, (_key, v) in got.items()}
    assert set(vals) == set(EXPECTED)
    for name, want in EXPECTED.items():
        if isinstance(want, float):
            assert vals[name] == pytest.approx(want), name
        else:
            assert vals[name] == want, name


def test_the_whole_capture_is_clean_and_in_range():
    c, got = client()
    seen = {}

    def keep(group, data):
        for e in data.values():
            seen.setdefault(e["register"].name, set()).add(e["value"])
    c.publish_callback = keep
    replay(c, FIX.read_text().splitlines())
    st = c.get_stats()["can"]
    assert st["frames"] == 1071 and st["unknown_frames"] == 0 and st["decode_errors"] == 0
    assert seen["soc"] == {29}
    assert {round(v, 2) for v in seen["pack_voltage"]} <= {52.49, 52.50}
    assert {round(v, 1) for v in seen["battery_current"]} <= {4.1, 4.2, 4.3, 4.4, 4.5}
    assert {round(v, 3) for v in seen["max_cell_voltage"]} == {3.288, 3.289}


def test_values_are_keyed_like_any_input_row_and_stamped_on_arrival():
    c, got = client()
    c.on_frame(0x355, False, False, False, bytes.fromhex("1D006400D90CB40C"),
               now_mono=123.0, now_ts=456.0)
    key, val = got["soc"]
    assert val == 29 and key == store_key(850, "input")
    assert c.last_rx_mono == 123.0 and c.last_rx_ts == 456.0


# ── counters and health ─────────────────────────────────────────────────────

def test_unknown_ids_short_frames_error_and_remote_frames_are_counted():
    c, got = client()
    c.on_frame(0x123, False, False, False, b"\x00")
    c.on_frame(0x18FF50E5, True, False, False, b"\x00")
    c.on_frame(0x351, True, False, False, bytes(8))        # 0x351 but 29-bit: another frame
    c.on_frame(0x356, False, False, False, bytes.fromhex("8214"))   # DLC 2: only the voltage
    c.on_frame(0x351, False, True, False, b"")
    c.on_frame(0x20000004, False, False, True, bytes(8))
    st = c.get_stats()
    assert st["can"]["unknown_ids"] == {"0x123": 1, "0x18FF50E5x": 1, "0x351x": 1}
    assert st["can"]["unknown_frames"] == 3
    # current + temperature lie wholly past a 2-byte frame: absent, not errors (CAN-8)
    assert st["can"]["decode_errors"] == 0 and st["can"]["absent_signals"] == 2
    assert st["can"]["remote_frames"] == 1 and st["can"]["error_frames"] == 1
    assert st["error_counts"]["decode_errors"] == 0
    assert got["pack_voltage"][1] == pytest.approx(52.5)
    assert "battery_current" not in got


def test_freshness_is_stale_after_s_only():
    c, _ = client(stale_after_s=5)
    c.connected = True
    assert c.data_health()["status"] == "stale"            # nothing heard yet
    c.on_frame(0x355, False, False, False, bytes(8))
    assert c.data_health()["status"] == "ok" and c.alive()
    c.last_rx_mono = time.monotonic() - 6
    h = c.data_health()
    assert h["status"] == "stale" and h["stale"] and not c.alive()
    c.connected = False
    assert c.data_health()["status"] == "down"


def test_reopens_when_the_interface_is_missing_then_goes_down():
    car = FakeCarrier(open_fails=2)
    c, got = client(carrier=car)
    c.REOPEN_S = 0.02
    assert c.connect() is False and c.open_error            # not there yet
    deadline = time.monotonic() + 3
    while not c.connected and time.monotonic() < deadline:
        time.sleep(0.01)
    assert c.connected and car.opens == 3 and c.reopens == 2
    car.frames.append((0x355, False, False, False, bytes.fromhex("1D006400D90CB40C")))
    car.lost = True                                         # link down after the frame
    deadline = time.monotonic() + 3
    while (car.opens < 4 or "soc" not in got) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert car.opens >= 4 and car.closed >= 1 and got["soc"][1] == 29
    c.disconnect()
    assert not c.connected
    assert not any(t.name.startswith("can:vcan0") and t.is_alive() for t in threading.enumerate())


def test_update_registers_swaps_the_map():
    c, got = client()
    c.update_registers([r for r in template().registers if r.name == "soh"])
    c.on_frame(0x355, False, False, False, bytes.fromhex("1D006400D90CB40C"))
    assert set(got) == {"soh"}


# ── listen-only by construction ─────────────────────────────────────────────

def test_the_can_driver_has_no_transmit_path():
    src = (ROOT / "multibus" / "can_client.py").read_text()
    calls = {n.func.attr for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not calls & {"send", "sendall", "sendto", "sendmsg", "write", "writelines"}, calls
    public = {n for n in vars(SocketCanCarrier) if not n.startswith("_")}
    assert public == {"open", "recv", "close"}
    assert not hasattr(CanClient, "write_value")


def test_a_can_device_cannot_be_written():
    from multibus.multi_source import MultiSourceClient
    from types import SimpleNamespace
    c, _ = client()
    m = MultiSourceClient("bms", [(SimpleNamespace(id="default", protocol="can"), c)])
    ok, why, _ = m.write_value(850, 1)
    assert not ok and "no source supports writing" in why
    from multibus.role import listens_only
    from multibus.config import READ_ONLY_PROTOCOLS
    assert listens_only("can") and "can" in READ_ONLY_PROTOCOLS


# ── config.yaml → source → client ───────────────────────────────────────────

CAN_DEVICE = """
devices:
  - id: bms-can
    template: pylontech_can_inverter
    connection: {protocol: can, carrier: socketcan, channel: can0, bitrate: 500000, stale_after_s: 5}
"""


def test_a_can_device_from_config_builds_a_can_client(tmp_path):
    from tests.test_devices import write_config
    from multibus.config import protocol_family
    from multibus.device_runtime import build_device_client
    from multibus.device_seed import seed_devices_at_boot
    cfg = write_config(tmp_path, extra_yaml=CAN_DEVICE)
    dev = cfg.get_device("bms-can")
    assert dev.protocol == "can" and protocol_family("can") == "can"
    src = dev.sources[0]
    assert src.can == {"carrier": "socketcan", "channel": "can0", "bitrate": 500000, "stale_after_s": 5}
    reg = TemplateRegistry(user_dir=tmp_path / "tpl")
    seed_devices_at_boot(cfg, reg)
    regs, _ = cfg.load_device_registers(dev, source=src)
    # the vendor extensions a genuine Pylontech fills otherwise are not seeded (CAN-8)
    assert len(regs) == len(template().registers) - 3
    soc = next(r for r in regs if r.name == "soc")
    assert (soc.can_id, soc.start_bit, soc.bit_length, soc.byte_order) == (0x355, 0, 16, "little")
    m = build_device_client(cfg, reg, dev)
    drv = m.parts[0][1]
    assert isinstance(drv, CanClient)
    assert (drv.channel, drv.bitrate, drv.stale_after_s) == ("can0", 500000, 5.0)
    assert m.get_stats()["can"]["channel"] == "can0"


def test_a_modbus_device_gets_no_can_block(tmp_path):
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml="""
devices:
  - id: m1
    connection: {protocol: tcp, host: 192.0.2.10, channel: can0}
""")
    assert cfg.get_device("m1").sources[0].can == {}


def test_selected_register_round_trip_keeps_the_can_fields():
    from multibus.config import Config
    regs = Config._parse_selected_payload({"registers": [
        {"address": 850, "name": "soc", "label": "SOC", "data_type": "uint16",
         "can_id": "0x355", "start_bit": 0, "bit_length": 16, "byte_order": "little"},
        {"address": 1, "name": "x", "label": "x", "data_type": "uint16"}]})
    assert regs[0].can_id == 0x355 and regs[0].bit_length == 16
    assert regs[1].can_id is None
    assert isinstance(regs[0], SelectedRegister)


# ── the map's choices (FINDINGS CAN-1, CAN-5) ───────────────────────────────

def test_no_row_reads_the_vendor_specific_tail_of_0x355_or_0x356():
    """0x355 bytes 4-7 are cell mV on a Seplos (Victron: a 0.01 % SOC) and
    0x356 bytes 6-7 a min temperature: no row may read them as SOC."""
    for r in template().registers:
        if r.can_id in (0x355, 0x356):
            assert r.start_bit + r.bit_length <= (32 if r.can_id == 0x355 else 48), r.name


def test_the_template_speaks_the_canonical_battery_names():
    from multibus.canonical_fields import canonical_unit_for
    names = {r.name: r for r in template().registers}
    for n in ("soc", "soh", "pack_voltage", "battery_current", "charge_voltage_limit",
              "charge_current_limit", "discharge_current_limit", "discharge_voltage_limit",
              "max_cell_voltage", "min_cell_voltage", "max_cell_temp", "min_cell_temp",
              "cycles", "total_capacity", "manufacturer"):
        assert n in names, n
        cu = canonical_unit_for(n)
        assert not cu or names[n].unit == cu, (n, names[n].unit, cu)
