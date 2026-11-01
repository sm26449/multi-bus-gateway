# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Serial bridge: board UARTs next to USB adapters (ADR-0016).

Everything runs against a FAKE sysfs + /dev + /proc tree (BRIDGE_SYSROOT),
laid out the way the kernel lays it out (relative symlinks, attribute files),
so no hardware and no real /dev is touched:

- a Raspberry Pi 5 with a two-channel SC16IS752 RS-485 HAT on spi0.0
  (ttySC0/ttySC1, dynamic major 509), the PL011 ttyAMA0 carrying Bluetooth,
  the debug UART ttyAMA10 as the kernel console;
- a PC's phantom 8250 ttyS1 next to a real ttyS0;
- two USB adapters whose stable ids (and stored ports) must not move.
"""
import errno
import importlib.util
import json
import os
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "serial-bridge" / "supervisor.py"


# ---- the fake tree -----------------------------------------------------------

def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _link(link: Path, target: Path) -> None:
    """A RELATIVE symlink, as sysfs has — so it resolves inside the fake root."""
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(os.path.relpath(target, link.parent))


def _tty(root: Path, name: str, device: str, dev: str, line: int, **attrs) -> Path:
    """/sys/class/tty/<name> → <device>/tty/<name>, with its `device` link,
    `dev`, `line` and any serial-core attribute, plus /dev/<name>."""
    dev_dir = root / "sys/devices" / device
    cls = dev_dir / "tty" / name
    _write(cls / "dev", dev + "\n")
    _write(cls / "line", f"{line}\n")
    for k, v in attrs.items():
        _write(cls / k, f"{v}\n")
    _link(cls / "device", dev_dir)
    _char(root, name, cls, dev)
    return dev_dir


def _char(root: Path, name: str, cls: Path, dev: str, subsystem: str = "tty") -> None:
    """The class link, /sys/dev/char/<major:minor> and the node's subsystem
    link (what serial-access.sh checks), plus /dev/<name>."""
    (root / "sys/class" / subsystem).mkdir(parents=True, exist_ok=True)
    _link(cls / "subsystem", root / "sys/class" / subsystem)
    _link(root / "sys/class/tty" / name, cls)
    _link(root / "sys/dev/char" / dev, cls)
    _write(root / "dev" / name, "")


def _driver(root: Path, dev_dir: Path, bus: str, name: str) -> None:
    drv = root / "sys/bus" / bus / "drivers" / name
    drv.mkdir(parents=True, exist_ok=True)
    _link(dev_dir / "driver", drv)


def _of_node(root: Path, dev_dir: Path, node: str, compatible: str, children=()) -> None:
    on = root / "sys/firmware/devicetree/base" / node
    _write(on / "compatible", compatible + "\0")
    for c in children:
        (on / c).mkdir(parents=True, exist_ok=True)
    _link(dev_dir / "of_node", on)


def _usb(root: Path, name: str, port_path: str, vid: str, pid: str, serial: str,
         minor: int, major: int = 188, driver: str = "ch341-uart") -> None:
    usb_dev = root / "sys/devices/pci0000:00/0000:00:14.0/usb1" / port_path
    _write(usb_dev / "idVendor", vid + "\n")
    _write(usb_dev / "idProduct", pid + "\n")
    _write(usb_dev / "manufacturer", "QinHeng\n")
    _write(usb_dev / "product", "USB Serial\n")
    if serial:
        _write(usb_dev / "serial", serial + "\n")
    port_dir = usb_dev / f"{port_path}:1.0" / name
    cls = port_dir / "tty" / name
    _write(cls / "dev", f"{major}:{minor}\n")
    _link(cls / "device", port_dir)
    _char(root, name, cls, f"{major}:{minor}")
    _driver(root, port_dir, "usb-serial", driver)


@pytest.fixture
def pi5(tmp_path):
    """A Pi 5 with an SC16IS752 HAT, BT on ttyAMA0, console on ttyAMA10, plus
    two USB adapters and a PC-style ttyS pair (one real, one phantom)."""
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
    real = _tty(root, "ttyS0", "pnp/00:05", "4:64", 0, type=4, port="0x3F8", iomem_base="0x0")
    _driver(root, real, "pnp", "serial")
    _tty(root, "ttyS1", "platform/serial8250", "4:65", 1, type=0, port="0x0", iomem_base="0x0")
    # a virtual console the bridge must never list
    _write(root / "sys/devices/virtual/tty/tty1/dev", "4:1\n")
    _link(root / "sys/class/tty/tty1", root / "sys/devices/virtual/tty/tty1")
    _usb(root, "ttyUSB0", "1-1", "1a86", "7523", "", 0)
    _usb(root, "ttyUSB1", "1-2", "0403", "6001", "B0045K08", 1, driver="ftdi_sio")
    _write(root / "proc/cmdline", "coherent_pool=1M console=ttyAMA10,115200 console=tty1 rootwait\n")
    _write(root / "sys/class/tty/console/active", "tty1 ttyAMA10\n")
    return root


def _load(monkeypatch, tmp_path, root, **env):
    monkeypatch.setenv("BRIDGE_STATE", str(tmp_path / "portmap.json"))
    monkeypatch.setenv("SER2NET_CFG", str(tmp_path / "ser2net.yaml"))
    monkeypatch.setenv("BRIDGE_SYSROOT", str(root))
    for k in ("BRIDGE_TOKEN", "BRIDGE_SERIAL_PARAMS", "BRIDGE_EXCLUDE", "BRIDGE_RS485",
              "BRIDGE_ACCESS_FIX", "BRIDGE_PORT_LOW", "BRIDGE_PORT_HIGH", "BRIDGE_INCLUDE",
              "BRIDGE_TTY_SLOTS", "BRIDGE_TTY_DRIVERS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("sb_supervisor_board", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def sv(monkeypatch, tmp_path, pi5):
    return _load(monkeypatch, tmp_path, pi5)


def _by_dev(adapters):
    return {a["dev"]: a for a in adapters}


# ---- enumeration ---------------------------------------------------------------

def test_usb_ids_unchanged(sv):
    """The ids earlier versions keyed the port map with: USB serial number,
    else vendor:product@port-path. A stored assignment must find its adapter."""
    usb = [a for a in sv.enumerate_adapters() if a["kind"] == "usb"]
    assert [(a["dev"], a["stable_id"], a["port_path"]) for a in usb] == [
        ("/dev/ttyUSB0", "1a86:7523@1-1", "1-1"),
        ("/dev/ttyUSB1", "B0045K08", "1-2"),
    ]
    assert usb[0]["vendor_id"] == "1a86" and usb[1]["serial"] == "B0045K08"
    assert usb[0]["driver"] == "ch341-uart" and usb[1]["driver"] == "ftdi_sio"
    assert usb[0]["dev_num"] == "188:0" and usb[1]["label"] == "USB Serial (B0045K08)"


def test_usb_ports_kept_from_an_old_portmap(monkeypatch, tmp_path, pi5):
    """A portmap written by an older bridge (USB only) keeps every port; the
    HAT channels take free ports after them (ttyS0, a SoC UART not enabled,
    takes none)."""
    (tmp_path / "portmap.json").write_text(json.dumps({"B0045K08": 7001, "1a86:7523@1-1": 7002}))
    m = _load(monkeypatch, tmp_path, pi5)
    monkeypatch.setattr(m, "_open", lambda *_a: os.open(os.devnull, os.O_RDONLY))
    monkeypatch.setattr(m, "_reload_ser2net", lambda: None)
    m.reconcile()
    ports = {a["stable_id"]: a.get("tcp_port") for a in m._adapters}
    assert ports["B0045K08"] == 7001 and ports["1a86:7523@1-1"] == 7002
    assert (ports["spi0.0:0"], ports["spi0.0:1"], ports["00:05"]) == (7003, 7004, None)


def test_sc16is752_channels(sv):
    a = _by_dev(sv.enumerate_adapters())
    sc0, sc1 = a["/dev/ttySC0"], a["/dev/ttySC1"]
    assert (sc0["stable_id"], sc1["stable_id"]) == ("spi0.0:0", "spi0.0:1")
    assert sc0["kind"] == "board" and sc0["driver"] == "sc16is7xx"
    assert sc0["label"] == "SC16IS752 ch0 (spi0.0)" and sc1["label"] == "SC16IS752 ch1 (spi0.0)"
    assert sc0["dev_num"] == "509:0" and sc0["port_path"] == "spi0.0"
    assert "status" not in sc0                         # decided by the access probe


def test_channel_follows_the_line_not_the_name(monkeypatch, tmp_path):
    """The id is the device + channel, not the tty name: a kernel that hands
    the same chip ttySC2/ttySC3 next boot keeps spi0.0:0 / spi0.0:1."""
    root = tmp_path / "root"
    d = _tty(root, "ttySC3", "platform/soc/fe204000.spi/spi_master/spi0/spi0.0", "509:3", 3)
    _tty(root, "ttySC2", "platform/soc/fe204000.spi/spi_master/spi0/spi0.0", "509:2", 2)
    _driver(root, d, "spi", "sc16is7xx")
    m = _load(monkeypatch, tmp_path, root)
    ids = {a["name"]: a["stable_id"] for a in m.enumerate_adapters()}
    assert ids == {"ttySC2": "spi0.0:0", "ttySC3": "spi0.0:1"}


def test_console_is_reserved(sv):
    dbg = _by_dev(sv.enumerate_adapters())["/dev/ttyAMA10"]
    assert dbg["status"] == "reserved" and "console" in dbg["reason"]
    assert dbg["stable_id"] == "107d001000.serial" and dbg["label"] == "pl011-axi (107d001000.serial)"


def test_console_from_sys_active_alone(monkeypatch, tmp_path, pi5):
    (pi5 / "proc/cmdline").write_text("rootwait\n")
    m = _load(monkeypatch, tmp_path, pi5)
    dbg = _by_dev(m.enumerate_adapters())["/dev/ttyAMA10"]
    assert dbg["status"] == "reserved" and "console/active" in dbg["reason"]


def test_bluetooth_uart_is_reserved(sv):
    bt = _by_dev(sv.enumerate_adapters())["/dev/ttyAMA0"]
    assert bt["status"] == "reserved" and "Bluetooth" in bt["reason"]
    assert bt["stable_id"] == "1f00030000.serial" and bt["driver"] == "uart-pl011"


def test_bluetooth_by_serdev_child_and_by_hci(monkeypatch, tmp_path):
    root = tmp_path / "root"
    a = _tty(root, "ttyAMA1", "platform/soc/fe201400.serial", "204:65", 1, type=1)
    (a / "serial0/serial0-0/bluetooth/hci0").mkdir(parents=True)
    b = _tty(root, "ttyAMA2", "platform/soc/fe201600.serial", "204:66", 2, type=1)
    _link(root / "sys/class/bluetooth/hci1/device", b)
    _tty(root, "ttyAMA3", "platform/soc/fe201800.serial", "204:67", 3, type=1)
    m = _load(monkeypatch, tmp_path, root)
    got = {x["name"]: x.get("status") for x in m.enumerate_adapters()}
    assert got == {"ttyAMA1": "reserved", "ttyAMA2": "reserved", "ttyAMA3": None}


def test_phantom_ttys_skipped_real_one_listed(sv):
    a = _by_dev(sv.enumerate_adapters())
    assert "/dev/ttyS1" not in a                       # type 0, no port, no memory
    assert a["/dev/ttyS0"]["stable_id"] == "00:05" and a["/dev/ttyS0"]["dev_num"] == "4:64"
    assert "/dev/tty1" not in a                        # virtual consoles never listed


def test_phantom_without_type_attribute(monkeypatch, tmp_path):
    root = tmp_path / "root"
    _tty(root, "ttyS2", "platform/serial8250", "4:66", 2, port="0x0", iomem_base="0x0")
    _tty(root, "ttyS3", "platform/serial8250", "4:67", 3, port="0x2E8")
    m = _load(monkeypatch, tmp_path, root)
    assert [a["name"] for a in m.enumerate_adapters()] == ["ttyS3"]


def test_no_hardware_behind_is_not_listed(monkeypatch, tmp_path):
    root = tmp_path / "root"
    _write(root / "sys/devices/virtual/tty/ttyS9/dev", "4:73\n")
    _link(root / "sys/class/tty/ttyS9", root / "sys/devices/virtual/tty/ttyS9")
    m = _load(monkeypatch, tmp_path, root)
    assert m.enumerate_adapters() == []


# ---- access status ---------------------------------------------------------------

def _raiser(code):
    def _open(*_a):
        raise OSError(code, os.strerror(code))
    return _open


def test_probe_no_access_names_the_major_and_the_fix(sv, monkeypatch):
    """No BRIDGE_ACCESS_FIX = a plain `docker run`: the fix is the exact rule."""
    monkeypatch.setattr(sv, "_open", _raiser(errno.EPERM))
    sc0 = _by_dev(sv.enumerate_adapters())["/dev/ttySC0"]
    r = sv.probe_access(sc0)
    assert r["status"] == "no-access" and r["needs_major"] == 509 and "509" in r["reason"]
    assert r["fix"] == "recreate the container with --device-cgroup-rule='c 509:* rmw'"
    r = sv.probe_access(_by_dev(sv.enumerate_adapters())["/dev/ttyS0"])
    assert r["fix"] == "recreate the container with --device-cgroup-rule='c 4:64 rmw'"


def test_probe_fix_follows_the_compose_file(monkeypatch, tmp_path, pi5):
    m = _load(monkeypatch, tmp_path, pi5,
              BRIDGE_ACCESS_FIX="sh serial-access.sh --compose docker-compose.bridge.yml")
    monkeypatch.setattr(m, "_open", _raiser(errno.EPERM))
    r = m.probe_access(_by_dev(m.enumerate_adapters())["/dev/ttySC1"])
    assert r["fix"] == "sh serial-access.sh --compose docker-compose.bridge.yml"


@pytest.mark.parametrize("code,status,needle", [
    (errno.EBUSY, "busy", "another program"),
    (errno.EACCES, "no-access", "dialout"),
    (errno.ENOENT, "no-access", "not in the container"),
    (errno.EIO, "no-access", "cannot open"),
])
def test_probe_other_errors(sv, monkeypatch, code, status, needle):
    monkeypatch.setattr(sv, "_open", _raiser(code))
    r = sv.probe_access(_by_dev(sv.enumerate_adapters())["/dev/ttySC0"])
    assert r["status"] == status and needle in r["reason"] and r["needs_major"] is None


def test_probe_busy_when_locked(sv, pi5):
    import fcntl
    path = pi5 / "dev/ttySC0"
    with open(path) as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        r = sv.probe_access(_by_dev(sv.enumerate_adapters())["/dev/ttySC0"])
    assert r["status"] == "busy"
    assert sv.probe_access(_by_dev(sv.enumerate_adapters())["/dev/ttySC0"])["status"] == "ok"


@pytest.fixture
def bridge(monkeypatch, tmp_path, pi5):
    """The supervisor over the Pi 5 tree with ser2net faked; `denied` is the
    set of /dev names the container's device rules refuse (EPERM)."""
    def start(denied=(), **env):
        m = _load(monkeypatch, tmp_path, pi5, **env)
        opened = []

        def _open(path, flags):
            name = os.path.basename(path)
            opened.append(name)
            if name in denied:
                raise OSError(errno.EPERM, "Operation not permitted")
            return os.open(path, os.O_RDONLY)
        monkeypatch.setattr(m, "_open", _open)

        class _Proc:
            hups = 0

            def poll(self):
                return None

            def send_signal(self, _sig):
                self.hups += 1
        monkeypatch.setattr(m, "_ser2net", _Proc())
        monkeypatch.setattr(m, "_clear_dead_pid_locks", lambda: None)
        m.reconcile()
        return m, opened
    return start


