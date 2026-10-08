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
import socket
import struct
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TYPES_DIR = Path(__file__).parent / "bridge_types"
# how a kind of bridge frames the bus on its TCP port → the device protocol
# (a kind's file names its framing; ascii lets a Modbus ASCII bus through)
FRAMING_PROTOCOL = {"rtu": "rtu-tcp", "ascii": "ascii-tcp", "modbus_tcp": "tcp"}
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
    if conn.get("protocol") == "rtu_tap" and t.get("framing", "rtu") == "rtu":
        # listen-only through a bridge that passes the bus's bytes on: the tap
        # reads the bridge's port and never writes to it
        out["protocol"] = "rtu_tap"
        out.pop("serial_port", None)
    out["max_connections"] = int(b.get("max_connections", t.get("max_connections", 1)) or 1)
    return out


def public_view(raw: Dict[str, Any]) -> Dict[str, Any]:
    """A bridge as the API shows it: the token never leaves the gateway."""
    out = {k: v for k, v in raw.items() if k != "token"}
    out["has_token"] = bool(raw.get("token"))
    return out


# ── speaking to a bus port: one register, as RTU or as Modbus TCP ─────────
def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _rtu_read(unit: int, address: int) -> bytes:
    body = struct.pack(">BBHH", unit, 3, address, 1)
    return body + struct.pack("<H", _crc16(body))


def _rtu_ok(resp: bytes, unit: int) -> bool:
    """A whole RTU answer from that slave, CRC right (an exception counts:
    it proves the slave and the framing)."""
    if len(resp) < 5 or resp[0] != unit:
        return False
    n = 5 if resp[1] & 0x80 else 3 + resp[2] + 2 if len(resp) > 2 else 0
    if len(resp) < n:
        return False
    frame = resp[:n]
    return struct.unpack("<H", frame[-2:])[0] == _crc16(frame[:-2])


def _mbap_read(unit: int, address: int, tid: int = 0x4D42) -> bytes:
    return struct.pack(">HHHBBHH", tid, 0, 6, unit, 3, address, 1)


def _mbap_ok(resp: bytes, tid: int = 0x4D42) -> Optional[int]:
    """The function byte of a Modbus TCP answer to our transaction, or None."""
    if len(resp) < 9:
        return None
    t, proto, _ln, _u, fn = struct.unpack(">HHHBB", resp[:8])
    return fn if t == tid and proto == 0 else None


def _exchange(host: str, port: int, frame: bytes, wait: float = 1.5) -> bytes:
    with socket.create_connection((host, port), timeout=3) as s:
        s.settimeout(wait)
        s.sendall(frame)
        buf, t0 = b"", time.monotonic()
        while time.monotonic() - t0 < wait:
            try:
                chunk = s.recv(256)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            time.sleep(0.05)              # a converter may split one frame
        return buf


# ── finding bridges on the LAN ───────────────────────────────────────────
def _http_json(host: str, port: int, path: str, timeout: float) -> Optional[Dict[str, Any]]:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=timeout) as r:  # noqa: S310 — LAN sweep
            return json.loads(r.read(4096).decode() or "{}")
    except Exception as e:  # noqa: BLE001 — a 500 from a degraded bridge still carries JSON
        body = getattr(e, "read", None)
        try:
            return json.loads(body().decode()) if body else None
        except Exception:  # noqa: BLE001
            return None


def _port_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def discover(hosts: List[str], types: Dict[str, Dict] = None, known: Dict[str, str] = None,
             timeout: float = 0.4, unit_id: int = 1) -> List[Dict[str, Any]]:
    """Which hosts on the LAN look like a bridge, and of which kind.

    Each kind says in its ``lan_discovery`` block which TCP ports to look at
    and how to recognise itself: an HTTP path whose JSON carries given keys
    (our bridge's /health), or — for a converter, which has no API — just an
    open port, then one register asked of ``unit_id`` as RTU and as Modbus TCP
    to tell transparent from gateway (the same test as Check mode). ``known``
    maps host → the bridge already added there."""
    types = TYPES if types is None else types
    known = known or {}
    by_port: Dict[int, List[Dict]] = {}
    for t in types.values():
        for p in (t.get("lan_discovery") or {}).get("ports") or []:
            by_port.setdefault(int(p), []).append(t)

    def one(job):
        host, port = job
        if not _port_open(host, port, timeout):
            return None
        kinds = by_port[port]
        hit: Dict[str, Any] = {"host": host, "port": port, "bridge": known.get(host, "")}
        for t in kinds:
            ld = t["lan_discovery"]
            if ld.get("http_path"):
                doc = _http_json(host, port, ld["http_path"], max(1.0, timeout * 3))
                if isinstance(doc, dict) and all(k in doc for k in ld.get("json_keys") or []):
                    hit.update(type=t["id"], type_name=t.get("name", t["id"]),
                               version=str(doc.get("version", "")),
                               detail=f"{doc.get('adapters', '?')} adapter(s)")
                    return hit
        framed = [t for t in kinds if not (t.get("lan_discovery") or {}).get("http_path")]
        if not framed:
            return None
        try:
            got = _exchange(host, port, _rtu_read(unit_id, 0), wait=max(0.6, timeout * 2))
            speaks = "rtu" if _rtu_ok(got, unit_id) else ""
            if not speaks:
                got = _exchange(host, port, _mbap_read(unit_id, 0), wait=max(0.6, timeout * 2))
                speaks = "modbus_tcp" if _mbap_ok(got) else ""
        except OSError:
            speaks = ""
        match = [t for t in framed if t.get("framing") == speaks]
        if match:
            hit.update(type=match[0]["id"], type_name=match[0].get("name", match[0]["id"]),
                       detail=f"unit {unit_id} answered as " + ("RTU" if speaks == "rtu" else "Modbus TCP"))
        else:
            # an open port and no answer from that unit: a converter, most
            # likely — which mode, Check mode will tell once a slave is known
            hit.update(type="", type_name="", candidates=[t["id"] for t in framed],
                       detail=f"port open, unit {unit_id} did not answer — add it, then Check mode with a known unit")
        return hit

    from concurrent.futures import ThreadPoolExecutor
    # a host already added is not knocked on: a converter that takes one
    # client would drop the gateway's own connection for the moment
    jobs = [(h, p) for h in hosts if h not in known for p in sorted(by_port)]
    out: List[Dict[str, Any]] = [{"host": h, "port": None, "bridge": b, "type": "", "detail": "already added"}
                                 for h, b in known.items() if h in hosts]
    with ThreadPoolExecutor(max_workers=min(64, max(1, len(jobs)))) as ex:
        for r in ex.map(one, jobs):
            if r:
                out.append(r)
    import ipaddress
    out.sort(key=lambda r: (ipaddress.ip_address(r["host"]), r["port"] or 0))
    return out
