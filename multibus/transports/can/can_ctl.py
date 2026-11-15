# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Control a CAN interface — its mode (transmit ↔ listen-only), bitrate and up/down (ADR-0046 L2).

Uses ``ip`` (iproute2, installed in the image) because hand-rolling the CAN rtnetlink attributes
(bittiming + ctrlmode) is error-prone. Needs ``CAP_NET_ADMIN``. Bittiming/ctrlmode can only change
while the link is DOWN, so ``apply`` brings it down, sets the type, and brings it up — exactly the
sequence an operator runs by hand. ``ip_argv`` builds the command list (pure, unit-tested);
``_run`` is the only subprocess seam.
"""
from __future__ import annotations

import re
import subprocess
from typing import List, Optional


def ip_argv(name: str, *, down: bool = False, up: bool = False, bitrate: Optional[int] = None,
            listen_only: Optional[bool] = None, restart_ms: int = 100) -> List[str]:
    """The ``ip`` argument list for one step. Pure (unit-tested)."""
    if down:
        return ["link", "set", name, "down"]
    if up:
        return ["link", "set", name, "up"]
    args = ["link", "set", name, "type", "can"]
    if bitrate is not None:
        args += ["bitrate", str(int(bitrate)), "restart-ms", str(int(restart_ms))]
    if listen_only is not None:
        args += ["listen-only", "on" if listen_only else "off"]
    return args


def _run(argv: List[str]) -> str:
    r = subprocess.run(["ip", *argv], capture_output=True, text=True)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or f"ip {' '.join(argv)} failed").strip()
        if "not permitted" in msg.lower() or "permission denied" in msg.lower():
            raise PermissionError(msg)      # MBG lacks CAP_NET_ADMIN
        raise OSError(msg)
    return r.stdout


def is_up(name: str) -> bool:
    r = subprocess.run(["ip", "-o", "link", "show", name], capture_output=True, text=True)
    m = re.search(r"<([^>]*)>", r.stdout)
    return bool(m) and "UP" in m.group(1).split(",")


def set_up(name: str, up: bool) -> None:
    _run(ip_argv(name, down=not up, up=up))


def apply(name: str, *, mode: Optional[str] = None, bitrate: Optional[int] = None,
          up: Optional[bool] = None, restart_ms: int = 100) -> None:
    """Apply mode ('transmit'|'listen_only'), bitrate and up to a CAN interface. The type change
    needs the link DOWN, so: down → set type → up. ``up=None`` restores the prior up state."""
    listen_only = {"transmit": False, "listen_only": True}.get(mode) if mode else None
    want_up = is_up(name) if up is None else up
    if bitrate is not None or listen_only is not None:
        _run(ip_argv(name, down=True))
        _run(ip_argv(name, bitrate=bitrate, listen_only=listen_only, restart_ms=restart_ms))
        _run(ip_argv(name, up=want_up, down=not want_up))
    elif up is not None:
        set_up(name, up)