def test_reconcile_serves_only_ok_ports(bridge, tmp_path):
    m, opened = bridge(denied={"ttySC0", "ttySC1"})
    views = {v["dev"]: v for v in (m.adapter_view(a) for a in m._adapters)}
    for dev in ("/dev/ttySC0", "/dev/ttySC1"):
        assert views[dev]["status"] == "no-access" and views[dev]["needs_major"] == 509
        assert views[dev]["available"] is False and views[dev]["tcp_port"] is None
        assert views[dev]["needs_enable"] is False             # a HAT channel: no opt-in
    for dev in ("/dev/ttyAMA0", "/dev/ttyAMA10"):
        assert views[dev]["status"] == "reserved" and views[dev]["available"] is False
    assert views["/dev/ttyUSB0"]["status"] == "ok" and views["/dev/ttyUSB0"]["tcp_port"]
    s0 = views["/dev/ttyS0"]
    assert s0["status"] == "not-enabled" and s0["needs_enable"] and not s0["enabled"]
    assert s0["tcp_port"] is None and "enabled" in s0["reason"]
    cfg = (tmp_path / "ser2net.yaml").read_text()
    for name in ("ttySC0", "ttySC1", "ttyAMA0", "ttyAMA10", "ttyS0", "ttyS1"):
        assert f"/dev/{name}," not in cfg
    assert "/dev/ttyUSB0," in cfg
    # never opened to look: a console, a Bluetooth UART, a SoC UART not enabled,
    # and a USB adapter (served as before, no DTR/RTS toggle)
    for name in ("ttyAMA0", "ttyAMA10", "ttyS0", "ttyUSB0", "ttyUSB1"):
        assert name not in opened, name


