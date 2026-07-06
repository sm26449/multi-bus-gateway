"""Live values + HTTP/JSON meter feeds + InfluxDB history read-back.

Moved verbatim from create_api(). ``last_update`` is the shared mutable dict
the poller callback stamps; ``influxdb_publisher`` is read from ctx at request
time (rebindable via /api/config/apply).
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException, Query

from ._shared import device_influx


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["values"])
    config, registry = ctx.config, ctx.registry
    current_values, last_update = ctx.current_values, ctx.last_update

    @r.get("/api/values")
    async def get_current_values(device: str = Query(default="")):
        """Get all current values. ``device`` selects a non-primary device's
        live value store (Tier 2); omitted/primary = device #1 (back-compat)."""
        values = current_values
        if device and device != config.primary_device.id:
            values = registry.store_for(device) or {}
        return {
            # snapshot: returning the live dict by reference lets a poller mutate
            # it mid-serialization ("dictionary changed size during iteration")
            "values": dict(values),
            "device": device or config.primary_device.id,
            "timestamp": last_update['timestamp'],
        }

    @r.get("/api/values/{address}")
    async def get_value(address: int):
        """Get current value for a specific register."""
        if address in current_values:
            return current_values[address]
        raise HTTPException(status_code=404, detail=f"Register {address} not found")

    # ---- HTTP/JSON output sink (Solar-API style) -------------------------------
    # A device can be exposed as a read-only JSON feed at /api/meters/<id>, keyed
    # by register NAME (the same canonical names used on MQTT), so any HTTP client
    # can pull live values without MQTT/InfluxDB. Opt-in per device; the app-level
    # IP allowlist / auth middleware already guards it.
    def _meter_payload(dev_cfg) -> Dict:
        store = registry.store_for(dev_cfg.id) or {}
        values: Dict[str, Dict] = {}
        newest = None
        for address, item in list(store.items()):     # snapshot vs concurrent poller writes
            name = item.get('name') or f"addr_{address}"
            ts = item.get('timestamp')
            values[name] = {
                'value': item.get('value'),
                'unit': item.get('unit', ''),
                'label': item.get('label', ''),
                'ts': ts,
            }
            if ts and (newest is None or ts > newest):
                newest = ts
        # Stale if the freshest value is older than the device's stale bound
        # (default 30s). No values yet → stale.
        stale = True
        if newest:
            try:
                age = (datetime.now() - datetime.fromisoformat(newest)).total_seconds()
                bound = max(1, int(getattr(dev_cfg.connection, 'stale_after_s', 30) or 30))
                stale = age > bound
            except (ValueError, TypeError):
                stale = False
        return {
            'device': dev_cfg.id,
            'name': dev_cfg.name,
            'ts': newest or last_update['timestamp'],
            'stale': stale,
            'values': values,
        }

    @r.get("/api/meters")
    async def list_meters():
        """List the devices exposed as a JSON feed (http_output enabled)."""
        out = []
        for dev_cfg in config.devices:
            if not dev_cfg.http_output_enabled:
                continue
            p = _meter_payload(dev_cfg)
            out.append({
                'device': dev_cfg.id, 'name': dev_cfg.name,
                'path': f"/api/meters/{dev_cfg.id}",
                'stale': p['stale'], 'ts': p['ts'], 'count': len(p['values']),
            })
        return {'meters': out}

    @r.get("/api/meters/{device_id}")
    async def get_meter(device_id: str):
        """Live values for one device as JSON (Solar-API style), keyed by name."""
        dev_cfg = config.get_device(device_id)
        if dev_cfg is None or not dev_cfg.http_output_enabled:
            # Same 404 whether the device is unknown or simply not exposed — don't
            # leak which device ids exist to an unauthenticated puller.
            raise HTTPException(status_code=404,
                                detail="no JSON feed for this device")
        return _meter_payload(dev_cfg)

    @r.get("/api/history/registers")
    async def history_registers(device: str = Query(default="")):
        """Registers with InfluxDB enabled — for the history view's picker.
        ``device`` selects a non-primary device's register set (Tier 2)."""
        dev = config.get_device(device) if device else None
        if dev is not None and not dev.primary:
            regs_src, _g = config.load_device_registers(dev)
        else:
            regs_src = getattr(config, "selected_registers", [])
        regs = [{"name": x.name, "label": getattr(x, "label", "") or x.name,
                 "unit": getattr(x, "unit", "")}
                for x in regs_src
                if getattr(x, "influxdb_enabled", False)]
        # Calculated measurements are written to InfluxDB too (tag name=<name>,
        # field value) so their history is queryable by name — list them in the
        # picker as well, grouped as 'calculated'.
        calc_did = dev.id if dev is not None else config.primary_device.id
        for c in config.load_calculated(calc_did):
            if c.get('name'):
                regs.append({"name": c['name'], "label": c.get('label') or c['name'],
                             "unit": c.get('unit', ''), "calculated": True})
        influxdb_publisher = ctx.influxdb_publisher
        influx_on = bool(influxdb_publisher and getattr(influxdb_publisher.config, "enabled", False))
        return {"registers": regs, "influx_enabled": influx_on}

    @r.get("/api/history")
    async def get_history(name: str = Query(...),
                          start: str = Query("-6h"), stop: str = Query("now()"),
                          every: str = Query("1m"), fn: str = Query("mean"),
                          measurement: Optional[str] = Query(None),
                          device: str = Query(default="")):
        """Aggregated history for a register, read back from InfluxDB.
        fn='all' returns mean/min/max series (for a band). ``device`` reads that
        device's bucket + tag (Tier 2)."""
        influxdb_publisher = ctx.influxdb_publisher          # request-time (rebindable)
        if influxdb_publisher is None or not influxdb_publisher.config.enabled:
            raise HTTPException(status_code=503, detail="InfluxDB not enabled")
        bucket, device_tag = device_influx(config, device)
        # off the event loop: a slow/hung InfluxDB must not stall the whole API
        res = await asyncio.to_thread(influxdb_publisher.query_history,
                                      name, start, stop, every, fn, measurement,
                                      bucket, device_tag)
        if "error" in res:
            err = res["error"]
            code = 503 if ("disabled" in err or "unavailable" in err) else 400
            raise HTTPException(status_code=code, detail=err)
        return res

    return r
