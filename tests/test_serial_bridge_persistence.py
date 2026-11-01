# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The serial bridge's port map under everything a site does to it: every
adapter keeps its TCP port — the address the gateway's devices are bound to —
across a restart, an upgrade, renumbering, replugging, a kernel renaming HAT
channels, a HAT taken out and put back, and a changed port range. A fake
sysfs per moment (no hardware); the state file is shared between "runs"."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

from tests.test_serial_bridge_board_uarts import _SRC, _driver, _tty, _usb, _write

SPI = "platform/axi/1f00050000.spi/spi_master/spi0/spi0.0"


def tree(base: Path, name: str, usb=(), hat=None):
    """A host at one moment. usb: (tty, socket, vid, pid, serial, minor);
    hat: {tty: line} for the two SC16IS752 channels, or None (no HAT)."""
    root = base / name
    for tty, sock, vid, pid, serial, minor in usb:
        _usb(root, tty, sock, vid, pid, serial, minor)
    if hat:
        for tty, line in hat.items():
            d = _tty(root, tty, SPI, f"509:{line}", line, type=114)
        _driver(root, d, "spi", "sc16is7xx")
    _write(root / "proc/cmdline", "rootwait\n")
    (root / "sys/class/tty").mkdir(parents=True, exist_ok=True)
    return root