def test_board_port_probed_once_per_lifetime(bridge, monkeypatch):
    """Each open raises DTR/RTS: a board port is probed once, never again per
    reconcile nor while ser2net restarts — a dead ser2net included."""
    m, opened = bridge()
    assert opened.count("ttySC0") == 1
    m.reconcile()

    class _Dead:
        def poll(self):
            return 1
    monkeypatch.setattr(m, "_ser2net", _Dead())
    monkeypatch.setattr(m, "_reload_ser2net", lambda: None)
    m.reconcile()
    m.reconcile()
    assert opened.count("ttySC0") == 1 and opened.count("ttySC1") == 1


def test_excluded_board_port_is_never_opened(bridge):
    m, opened = bridge(BRIDGE_EXCLUDE="spi0.0:1")
    sc1 = next(a for a in m._adapters if a["name"] == "ttySC1")
    assert sc1["status"] == "excluded" and sc1["excluded"] and "ttySC1" not in opened


def test_access_granted_is_served_after_the_recreate(bridge):
    """The fix recreates the container: a new bridge lifetime probes again."""
    m, _opened = bridge(denied={"ttySC0"})
    assert next(a for a in m._adapters if a["name"] == "ttySC0")["status"] == "no-access"
    m2, _o = bridge()
    sc0 = next(a for a in m2._adapters if a["name"] == "ttySC0")
    assert sc0["status"] == "ok" and sc0["tcp_port"]


