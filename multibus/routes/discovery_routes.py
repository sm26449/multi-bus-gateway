"""Device discovery — Modbus TCP scan / unit sweep + Fronius Solar-API probe.

Moved verbatim from create_api(). The Fronius fetch keeps its SSRF egress
guard: LAN-only resolution pinned to a literal IP, redirects refused.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Dict

from fastapi import APIRouter, Body, HTTPException, Query

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


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["discovery"])
    config = ctx.config

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
        try:
            hosts = discovery.hosts_from_cidr(cidr, allow_nonlan=config.security.allow_nonlan_http_devices)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        results = await asyncio.to_thread(discovery.scan_tcp, hosts, port, unit, timeout)
        return {"scanned": len(hosts), "results": results}

    @r.post("/api/discover/modbus/units")
    async def discover_modbus_units(payload: Dict = Body(...)):
        """Sweep unit/slave ids on ONE endpoint (TCP host or RTU serial line)."""
        from .. import discovery
        proto = str(payload.get('protocol', 'tcp')).lower()
        try:
            u0 = int(payload.get('unit_start', 1))
            u1 = int(payload.get('unit_end', 32))
            timeout = min(3.0, max(0.1, float(payload.get('timeout', 0.5))))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail={"errors": ["unit range / timeout must be numbers"]})
        if proto == 'rtu':
            sp = str(payload.get('serial_port', '') or '').strip()
            if not sp:
                raise HTTPException(status_code=422, detail={"errors": ["serial_port required for RTU"]})
            units = await asyncio.to_thread(
                discovery.sweep_units_rtu, sp, u0, u1, timeout,
                int(payload.get('baudrate', 9600)), str(payload.get('parity', 'N')),
                int(payload.get('stopbits', 1)), int(payload.get('bytesize', 8)))
        else:
            host = str(payload.get('host', '') or '').strip()
            if not host:
                raise HTTPException(status_code=422, detail={"errors": ["host required for TCP"]})
            # LAN-guard the single host — resolves hostnames (meter.local) too, not
            # just literal IPs, then validates the resolved addresses are private.
            _e = discovery.lan_host_error(host, config.security.allow_nonlan_http_devices)
            if _e:
                raise HTTPException(status_code=422, detail={"errors": [_e]})
            units = await asyncio.to_thread(discovery.sweep_units_tcp, host,
                                            int(payload.get('port', 502)), u0, u1, timeout)
        return {"protocol": proto, "units": units}

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
        return await asyncio.to_thread(
            discovery.mqtt_browse, broker, port,
            str(payload.get('username', '') or ''), str(payload.get('password', '') or ''),
            bool(payload.get('tls')), str(payload.get('filter', '#') or '#'),
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
        _ip = _require_lan_host(host)   # SSRF egress guard (raises 400 if not LAN)

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
