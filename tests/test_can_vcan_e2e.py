# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""CAN end to end on a real vcan interface (tools/e2e/can_vcan/run.sh).

The bench capture is replayed on ``vcan0`` with its own timing (sped up),
a CAN device built from config.yaml listens through the real ``socketcan``
carrier, and a separate sniffer counts every frame on the bus. Skipped
unless ``MBG_VCAN`` names the interface — it only runs in the throwaway
container the script makes (CAP_NET_ADMIN, --network none)."""
import importlib.util
import os
import socket
import struct
import threading
import time
from pathlib import Path

import pytest

IFACE = os.environ.get("MBG_VCAN", "")
pytestmark = pytest.mark.skipif(not IFACE or not hasattr(socket, "AF_CAN"),
                                reason="needs a vcan interface (tools/e2e/can_vcan/run.sh)")

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "can" / "deye_seplos_v2_20261026.candump"
SPEED = float(os.environ.get("MBG_VCAN_SPEED", "10"))
FRAME = struct.Struct("=IB3x8s")


def _vcan():
    spec = importlib.util.spec_from_file_location("vcan", ROOT / "tools/e2e/can_vcan/vcan.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Sniffer(threading.Thread):
    """Every frame on the bus, whoever sent it."""

    def __init__(self):
        super().__init__(daemon=True)
        self.s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        self.s.settimeout(0.2)
        self.s.bind((IFACE,))
        self.frames = []
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                raw = self.s.recv(FRAME.size)
            except socket.timeout:
                continue
            except OSError:                    # ENETDOWN while the link is down
                time.sleep(0.05)
                continue
            cid, dlc, data = FRAME.unpack(raw)
            self.frames.append((cid, data[:dlc]))


def test_replay_on_vcan_through_the_socketcan_carrier(tmp_path):
    from multibus.can_client import CanClient
    from multibus.can_signal import parse_candump_line
    from multibus.device_runtime import build_device_client
    from multibus.device_seed import seed_devices_at_boot
    from multibus.device_template import TemplateRegistry
    from tests.test_can_client import EXPECTED
    from tests.test_devices import write_config

    vcan = _vcan()
    cfg = write_config(tmp_path, extra_yaml=f"""
devices:
  - id: bms-can
    template: pylontech_can_inverter
    connection: {{protocol: can, carrier: socketcan, channel: {IFACE}, bitrate: 500000, stale_after_s: 5}}
""")
    reg = TemplateRegistry(user_dir=tmp_path / "tpl")
    seed_devices_at_boot(cfg, reg)
    unit = build_device_client(cfg, reg, cfg.get_device("bms-can"))
    drv = unit.parts[0][1]
    assert isinstance(drv, CanClient)
    seen = {}
    unit.publish_callback = lambda g, data: seen.update(
        {e["register"].name: e["value"] for e in data.values()})

    sniff = Sniffer()
    sniff.start()
    assert unit.connect() and drv.connected

    tx = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    tx.bind((IFACE,))
    lines = [parse_candump_line(x) for x in FIX.read_text().splitlines()]
    t0, start = lines[0][0], time.monotonic()
    sent, cut_at, frames_before_cut, down_s = 0, len(lines) * 4 // 10, None, 1.0
    for i, (ts, _if, cid, ext, data) in enumerate(lines):
        if i == cut_at:
            frames_before_cut = drv.frames
            vcan.set_up(IFACE, False)          # the interface goes away for a second
            time.sleep(down_s)
            vcan.set_up(IFACE, True)
            start += down_s
        delay = start + (ts - t0) / SPEED - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        try:
            tx.send(FRAME.pack(cid | (0x80000000 if ext else 0), len(data), data.ljust(8, b"\0")))
            sent += 1
        except OSError:
            pass
    time.sleep(0.5)
    sniff.stop.set()
    sniff.join(2)
    sniff.s.close()
    st = drv.get_stats()["can"]
    sock = drv._carrier._sock
    unit.disconnect()
    tx.close()

    # what the gateway decoded through the socket = the same capture decoded
    # offline (the last frame set wins in both), every row present
    offline = {}
    ref = CanClient({}, drv.registers, carrier=object())
    ref.publish_callback = lambda g, data: offline.update(
        {e["register"].name: e["value"] for e in data.values()})
    for _ts, _if, cid, ext, data in lines:
        ref.on_frame(cid, ext, False, False, data)
    assert set(seen) == set(offline) == set(EXPECTED) - {"total_capacity", "online_packs", "cycles"}
    assert seen == offline
    # the interface's own listen-only flag: a vcan has none to report
    assert st["listen_only"] is None
    # the socket the driver holds has no way to send
    from multibus.can_client import _ReceiveOnlySocket
    assert isinstance(sock, _ReceiveOnlySocket)
    assert not any(hasattr(sock, n) for n in ("send", "sendto", "sendmsg", "fileno"))
    assert st["unknown_frames"] == 0 and st["decode_errors"] == 0
    # it came back after the interface went down and up, and kept reading
    assert st["reopens"] >= 1 and st["open_error"] is None
    assert st["frames"] > frames_before_cut + 100
    # listen-only: the bus carried exactly what the test sent — nothing else
    assert len(sniff.frames) == sent, (len(sniff.frames), sent)


def test_a_device_test_hears_the_bus_and_sends_nothing():
    """The Test button's probe (can_client.hear) on a live vcan: the interface
    exists and is up, has no listen-only flag (vcan), and every frame the
    bus carried was one the test sent — the probe transmitted nothing."""
    from multibus.can_client import hear, hear_verdict

    sniff = Sniffer()
    sniff.start()
    tx = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    tx.bind((IFACE,))
    stop, sent = threading.Event(), [0]

    def send():
        while not stop.is_set():
            tx.send(FRAME.pack(0x351, 8, bytes(8)))
            sent[0] += 1
            time.sleep(0.05)
    th = threading.Thread(target=send, daemon=True)
    th.start()
    h = hear({"carrier": "socketcan", "channel": IFACE}, seconds=1.5)
    stop.set()
    th.join(2)
    time.sleep(0.3)
    sniff.stop.set()
    sniff.join(2)
    sniff.s.close()
    tx.close()
    assert h["exists"] is True and h["up"] is True and h["listen_only"] is None
    assert h["frames"] >= 10 and h["ids"] == {"0x351": h["frames"]}
    ok, msg = hear_verdict(h)
    assert ok is True and "listen-only: unknown" in msg, msg
    assert len(sniff.frames) == sent[0], (len(sniff.frames), sent[0])

    # a down interface is said as down, not as missing
    vcan = _vcan()
    vcan.set_up(IFACE, False)
    try:
        h = hear({"carrier": "socketcan", "channel": IFACE}, seconds=0.2)
    finally:
        vcan.set_up(IFACE, True)
    assert h["exists"] is True and h["up"] is False
    assert hear_verdict(h)[0] is False and "down" in hear_verdict(h)[1]