def test_usb_console_is_reserved(monkeypatch, tmp_path, pi5):
    (pi5 / "proc/cmdline").write_text("console=ttyUSB0,115200 rootwait\n")
    m = _load(monkeypatch, tmp_path, pi5)
    usb0 = _by_dev(m.enumerate_adapters())["/dev/ttyUSB0"]
    assert usb0["status"] == "reserved" and "console=ttyUSB0" in usb0["reason"]


def test_probe_clears_hupcl(sv, pi5):
    """Closing the probe must not drop DTR: HUPCL is cleared on a real tty."""
    import pty
    import termios
    master, slave = pty.openpty()
    try:
        attrs = termios.tcgetattr(slave)
        attrs[2] |= termios.HUPCL
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        node = pi5 / "dev/ttySC0"
        node.unlink()
        node.symlink_to(os.ttyname(slave))
        assert sv.probe_access(_by_dev(sv.enumerate_adapters())["/dev/ttySC0"])["status"] == "ok"
        assert not termios.tcgetattr(slave)[2] & termios.HUPCL
    finally:
        os.close(master)
        os.close(slave)


# ---- SoC UARTs: opt-in only -----------------------------------------------------

def test_soc_uart_served_when_included_by_env(bridge, tmp_path):
    m, opened = bridge(BRIDGE_INCLUDE="00:05")
    s0 = next(a for a in m._adapters if a["name"] == "ttyS0")
    assert s0["status"] == "ok" and s0["enabled"] and s0["tcp_port"]
    assert "/dev/ttyS0," in (tmp_path / "ser2net.yaml").read_text() and "ttyS0" in opened


