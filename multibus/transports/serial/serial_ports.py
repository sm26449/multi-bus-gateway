# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The serial ports of a host, from sysfs (ADR-0028).

One enumerator for the gateway (``GET /api/serial-ports``) and the serial
bridge (``GET /adapters``), so the two lists show the same thing. The bridge
image builds from ``serial-bridge/`` alone, so it carries a byte-identical
copy of this file (``serial-bridge/serial_ports.py``, kept identical by
``tests/test_serial_ports.py``): edit this one, then copy it. Standard
library only, and nothing from ``multibus``.

It walks ``<root>/sys/class/tty`` and never opens a port: it reads sysfs,
``/dev`` (is the node there, which ``/dev/serial/by-id`` link points at it)
and ``/proc`` (the kernel command line, a getty's arguments, which process
holds a port open). ``root`` is "/" on a host or in a container; the tests
point it at a fake tree.

A container sees every host tty in ``/sys/class/tty`` but only the mapped
ones in ``/dev``, possibly under other names (a udev alias node such as
``/dev/ttyFRONIUS``, a renamed mapping ``/dev/ttyUSB1:/dev/ttyUSB0``): a port
is matched to ``/dev`` by its device number (``stat``, never ``open``), and
``mapped`` / ``aliases`` say under which paths the container has it.

Reading a UART's serial-core attributes (``type``, ``port``, ``iomem_base``)
briefly takes the kernel's per-port mutex; nothing is opened and no line
setting changes.
"""
from __future__ import annotations

import glob
import os
import re
import stat
from typing import Dict, List, Optional

# The board's own UART families: SC16IS7xx HAT channels, ARM PL011, i.MX,
# OMAP, 8250/16550 (PCs and many SoCs). `\d+$` keeps ttySC0 out of ttyS.
BOARD_TTY = re.compile(r"^(ttySC|ttyAMA|ttymxc|ttyO|ttyS)(\d+)$")
USB_TTY = re.compile(r"^(ttyUSB|ttyACM)\d+$")
# pseudo-terminals (legacy BSD ptys and the master side); /dev/pts never
# shows in /sys/class/tty
_PTY = re.compile(r"^(pty[p-za-e][0-9a-f]|tty[p-za-e][0-9a-f]|ptmx)$")
_GETTY = re.compile(r"(^|/)(a|m|s|min)?getty$")


def _p(root: str, path: str) -> str:
    """A host path (/sys/..., /dev/..., /proc/...) under ``root``."""
    return os.path.join(root or "/", path.lstrip("/"))


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _natural(name: str):
    m = re.match(r"^(\D*)(\d*)$", name)
    return (m.group(1), int(m.group(2) or -1)) if m else (name, -1)


def _driver(device: str) -> str:
    link = os.path.join(device, "driver")
    return os.path.basename(os.path.realpath(link)) if os.path.exists(link) else ""


def _dev_num(root: str, name: str) -> str:
    """"major:minor" of a tty from sysfs, "" when unknown."""
    num = _read(_p(root, f"/sys/class/tty/{name}/dev"))
    return num if re.fullmatch(r"\d+:\d+", num) else ""


def _phantom(cls_dir: str, name: str) -> bool:
    """A UART the kernel registered with no hardware behind it: the 8250
    driver's ttyS0..31 placeholders on PCs (`type` 0 = PORT_UNKNOWN, or
    neither an I/O port nor a memory address)."""
    if _read(f"{cls_dir}/type") == "0":
        return True
    m = BOARD_TTY.match(name)
    if m and m.group(1) == "ttyS":
        unset = ("", "0", "0x0", "0x00000000")
        return (_read(f"{cls_dir}/port") in unset
                and _read(f"{cls_dir}/iomem_base") in unset)
    return False


def _bluetooth(root: str, device: str) -> bool:
    """The UART carries the board's Bluetooth controller: a `bluetooth` node
    under it in the device tree, a serdev child with a bluetooth device, or an
    hci device bound to it."""
    of_node = os.path.join(device, "of_node")
    try:
        if any(n.split("@")[0] == "bluetooth" for n in os.listdir(of_node)
               if os.path.isdir(os.path.join(of_node, n))):
            return True
    except OSError:
        pass
    if glob.glob(os.path.join(device, "*", "bluetooth")) or \
            glob.glob(os.path.join(device, "*", "*", "bluetooth")):
        return True
    return any(os.path.realpath(hci) == device
               for hci in glob.glob(_p(root, "/sys/class/bluetooth/*/device")))


def _chip(device: str, driver: str) -> str:
    """The part's name for a label: the device-tree compatible's model
    ("nxp,sc16is752" → "SC16IS752"), else the driver's name."""
    compat = _read(os.path.join(device, "of_node", "compatible")).split("\0")[0]
    model = compat.split(",")[-1] if compat else driver
    return model.upper() if model.lower().startswith("sc16is") else (model or "UART")


