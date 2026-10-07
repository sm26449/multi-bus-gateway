# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Bridges: the network equipment a serial bus is reached through.

A **bridge** is a box on the LAN (our ser2net container on a Raspberry Pi, a
Waveshare / USR / Elfin RS-485-to-Ethernet converter) with one or more
**ports**, each one RS-485 bus on its own TCP port. A device on such a bus
names its bridge and port instead of a host and a protocol:

    connection: { bridge: pi-garage, bridge_port: 7001, unit_id: 2 }

and gets them from the bridge when the device list is built: host, TCP port,
protocol (``rtu-tcp`` for a transparent bridge, ``tcp`` for a Modbus TCP
gateway) and how many connections the box accepts. Move the Pi to another IP
and every device on it follows.

What a KIND of bridge can do is data, not code: ``bridge_types/*.json`` says
how it frames (RTU over TCP or Modbus TCP), whether its ports are discovered
or declared, whether the gateway can set its serial parameters, how its health
is known, and what to set on the box. A new kind of equipment is a new file.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

TYPES_DIR = Path(__file__).parent / "bridge_types"
FRAMING_PROTOCOL = {"rtu": "rtu-tcp", "modbus_tcp": "tcp"}
_ID_RX = re.compile(r"[a-z][a-z0-9_-]{1,47}")
_HOST_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-:]{0,252}")


def load_types(directory: Path = TYPES_DIR) -> Dict[str, Dict[str, Any]]:
    """Every bridge type the gateway knows, by id."""
    out: Dict[str, Dict[str, Any]] = {}
    for p in sorted(Path(directory).glob("*.json")):
        try:
            t = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001 — one bad file must not hide the rest
            logger.warning("bridge type %s unreadable: %s", p.name, e)
            continue
        if t.get("framing") not in FRAMING_PROTOCOL or not t.get("id"):
            logger.warning("bridge type %s: needs an id and framing rtu|modbus_tcp", p.name)
            continue
        out[t["id"]] = t
    return out


TYPES = load_types()


def validate_bridge(raw: Dict[str, Any], types: Dict[str, Dict] = None) -> List[str]:
    """Errors in words for one ``bridges:`` entry (empty = fine)."""
    types = TYPES if types is None else types
    errs: List[str] = []
    if not isinstance(raw, dict):
        return ["a bridge must be a mapping"]
    if not _ID_RX.fullmatch(str(raw.get("id") or "")):
        errs.append("id: lowercase letters, digits, - and _ (2-48 chars, starts with a letter)")
    t = types.get(str(raw.get("type") or ""))
    if t is None:
        errs.append(f"type: one of {', '.join(sorted(types))}")
    if not _HOST_RX.fullmatch(str(raw.get("host") or "")):
        errs.append("host: the bridge's IP address or host name")
    try:
        mc = int(raw.get("max_connections", (t or {}).get("max_connections", 1)))
        if not 1 <= mc <= 16:
            raise ValueError
    except (TypeError, ValueError):
        errs.append("max_connections: 1-16 (how many clients the box accepts at once)")
    ports = raw.get("ports") or []
    if not isinstance(ports, list):
        errs.append("ports: a list of {port, label?, serial?}")
        ports = []
    seen = set()
    for i, p in enumerate(ports):
        try:
            n = int((p or {}).get("port"))
            if not 1 <= n <= 65535:
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            errs.append(f"ports[{i}].port: a TCP port 1-65535")
            continue
        if n in seen:
            errs.append(f"ports[{i}]: port {n} listed twice")
        seen.add(n)
    if t is not None and t.get("discovery") == "manual" and not ports:
        errs.append(f"ports: a {t['name']} has its buses declared — add at least one "
                    f"(the converter's TCP port, usually {t.get('default_port')})")
    if t is not None and len(ports) > int(t.get("max_ports", 99)):
        errs.append(f"ports: a {t['name']} has at most {t.get('max_ports')}")
    if "control_port" in raw:
        try:
            if not 1 <= int(raw["control_port"]) <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            errs.append("control_port: a TCP port 1-65535")
    return errs


def resolve_connection(conn: Dict[str, Any], bridges: List[Dict[str, Any]],
                       types: Dict[str, Dict] = None) -> Dict[str, Any]:
    """A connection that names a bridge, with what the bridge supplies filled
    in: host, TCP port, protocol and its connection limit. A connection that
    names none, or a bridge that no longer exists, comes back as it was."""
    if not isinstance(conn, dict) or not conn.get("bridge"):
        return conn
    types = TYPES if types is None else types
    b = next((x for x in bridges or [] if x.get("id") == conn.get("bridge")), None)
    if b is None:
        logger.warning("connection names bridge %r, which does not exist", conn.get("bridge"))
        return conn
    t = types.get(str(b.get("type") or ""), {})
    port = conn.get("bridge_port") or (b.get("ports") or [{}])[0].get("port") or t.get("default_port")
    out = dict(conn)
    out["host"] = b.get("host", "")
    out["port"] = int(port or 502)
    out["protocol"] = FRAMING_PROTOCOL.get(t.get("framing", "rtu"), "rtu-tcp")
    out["max_connections"] = int(b.get("max_connections", t.get("max_connections", 1)) or 1)
    return out


def public_view(raw: Dict[str, Any]) -> Dict[str, Any]:
    """A bridge as the API shows it: the token never leaves the gateway."""
    out = {k: v for k, v in raw.items() if k != "token"}
    out["has_token"] = bool(raw.get("token"))
    return out