def test_soc_uart_enable_and_disable_through_the_api(bridge, tmp_path):
    m, opened = bridge()
    view = m.set_adapter_enabled("00:05", {"enabled": True})
    assert view["status"] == "ok" and view["enabled"] and view["tcp_port"]
    assert json.loads((tmp_path / "portmap.json").read_text())[m.ENABLED_STATE_KEY] == ["00:05"]
    assert "/dev/ttyS0," in (tmp_path / "ser2net.yaml").read_text()
    view = m.set_adapter_enabled("00:05", {"enabled": False})
    assert view["status"] == "not-enabled" and view["tcp_port"] is None
    assert m.ENABLED_STATE_KEY not in json.loads((tmp_path / "portmap.json").read_text())
    assert "/dev/ttyS0," not in (tmp_path / "ser2net.yaml").read_text()


@pytest.mark.parametrize("key,body,needle", [
    ("00:05", {"enabled": "yes"}, "body must be"),
    ("00:05", {"enabled": True, "x": 1}, "body must be"),
    ("spi0.0:0", {"enabled": True}, "needs no enabling"),
    ("1a86:7523@1-1", {"enabled": True}, "needs no enabling"),
    ("107d001000.serial", {"enabled": True}, "never served"),
])
def test_enable_refusals(bridge, key, body, needle):
    m, _opened = bridge()
    with pytest.raises(ValueError, match=needle):
        m.set_adapter_enabled(key, body)
    assert m.set_adapter_enabled("nope", {"enabled": True}) is None