def _rs485(device: str) -> Optional[bool]:
    """Kernel RS-485 mode as the device tree sets it at boot
    (``linux,rs485-enabled-at-boot-time``): None when there is no device tree
    node to read — the running mode is only readable by opening the port."""
    of_node = os.path.join(device, "of_node")
    if not os.path.isdir(of_node):
        return None
    return os.path.exists(os.path.join(of_node, "linux,rs485-enabled-at-boot-time"))


def _in_root(root: str, link: str, hops: int = 8) -> str:
    """Follow a /dev symlink inside ``root`` (an absolute target is taken
    relative to ``root``), to the host-style path it ends at."""
    base = os.path.normpath(root or "/")
    cur = link
    for _ in range(hops):
        if not os.path.islink(cur):
            break
        target = os.readlink(cur)
        cur = os.path.normpath(_p(base, target) if os.path.isabs(target)
                               else os.path.join(os.path.dirname(cur), target))
    rel = os.path.relpath(cur, base)
    return "/" + rel if not rel.startswith("..") else cur


def by_id_links(root: str) -> Dict[str, List[str]]:
    """tty name → its ``/dev/serial/by-id`` links (host-style paths)."""
    out: Dict[str, List[str]] = {}
    for link in sorted(glob.glob(_p(root, "/dev/serial/by-id/*"))):
        target = _in_root(root, link)
        if target.startswith("/dev/"):
            out.setdefault(target[len("/dev/"):], []).append(
                "/dev/serial/by-id/" + os.path.basename(link))
    return out


def _pids(root: str) -> List[str]:
    try:
        return sorted((d for d in os.listdir(_p(root, "/proc")) if d.isdigit()), key=int)
    except OSError:
        return []


def console_ttys(root: str = "/", gettys: bool = False) -> Dict[str, str]:
    """tty name → why it is a console: `console=` on the kernel command line,
    what /sys/class/tty/console/active lists and — with ``gettys`` — a getty
    running on it (only the processes this namespace can see)."""
    out: Dict[str, str] = {}
    for tok in _read(_p(root, "/proc/cmdline")).split():
        if tok.startswith("console="):
            name = tok[len("console="):].split(",")[0]
            if name:
                out.setdefault(name, f"kernel console ({tok.split(',')[0]} in /proc/cmdline)")
    for name in _read(_p(root, "/sys/class/tty/console/active")).split():
        out.setdefault(name, "active kernel console (/sys/class/tty/console/active)")
    if gettys:
        for pid in _pids(root):
            args = [a for a in _read(_p(root, f"/proc/{pid}/cmdline")).split("\0") if a]
            if not args or not _GETTY.search(args[0]):
                continue
            for a in args[1:]:
                name = a[len("/dev/"):] if a.startswith("/dev/") else a
                if re.fullmatch(r"tty[A-Za-z]+\d+", name):
                    out.setdefault(name, f"a getty runs on it ({os.path.basename(args[0])})")
    return out


