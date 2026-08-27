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

from ..pq_recorder import (WAVEFORM_CHANNELS, device_base_url,
                           fetch_waveform_live, template_supports_pq)


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["power-quality"])
    config = ctx.config

    def _manager():
        return getattr(ctx.app.state, "pq_manager", None)

    def _supports(dev) -> bool:
        reg = getattr(ctx, "template_registry", None)
        tpl = reg.get(dev.template) if reg and dev.template else None
        return template_supports_pq(tpl, dev.template)

    def _pq_bucket(dev) -> str:
        """PQ history bucket: the device's pq_recorder.bucket override, or
        its normal telemetry bucket."""
        return ((dev.pq_recorder or {}).get("bucket") or "").strip() \
            or dev.influxdb_bucket

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
                "supported": _supports(d),
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
            influx.query_pq_events, _pq_bucket(dev),
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
            _pq_bucket(dev), dev.influxdb_device_tag or dev.id)
        if "error" in res:
            raise HTTPException(status_code=400, detail=res["error"])
        if res.get("series"):
            return res

        # Read-through fallback: nothing archived (e.g. a ring event that
        # predates the recorder being enabled) but the meter may still hold
        # the capture window in its own few-day retention — fetch it live
        # and archive it so the next view is served from InfluxDB.
        base = device_base_url(dev)
        if not base or not _supports(dev):
            return res

        def _live_and_archive():
            from datetime import datetime, timezone
            data = fetch_waveform_live(base, event / 1000.0, channel)
            if not data:
                return {"series": []}
            try:
                from influxdb_client import Point, WritePrecision
                tag = dev.influxdb_device_tag or dev.id
                for ts, value in data:
                    p = (Point("pq_waveforms")
                         .tag("device", tag)
                         .tag("event", str(int(event)))
                         .tag("channel", channel)
                         .field("value", float(value))
                         .time(int(ts * 1000), WritePrecision.MS))
                    influx.write_point(p, bucket=_pq_bucket(dev))
            except Exception:  # noqa: BLE001 — archive is best-effort here
                pass
            iso = (lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc)
                   .isoformat().replace("+00:00", "Z"))
            return {"series": [{"t": iso(ts), "v": v} for ts, v in data],
                    "source": "device"}

        try:
            return await asyncio.to_thread(_live_and_archive)
        except Exception:  # noqa: BLE001 — meter dark/unreachable → empty
            return res

    @r.post("/api/pq/config")
    async def pq_config(payload: Dict = Body(...),
                        device: str = Query(default="")):
        """Set a device's PQ recorder config and (re)start its poller.
        Body: {enabled, poll_s?, archive_waveforms?, base_url?}. Non-GET, so
        the auth middleware already requires an operator/admin session."""
        dev = _device(device)
        if payload.get("enabled") and not _supports(dev):
            raise HTTPException(
                status_code=400,
                detail=f"template {dev.template!r} has no Jasic PQ recorder")
        cfg = {k: payload[k] for k in
               ("enabled", "poll_s", "archive_waveforms", "base_url", "bucket")
               if k in payload}
        try:
            config.set_pq_recorder(dev.id, cfg)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # set_pq_recorder rebuilds config.devices with NEW DeviceConfig
        # objects — swap the registry's instance too (keeping the running
        # client), or /api/devices keeps serving the stale block.
        new_dev = next((d for d in config.devices if d.id == dev.id), None)
        reg = getattr(ctx, "registry", None)
        if reg is not None and new_dev is not None:
            try:
                reg.replace(dev.id, new_dev)
            except Exception:  # noqa: BLE001 — registry sync is best-effort
                pass
        mgr = _manager()
        if mgr is not None:
            mgr.apply_device(dev.id)
        ctx.event_log.add("info", "pq",
                          f"{dev.id}: PQ recorder config set "
                          f"(enabled={bool(cfg.get('enabled'))})")
        return {"ok": True, "device": dev.id, "pq_recorder": cfg}

    return r
