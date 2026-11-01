# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""scripts/serial-access.sh (ADR-0016): the board UARTs' exact device rules
written into .env slots, against a fake /sys + /proc tree and a temp .env —
idempotent, never a wildcard, and refusing anything that is not a tty."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_serial_bridge_board_uarts import _char, _driver, _link, _of_node, _tty, _usb, _write

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "serial-access.sh"
SH = shutil.which("sh")


def run(root, env_file, *args):
    return subprocess.run([SH, str(SCRIPT), "--root", str(root), "--env-file", str(env_file), *args],
                          capture_output=True, text=True, timeout=30)


def slots(env_file: Path) -> dict:
    return dict(line.split("=", 1) for line in env_file.read_text().splitlines()
                if line.startswith("BRIDGE_TTY_") and line[11:12].isdigit())


@pytest.fixture
def board(tmp_path):
    """A Pi 5 with an SC16IS752 HAT (509), PL011 UARTs (204, allowed already),
    a USB adapter (188), a real ttyS0 (4:64) and a phantom ttyS1."""
    root = tmp_path / "root"
    spi = "platform/axi/1f00050000.spi/spi_master/spi0/spi0.0"
    _driver(root, _tty(root, "ttySC0", spi, "509:0", 0, type=114), "spi", "sc16is7xx")
    _tty(root, "ttySC1", spi, "509:1", 1, type=114)
    _tty(root, "ttyAMA0", "platform/axi/1f00030000.serial", "204:64", 0, type=1)
    _tty(root, "ttyAMA10", "platform/107d001000.serial", "204:74", 10, type=1)
    _driver(root, _tty(root, "ttyS0", "pnp/00:05", "4:64", 0, type=4, port="0x3F8",
                       iomem_base="0x0"), "pnp", "serial")
    _tty(root, "ttyS1", "platform/serial8250", "4:65", 1, type=0, port="0x0", iomem_base="0x0")
    _usb(root, "ttyUSB0", "1-1", "1a86", "7523", "", 0)
    # a virtual console with no device behind it: never touched
    _write(root / "sys/devices/virtual/tty/tty1/dev", "4:1\n")
    _link(root / "sys/class/tty/tty1", root / "sys/devices/virtual/tty/tty1")
    _write(root / "proc/cmdline", "console=ttyAMA10,115200 rootwait\n")
    return root


def test_writes_exact_majors_into_slots(board, tmp_path):
    env = tmp_path / ".env"
    env.write_text("BRIDGE_TOKEN=abc\n# my note\nMBG_VERSION=3.92.1\n")
    os.chmod(env, 0o600)
    r = run(board, env)
    assert r.returncode == 0, r.stderr
    got = slots(env)
    assert sorted(got) == [f"BRIDGE_TTY_{i}" for i in range(1, 5)]
    assert {got["BRIDGE_TTY_1"], got["BRIDGE_TTY_2"]} == {"509:*", "4:64"}   # sysfs order
    assert (got["BRIDGE_TTY_3"], got["BRIDGE_TTY_4"]) == ("188:2", "188:3")
    text = env.read_text()
    assert text.startswith("BRIDGE_TOKEN=abc\n# my note\nMBG_VERSION=3.92.1\n")   # kept
    assert "*:*" not in text and "204" not in text and "4:65" not in text
    assert oct(env.stat().st_mode & 0o777) == "0o600"                           # mode kept
    assert "docker compose --profile rtu-bridge up -d serial-bridge" in r.stdout
    assert "'c 509:* rmw'" in r.stdout
    # the driver each slot was written for: in a comment, and for the bridge
    drivers = dict(x.split(":") for x in
                   next(ln for ln in text.splitlines() if ln.startswith("BRIDGE_TTY_DRIVERS="))
                   .split("=", 1)[1].split(","))
    by_rule = {got[f"BRIDGE_TTY_{n}"]: drv for n, drv in drivers.items()}
    assert by_rule == {"509:*": "sc16is7xx", "4:64": "serial"}
    assert "# serial-access.sh: BRIDGE_TTY_1 is for driver" in text