def test_enable_over_http(bridge):
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    m, _opened = bridge()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), m._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        req = urllib.request.Request(base + "/adapters/00%3A05/enable", data=b'{"enabled": true}',
                                     method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert json.loads(r.read())["status"] == "ok"
        assert m.route("GET", "/adapters/x/enable") == ("method_not_allowed", "x")
    finally:
        srv.shutdown()
        srv.server_close()


# ---- RS-485 mode ------------------------------------------------------------------

def test_rs485_validation(sv):
    base = sv.DEFAULT_SERIAL
    assert sv.validate_serial({"rs485": True}, base) == {**base, "rs485": True}
    assert sv.validate_serial({"rs485": False}, {**base, "rs485": True}) == base
    assert "rs485" not in sv.validate_serial({"baud": 19200}, base)   # off = not stored
    for bad in ("yes", 1, None, "true"):
        with pytest.raises(ValueError):
            sv.validate_serial({"rs485": bad}, base)


def test_rs485_rendering(sv):
    cfg = sv.render_ser2net_config([
        {"name": "ttySC0", "dev": "/dev/ttySC0", "tcp_port": 7003,
         "serial": {**sv.DEFAULT_SERIAL, "rs485": True}},
        {"name": "ttySC1", "dev": "/dev/ttySC1", "tcp_port": 7004},
    ])
    assert "connector: serialdev,/dev/ttySC0,9600n81,local,rs485=0:0:rts_on_send\n" in cfg
    assert "connector: serialdev,/dev/ttySC1,9600n81,local\n" in cfg
    assert sv.serial_text({**sv.DEFAULT_SERIAL, "rs485": True}) == "9600 8N1 RS-485"


def test_rs485_spec_env(monkeypatch, tmp_path, pi5):
    m = _load(monkeypatch, tmp_path, pi5, BRIDGE_RS485="1:1:rts_after_send:rx_during_tx")
    assert m.RS485_SPEC == "1:1:rts_after_send:rx_during_tx"
    m = _load(monkeypatch, tmp_path, pi5, BRIDGE_RS485="0:0,local,9600")   # an injection
    assert m.RS485_SPEC == "0:0:rts_on_send"


def test_rs485_state_roundtrip_and_reload(bridge, tmp_path):
    m, _opened = bridge()
    view = m.set_adapter_serial("spi0.0:0", {"rs485": True})
    assert view["rs485"] is True and view["serial_params"] == m.DEFAULT_SERIAL
    assert view["serial_text"] == "9600 8N1 RS-485" and view["kind"] == "board"
    cfg = (tmp_path / "ser2net.yaml").read_text()
    assert "/dev/ttySC0,9600n81,local,rs485=0:0:rts_on_send" in cfg
    state = json.loads((tmp_path / "portmap.json").read_text())
    # its own key: the line settings stay exactly what older bridges read
    assert state[m.RS485_STATE_KEY] == ["spi0.0:0"]
    assert state[m.SERIAL_STATE_KEY]["spi0.0:0"] == m.DEFAULT_SERIAL
    # a baud change keeps rs485; turning it off drops it from the state file
    assert m.set_adapter_serial("spi0.0:0", {"baud": 19200})["rs485"] is True
    m.set_adapter_serial("spi0.0:0", {"rs485": False})
    state = json.loads((tmp_path / "portmap.json").read_text())
    assert m.RS485_STATE_KEY not in state and state[m.SERIAL_STATE_KEY]["spi0.0:0"]["baud"] == 19200
    assert "rs485=" not in (tmp_path / "ser2net.yaml").read_text()


def test_rs485_refused_on_usb(bridge):
    m, _opened = bridge()
    with pytest.raises(ValueError, match="board UART"):
        m.set_adapter_serial("1a86:7523@1-1", {"rs485": True})
    assert m.set_adapter_serial("1a86:7523@1-1", {"rs485": False})["rs485"] is False


def test_downgrade_keeps_the_line_settings(monkeypatch, tmp_path, pi5):
    """A bridge from before board UARTs reads the state file this one writes:
    it keeps every port and the baud/parity of an adapter that has rs485 and
    an opt-in — it only logs the keys it does not know."""
    m = _load(monkeypatch, tmp_path, pi5)
    serial = {"spi0.0:0": {"baud": 19200, "parity": "E", "databits": 8, "stopbits": 1,
                           "rs485": True}}
    dumped = json.loads(json.dumps(m.dump_state({"spi0.0:0": 7003, "00:05": 7004}, serial,
                                                {"00:05"})))
    spec = importlib.util.spec_from_file_location(
        "sb_old", Path(__file__).parent / "fixtures" / "serial_bridge_supervisor_pre_e1.py")
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    ports, old_serial = old.parse_state(dumped)
    assert ports == {"spi0.0:0": 7003, "00:05": 7004}
    assert old_serial == {"spi0.0:0": {"baud": 19200, "parity": "E", "databits": 8, "stopbits": 1}}
    assert m.parse_state(dumped) == ({"spi0.0:0": 7003, "00:05": 7004}, serial)
    assert m.parse_enabled(dumped) == {"00:05"}


# ---- stale device-rule slots -------------------------------------------------------

def test_slots_checked_against_sysfs(sv, pi5):
    cls = pi5 / "sys/devices/virtual/misc/foo"
    _write(cls / "dev", "10:200\n")
    (pi5 / "sys/class/misc").mkdir(parents=True, exist_ok=True)
    _link(cls / "subsystem", pi5 / "sys/class/misc")
    _link(pi5 / "sys/dev/char/10:200", cls)
    got = sv.check_slots(["509:*", "4:64", "510:*", "10:*", "188:2"],
                         {"3": "sc16is7xx", "4": "sc16is7xx"})
    assert [w["slot"] for w in got] == [3, 4]
    assert "no device has that number" in got[0]["message"] and "sc16is7xx" in got[0]["message"]
    assert "misc, not a serial port" in got[1]["message"]
    assert got[0]["fix"] == "sh scripts/serial-access.sh"


def test_slots_unchecked_without_sys_dev_char(monkeypatch, tmp_path):
    m = _load(monkeypatch, tmp_path, tmp_path / "empty")
    assert m.check_slots(["510:*"], {}) == []


def test_slots_from_the_env_and_warnings_reported(monkeypatch, tmp_path, pi5):
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    m = _load(monkeypatch, tmp_path, pi5, BRIDGE_TTY_SLOTS="510:* 188:1 188:2 188:3",
              BRIDGE_TTY_DRIVERS="1:sc16is7xx", BRIDGE_TOKEN="t")
    m._warnings[:] = m.check_slots()
    assert len(m._warnings) == 1 and "BRIDGE_TTY_1=510:* (written for sc16is7xx)" in m._warnings[0]["message"]
    srv = ThreadingHTTPServer(("127.0.0.1", 0), m._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        with urllib.request.urlopen(base + "/health", timeout=5) as r:
            assert json.loads(r.read())["warnings"] == 1          # anonymous: a count
        req = urllib.request.Request(base + "/health", headers={"Authorization": "Bearer t"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert "510" in json.loads(r.read())["warnings"][0]
        req = urllib.request.Request(base + "/adapters", headers={"Authorization": "Bearer t"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert json.loads(r.read())["warnings"][0]["slot"] == 1
    finally:
        srv.shutdown()
        srv.server_close()
