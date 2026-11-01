# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The serial ports of this host, listed the way the bridge lists them
(ADR-0028, FINDINGS ADD-16).

Everything runs against a FAKE /sys + /dev + /proc tree laid out the way the
kernel lays it out (the bridge tests' builders), so no hardware is touched:
a Pi 5 with a two-channel SC16IS752 HAT (ttySC0/ttySC1), the Bluetooth PL011
ttyAMA0, the debug UART ttyAMA10 as the kernel console (in /sys only, as a
container sees it), a PC-style ttyS pair (one phantom) and two USB adapters,
the FTDI one with a /dev/serial/by-id link.
"""
import filecmp
import importlib.util
import os
from pathlib import Path

import pytest

from tests.test_devices_api import make_app, needs_tc
from tests.test_serial_bridge_board_uarts import _driver, _link, _load, _of_node, _tty, _usb, _write

REPO = Path(__file__).resolve().parent.parent
BY_ID = "usb-FTDI_FT232R_USB_UART_B0045K08-if00-port0"
OTHER_PID = os.getpid() + 1


@pytest.fixture
def host(tmp_path):
    root = tmp_path / "root"
    spi = _tty(root, "ttySC0", "platform/axi/1f00050000.spi/spi_master/spi0/spi0.0",
               "509:0", 0, type=114, port="0x0", iomem_base="0x0")
    _tty(root, "ttySC1", "platform/axi/1f00050000.spi/spi_master/spi0/spi0.0",
         "509:1", 1, type=114, port="0x0", iomem_base="0x0")
    _driver(root, spi, "spi", "sc16is7xx")
    _of_node(root, spi, "axi/spi@50000/sc16is752@0", "nxp,sc16is752")
    bt = _tty(root, "ttyAMA0", "platform/axi/1f00030000.serial", "204:64", 0,
              type=1, iomem_base="0x1F00030000")
    _driver(root, bt, "amba", "uart-pl011")
    _of_node(root, bt, "axi/serial@30000", "arm,pl011", children=("bluetooth",))
    dbg = _tty(root, "ttyAMA10", "platform/107d001000.serial", "204:74", 10,
               type=1, iomem_base="0x107D001000")
    _driver(root, dbg, "amba", "uart-pl011")
    _of_node(root, dbg, "soc/serial@7d001000", "arm,pl011-axi")
    os.remove(root / "dev/ttyAMA10")                 # in /sys, not mapped (the Pi 5 bench)
    real = _tty(root, "ttyS0", "pnp/00:05", "4:64", 0, type=4, port="0x3F8", iomem_base="0x0")
    _driver(root, real, "pnp", "serial")
    _write(root / "sys/devices/pnp/00:05/of_node/linux,rs485-enabled-at-boot-time", "")
    _tty(root, "ttyS1", "platform/serial8250", "4:65", 1, type=0, port="0x0", iomem_base="0x0")
    _write(root / "sys/devices/virtual/tty/tty1/dev", "4:1\n")
    _link(root / "sys/class/tty/tty1", root / "sys/devices/virtual/tty/tty1")
    _write(root / "sys/devices/virtual/tty/ptmx/dev", "5:2\n")
    _link(root / "sys/class/tty/ptmx", root / "sys/devices/virtual/tty/ptmx")
    _usb(root, "ttyUSB0", "1-1", "1a86", "7523", "", 0)
    _usb(root, "ttyUSB1", "1-2", "0403", "6001", "B0045K08", 1, driver="ftdi_sio")
    (root / "dev/serial/by-id").mkdir(parents=True)
    os.symlink("../../ttyUSB1", root / "dev/serial/by-id" / BY_ID)
    _write(root / "proc/cmdline", "coherent_pool=1M console=ttyAMA10,115200 console=tty1 rootwait\n")
    _write(root / "sys/class/tty/console/active", "tty1 ttyAMA10\n")
    # a process that holds no port — never this one: the gateway skips
    # itself, and pytest in a container is often pid 1
    _write(root / f"proc/{OTHER_PID}/comm", "tini\n")
    (root / f"proc/{OTHER_PID}/fd").mkdir(parents=True)
    os.symlink("/dev/null", root / f"proc/{OTHER_PID}/fd/0")
    return root


def _ports(root, **kw):
    from multibus import serial_ports
    return {p["name"]: p for p in serial_ports.enumerate_ports(str(root), **kw)}


# ── the enumerator ─────────────────────────────────────────────────────────
def test_usb_adapter_stores_its_by_id_link(host):
    p = _ports(host)
    ftdi, ch = p["ttyUSB1"], p["ttyUSB0"]
    assert ftdi["kind"] == "usb" and ftdi["path"] == "/dev/ttyUSB1"
    assert ftdi["stable_path"] == f"/dev/serial/by-id/{BY_ID}"
    assert (ftdi["vendor_id"], ftdi["product_id"], ftdi["serial"], ftdi["port_path"]) == \
        ("0403", "6001", "B0045K08", "1-2")
    assert ftdi["model"] == "USB Serial" and ftdi["driver"] == "ftdi_sio"
    # no by-id link: the plain path is what is stored
    assert ch["stable_path"] == "/dev/ttyUSB0" and ch["serial"] == ""


def test_hat_channels_are_hat_ports(host):
    p = _ports(host)
    sc0, sc1 = p["ttySC0"], p["ttySC1"]
    assert sc0["kind"] == sc1["kind"] == "hat"
    assert sc0["driver"] == "sc16is7xx" and sc0["chip"] == "SC16IS752"
    assert sc0["stable_path"] == "/dev/ttySC0" and sc0["mapped"] is True
    assert (sc0["channel"], sc1["channel"]) == (0, 1)
    assert sc0["label"] == "SC16IS752 ch0 (spi0.0)"


def test_board_uart_kind_and_kernel_rs485(host):
    p = _ports(host)
    assert p["ttyS0"]["kind"] == "board_uart" and p["ttyS0"]["rs485"] is True
    assert p["ttySC0"]["rs485"] is False                  # a device tree node without it
    assert p["ttyUSB0"]["rs485"] is None                  # cannot be read without opening it
    assert p["ttyAMA0"]["kind"] == "board_uart" and p["ttyAMA0"]["bluetooth"] is True


def test_virtual_ttys_and_phantoms_are_skipped_the_console_is_marked(host):
    p = _ports(host)
    assert not {"tty1", "ptmx", "console", "ttyS1"} & set(p)
    assert "console" in p["ttyAMA10"]["console"]
    assert p["ttySC0"]["console"] == ""


def test_a_getty_marks_its_port_a_console(host):
    _write(host / "proc/77/cmdline", "\x00".join(["/sbin/agetty", "-L", "ttyS0", "115200", "vt100", ""]))
    assert "getty" in _ports(host, gettys=True)["ttyS0"]["console"]


def test_a_port_not_mapped_is_still_listed(host):
    os.remove(host / "dev/ttySC1")
    sc1 = _ports(host)["ttySC1"]
    assert sc1["mapped"] is False and sc1["path"] == "/dev/ttySC1"


# ── the state of each port ────────────────────────────────────────────────
class _Cfg:
    def __init__(self, devices):
        self.devices = devices


def _dev(did, protocol, serial_port, **conn):
    from types import SimpleNamespace
    return SimpleNamespace(id=did, enabled=conn.pop("enabled", True), protocol=protocol,
                           connection={"protocol": protocol, "serial_port": serial_port, **conn},
                           template="", sources=[])


def _inv(host, devices=(), **kw):
    from multibus import serial_inventory
    out = serial_inventory.inventory(_Cfg(list(devices)), None, root=str(host), **kw)
    return {p["name"]: p for p in out["ports"]}, out


def test_the_bench_listened_polled_and_the_console_hidden(host):
    """ADR-0028's bench: ttySC0 listened to by two Seplos packs, ttySC1
    polled by the Deye, ttyAMA10 hidden as the console."""
    devs = [_dev("seplos-adr1", "seplos_v2", "/dev/ttySC0", mode="listen"),
            _dev("seplos-adr2", "seplos_v2", "/dev/ttySC0", mode="listen"),
            _dev("deye", "rtu", "/dev/ttySC1")]
    p, out = _inv(host, devs)
    assert p["ttySC0"]["state"] == "listened"
    assert p["ttySC0"]["listened_by"] == ["seplos-adr1", "seplos-adr2"]
    assert p["ttySC1"]["state"] == "polled" and p["ttySC1"]["polled_by"] == ["deye"]
    assert "ttyAMA10" not in p and out["hidden"] == 1
    assert p["ttyUSB0"]["state"] == "free"


def test_console_shown_with_all(host):
    p, out = _inv(host, show_all=True)
    assert p["ttyAMA10"]["state"] == "console" and out["hidden"] == 0


def test_a_claim_through_a_by_id_alias_is_the_same_port(host):
    p, _ = _inv(host, [_dev("meter", "rtu_tap", f"/dev/serial/by-id/{BY_ID}")])
    assert p["ttyUSB1"]["state"] == "listened" and p["ttyUSB1"]["listened_by"] == ["meter"]


def test_a_disabled_device_claims_nothing(host):
    p, _ = _inv(host, [_dev("off", "rtu", "/dev/ttyUSB0", enabled=False)])
    assert p["ttyUSB0"]["state"] == "free"


def test_not_mapped_gives_the_device_flag_and_the_compose_line(host):
    os.remove(host / "dev/ttySC1")
    sc1 = _inv(host)[0]["ttySC1"]
    assert sc1["state"] == "not_mapped"
    assert sc1["fix"] == {"docker_run": "--device /dev/ttySC1:/dev/ttySC1",
                          "compose": "- /dev/ttySC1:/dev/ttySC1"}


def test_busy_when_another_process_holds_it(host):
    _write(host / "proc/4242/comm", "gpsd\n")
    (host / "proc/4242/fd").mkdir(parents=True)
    os.symlink("/dev/ttyUSB0", host / "proc/4242/fd/5")
    usb0 = _inv(host)[0]["ttyUSB0"]
    # the program, not its pid (review MINOR 4: nothing a viewer needs)
    assert usb0["state"] == "busy" and usb0["processes"] == ["gpsd"]
    assert usb0["base_state"] == "busy"


def test_unknown_when_no_process_can_be_read(host):
    import shutil
    shutil.rmtree(host / f"proc/{OTHER_PID}")
    assert _inv(host)[0]["ttyUSB0"]["state"] == "unknown"


# ── the API ───────────────────────────────────────────────────────────────
@needs_tc
def test_api_lists_the_ports_with_the_old_keys(tmp_path, host, monkeypatch):
    from multibus import serial_inventory
    monkeypatch.setattr(serial_inventory, "SYSROOT", str(host))
    _cfg, client = make_app(tmp_path, extra_yaml="""
devices:
  - id: deye
    enabled: true
    connection: { protocol: rtu, serial_port: /dev/ttySC1, unit_id: 1 }
  - id: seplos-adr1
    enabled: true
    connection: { protocol: seplos_v2, serial_port: /dev/ttySC0, unit_id: 1, mode: listen }
""")
    r = client.get("/api/serial-ports")
    assert r.status_code == 200, r.text
    body = r.json()
    ports = {p["name"]: p for p in body["ports"]}
    ftdi = ports["ttyUSB1"]
    # what the endpoint returned before ADR-0028, unchanged
    assert (ftdi["dev"], ftdi["vendor_id"], ftdi["product_id"], ftdi["serial"], ftdi["model"]) == \
        ("/dev/ttyUSB1", "0403", "6001", "B0045K08", "USB Serial")
    for k in ("name", "path", "stable_path", "kind", "driver", "rs485", "state", "reason",
              "polled_by", "listened_by", "mapped"):
        assert k in ftdi, k
    assert ports["ttySC1"]["polled_by"] == ["deye"] and ports["ttySC0"]["state"] == "listened"
    assert "ttyAMA10" not in ports and body["hidden"] == 1
    assert "ttyAMA10" in {p["name"] for p in client.get("/api/serial-ports?all=1").json()["ports"]}
    assert "ttyAMA10" in {p["name"] for p in client.get("/api/serial-ports?all=true").json()["ports"]}


@needs_tc
def test_api_error_says_no_internals(tmp_path, monkeypatch):
    from multibus import serial_inventory

    def boom(*_a, **_k):
        raise RuntimeError("/sys/secret/path exploded")
    monkeypatch.setattr(serial_inventory, "inventory", boom)
    _cfg, client = make_app(tmp_path)
    body = client.get("/api/serial-ports").json()
    assert body["ports"] == [] and body["error"] and "secret" not in body["error"]


# ── what the container really has in /dev (review B1, M1) ─────────────────
@pytest.fixture
def nodes(monkeypatch):
    """Character-device numbers for fake /dev nodes and /proc fd links: the
    fake tree's files are plain files, so the stat that reads st_rdev is
    answered from this table (path → "major:minor")."""
    from multibus import serial_ports
    table = {}
    real = serial_ports._node_num

    def fake(path):
        return table.get(os.path.normpath(str(path)), "") or real(path)
    monkeypatch.setattr(serial_ports, "_node_num", fake)
    return lambda path, num: table.__setitem__(os.path.normpath(str(path)), num)


def test_a_udev_alias_is_the_port_the_container_has(host, nodes):
    """Production: compose maps the Fronius adapter as /dev/ttyFRONIUS only;
    sysfs lists ttyUSB0. The port is mapped through the alias, stored by
    it, polled by fronius, and never offered as /dev/ttyUSB0."""
    os.remove(host / "dev/ttyUSB0")
    _write(host / "dev/ttyFRONIUS", "")
    nodes(host / "dev/ttyFRONIUS", "188:0")
    p, _ = _inv(host, [_dev("fronius", "rtu", "/dev/ttyFRONIUS")])
    usb0 = p["ttyUSB0"]
    assert usb0["mapped"] is True and usb0["aliases"] == ["/dev/ttyFRONIUS"]
    assert usb0["stable_path"] == "/dev/ttyFRONIUS"
    assert usb0["state"] == "polled" and usb0["polled_by"] == ["fronius"]
    assert usb0["base_state"] == "free"                  # what it is without the claims


def test_a_renamed_mapping_is_told_by_the_device_number(host, nodes):
    """compose /dev/ttyUSB1:/dev/ttyUSB0: the container's /dev/ttyUSB0 is the
    host's ttyUSB1 (188:1). ttyUSB0 is not in the container; ttyUSB1 is,
    under the other name (its by-id link points at nothing here)."""
    os.remove(host / "dev/ttyUSB1")
    nodes(host / "dev/ttyUSB0", "188:1")
    p, _ = _inv(host)
    assert p["ttyUSB0"]["mapped"] is False and p["ttyUSB0"]["state"] == "not_mapped"
    usb1 = p["ttyUSB1"]
    assert usb1["mapped"] is True and usb1["aliases"] == ["/dev/ttyUSB0"]
    assert usb1["stable_path"] == "/dev/ttyUSB0" and usb1["model"] == "USB Serial"


def test_a_name_taken_by_another_device_is_never_offered(host, nodes):
    """The same renamed mapping, seen from the host's ttyUSB0: /dev/ttyUSB0
    here is another device. It is never this port's stored path, a device
    on /dev/ttyUSB0 claims only ttyUSB1, and the fix line maps it under a
    path that does not collide, saying why."""
    os.remove(host / "dev/ttyUSB1")
    nodes(host / "dev/ttyUSB0", "188:1")
    p, _ = _inv(host, [_dev("meter", "rtu", "/dev/ttyUSB0")])
    usb0, usb1 = p["ttyUSB0"], p["ttyUSB1"]
    assert usb0["path_taken"] is True and usb0["stable_path"] == ""
    assert usb1["path_taken"] is False
    assert usb0["polled_by"] == [] and usb1["polled_by"] == ["meter"]
    fix = usb0["fix"]
    assert fix["docker_run"] == "--device /dev/ttyUSB0:/dev/ttyUSB0-1-1"
    assert fix["compose"] == "- /dev/ttyUSB0:/dev/ttyUSB0-1-1"
    assert "another device" in fix["note"]


def test_holders_are_not_guessed_from_an_ambiguous_path(tmp_path):
    from multibus import serial_ports
    root = tmp_path / "root"
    _write(root / "proc/4244/comm", "x\n")
    (root / "proc/4244/fd").mkdir(parents=True)
    os.symlink("/dev/ttyX", root / "proc/4244/fd/3")
    got = serial_ports.fd_holders(str(root), {"ttyA": ["/dev/ttyX"], "ttyB": ["/dev/ttyX"]})
    assert got == {}
    got = serial_ports.fd_holders(str(root), {"ttyA": ["/dev/ttyX"]})
    assert got == {"ttyA": ["x"]}


def test_busy_is_told_by_the_device_number(host, nodes):
    """A process that opened the port under another name (an alias) holds the
    same character device: stat of its fd says so, nothing is opened."""
    _write(host / "proc/4243/comm", "modpoll\n")
    (host / "proc/4243/fd").mkdir(parents=True)
    os.symlink("/dev/ttyMETER", host / "proc/4243/fd/7")
    nodes(host / "proc/4243/fd/7", "188:0")
    assert _inv(host)[0]["ttyUSB0"]["processes"] == ["modpoll"]


def test_the_gateway_is_not_its_own_holder(host):
    pid = str(os.getpid())
    _write(host / f"proc/{pid}/comm", "python\n")
    (host / f"proc/{pid}/fd").mkdir(parents=True, exist_ok=True)
    os.symlink("/dev/ttyUSB0", host / f"proc/{pid}/fd/9")
    assert _inv(host)[0]["ttyUSB0"]["state"] == "free"


def test_a_usb_port_not_mapped_says_its_name_may_change(host):
    os.remove(host / "dev/ttyUSB0")
    fix = _inv(host)[0]["ttyUSB0"]["fix"]
    assert fix["docker_run"] == "--device /dev/ttyUSB0:/dev/ttyUSB0"
    assert "replug" in fix["note"] and "/dev/serial/by-id" in fix["note"]


def test_gettys_are_looked_for_only_when_asked(host):
    from multibus import serial_ports
    _write(host / "proc/77/cmdline", "\x00".join(["/sbin/agetty", "-L", "ttyS0", "115200", ""]))
    by = {p["name"]: p for p in serial_ports.enumerate_ports(str(host))}
    assert by["ttyS0"]["console"] == ""                 # the bridge's call: no getty scan
    by = {p["name"]: p for p in serial_ports.enumerate_ports(str(host), gettys=True)}
    assert "getty" in by["ttyS0"]["console"]


# ── the bridge lists the same ports ───────────────────────────────────────
def test_the_bridge_ships_the_same_module():
    """The bridge image builds from serial-bridge/ alone: it carries a copy,
    kept identical, and its Dockerfile puts it beside the supervisor."""
    assert filecmp.cmp(REPO / "multibus/serial_ports.py", REPO / "serial-bridge/serial_ports.py",
                       shallow=False)
    assert "COPY serial_ports.py /app/serial_ports.py" in (REPO / "serial-bridge/Dockerfile").read_text()


def test_the_bridge_produces_the_same_entries(monkeypatch, tmp_path, host, nodes):
    # ttyUSB0 is in this /dev only as an alias: mapped, but no /dev/ttyUSB0
    # node — the bridge (which serves /dev/<name>) leaves it out, as before
    os.remove(host / "dev/ttyUSB0")
    _write(host / "dev/ttyFRONIUS", "")
    nodes(host / "dev/ttyFRONIUS", "188:0")
    sv = _load(monkeypatch, tmp_path, host)
    assert sv._sp.__file__ and Path(sv._sp.__file__).name == "serial_ports.py"
    from multibus import serial_ports
    monkeypatch.setattr(sv._sp, "_node_num", serial_ports._node_num)   # the same table
    ours = _ports(host)
    assert ours["ttyUSB0"]["mapped"] is True and ours["ttyUSB0"]["in_dev"] is False
    bridge = sv.enumerate_adapters()
    # the bridge serves USB adapters with a /dev/<name> node and the board's UARTs
    want = [n for n, p in ours.items()
            if (p["kind"] == "usb" and p["in_dev"]) or p["kind"] in ("hat", "board_uart")]
    assert "ttyUSB0" not in want
    assert sorted(a["name"] for a in bridge) == sorted(want)
    for a in bridge:
        p = ours[a["name"]]
        assert (a["dev"], a["label"], a["driver"], a["dev_num"], a["port_path"]) == \
            (p["path"], p["label"], p["driver"], p["dev_num"], p["port_path"]), a["name"]
        assert a["kind"] == ("usb" if p["kind"] == "usb" else "board")


def test_the_bridge_lists_what_3961_listed(monkeypatch, tmp_path, host):
    """Port-map safety: the 3.96.1 supervisor (tests/fixtures, verbatim from
    f4ba766) and this one, over the same fake tree, give the same adapters —
    the same stable ids, order and status — so no stored port moves."""
    new = _load(monkeypatch, tmp_path, host)
    spec = importlib.util.spec_from_file_location(
        "sb_supervisor_3961", REPO / "tests/fixtures/serial_bridge_supervisor_3961.py")
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)                         # same env: BRIDGE_SYSROOT = host
    keys = ("dev", "name", "stable_id", "kind", "label", "driver", "dev_num", "port_path",
            "vendor_id", "product_id", "serial", "manufacturer", "model", "needs_enable",
            "status", "reason")

    def shape(ads):
        return [{k: a.get(k) for k in keys} for a in ads]
    got, want = shape(new.enumerate_adapters()), shape(old.enumerate_adapters())
    assert len(want) == 7 and got == want
