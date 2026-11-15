# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Small, pure connection-shape checks the API runs before a save: a
virtual-map bus's poll/listen mode, and the "a serial port was typed where a
network address goes" guard (FINDINGS BANK-1020). No FastAPI, no state."""
import re
from typing import Dict, Optional

from multibus.core.config import VIRTUAL_MAP_PROTOCOLS


def _vmap_mode_missing(conn: Dict, stored: Optional[Dict]) -> bool:
    """A virtual-map connection that does not say its mode. Only a device
    stored that way (before the mode was asked) keeps the old default, poll;
    anything new, or moved onto this protocol, must choose."""
    if str((conn or {}).get('mode') or '').strip():
        return False
    return str((stored or {}).get('protocol', '')).lower() not in VIRTUAL_MAP_PROTOCOLS


def _vmap_mode(conn) -> str:
    """A virtual-map bus's mode as stored (absent = poll)."""
    raw = conn.get('mode') if isinstance(conn, dict) else getattr(conn, 'mode', '')
    return str(raw or 'poll').lower()


# a serial line named where a network address goes: "/dev/ttySEPLOS" as a TCP
# host opened a socket to "/dev/ttySEPLOS:502" and the bank went dark
# (2026-10-20, FINDINGS BANK-1020)
_SERIAL_PATH_RE = re.compile(r'^(?:/|\\\\\.\\|COM\d+$|tty[A-Za-z]*\d+$)', re.I)


def _is_serial_path(host) -> bool:
    return bool(_SERIAL_PATH_RE.match(str(host or '').strip()))


def _serial_host_error(field: str, host) -> str:
    return (f"{field}: '{str(host).strip()}' is a serial port, not a network address "
            f"— it goes in serial_port")
