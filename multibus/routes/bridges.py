# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Bridges on the Devices page: the boxes a serial bus is reached through.

A bridge lists its buses (ports) and the devices on each; its health is the
box's own (our bridge answers /health) or, for a converter that has no API,
what its devices see. Adding one, the gateway says what to set on the box —
or, for our container, gives the exact command to start it on the host.
"""
from __future__ import annotations

import json
import logging
import secrets
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Request

from ..bridges import FRAMING_PROTOCOL, TYPES, public_view, validate_bridge
from ._shared import secrets_visible

logger = logging.getLogger(__name__)


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


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["bridges"])
    config, registry = ctx.config, ctx.registry
    _health_cache: Dict[str, Any] = {}

    def _bridge(bid: str) -> Dict:
        b = config.get_raw_bridge(bid)
        if b is None:
            raise HTTPException(status_code=404, detail="bridge not found")
        return b

    def _control_url(b: Dict, path: str) -> str:
        port = int(b.get("control_port") or TYPES.get(b.get("type"), {}).get("control_port", 7000))
        return f"http://{b['host']}:{port}{path}"

    def _api_get(b: Dict, path: str, timeout: float = 4) -> Dict:
        req = urllib.request.Request(_control_url(b, path))
        if b.get("token"):
            req.add_header("Authorization", f"Bearer {b['token']}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — operator-configured LAN box
            return json.loads(resp.read().decode() or "{}")

    def _device_health(dev_id: str) -> str:
        for d, c in registry:
            if d.id == dev_id:
                if not d.enabled:
                    return "disabled"
                try:
                    return (c.data_health() or {}).get("status", "idle") if c else "idle"
                except Exception:  # noqa: BLE001
                    return "idle"
        return "idle"

    _checking: set = set()

    def _refresh_health(b: Dict) -> None:
        if b["id"] in _checking:
            return
        _checking.add(b["id"])

        def run():
            try:
                h = _api_get(b, "/health", timeout=3)
                st = {"status": "online" if h.get("status") == "ok" else "degraded",
                      "detail": h.get("reason") or "", "version": h.get("version", ""),
                      "source": "bridge"}
            except Exception as e:  # noqa: BLE001
                st = {"status": "offline", "detail": f"no answer from {b['host']}: {e}",
                      "source": "bridge"}
            _health_cache[b["id"]] = (time.monotonic(), st)
            _checking.discard(b["id"])
        threading.Thread(target=run, daemon=True, name=f"bridge-health-{b['id']}").start()

    def _status(b: Dict, devs: List) -> Dict:
        """The box's own word when it has an API; otherwise what its devices
        see — online if any reads, offline when every one of them fails."""
        t = TYPES.get(b.get("type"), {})
        if t.get("health") == "api":
            # asked in the background: a bridge that does not answer must not
            # hold up the Devices page (3 s per dead bridge, every refresh)
            hit = _health_cache.get(b["id"])
            if not hit or time.monotonic() - hit[0] >= 10:
                _refresh_health(b)
            return hit[1] if hit else {"status": "checking", "detail": "", "source": "bridge"}
        hs = [_device_health(d.id) for d in devs if d.enabled]
        if not hs:
            return {"status": "unknown", "detail": "no device reads through it yet", "source": "devices"}
        if any(h in ("ok", "degraded") for h in hs):
            return {"status": "online", "detail": "", "source": "devices"}
        return {"status": "offline", "detail": "none of its devices answers", "source": "devices"}

    def _busy_share(dev_id: str) -> Optional[float]:
        """How much of the bus this device takes: Σ over its poll groups of
        (wire time of one sweep ÷ the group's interval), as MEASURED."""
        for d, c in registry:
            if d.id != dev_id or c is None or not d.enabled:
                continue
            try:
                groups = (c.get_stats() or {}).get("poll_groups_detail") or []
            except Exception:  # noqa: BLE001
                return None
            shares = [g["cycle_s"] / g["interval"] for g in groups
                      if g.get("cycle_s") and g.get("interval")]
            return round(sum(shares), 4) if shares else None
        return None

    def _view(b: Dict, deep: bool = False) -> Dict:
        devs = config.bridge_devices(b["id"])
        t = TYPES.get(b.get("type"), {})
        ports = {int(p["port"]): dict(p) for p in (b.get("ports") or []) if p.get("port")}
        on: Dict[int, List[Dict]] = {}
        for d in devs:
            on.setdefault(int(d.connection.port), []).append(
                {"id": d.id, "name": d.name, "unit_id": d.connection.unit_id,
                 "endpoint_id": d.endpoint_id, "health": _device_health(d.id),
                 "bus_share": _busy_share(d.id)})
        for p in on:
            ports.setdefault(p, {"port": p})
        out = public_view(b)
        out["type_name"] = t.get("name", b.get("type"))
        out["protocol"] = FRAMING_PROTOCOL.get(t.get("framing"), "")
        def busy(devlist):
            shares = [x["bus_share"] for x in devlist if x.get("bus_share") is not None]
            if not shares:
                return None
            pct = round(100 * sum(shares))
            return {"pct": pct, "level": "full" if pct >= 100 else "high" if pct >= 70 else "ok"}
        out["ports"] = [{**ports[p], "devices": sorted(on.get(p, []), key=lambda x: x["unit_id"]),
                         "busy": busy(on.get(p, []))}
                        for p in sorted(ports)]
        out["devices"] = len(devs)
        out["state"] = _status(b, devs)
        return out

    # ── types ────────────────────────────────────────────────────────────────
    @r.get("/api/bridge-types")
    def bridge_types():
        return {"types": list(TYPES.values())}

    # ── the bridges ──────────────────────────────────────────────────────────
    @r.get("/api/bridges")
    def list_bridges():
        return {"bridges": [_view(b) for b in config.bridges]}

    # ── moving bridges between gateways ──────────────────────────────────────
    @r.get("/api/bridges/export")
    def export_bridges(request: Request):
        """Every bridge as YAML. Tokens ride along only for an admin (or a box
        with no login and no API key) — anyone holding one drives that bridge."""
        import yaml
        from fastapi import Response
        open_box = not (getattr(ctx.auth_state, "enabled", False) or ctx.api_key)
        keep = open_box or secrets_visible(request, ctx.auth_state, ctx.api_key)
        out = [{k: v for k, v in b.items() if keep or k != "token"} for b in config.bridges]
        body = ("# Multi-Bus Gateway bridges — import with Devices → Import bridges\n"
                + ("" if keep else "# tokens left out: re-enter them, or export as an admin\n")
                + yaml.safe_dump({"bridges": out}, sort_keys=False, allow_unicode=True))
        return Response(content=body, media_type="application/x-yaml",
                        headers={"Content-Disposition": 'attachment; filename="bridges.yaml"'})

    @r.post("/api/bridges/import")
    def import_bridges(payload: Dict = Body(...)):
        """``{yaml, apply, replace}``: every bridge checked first (new /
        replace / exists / invalid, with the reason); valid ones saved on
        apply. A bridge here that devices use keeps the buses they are on."""
        import yaml
        try:
            doc = yaml.safe_load(str(payload.get("yaml") or ""))
        except yaml.YAMLError as e:
            raise HTTPException(status_code=422, detail={"errors": [f"invalid YAML: {e}"]})
        items = doc.get("bridges") if isinstance(doc, dict) and "bridges" in doc else doc
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list) or not items:
            raise HTTPException(status_code=422, detail={"errors": ["expected a `bridges:` list, or one bridge"]})
        apply, replace = bool(payload.get("apply")), bool(payload.get("replace"))
        out = []
        for i, raw in enumerate(items):
            if not isinstance(raw, dict):
                out.append({"id": f"#{i + 1}", "status": "invalid", "errors": ["not a mapping"]})
                continue
            bid = str(raw.get("id") or f"#{i + 1}")
            here = config.get_raw_bridge(bid)
            entry = {"id": bid, "name": raw.get("name", ""), "type": raw.get("type", "")}
            if here and not replace:
                out.append({**entry, "status": "exists", "errors": ["already here — tick replace to overwrite it"]})
                continue
            errs = validate_bridge(raw)
            if not errs and here:
                used = {int(d.connection.port) for d in config.bridge_devices(bid)}
                new_ports = {int(p["port"]) for p in (raw.get("ports") or []) if p.get("port")}
                lost = sorted(used - new_ports) if TYPES.get(raw.get("type"), {}).get("discovery") == "manual" else []
                if lost:
                    errs = [f"devices here use bus :{', :'.join(map(str, lost))}, which the file drops"]
            if errs:
                out.append({**entry, "status": "invalid", "errors": errs})
                continue
            if apply:
                try:
                    _save(dict(raw), here)
                except HTTPException as e:
                    out.append({**entry, "status": "invalid",
                                "errors": (e.detail or {}).get("errors", [str(e.detail)])})
                    continue
                out.append({**entry, "status": "replaced" if here else "created", "errors": []})
            else:
                out.append({**entry, "status": "replace" if here else "new", "errors": []})
        ok = sum(1 for x in out if x["status"] in ("new", "replace", "created", "replaced"))
        return {"applied": apply, "bridges": out, "ok": ok, "total": len(out)}

    @r.get("/api/bridges/{bridge_id}")
    def get_bridge(bridge_id: str):
        return _view(_bridge(bridge_id))

    def _save(raw: Dict, existing: Optional[Dict]) -> Dict:
        raw = {k: v for k, v in raw.items() if k not in ("state", "devices", "has_token",
                                                        "type_name", "protocol")}
        raw["ports"] = [{k: v for k, v in (p or {}).items() if k != "devices"}
                        for p in (raw.get("ports") or [])]
        if existing and not raw.get("token") and existing.get("token"):
            raw["token"] = existing["token"]          # the form never sees it
        t = TYPES.get(raw.get("type"), {})
        if t.get("serial_config") == "api" and not raw.get("token"):
            raw["token"] = secrets.token_urlsafe(24)   # made here, shown once in the setup
        errs = validate_bridge(raw)
        if errs:
            raise HTTPException(status_code=422, detail={"errors": errs})
        # a bus renumbered in place (same row, new port) takes its devices
        # along; a bus that is gone while devices use it is refused
        moves: Dict[int, int] = {}
        if existing and t.get("discovery") == "manual":
            old = [int(p["port"]) for p in (existing.get("ports") or []) if p.get("port")]
            new = [int(p["port"]) for p in (raw.get("ports") or []) if p.get("port")]
            for i, o in enumerate(old):
                if o not in new and i < len(new) and new[i] not in old:
                    moves[o] = new[i]
            used = {}
            for d in config.bridge_devices(raw["id"]):
                used.setdefault(int(d.connection.port), []).append(d.id)
            orphan = {p: ids for p, ids in used.items() if p not in new and p not in moves}
            if orphan:
                raise HTTPException(status_code=409, detail={"errors": [
                    f"bus :{p} is used by {', '.join(ids)} — move or delete those devices, "
                    f"or keep the bus" for p, ids in orphan.items()]})
        config.upsert_raw_bridge(raw, port_moves=moves)
        _health_cache.pop(raw["id"], None)
        # devices on it follow a changed host / port / connection limit now
        moved = [d.id for d in config.bridge_devices(raw["id"])]
        restart = getattr(ctx, "restart_devices", None)
        if moved and restart:
            restart(moved)
        return {**_view(config.get_raw_bridge(raw["id"])), "restarted": moved}

    @r.post("/api/bridges")
    def create_bridge(payload: Dict = Body(...)):
        if config.get_raw_bridge(str(payload.get("id") or "")):
            raise HTTPException(status_code=409, detail={"errors": [f"bridge '{payload.get('id')}' exists"]})
        return _save(dict(payload), None)

    @r.put("/api/bridges/{bridge_id}")
    def update_bridge(bridge_id: str, payload: Dict = Body(...)):
        existing = _bridge(bridge_id)
        return _save({**dict(payload), "id": bridge_id}, existing)

    @r.delete("/api/bridges/{bridge_id}")
    def delete_bridge(bridge_id: str):
        _bridge(bridge_id)
        try:
            config.delete_bridge(bridge_id)
        except ValueError as e:
            raise HTTPException(status_code=409, detail={"errors": [str(e)]})
        return {"status": "deleted"}

    # ── its buses ────────────────────────────────────────────────────────────
    @r.get("/api/bridges/{bridge_id}/scan")
    def scan_bridge(bridge_id: str):
        """The buses the box has now: asked of our bridge, declared for a
        converter (which cannot be asked)."""
        b = _bridge(bridge_id)
        t = TYPES.get(b.get("type"), {})
        if t.get("discovery") != "api":
            return {"source": "declared", "ports": b.get("ports") or []}
        try:
            data = _api_get(b, "/adapters")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail={"errors": [
                f"the bridge at {b['host']} did not answer ({e}) — is it running, "
                "is the control port open to the gateway, is the token right?"]})
        ports = []
        for a in data.get("adapters") or []:
            if not a.get("tcp_port"):
                continue
            ports.append({"port": int(a["tcp_port"]), "key": a.get("stable_id") or "",
                          "label": " ".join(x for x in (a.get("model"), a.get("serial")) if x) or a.get("dev", ""),
                          "dev": a.get("dev", ""),
                          "serial_params": a.get("serial_params") or {},
                          "serial_text": a.get("serial_text", ""),
                          "available": a.get("available", True), "excluded": a.get("excluded", False)})
        return {"source": "bridge", "hostname": data.get("hostname", ""),
                "version": data.get("version", ""), "ports": ports}

    @r.post("/api/bridges/{bridge_id}/ports/{key}/serial")
    def set_port_serial(bridge_id: str, key: str, payload: Dict = Body(...)):
        """Baud / parity / data / stop bits of one bus, set on our bridge."""
        b = _bridge(bridge_id)
        if TYPES.get(b.get("type"), {}).get("serial_config") != "api":
            raise HTTPException(status_code=422, detail={"errors": [
                "this kind of bridge is set up in its own web page — the gateway cannot change it"]})
        body = json.dumps({k: payload.get(k) for k in ("baud", "parity", "databits", "stopbits")
                           if payload.get(k) is not None}).encode()
        req = urllib.request.Request(
            _control_url(b, f"/adapters/{urllib.parse.quote(key, safe='')}/serial"),
            data=body, method="POST", headers={"Content-Type": "application/json"})
        if b.get("token"):
            req.add_header("Authorization", f"Bearer {b['token']}")
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:  # noqa: S310
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raise HTTPException(status_code=e.code if e.code in (400, 401, 404, 422) else 502,
                                detail={"errors": [e.read().decode(errors="replace")[:300]]})
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail={"errors": [f"bridge unreachable: {e}"]})

    # ── scan a bus: which slaves answer, and what they probably are ──────────
    @r.post("/api/bridges/{bridge_id}/ports/{port}/scan")
    def scan_bus(bridge_id: str, port: int, payload: Dict = Body(default={})):
        """Start sweeping unit ids on one bus through its shared connection.
        Poll GET /api/bus-scan/{job}. ``{from, to, timeout}`` (default 1-247,
        0.3 s per unit — a full bus takes about a minute and a quarter)."""
        from .. import bus_scan
        from ..bridges import resolve_connection
        b = _bridge(bridge_id)
        try:
            u0 = max(1, int(payload.get("from", 1)))
            u1 = min(247, int(payload.get("to", 247)))
            tmo = min(2.0, max(0.1, float(payload.get("timeout", 0.3))))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail={"errors": ["from/to/timeout must be numbers"]})
        if u1 < u0:
            raise HTTPException(status_code=422, detail={"errors": ["from must not exceed to"]})
        conn = resolve_connection({"bridge": b["id"], "bridge_port": port}, config.bridges)
        known = {int(d.connection.unit_id): d.id for d in config.bridge_devices(b["id"])
                 if int(d.connection.port) == int(port)}
        tr = getattr(ctx, "template_registry", None)
        tpls = [t for t in (tr.list() if tr else []) if getattr(t, "identify", None)]
        job = bus_scan.start(conn, u0, u1, tmo, tpls, known)
        return {"job": job, "total": u1 - u0 + 1}

    @r.get("/api/bus-scan/{job}")
    def bus_scan_status(job: str):
        from .. import bus_scan
        s = bus_scan.get(job)
        if s is None:
            raise HTTPException(status_code=404, detail="no such scan")
        return s

    @r.delete("/api/bus-scan/{job}")
    def bus_scan_cancel(job: str):
        from .. import bus_scan
        return {"cancelled": bus_scan.cancel(job)}

    @r.get("/api/bridges/{bridge_id}/setup")
    def bridge_setup(bridge_id: str, request: Request):
        """What to run on the host for our bridge (with its token), or what to
        set on a converter."""
        b = _bridge(bridge_id)
        t = TYPES.get(b.get("type"), {})
        out = {"type": t.get("id"), "steps": t.get("setup", [])}
        if t.get("serial_config") == "api":
            # the token goes to an admin only — anyone holding it drives the
            # bridge (a box with no login and no API key has nothing to hide behind)
            _open_box = not (getattr(ctx.auth_state, "enabled", False) or ctx.api_key)
            if not (_open_box or secrets_visible(request, ctx.auth_state, ctx.api_key)):
                b = {**b, "token": "<ask an admin>"}
            img = "ghcr.io/sm26449/multi-bus-gateway-serial-bridge:latest"
            ctrl = int(b.get("control_port") or 7000)
            out["docker_run"] = (
                "docker run -d --name mbg-serial-bridge --restart unless-stopped \\\n"
                f"  -e BRIDGE_TOKEN='{b.get('token', '')}' \\\n"
                "  -e BRIDGE_PORT_LOW=7001 -e BRIDGE_PORT_HIGH=7016 \\\n"
                "  -v /dev:/dev:ro -v /etc/hostname:/etc/host-hostname:ro \\\n"
                "  -v serial-bridge-data:/data \\\n"
                "  --device-cgroup-rule='c 188:* rmw' --device-cgroup-rule='c 166:* rmw' \\\n"
                "  --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \\\n"
                "  --cap-add SETUID --cap-add SETGID --security-opt no-new-privileges:true \\\n"
                f"  -p {ctrl}:7000 -p 7001-7016:7001-7016 \\\n"
                f"  {img}")
            out["compose"] = "serial-bridge/docker-compose.bridge.yml (set BRIDGE_TOKEN to the value above)"
            out["token"] = b.get("token", "")
        return out

    @r.post("/api/bridges/{bridge_id}/probe")
    def probe_bridge(bridge_id: str, payload: Dict = Body(...)):
        """Which language a bus port speaks: ask one register of one slave as
        an RTU frame, then as Modbus TCP. Opens its OWN connection — a
        converter that takes one client drops the gateway's for a moment."""
        b = _bridge(bridge_id)
        t = TYPES.get(b.get("type"), {})
        port = int(payload.get("port") or ((b.get("ports") or [{}])[0].get("port") or t.get("default_port", 502)))
        unit = int(payload.get("unit_id", 1))
        addr = int(payload.get("address", 0))
        res: Dict[str, Any] = {"port": port, "unit_id": unit, "configured_as": t.get("framing")}
        try:
            got = _exchange(b["host"], port, _rtu_read(unit, addr))
            res["rtu"] = "answers" if _rtu_ok(got, unit) else ("garbage" if got else "silent")
            if res["rtu"] != "answers":
                got2 = _exchange(b["host"], port, _mbap_read(unit, addr))
                fn = _mbap_ok(got2)
                res["modbus_tcp"] = ("answers" if fn == 3 else f"exception {got2[8]:#04x}"
                                     if fn and fn & 0x80 and len(got2) > 8 else
                                     "garbage" if got2 else "silent")
            else:
                res["modbus_tcp"] = "not tried"
        except OSError as e:
            res["error"] = f"cannot connect to {b['host']}:{port} ({e})"
            res["verdict"] = "The box does not accept connections on that port — check its IP, port and that it is on."
            return res
        speaks = "rtu" if res["rtu"] == "answers" else \
                 "modbus_tcp" if str(res.get("modbus_tcp", "")).startswith(("answers", "exception")) else ""
        res["speaks"] = speaks
        if speaks and speaks == t.get("framing"):
            res["verdict"] = "It speaks the way this bridge is set up."
            if str(res.get("modbus_tcp", "")).startswith("exception 0x0b"):
                res["verdict"] = ("Gateway mode, but the slave did not answer in time (0x0B): check the unit id, "
                                  "the wiring and the converter's response timeout.")
        elif speaks == "modbus_tcp":
            res["verdict"] = ("It answers Modbus TCP: the converter is in GATEWAY mode ('Modbus TCP to RTU'). "
                              "Change this bridge's type to Modbus TCP gateway, or set the converter to transparent.")
        elif speaks == "rtu":
            res["verdict"] = ("It answers raw RTU: the converter is in TRANSPARENT mode. Change this bridge's "
                              "type to transparent, or set the converter to 'Modbus TCP to RTU'.")
        elif res["rtu"] == "garbage":
            res["verdict"] = ("Bytes came back but not a valid frame: baud/parity on the converter differ from "
                              "the bus, or its packet interval cuts frames in two.")
        else:
            res["verdict"] = ("No answer either way: wrong unit id, slave off or miswired (A/B swapped), or "
                              "the converter's serial settings do not match the bus.")
        return res

    return r