def test_idempotent(board, tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    assert run(board, env).returncode == 0
    first = env.read_text()
    assert run(board, env).returncode == 0
    assert env.read_text() == first
    assert first.count("BRIDGE_TTY_1=") == 1 and first.count("BRIDGE_TTY_DRIVERS=") == 1
    assert first.count("# serial-access.sh:") == 3                  # the mark + one per slot


def test_bluetooth_uart_not_granted(board, tmp_path):
    """A Pi 3/4 mini-UART carrying Bluetooth (ttyS0, 4:64): never granted —
    a device-tree bluetooth child, a serdev bluetooth child or a bound hci."""
    dev = board / "sys/devices/pnp/00:05"
    _of_node(board, dev, "soc/serial@7e215040", "brcm,bcm2835-aux-uart", children=("bluetooth",))
    env = tmp_path / ".env"
    r = run(board, env)
    assert r.returncode == 0, r.stderr
    assert "4:64" not in slots(env).values() and "skipped: Bluetooth UART" in r.stdout


def test_bluetooth_by_hci_not_granted(board, tmp_path):
    _link(board / "sys/class/bluetooth/hci0/device", board / "sys/devices/pnp/00:05")
    env = tmp_path / ".env"
    assert run(board, env).returncode == 0
    assert "4:64" not in slots(env).values()


def _shim(tmp_path, name, body):
    """A PATH directory where `name` is a failing stand-in."""
    d = tmp_path / "shim"
    d.mkdir(exist_ok=True)
    (d / name).write_text(f"#!/bin/sh\n{body}\n")
    (d / name).chmod(0o755)
    return {**os.environ, "PATH": f"{d}:{os.environ['PATH']}"}


def test_failed_write_leaves_env_whole(board, tmp_path):
    """A rename that fails (or a write: disk full) must not truncate .env:
    the old file stays byte for byte, and no temp file is left behind."""
    env = tmp_path / "cfg" / ".env"
    env.parent.mkdir()
    env.write_text("BRIDGE_TOKEN=secret\nKEEP=1\n")
    r = subprocess.run([SH, str(SCRIPT), "--root", str(board), "--env-file", str(env)],
                       capture_output=True, text=True, timeout=30,
                       env=_shim(tmp_path, "mv", "exit 1"))
    assert r.returncode == 1 and "unchanged" in r.stderr
    assert env.read_text() == "BRIDGE_TOKEN=secret\nKEEP=1\n"
    assert sorted(p.name for p in env.parent.iterdir()) == [".env"]


def test_symlinked_env_updated_at_its_target(board, tmp_path):
    real = tmp_path / "secrets" / "bridge.env"
    real.parent.mkdir()
    real.write_text("A=1\n")
    os.chmod(real, 0o640)
    link = tmp_path / ".env"
    link.symlink_to(real)
    assert run(board, link).returncode == 0
    assert link.is_symlink() and "BRIDGE_TTY_1=" in real.read_text()
    assert oct(real.stat().st_mode & 0o777) == "0o640"


def test_new_env_is_private(board, tmp_path):
    env = tmp_path / "sub" / ".env"
    env.parent.mkdir()
    assert run(board, env).returncode == 0
    assert oct(env.stat().st_mode & 0o777) == "0o600"


def test_nothing_extra_needed_keeps_harmless_slots(tmp_path):
    root = tmp_path / "root"
    _tty(root, "ttyAMA0", "platform/soc/fe201000.serial", "204:64", 0, type=1)
    _usb(root, "ttyUSB0", "1-1", "1a86", "7523", "", 0)
    env = tmp_path / ".env"
    r = run(root, env)
    assert r.returncode == 0, r.stderr
    assert slots(env) == {f"BRIDGE_TTY_{i}": f"188:{i - 1}" for i in range(1, 5)}
    assert "No extra device rules" in r.stdout


def test_console_ttys_not_granted(board, tmp_path):
    (board / "proc/cmdline").write_text("console=ttyS0,115200n8\n")
    env = tmp_path / ".env"
    r = run(board, env)
    assert r.returncode == 0, r.stderr
    assert "4:64" not in slots(env).values() and "kernel console" in r.stdout


def test_refuses_a_non_tty_device(board, tmp_path):
    """A tty-looking name whose char node belongs to another subsystem (here a
    misc device, major 10) is refused, and nothing is written."""
    cls = board / "sys/devices/virtual/misc/ttySC7"
    _write(cls / "dev", "10:200\n")
    _link(cls / "device", board / "sys/devices/platform")
    _char(board, "ttySC7", cls, "10:200", subsystem="misc")
    env = tmp_path / ".env"
    env.write_text("KEEP=1\n")
    r = run(board, env)
    assert r.returncode == 1
    assert "not a tty device" in r.stderr and "10:200" in r.stderr
    assert env.read_text() == "KEEP=1\n"


def test_refuses_virtual_console_minor(board, tmp_path):
    """A ttyS on major 4 below minor 64 would be a virtual console: refused."""
    _tty(board, "ttyS5", "pnp/00:09", "4:5", 5, type=4, port="0x2E8")
    env = tmp_path / ".env"
    r = run(board, env)
    assert r.returncode == 1 and "virtual console" in r.stderr
    assert not env.exists()


def test_too_many_rules_for_the_slots(board, tmp_path):
    for i, minor in enumerate((66, 67, 68, 69), start=2):
        _tty(board, f"ttyS{i}", f"pnp/00:1{i}", f"4:{minor}", i, type=4, port=f"0x{0x2E8 + i:X}")
    r = run(board, tmp_path / ".env")
    assert r.returncode == 1 and "slots" in r.stderr


def test_bridge_compose_command_and_dry_run(board, tmp_path):
    env = tmp_path / ".env"
    r = run(board, env, "--compose", "docker-compose.bridge.yml", "--dry-run")
    assert r.returncode == 0
    assert not env.exists()
    assert "=509:*" in r.stdout and "=4:64" in r.stdout
    r = run(board, env, "--compose", "docker-compose.bridge.yml")
    assert "docker compose -f docker-compose.bridge.yml up -d" in r.stdout


def test_compose_files_read_the_slots():
    """Both compose files carry the four slots with distinct harmless
    defaults, ttyAMA allowed, and never a wildcard major."""
    repo = SCRIPT.parent.parent
    for f in ("docker-compose.yml", "serial-bridge/docker-compose.bridge.yml"):
        text = (repo / f).read_text()
        for i in range(1, 5):
            assert f"- 'c ${{BRIDGE_TTY_{i}:-188:{i - 1}}} rmw'" in text, f
        assert "- 'c 204:* rmw'" in text and "c *:" not in text, f