class Run:
    """One bridge lifetime over a state directory."""

    def __init__(self, monkeypatch, state_dir: Path, root: Path, **env):
        monkeypatch.setenv("BRIDGE_STATE", str(state_dir / "portmap.json"))
        monkeypatch.setenv("SER2NET_CFG", str(state_dir / "ser2net.yaml"))
        monkeypatch.setenv("BRIDGE_SYSROOT", str(root))
        for k in ("BRIDGE_PORT_LOW", "BRIDGE_PORT_HIGH", "BRIDGE_EXCLUDE", "BRIDGE_INCLUDE",
                  "BRIDGE_ACCESS_FIX", "BRIDGE_TTY_SLOTS"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        spec = importlib.util.spec_from_file_location("sb_persist", _SRC)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        monkeypatch.setattr(self.m, "_open", lambda path, flags: os.open(path, os.O_RDONLY))

        class _Proc:
            def poll(self):
                return None

            def send_signal(self, _sig):
                pass
        monkeypatch.setattr(self.m, "_ser2net", _Proc())
        monkeypatch.setattr(self.m, "_clear_dead_pid_locks", lambda: None)
        self.state_dir = state_dir
        self.reconcile()

    def at(self, root: Path):
        """The host changes under a running bridge (a replug)."""
        self.m.SYSROOT = str(root)
        return self.reconcile()

    def reconcile(self):
        self.m.reconcile()
        return self.ports()

    def ports(self):
        return {a["stable_id"]: a.get("tcp_port") for a in self.m._adapters}

    def devs(self):
        return {a["stable_id"]: a["dev"] for a in self.m._adapters}

    def stored(self):
        return json.loads((self.state_dir / "portmap.json").read_text())

    def served(self):
        """tcp port → /dev path, as ser2net is told."""
        out, port = {}, None
        for line in (self.state_dir / "ser2net.yaml").read_text().splitlines():
            if "accepter: tcp," in line:
                port = int(line.rsplit(",", 1)[1])
            if "connector: serialdev," in line:
                out[port] = line.split(",")[1]
        return out


FTDI = ("ttyUSB0", "1-1", "0403", "6001", "B0045K08", 0)     # has a serial number
CH340 = ("ttyUSB1", "1-2", "1a86", "7523", "", 1)              # has none: keyed by socket
HAT = {"ttySC0": 0, "ttySC1": 1}


@pytest.fixture
def state(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


def test_restart_keeps_every_port(monkeypatch, tmp_path, state):
    root = tree(tmp_path, "t0", usb=[FTDI, CH340], hat=HAT)
    first = Run(monkeypatch, state, root).ports()
    assert first == {"B0045K08": 7001, "1a86:7523@1-2": 7002, "spi0.0:0": 7003, "spi0.0:1": 7004}
    assert Run(monkeypatch, state, root).ports() == first


def test_upgrade_from_a_3_91_state_file(monkeypatch, tmp_path, state):
    """The pre-E1 bridge wrote this file (its own dump_state): USB ports and
    their serial settings come through unchanged; HAT channels take free ports."""
    spec = importlib.util.spec_from_file_location(
        "sb_old", Path(__file__).parent / "fixtures" / "serial_bridge_supervisor_pre_e1.py")
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    (state / "portmap.json").write_text(json.dumps(old.dump_state(
        {"1a86:7523@1-2": 7001, "B0045K08": 7002},
        {"B0045K08": {"baud": 19200, "parity": "E", "databits": 8, "stopbits": 1}})))
    run = Run(monkeypatch, state, tree(tmp_path, "t0", usb=[FTDI, CH340], hat=HAT))
    assert run.ports() == {"B0045K08": 7002, "1a86:7523@1-2": 7001, "spi0.0:0": 7003, "spi0.0:1": 7004}
    ftdi = next(a for a in run.m._adapters if a["stable_id"] == "B0045K08")
    assert run.m.serial_text(ftdi["serial_params"]) == "19200 8E1"


def test_ttyusb_renumbering_keeps_ports_and_follows_the_node(monkeypatch, tmp_path, state):
    run = Run(monkeypatch, state, tree(tmp_path, "t0", usb=[FTDI, CH340]))
    before = run.ports()
    swapped = [("ttyUSB1",) + FTDI[1:5] + (1,), ("ttyUSB0",) + CH340[1:5] + (0,)]
    assert run.at(tree(tmp_path, "t1", usb=swapped)) == before
    assert run.devs() == {"B0045K08": "/dev/ttyUSB1", "1a86:7523@1-2": "/dev/ttyUSB0"}
    # ser2net is pointed at the NEW node for the SAME port (audit DP-4)
    assert run.served() == {before["B0045K08"]: "/dev/ttyUSB1", before["1a86:7523@1-2"]: "/dev/ttyUSB0"}


def test_replug_same_socket_keeps_the_port(monkeypatch, tmp_path, state):
    run = Run(monkeypatch, state, tree(tmp_path, "t0", usb=[FTDI, CH340]))
    before = run.ports()
    run.at(tree(tmp_path, "t1", usb=[FTDI]))                       # CH340 unplugged
    assert "1a86:7523@1-2" not in run.ports()
    assert run.stored()["1a86:7523@1-2"] == before["1a86:7523@1-2"]   # its port stays its own
    assert run.at(tree(tmp_path, "t2", usb=[FTDI, CH340])) == before


def test_replug_into_another_socket(monkeypatch, tmp_path, state):
    """With a serial number the id — and the port — follows the adapter. A
    CH340 has none: its id is the socket, so in another socket it is a NEW
    adapter with a new port, and its old port is never handed to anyone else
    (a device bound to the old port reads nothing rather than another bus)."""
    run = Run(monkeypatch, state, tree(tmp_path, "t0", usb=[FTDI, CH340]))
    before = run.ports()
    moved = [("ttyUSB0", "1-4") + FTDI[2:], ("ttyUSB1", "1-3") + CH340[2:]]
    after = run.at(tree(tmp_path, "t1", usb=moved))
    assert after["B0045K08"] == before["B0045K08"]                       # follows the adapter
    assert "1a86:7523@1-2" not in after and after["1a86:7523@1-3"] not in before.values()
    assert run.stored()["1a86:7523@1-2"] == before["1a86:7523@1-2"]      # reserved for the socket
    # and back in its own socket, it is the old bus again
    assert run.at(tree(tmp_path, "t2", usb=[FTDI, CH340]))["1a86:7523@1-2"] == before["1a86:7523@1-2"]


def test_kernel_swapping_hat_channel_names(monkeypatch, tmp_path, state):
    run = Run(monkeypatch, state, tree(tmp_path, "t0", hat=HAT))
    before = run.ports()
    assert run.devs() == {"spi0.0:0": "/dev/ttySC0", "spi0.0:1": "/dev/ttySC1"}
    # next boot: the same chip, channel 0 is now called ttySC1 (line order kept)
    after = Run(monkeypatch, state, tree(tmp_path, "t1", hat={"ttySC1": 0, "ttySC0": 1}))
    assert after.ports() == before
    assert after.devs() == {"spi0.0:0": "/dev/ttySC1", "spi0.0:1": "/dev/ttySC0"}
    assert after.served() == {before["spi0.0:0"]: "/dev/ttySC1", before["spi0.0:1"]: "/dev/ttySC0"}


def test_hat_removed_and_put_back(monkeypatch, tmp_path, state):
    """While the HAT is out, a new adapter plugged in takes a FREE port — never
    one of the HAT's; the HAT gets its own back."""
    run = Run(monkeypatch, state, tree(tmp_path, "t0", usb=[FTDI], hat=HAT))
    before = run.ports()
    out = run.at(tree(tmp_path, "t1", usb=[FTDI, CH340]))
    assert out["1a86:7523@1-2"] not in before.values()
    back = Run(monkeypatch, state, tree(tmp_path, "t2", usb=[FTDI, CH340], hat=HAT)).ports()
    assert {k: back[k] for k in before} == before


def test_narrowed_port_range_drops_loudly_and_never_takes_anothers_port(
        monkeypatch, tmp_path, state, capsys):
    (state / "portmap.json").write_text(json.dumps(
        {"B0045K08": 7001, "absent-adapter": 7002, "1a86:7523@1-2": 7050}))
    run = Run(monkeypatch, state, tree(tmp_path, "t0", usb=[FTDI, CH340]),
              BRIDGE_PORT_LOW="7001", BRIDGE_PORT_HIGH="7016")
    assert "portmap entry dropped (invalid): 1a86:7523@1-2=7050" in capsys.readouterr().out
    ports = run.ports()
    assert ports["B0045K08"] == 7001
    # the CH340 is re-assigned — but not 7002, which an absent adapter still owns
    assert ports["1a86:7523@1-2"] == 7003 and run.stored()["absent-adapter"] == 7002


def test_an_unreadable_map_is_loud(monkeypatch, tmp_path, state, capsys):
    """A corrupt port map cannot be trusted: the bridge says so loudly and
    starts afresh. Ports may then differ — which is why the gateway checks the
    adapter behind a port before every connect (multibus/bridge_guard.py)."""
    (state / "portmap.json").write_text("{not json")
    Run(monkeypatch, state, tree(tmp_path, "t0", usb=[FTDI]))
    assert "PORTMAP UNREADABLE" in capsys.readouterr().out
