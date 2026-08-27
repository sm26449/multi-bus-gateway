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
"""Power-quality recorder routes — archived PQ events, waveforms, recorder
status/config. Acquisition itself lives in multibus/pq_recorder.py."""
from __future__ import annotations

import asyncio
from typing import Dict

from fastapi import APIRouter, Body, HTTPException, Query

from ..pq_recorder import WAVEFORM_CHANNELS, supports_pq_recorder


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["power-quality"])
    config = ctx.config

    def _manager():
        return getattr(ctx.app.state, "pq_manager", None)

    def _device(device: str):
        did = device or config.primary_device.id
        for d in config.devices:
            if d.id == did:
                return d
        raise HTTPException(status_code=404, detail=f"device {did!r} not found")

    @r.get("/api/pq/status")
    async def pq_status():
        """Recorder support/enabled/health per device."""
        mgr = _manager()
        if mgr is None:
            return {"devices": [{
                "device": d.id,
                "supported": supports_pq_recorder(d.template),
                "enabled": bool((d.pq_recorder or {}).get("enabled")),
                "running": False,
            } for d in config.devices]}
        return {"devices": mgr.status()}

    @r.get("/api/pq/events")
    async def pq_events(device: str = Query(default=""),
                        start: str = Query("-30d"),
                        limit: int = Query(200, ge=1, le=2000)):
        """Archived PQ events (InfluxDB), newest first — the full retained
        history, not just the meter's 32-entry ring."""
        dev = _device(device)
        influx = ctx.influxdb_publisher          # request-time (rebindable)
        if influx is None or not influx.config.enabled:
            raise HTTPException(status_code=503, detail="InfluxDB not enabled")
        res = await asyncio.to_thread(
            influx.query_pq_events, dev.influxdb_bucket,
            dev.influxdb_device_tag or dev.id, start, limit)
        if "error" in res:
            raise HTTPException(status_code=400, detail=res["error"])
        return res

    @r.get("/api/pq/waveform")
    async def pq_waveform(event: int = Query(..., description="event start, ms epoch"),
                          channel: str = Query(...),
                          device: str = Query(default="")):
        """One archived RMS trace of one event (channel e.g. UL2, IL1,
        UL1-L2)."""
        dev = _device(device)
        if channel not in WAVEFORM_CHANNELS:
            raise HTTPException(status_code=400,
                                detail=f"channel must be one of "
                                       f"{sorted(WAVEFORM_CHANNELS)}")
        influx = ctx.influxdb_publisher
        if influx is None or not influx.config.enabled:
            raise HTTPException(status_code=503, detail="InfluxDB not enabled")
        res = await asyncio.to_thread(
            influx.query_pq_waveform, event, channel,
            dev.influxdb_bucket, dev.influxdb_device_tag or dev.id)
        if "error" in res:
            raise HTTPException(status_code=400, detail=res["error"])
        return res

    @r.post("/api/pq/config")
    async def pq_config(payload: Dict = Body(...),
                        device: str = Query(default="")):
        """Set a device's PQ recorder config and (re)start its poller.
        Body: {enabled, poll_s?, archive_waveforms?, base_url?}. Non-GET, so
        the auth middleware already requires an operator/admin session."""
        dev = _device(device)
        if payload.get("enabled") and not supports_pq_recorder(dev.template):
            raise HTTPException(
                status_code=400,
                detail=f"template {dev.template!r} has no Jasic PQ recorder")
        cfg = {k: payload[k] for k in
               ("enabled", "poll_s", "archive_waveforms", "base_url")
               if k in payload}
        try:
            config.set_pq_recorder(dev.id, cfg)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        mgr = _manager()
        if mgr is not None:
            mgr.apply_device(dev.id)
        ctx.event_log.add("info", "pq",
                          f"{dev.id}: PQ recorder config set "
                          f"(enabled={bool(cfg.get('enabled'))})")
        return {"ok": True, "device": dev.id, "pq_recorder": cfg}

    return r
