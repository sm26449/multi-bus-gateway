# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
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
"""Device discovery — Modbus TCP scan / unit sweep + Fronius Solar-API probe.

Moved verbatim from create_api(). The Fronius fetch keeps its SSRF egress
guard: LAN-only resolution pinned to a literal IP, redirects refused.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Dict

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ..payload import flag

_FRONIUS_HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


def _require_lan_host(host: str) -> str:
    """SSRF guard: resolve the host and require every resolved address to be
    a PRIVATE/LAN address. Blocks using this server-side fetch to reach the
    public internet, loopback services, or cloud metadata (169.254.169.254).
    Returns a literal IP to connect to (defeats DNS-rebinding)."""
    import socket
    import ipaddress
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        raise HTTPException(status_code=400, detail="host does not resolve")
    addrs = {i[4][0] for i in infos}
    if not addrs:
        raise HTTPException(status_code=400, detail="host does not resolve")
    for a in addrs:
        ip = ipaddress.ip_address(a)
        # Normalize IPv4-mapped IPv6 so ::ffff:<v4> can't slip a metadata /
        # loopback target past the class checks.
        if getattr(ip, "ipv4_mapped", None) is not None:
            ip = ip.ipv4_mapped
        # private LAN only; explicitly reject loopback, link-local (metadata),
        # unspecified (0.0.0.0 → loopback), and anything global/multicast/reserved.
        if (not ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_unspecified or ip.is_reserved):
            raise HTTPException(status_code=400,
                                detail="host must be a private LAN address")
    return sorted(addrs)[0]


UNITS_WAIT_S = 120.0     # the longest POST /api/discover/modbus/units waits for its sweep


def _port_or_422(port: int) -> int:
    """Shared TCP-port bound for every discovery route — an out-of-range port
    must 422 here, not surface from the client code."""
    if not 1 <= port <= 65535:
        raise HTTPException(status_code=422,
                            detail={"errors": ["port must be 1..65535"]})
    return port


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["discovery"])
    config = ctx.config

    def _refuse_if_listened(what: str, protocol: str, conn: Dict) -> None:
        """409 on a bus a device only listens to (ADR-0025): nothing is sent."""
        from ..bus_claims import bus_of, listeners, refusal_detail, where_of
        bus = bus_of(protocol, conn)
        ids = listeners(config, bus, getattr(ctx, "template_registry", None))
        if ids:
            raise HTTPException(status_code=409, detail=refusal_detail(what, bus, ids, where_of(protocol, conn)))

    @r.post("/api/discover/modbus/scan")
    async def discover_modbus_scan(payload: Dict = Body(...)):
        """Scan a private CIDR on a port (default 502) for Modbus devices. Read-only
        probes; the range is LAN-restricted unless security.allow_nonlan_http_devices."""
        from .. import discovery
        cidr = str(payload.get('cidr', '') or '').strip()
        try:
            port = int(payload.get('port', 502))
            unit = int(payload.get('unit_id', 1))
            timeout = min(3.0, max(0.1, float(payload.get('timeout', 0.5))))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail={"errors": ["port/unit_id/timeout must be numbers"]})
        if not 1 <= port <= 65535:
            raise HTTPException(status_code=422, detail={"errors": ["port must be 1..65535"]})
        try:
            hosts = discovery.hosts_from_cidr(cidr, allow_nonlan=config.security.allow_nonlan_http_devices)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        # a host:port a device only listens to is not knocked on (ADR-0025)
        from ..bus_claims import bus_of, listeners
        tr = getattr(ctx, "template_registry", None)
        skipped = [{"host": h, "listeners": ids} for h in hosts
                   for ids in [listeners(config, bus_of("tcp", {"host": h, "port": port}), tr)] if ids]
        held = {s["host"] for s in skipped}
        # …nor a host:port a device of ours polls, nor a bridge port of ours
        # with a client that is not us (or that nobody can name, unless
        # confirm_open): never a second connection there (L3.7 m1)
        from .. import preview
        from .previews import env_of
        env = env_of(ctx)
        confirm = payload.get("confirm_open") is True
        for h in [h for h in hosts if h not in held]:
            why = preview.second_connection(env, h, port, confirm)
            if why:
                skipped.append({"host": h, **{k: v for k, v in why.items() if k in ("pollers", "client", "unknown")},
                                "reason": why["errors"][0]})
                held.add(h)
        hosts = [h for h in hosts if h not in held]
        results = await asyncio.to_thread(discovery.scan_tcp, hosts, port, unit, timeout)
        return {"scanned": len(hosts), "results": results, **({"skipped": skipped} if skipped else {})}

    @r.post("/api/discover/esphome")
    async def discover_esphome(payload: Dict = Body(...)):
        """Sweep a private CIDR on the ESPHome native-API port (6053). mDNS
        can't cross the Docker bridge, but a unicast TCP sweep can: each
        responder is asked for its identity with a plaintext HelloRequest
        (answered pre-auth); encrypted-API nodes are still detected and
        flagged. Read-only, LAN-restricted, same bounds as the Modbus scan."""
        from .. import discovery
        cidr = str(payload.get('cidr', '') or '').strip()
        try:
            port = int(payload.get('port', discovery.ESPHOME_API_PORT))
            timeout = min(3.0, max(0.1, float(payload.get('timeout', 0.5))))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail={"errors": ["port/timeout must be numbers"]})
        if not 1 <= port <= 65535:
            raise HTTPException(status_code=422, detail={"errors": ["port must be 1..65535"]})
        try:
            hosts = discovery.hosts_from_cidr(cidr, allow_nonlan=config.security.allow_nonlan_http_devices)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        return await asyncio.to_thread(discovery.scan_esphome, hosts, port, timeout)

    @r.post("/api/discover/modbus/units")
    async def discover_modbus_units(request: Request, payload: Dict = Body(...)):
        """Sweep unit/slave ids on ONE endpoint, waited for: the generic bus
        scan (``POST /api/bus-scan``, bus_scan.py) — through the bus's shared
        transport, reads only, 409 on a listened bus. RTU never opens a serial
        client of its own beside a device that holds the line (L3-F1). The
        protocol is kept (rtu, ascii, tcp, udp, rtu-tcp, ascii-tcp); what the
        scan cannot do is refused. Answers ``{protocol, units, job}``; past
        120 s it answers 202 ``{protocol, job, state: running}`` (poll
        ``GET /api/bus-scan/{job}``). A client that goes away stops the job."""
        from .. import bus_scan
        from ..config import NETWORK_PROTOCOLS, SERIAL_PROTOCOLS
        from .bus_scan import launch
        proto = str(payload.get('protocol', 'tcp')).lower()
        try:
            u0 = int(payload.get('unit_start', 1))
            u1 = int(payload.get('unit_end', 32))
            timeout = min(3.0, max(0.2, float(payload.get('timeout', 0.5))))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail={"errors": ["unit range / timeout must be numbers"]})
        if proto in SERIAL_PROTOCOLS:
            sp = str(payload.get('serial_port', '') or '').strip()
            if not sp:
                raise HTTPException(status_code=422, detail={"errors": [f"serial_port required for {proto.upper()}"]})
            conn = {"protocol": proto, "serial_port": sp,
                    **{k: payload[k] for k in ('baudrate', 'parity', 'stopbits', 'bytesize') if k in payload}}
        elif proto in NETWORK_PROTOCOLS:
            host = str(payload.get('host', '') or '').strip()
            if not host:
                raise HTTPException(status_code=422, detail={"errors": [f"host required for {proto}"]})
            try:
                tcp_port = _port_or_422(int(payload.get('port', 502)))
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail={"errors": ["port must be a number"]})
            conn = {"protocol": proto, "host": host, "port": tcp_port}
        else:
            raise HTTPException(status_code=422, detail={"errors": [
                f"protocol: '{proto}' cannot be swept (rtu, ascii, tcp, udp, rtu-tcp, ascii-tcp)"]})
        body = {"connection": conn, "from": max(1, u0), "to": min(247, u1), "timeout": timeout,
                "fc43": False, "confirm_open": payload.get("confirm_open") is True}
        job = launch(ctx, request, body)
        end = asyncio.get_running_loop().time() + UNITS_WAIT_S
        while job.finished is None:
            if await request.is_disconnected():
                bus_scan.cancel(job.id)               # nobody waits for it: it stops asking
                break
            if asyncio.get_running_loop().time() > end:
                return JSONResponse(status_code=202, content={"protocol": proto, "job": job.id, "state": "running"})
            await asyncio.sleep(0.1)
        if job.final == 'failed' and not job.found:
            raise HTTPException(status_code=502, detail={"errors": [job.error or "the sweep failed"], "job": job.id})
        return {"protocol": proto, "units": [f["unit_id"] for f in job.found if f.get("asked")],
                "known": [{"unit": f["unit_id"], "device": f["device"]} for f in job.found if not f.get("asked")],
                "job": job.id}

    @r.post("/api/discover/sunspec")
    async def discover_sunspec(payload: Dict = Body(...)):
        """Walk the SunSpec model chain on one endpoint (read-only, FC3): the
        SunS marker, then every model the device DECLARES — identity included.
        The reflex of anyone coming from Fronius/SolarEdge/Huawei."""
        from .. import discovery
        host = str(payload.get('host', '') or '').strip()
        if not host:
            raise HTTPException(status_code=422, detail={"errors": ["host required"]})
        _e = discovery.lan_host_error(host, config.security.allow_nonlan_http_devices)
        if _e:
            raise HTTPException(status_code=422, detail={"errors": [_e]})
        try:
            port = int(payload.get('port', 502))
            unit = int(payload.get('unit_id', 1))
            timeout = min(5.0, max(0.5, float(payload.get('timeout', 2.0))))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail={"errors": ["port/unit_id/timeout must be numbers"]})
        _port_or_422(port)
        _refuse_if_listened("A SunSpec walk", "tcp", {"host": host, "port": port})
        from .. import preview
        from .previews import env_of
        why = preview.second_connection(env_of(ctx), host, port, payload.get("confirm_open") is True)
        if why:
            raise HTTPException(status_code=409, detail=why)
        return await asyncio.to_thread(discovery.sunspec_walk, host, port, unit, timeout)

    @r.post("/api/discover/mqtt/browse")
    async def discover_mqtt_browse(payload: Dict = Body(...)):
        """Collect the broker's speaking topics with payload previews so the
        user PICKS a topic instead of typing it blind. Retained messages give
        an instant tree; the listen window catches live publishers. Read-only."""
        from .. import discovery
        broker = str(payload.get('broker', '') or '').strip()
        if not broker:
            raise HTTPException(status_code=422, detail={"errors": ["broker required"]})
        _e = discovery.lan_host_error(broker, config.security.allow_nonlan_http_devices)
        if _e:
            raise HTTPException(status_code=422, detail={"errors": [_e]})
        try:
            port = int(payload.get('port', 1883))
            duration = float(payload.get('duration_s', 3.0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail={"errors": ["port/duration_s must be numbers"]})
        _port_or_422(port)
        return await asyncio.to_thread(
            discovery.mqtt_browse, broker, port,
            str(payload.get('username', '') or ''), str(payload.get('password', '') or ''),
            flag(payload, 'tls', False), str(payload.get('filter', '#') or '#'),
            duration, int(payload.get('max_topics', 500) or 500))

    @r.get("/api/fronius/discover")
    async def fronius_discover(host: str = Query(...), port: int = Query(80)):
        """Enumerate the devices behind a Fronius DataManager via its Solar API
        (HTTP JSON) — inverters + meters with their ids/models, plus live meter
        readings. Read-only; the Solar API tolerates concurrent clients (unlike
        the DataManager's single-client Modbus TCP)."""
        if not _FRONIUS_HOST_RE.match(host):
            raise HTTPException(status_code=400, detail="invalid host")
        if not (1 <= int(port) <= 65535):
            raise HTTPException(status_code=400, detail="invalid port")
        # SSRF egress guard (raises 400 if not LAN) — the same operator opt-out
        # the HTTP devices honour lets a lab reach a simulator on loopback
        if config.security.allow_nonlan_http_devices:
            from .. import discovery
            _e = discovery.lan_host_error(host, True)
            if _e:
                raise HTTPException(status_code=400, detail=_e)
            import socket
            _ip = sorted({i[4][0] for i in socket.getaddrinfo(host, None)})[0]
        else:
            _ip = _require_lan_host(host)

        def _fetch(path):
            import urllib.request
            from ..alerts import _NO_REDIRECT_OPENER
            # connect to the validated literal IP, pass Host header for vhosts
            url = f"http://{_ip}:{int(port)}{path}"
            req = urllib.request.Request(url, headers={"Host": host})
            # Refuse redirects: a malicious/compromised device could 302 us to
            # loopback/metadata, and urllib would follow it WITHOUT re-validating
            # (re-resolving off the pinned IP) — that would defeat the LAN guard.
            with _NO_REDIRECT_OPENER.open(req, timeout=8) as r_:   # noqa: S310
                return json.loads(r_.read().decode("utf-8", "replace"))

        try:
            info = await asyncio.to_thread(_fetch, "/solar_api/v1/GetActiveDeviceInfo.cgi?DeviceClass=System")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Solar API unreachable: {e}")
        data = (info.get("Body", {}) or {}).get("Data", {}) or {}

        meters = []
        try:
            md = await asyncio.to_thread(_fetch, "/solar_api/v1/GetMeterRealtimeData.cgi?Scope=System")
            mdata = (md.get("Body", {}) or {}).get("Data", {}) or {}
        except Exception:  # noqa: BLE001
            mdata = {}
        # The DataManager's GetActiveDeviceInfo flakily omits the meter; the
        # realtime endpoint is more reliable — union both so we never miss it.
        meter_ids = list(dict.fromkeys(list((data.get("Meter", {}) or {}).keys())
                                       + list(mdata.keys())))
        for mid in meter_ids:
            m = (data.get("Meter", {}) or {}).get(mid, {}) or {}
            det = (mdata.get(mid, {}) or {}).get("Details", {}) or {}
            live = mdata.get(mid, {}) or {}
            meters.append({
                "solar_api_id": mid,
                "model": det.get("Model"),
                "serial": det.get("Serial") or m.get("Serial"),
                "manufacturer": det.get("Manufacturer"),
                "location": live.get("Meter_Location_Current"),
                "power_w": live.get("PowerReal_P_Sum"),
                "freq_hz": live.get("Frequency_Phase_Average"),
                # Fronius exposes the meter over Modbus at unit 240 (SunSpec) by
                # default — the Solar-API id is NOT the Modbus unit id.
                "modbus_unit_hint": 240,
            })
        inverters = [{"solar_api_id": iid, "dt": iv.get("DT"), "serial": iv.get("Serial")}
                     for iid, iv in (data.get("Inverter", {}) or {}).items()]
        storages = list((data.get("Storage", {}) or {}).keys())
        return {"host": host, "inverters": inverters, "meters": meters,
                "storage_ids": storages, "modbus_note":
                "The DataManager Modbus TCP is single-client and slow — poll it "
                "with small blocks and only one reader. The Solar-API id differs "
                "from the Modbus unit id (meter is unit 240)."}

    return r
