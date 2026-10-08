# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Scan one bus: which unit ids answer, and what each one probably is.

The scan goes through the bus's SHARED connection — the one its devices
already use — one question at a time, holding the bus lock only for that
question. A single-client converter is never fought over and the devices on
the bus keep reading between the questions.

Recognising a slave uses what the device or its template says, never a list
in code: the Modbus device identification (FC43/14), the SunSpec marker, and
each template's own ``identify`` block (registers that must read given
values). A unit nothing recognises is still listed — the operator picks.
"""
from __future__ import annotations

import logging
import re
import struct
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_JOBS: Dict[str, Dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()
SUNS = (0x5375, 0x6E53)


def _alive(client, uid: int) -> bool:
    """Any Modbus answer — data or an exception — proves a slave."""
    from pymodbus.pdu import ExceptionResponse
    try:
        r = client.read_holding_registers(address=0, count=1, device_id=uid)
    except Exception:  # noqa: BLE001
        return False
    return r is not None and (isinstance(r, ExceptionResponse) or not r.isError())


def _regs(client, uid: int, address: int, count: int, rtype: str = "holding") -> Optional[List[int]]:
    try:
        fn = client.read_input_registers if rtype == "input" else client.read_holding_registers
        r = fn(address=address, count=count, device_id=uid)
    except Exception:  # noqa: BLE001
        return None
    if r is None or r.isError():
        return None
    return list(r.registers)


def _decode(words: List[int], data_type: str, byte_order: str) -> Optional[float]:
    dt = (data_type or "uint16").lower()
    if dt in ("uint16", "int16", "short"):
        v = words[0]
        return v - 0x10000 if dt != "uint16" and v >= 0x8000 else v
    if len(words) < 2:
        return None
    w = words[:2] if byte_order in ("big", "badc") else words[:2][::-1]
    raw = struct.pack(">HH", *w)
    if byte_order in ("badc", "dcba"):
        raw = bytes([raw[1], raw[0], raw[3], raw[2]])
    fmt = {"uint32": ">I", "int32": ">i", "float": ">f", "float32": ">f"}.get(dt)
    return struct.unpack(fmt, raw)[0] if fmt else None


def _check(value: Optional[float], rule: Dict[str, Any]) -> bool:
    if value is None:
        return False
    if "equals" in rule:
        return value == rule["equals"]
    if "in" in rule:
        return value in rule["in"]
    lo, hi = rule.get("min"), rule.get("max")
    return (lo is None or value >= lo) and (hi is None or value <= hi)


def _fc43(client, uid: int) -> Dict[str, str]:
    try:
        r = client.read_device_information(device_id=uid)
    except Exception:  # noqa: BLE001
        return {}
    info = getattr(r, "information", None) or {}
    dec = lambda b: b.decode(errors="replace").strip() if isinstance(b, (bytes, bytearray)) else str(b)  # noqa: E731
    out = {k: dec(info[i]) for i, k in ((0, "vendor"), (1, "product"), (2, "version")) if i in info}
    return out


def identify(client, uid: int, templates) -> Dict[str, Any]:
    """What this slave says it is, and which templates' fingerprints match."""
    out: Dict[str, Any] = {"unit_id": uid, "fc43": _fc43(client, uid)}
    words = _regs(client, uid, 40000, 2)
    out["sunspec"] = bool(words and tuple(words) == SUNS)
    matches = []
    for t in templates:
        idf = getattr(t, "identify", None) or {}
        if not idf:
            continue
        bo = (getattr(t, "protocol", None) or {}).get("byte_order", "big")
        ok = True
        for rule in idf.get("registers") or []:
            dt = rule.get("data_type", "uint16")
            n = 1 if dt in ("uint16", "int16", "short") else 2
            w = _regs(client, uid, rule["address"], n, rule.get("register_type", "holding"))
            if not w or not _check(_decode(w, dt, bo), rule):
                ok = False
                break
        fc = idf.get("fc43") or {}
        if ok and fc:
            info = out["fc43"]
            ok = bool(info) and all(re.search(str(fc[k]), info.get(k, ""), re.I) for k in ("vendor", "product") if fc.get(k))
        if ok and (idf.get("registers") or fc):
            matches.append({"id": t.id, "name": t.name})
    out["matches"] = matches
    out["suggested"] = matches[0]["id"] if len(matches) == 1 else None
    return out


def start(conn: Dict[str, Any], unit_from: int, unit_to: int, timeout: float,
          templates, known: Dict[int, str]) -> str:
    """Start a scan in the background; returns its job id."""
    job = uuid.uuid4().hex[:12]
    state = {"id": job, "state": "running", "done": 0, "total": unit_to - unit_from + 1,
             "found": [], "error": "", "started": time.time()}
    with _JOBS_LOCK:
        for k in [k for k, v in _JOBS.items() if time.time() - v.get("started", 0) > 3600]:
            _JOBS.pop(k, None)
        _JOBS[job] = state
    threading.Thread(target=_run, args=(state, conn, unit_from, unit_to, timeout, templates, known),
                     daemon=True, name=f"bus-scan-{job}").start()
    return job


def get(job: str) -> Optional[Dict[str, Any]]:
    with _JOBS_LOCK:
        s = _JOBS.get(job)
        return dict(s, found=list(s["found"])) if s else None


def cancel(job: str) -> bool:
    with _JOBS_LOCK:
        s = _JOBS.get(job)
        if s and s["state"] == "running":
            s["state"] = "cancelling"
            return True
    return False


def _run(state, conn, unit_from, unit_to, timeout, templates, known) -> None:
    from .config import ModbusConfig
    from .modbus_client import ModbusConnection
    cfg = ModbusConfig(host=str(conn.get("host", "")), port=int(conn.get("port", 502) or 502),
                       unit_id=unit_from, timeout=timeout, retry_attempts=1, retry_delay=0,
                       protocol=str(conn.get("protocol", "rtu-tcp")),
                       serial_port=str(conn.get("serial_port", "") or ""),
                       baudrate=int(conn.get("baudrate", 9600) or 9600),
                       parity=str(conn.get("parity", "N") or "N"),
                       stopbits=int(conn.get("stopbits", 1) or 1))
    mc = ModbusConnection(cfg, trace_label="bus-scan")
    try:
        # make sure the shared connection is open (a no-op when devices hold it)
        mc.read_registers(0, 1, "holding")
        for uid in range(unit_from, unit_to + 1):
            if state["state"] == "cancelling":
                break
            with mc.lock:
                client = mc.client
                if client is None:
                    mc.connect()
                    client = mc.client
                alive = client is not None and _alive(client, uid)
                info = identify(client, uid, templates) if alive else None
            if info is not None:
                info["device"] = known.get(uid, "")
                state["found"].append(info)
            state["done"] += 1
        state["state"] = "cancelled" if state["state"] == "cancelling" else "done"
    except Exception as e:  # noqa: BLE001
        logger.warning("bus scan failed: %s", e)
        state["state"], state["error"] = "failed", str(e)
    finally:
        mc.disconnect()