def _node_num(path: str) -> str:
    """"major:minor" of the character device at ``path`` (a symlink and a
    /proc/<pid>/fd link are followed), "" for anything else. A ``stat``:
    nothing is opened."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return ""
    if not stat.S_ISCHR(st.st_mode):
        return ""
    return f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"


def dev_nodes(root: str) -> Dict[str, List[str]]:
    """"major:minor" → the /dev paths (host-style) that are that character
    device: the top-level nodes and the /dev/serial/by-id links."""
    out: Dict[str, List[str]] = {}
    for full in sorted(glob.glob(_p(root, "/dev/*")) + glob.glob(_p(root, "/dev/serial/by-id/*"))):
        if os.path.isdir(full) and not os.path.islink(full):
            continue
        num = _node_num(full)
        if num:
            out.setdefault(num, []).append("/" + os.path.relpath(full, os.path.normpath(root or "/")))
    return out


def fd_holders(root: str, paths: Dict[str, List[str]],
               nums: Optional[Dict[str, str]] = None) -> Optional[Dict[str, List[str]]]:
    """A non-blocking ``fuser``: tty name → the program name of each process
    with it open, from /proc/<pid>/fd. A descriptor is matched by the device
    number its link leads to (``stat``, never ``open``: ``nums`` maps
    "major:minor" → tty name), else by the path it names (``paths``: tty name
    → /dev/<name>, its aliases, its by-id links). This process itself is
    never a holder. None when no process's descriptors could be read at all
    (nothing can be said)."""
    # a path two ports share says nothing: attributed to neither, never guessed
    want: Dict[str, Optional[str]] = {}
    for name, ps in paths.items():
        for p in ps:
            want[p] = name if want.get(p, name) == name else None
    nums = nums or {}
    me = str(os.getpid())
    out: Dict[str, List[str]] = {}
    seen = False
    for pid in _pids(root):
        if pid == me:
            continue
        fd_dir = _p(root, f"/proc/{pid}/fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        seen = True
        for fd in fds:
            link = os.path.join(fd_dir, fd)
            name = nums.get(_node_num(link)) if nums else None
            if not name:
                try:
                    name = want.get(os.readlink(link))
                except OSError:
                    continue
            if name:
                who = _read(_p(root, f"/proc/{pid}/comm")) or "?"
                if who not in out.setdefault(name, []):
                    out[name].append(who)
    return out if seen else None


def _kind(name: str, usb: str) -> str:
    if usb or USB_TTY.match(name):
        return "usb"
    m = BOARD_TTY.match(name)
    if m:
        return "hat" if m.group(1) == "ttySC" else "board_uart"
    return "other"


def _usb_dir(device: str) -> str:
    """The USB device directory above a tty's device (it holds idVendor), or ''."""
    usb = device
    for _ in range(8):
        if os.path.exists(os.path.join(usb, "idVendor")):
            return usb
        usb = os.path.dirname(usb)
    return ""


def _where(root: str, name: str, dev_num: str, links: Dict[str, List[str]],
           nodes: Dict[str, List[str]]) -> dict:
    """Under which /dev paths this container has a port: its own node (when
    it is that device: a renamed mapping puts another one there), its by-id
    links, other nodes with its device number (a udev alias)."""
    path = f"/dev/{name}"
    in_dev = os.path.lexists(_p(root, path))
    own_num = _node_num(_p(root, path))
    own = (own_num == dev_num) if (own_num and dev_num) else in_dev
    same = nodes.get(dev_num, []) if dev_num else []
    by_id = []
    for link in links.get(name, []) + [x for x in same if x.startswith("/dev/serial/by-id/")]:
        num = _node_num(_p(root, link))
        if link not in by_id and ((num == dev_num) if (num and dev_num) else in_dev):
            by_id.append(link)
    aliases = [x for x in same if x != path and not x.startswith("/dev/serial/by-id/")]
    return {"in_dev": in_dev, "own": own, "by_id": by_id, "aliases": aliases,
            "mapped": bool(own or by_id or aliases)}


