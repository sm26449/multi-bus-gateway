"""Diagnostics — commissioning tools: the frame-level bus monitor and the
register probe (one-shot read with every interpretation side by side).

The trace is runtime-only by design: it never persists, never survives a
restart, and boots disabled, so a forgotten session can't slowly grow into
the memory of a box that's been up for a year.
"""
from __future__ import annotations

import math
import struct
from typing import Dict, List

from fastapi import APIRouter, Body, HTTPException, Query

from ..bus_trace import trace
from ..register_parser import RegisterParser

_PROBE_ORDERS = ("abcd", "cdab", "badc", "dcba")


def interpret_words(words: List[int]) -> List[Dict]:
    """Every (data type × word order) reading of the same raw words — the
    endianness workbench. Decoding goes through RegisterParser so what you see
    here is exactly what a configured register would produce."""
    types = ["uint16", "int16"]
    if len(words) >= 2:
        types += ["float", "uint32", "int32"]
    if len(words) >= 4:
        types += ["double", "uint64", "int64"]
    parsers = {o: RegisterParser(o) for o in _PROBE_ORDERS}
    out = []
    for t in types:
        n = RegisterParser.REGISTER_COUNTS[t]
        vals = {}
        for order, p in parsers.items():
            if t in ("float", "double"):
                # decode directly over the parser's canonicalization: the
                # telemetry path maps NaN/inf to None, but for a probe the
                # NaN itself is the finding (e.g. a SunSpec not-available)
                regs = p._canon(list(words[:n]))
                v = struct.unpack(">f" if t == "float" else ">d",
                                  struct.pack(">" + "H" * n, *regs))[0]
                if not math.isfinite(v):
                    v = str(v)  # NaN/inf are not JSON numbers
            else:
                v = p.parse_value(list(words[:n]), t)
            vals[order] = v
        out.append({"type": t, "words": n, "orders": vals})
    return out


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["diagnostics"])
    event_log = ctx.event_log

    @r.get("/api/bus-trace")
    async def get_bus_trace(after: int = Query(0, ge=0),
                            limit: int = Query(200, ge=1, le=1000),
                            device: str = Query("")):
        """Captured transactions with seq > ``after`` (incremental polling)."""
        return trace.snapshot(after=after, limit=limit, device=device or None)

    @r.post("/api/bus-trace/config")
    async def set_bus_trace(payload: Dict = Body(...)):
        was = trace.enabled
        trace.configure(
            enabled=bool(payload["enabled"]) if "enabled" in payload else None,
            capacity=payload.get("capacity"),
            clear=bool(payload.get("clear")),
        )
        if trace.enabled != was:
            event_log.add("info", "bus_trace",
                          f"frame trace {'enabled' if trace.enabled else 'disabled'}")
        return {"enabled": trace.enabled, "capacity": trace.capacity,
                "captured_total": trace.captured_total}

    @r.post("/api/diagnostics/probe")
    async def probe_register(payload: Dict = Body(...)):
        """One-shot read on ANY Modbus device, decoded every plausible way.
        Read-only: FC1-4 only, never a write. With the bus monitor capturing,
        the probe's own frames (including an exception reply) land in the trace."""
        dev_id = str(payload.get("device") or "")
        try:
            addr = int(payload.get("address"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="address must be an integer")
        if not 0 <= addr <= 0xFFFF:
            raise HTTPException(status_code=400, detail="address out of range (0..65535)")
        rtype = str(payload.get("register_type", "holding")).lower()
        count = max(1, min(8, int(payload.get("count") or 2)))

        client = next((c for d, c in ctx.registry if d.id == dev_id), None)
        if client is None:
            raise HTTPException(status_code=404, detail=f"unknown device {dev_id!r}")
        conn = getattr(client, "connection", None)
        if conn is None or not hasattr(conn, "read_registers"):
            raise HTTPException(status_code=400,
                                detail="register probe works on Modbus devices only")

        base = {"device": dev_id, "address": addr, "register_type": rtype, "count": count}
        if rtype in ("coil", "discrete"):
            bits = conn.read_bits(addr, count, rtype)
            if bits is None:
                return {**base, "ok": False, "error": "no response"}
            return {**base, "ok": True, "bits": bits}
        if rtype not in ("holding", "input"):
            raise HTTPException(status_code=400,
                                detail="register_type must be holding|input|coil|discrete")
        words = conn.read_registers(addr, count, rtype)
        if words is None:
            return {**base, "ok": False, "error": "no response"}
        raw = b"".join(struct.pack(">H", w) for w in words)
        return {**base, "ok": True,
                "hex": [f"{w:04x}" for w in words],
                "ascii": "".join(chr(b) if 32 <= b < 127 else "·" for b in raw),
                "interpretations": interpret_words(list(words))}

    @r.post("/api/devices/{device_id}/payload-sample")
    async def payload_sample(device_id: str, payload: Dict = Body(default={})):
        """One full payload from a saved MQTT/HTTP device, for the json_path
        picker: MQTT grabs a message from the topic (retained → instant, creds
        come from the saved device so none cross the wire); HTTP fetches the
        device's URL through its SSRF-guarded client."""
        import asyncio
        import json as _json
        from .. import discovery

        pair = next(((d, c) for d, c in ctx.registry if d.id == device_id), None)
        if pair is None:
            raise HTTPException(status_code=404, detail=f"unknown device {device_id!r}")
        dev_cfg, client = pair

        if dev_cfg.protocol == "mqtt":
            m = dev_cfg.mqtt_in or {}
            topic = str(payload.get("topic") or m.get("topic") or "").strip()
            if not topic:
                raise HTTPException(status_code=422,
                                    detail={"errors": ["no topic — set one on the register or the device"]})
            return await asyncio.to_thread(
                discovery.mqtt_sample, str(m.get("broker", "")), int(m.get("port", 1883) or 1883),
                str(m.get("username", "") or ""), str(m.get("password", "") or ""),
                bool(m.get("tls")), topic)
        if dev_cfg.protocol == "http":
            if client is None or not hasattr(client, "_fetch"):
                raise HTTPException(status_code=503, detail="HTTP client not available")
            try:
                doc = await asyncio.to_thread(client._fetch)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": str(e)}
            if doc is None:
                return {"ok": False, "error": "fetch returned nothing — is the device reachable?"}
            return {"ok": True, "payload": _json.dumps(doc)[:16384]}
        raise HTTPException(status_code=400,
                            detail="payload sampling applies to MQTT/HTTP devices only")

    return r
