# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The serial ports of this host and what each is to the gateway (ADR-0028).

:func:`inventory` takes the ports :mod:`serial_ports` lists and gives each
one state, with its reason:

- ``listened``: an enabled device only listens on it (bus claims, ADR-0025);
- ``polled``: an enabled device transmits on it;
- ``console``: the kernel console or a getty — hidden unless ``show_all``;
- ``not_mapped``: in /sys, but no node of this container's /dev is that
  device; ``fix`` holds the ``--device`` flag and the compose ``devices:``
  line (and, for USB, that a ttyUSB name can change on a replug);
- ``busy``: another process has it open (only the processes this container
  can see, never the gateway itself; ``processes`` names the programs);
- ``unknown``: it is here but no process could be read to tell;
- ``free``.

``base_state`` is the same without the claims (console, not_mapped, busy,
unknown, free): what the port is to a device that edits its own claim.

A claim matches a port whatever name it is made by: the character device
(``major:minor``, ADR-0025's canonical form), ``/dev/<name>``, a udev alias
node or a ``/dev/serial/by-id`` link to it. Nothing is opened.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from . import serial_ports
from .bus_claims import canonical_serial, source_claims

# where /sys, /dev and /proc are read from: "/" here, a fake tree in the tests
SYSROOT = "/"

# the keys GET /api/serial-ports answered with before ADR-0028
_OLD_KEYS = ("dev", "vendor_id", "product_id", "serial", "model")


def _claim_keys(root: str, where: str) -> set:
    """Every name a configured serial path can match a port by."""
    sp = str(where or "").strip()
    if not sp:
        return set()
    if "/" not in sp and not sp.startswith("\\"):
        sp = "/dev/" + sp                                 # a bare ttyUSB0
    keys = {sp, canonical_serial(sp)}
    if sp.startswith("/"):
        local = serial_ports._p(root, sp)
        if os.path.lexists(local):
            keys.add(serial_ports._in_root(root, local))
            num = serial_ports._node_num(local)
            if num:
                keys.add("chr:" + num)
    return keys


def _port_keys(p: dict, root: str) -> set:
    """Every name a claim can reach this port by: its device number, its
    by-id links and aliases, and /dev/<name> unless that node is another
    device (a renamed mapping)."""
    keys = {*p.get("by_id", []), *p.get("aliases", [])}
    if p.get("dev_num"):
        keys.add("chr:" + p["dev_num"])
    own = serial_ports._node_num(serial_ports._p(root, p["path"]))
    if not own or own == p.get("dev_num"):
        keys.add(p["path"])
        if os.path.normpath(root or "/") == "/":
            keys.add(canonical_serial(p["path"]))
    keys.discard("")
    return keys


def _claims(config, templates, root: str) -> List[tuple]:
    """(device id, listens, keys) of each enabled source on a serial line."""
    out = []
    for dev in getattr(config, "devices", None) or []:
        if not getattr(dev, "enabled", False):
            continue
        for _i, bus, listens, where in source_claims(dev, templates, standby=False):
            if bus.startswith("chr:") or bus.startswith("/") or where.startswith("/") \
                    or where.startswith("tty"):
                out.append((dev.id, listens, _claim_keys(root, where) | {bus}))
    return out


def _fix(p: dict) -> Dict[str, str]:
    path = p["path"]
    if p.get("path_taken"):
        # /dev/<name> in this container is another device (a renamed
        # mapping): map this one under a path that does not collide
        inside = f"{path}-{p.get('serial') or p.get('port_path') or 'host'}"
        return {"docker_run": f"--device {path}:{inside}", "compose": f"- {path}:{inside}", "taken": True,
                "note": (f"{path} in this container is already another device (a renamed "
                         f"mapping), so this one goes in under {inside}; a /dev/serial/by-id/… "
                         f"link or a udev alias on the host, as the same path inside, also "
                         f"survives a replug")}
    fix = {"docker_run": f"--device {path}:{path}", "compose": f"- {path}:{path}"}
    if p.get("kind") == "usb":
        # a ttyUSB name is handed out in plug order: say so, and the stable form
        fix["note"] = (f"{p['name']} can change when the adapter is replugged: map its "
                       f"/dev/serial/by-id/… link (ls -l /dev/serial/by-id on the host) or a "
                       f"udev alias instead, as the same path inside")
    return fix


def _base_state(p: dict, holders: Optional[Dict[str, List[str]]]) -> tuple:
    """(state, reason) of the port itself, whatever device claims it."""
    if p["console"]:
        return "console", p["console"]
    if not p["mapped"]:
        return "not_mapped", (f"{p['path']} is not mapped into this container: add "
                              f"{p['fix']['docker_run']}, or under the compose service's devices: "
                              f"{p['fix']['compose']}")
    if holders is None:
        return "unknown", "no process of this host could be read to tell whether it is in use"
    if p["processes"]:
        return "busy", f"open in {', '.join(p['processes'])}"
    return "free", ""


def _state(p: dict, base: tuple) -> tuple:
    """(state, reason): a claim says most, then what the port itself is."""
    if p["listened_by"]:
        return "listened", (f"listened to by {', '.join(p['listened_by'])}: another master owns "
                            f"this bus, nothing may poll on it")
    if p["polled_by"]:
        return "polled", f"polled by {', '.join(p['polled_by'])}"
    return base


def inventory(config: Any, templates: Any = None, *, show_all: bool = False,
              root: Optional[str] = None) -> dict:
    """{ports: [...], hidden: n}: each port of :func:`serial_ports.enumerate_ports`
    with ``state`` and ``reason``, ``base_state``, ``polled_by``,
    ``listened_by``, ``processes`` (program names), ``fix`` (not mapped) and
    the old keys (``dev`` …). Consoles are left out unless ``show_all``;
    ``hidden`` counts them."""
    root = root if root is not None else SYSROOT
    ports = serial_ports.enumerate_ports(root, gettys=True)
    claims = _claims(config, templates, root)
    paths = {p["name"]: [*([] if p["path_taken"] else [p["path"]]), *p["by_id"], *p["aliases"]]
             for p in ports if p["mapped"]}
    nums = {p["dev_num"]: p["name"] for p in ports if p["mapped"] and p["dev_num"]}
    holders = serial_ports.fd_holders(root, paths, nums)
    out, hidden = [], 0
    for p in ports:
        keys = _port_keys(p, root)
        p["polled_by"], p["listened_by"] = [], []
        for did, listens, ckeys in claims:
            lst = p["listened_by"] if listens else p["polled_by"]
            if keys & ckeys and did not in lst:
                lst.append(did)
        p["processes"] = list((holders or {}).get(p["name"], []))
        p["fix"] = _fix(p) if not p["mapped"] else None
        base = _base_state(p, holders)
        p["base_state"] = base[0]
        p["state"], p["reason"] = _state(p, base)
        p["dev"] = p["path"]
        for k in _OLD_KEYS:
            p.setdefault(k, "")
        if p["state"] == "console" and not show_all:
            hidden += 1
            continue
        out.append(p)
    return {"ports": out, "hidden": hidden}