def enumerate_ports(root: str = "/", gettys: bool = False) -> List[dict]:
    """Every real serial port of the host: USB adapters (by name), then the
    board's UARTs and anything else with hardware behind it (natural order).
    Virtual ttys, ptys and phantom UARTs are left out. Each entry:

    name, path (/dev/<name>), stable_path (the path the container has it
    under: a USB adapter's by-id link, else its own node, else an alias;
    path when it has none, '' when /dev/<name> here is another device),
    path_taken (that case: a renamed mapping), by_id, aliases (other /dev nodes with its device
    number), kind (usb | hat | board_uart | other), vendor_id, product_id,
    serial, manufacturer, model, port_path (the USB port path, or the bus
    address of the device behind a UART), channel (a UART's index on a chip
    with several, else None), driver, chip, label, dev_num, rs485 (None when
    unknown), bluetooth, console (why it is one, or ''), in_dev (a node
    named /dev/<name> exists), mapped (the container has it under some
    path). ``gettys``: also look for a getty on each port in /proc."""
    links = by_id_links(root)
    nodes = dev_nodes(root)
    consoles = console_ttys(root, gettys=gettys)
    found = []                                   # (name, cls_dir, device)
    for cls_dir in glob.glob(_p(root, "/sys/class/tty/*")):
        name = os.path.basename(cls_dir)
        if name in ("console", "tty") or _PTY.match(name):
            continue
        link = os.path.join(cls_dir, "device")
        if not os.path.exists(link):             # virtual: no hardware behind it
            continue
        found.append((name, cls_dir, os.path.realpath(link)))

    def by_line(entry):                          # a chip's channels, in order
        num = _read(f"{entry[1]}/line")
        return (int(num) if num.isdigit() else 1 << 30, _natural(entry[0]))

    # channels per device, phantoms included (the kernel numbers them all)
    channels: Dict[str, List[str]] = {}
    for entry in sorted(found, key=by_line):
        if BOARD_TTY.match(entry[0]):
            channels.setdefault(entry[2], []).append(entry[0])

    out = []
    for name, cls_dir, device in found:
        usb = _usb_dir(device)
        kind = _kind(name, usb)
        if kind != "usb" and _phantom(cls_dir, name):
            continue
        driver = _driver(device)
        path = f"/dev/{name}"
        dev_num = _dev_num(root, name)
        w = _where(root, name, dev_num, links, nodes)
        # /dev/<name> here may be another device (a renamed mapping): then it
        # is never this port's path to store
        taken = w["in_dev"] and not w["own"]
        stable = (w["by_id"][0] if (kind == "usb" and w["by_id"]) else path if w["own"]
                  else (w["aliases"] or ["" if taken else path])[0])
        p = {
            "name": name, "path": path, "stable_path": stable,
            "by_id": w["by_id"], "aliases": w["aliases"], "kind": kind, "vendor_id": "",
            "product_id": "", "serial": "", "manufacturer": "", "model": "", "port_path": "",
            "channel": None, "driver": driver, "chip": "", "label": "", "dev_num": dev_num,
            "rs485": None, "bluetooth": False, "console": consoles.get(name, ""),
            "in_dev": w["in_dev"], "path_taken": taken, "mapped": w["mapped"],
        }
        if kind == "usb":
            serial = _read(f"{usb}/serial") if usb else ""
            port_path = os.path.basename(usb) if usb else ""
            manufacturer = _read(f"{usb}/manufacturer") if usb else ""
            model = _read(f"{usb}/product") if usb else ""
            p.update(vendor_id=_read(f"{usb}/idVendor") if usb else "",
                     product_id=_read(f"{usb}/idProduct") if usb else "",
                     serial=serial, manufacturer=manufacturer, model=model, port_path=port_path,
                     label=(model or manufacturer or "USB serial")
                     + (f" ({serial})" if serial else f" (USB {port_path})"))
        else:
            addr = os.path.basename(device)
            chan = channels.get(device, [name])
            multi = len(chan) > 1
            chip = _chip(device, driver)
            p.update(port_path=addr, channel=chan.index(name) if multi else None, chip=chip,
                     label=f"{chip} ch{chan.index(name)} ({addr})" if multi else f"{chip} ({addr})",
                     rs485=_rs485(device),
                     bluetooth=kind in ("hat", "board_uart") and _bluetooth(root, device))
        out.append(p)
    order = {"usb": 0, "hat": 1, "board_uart": 1, "other": 2}
    return sorted(out, key=lambda p: (order[p["kind"]],
                                      p["name"] if p["kind"] == "usb" else _natural(p["name"])))
