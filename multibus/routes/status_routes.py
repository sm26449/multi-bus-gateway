"""System status + resource footprint + container health probe.

Moved verbatim from create_api(). Publishers are read from ctx at request time
(rebindable via /api/config/apply).
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["status"])
    config, registry = ctx.config, ctx.registry
    modbus_client, ws_manager, last_update = ctx.modbus_client, ctx.ws_manager, ctx.last_update

    @r.get("/api/status")
    async def get_status():
        """Get system status. Top-level modbus = device #1 (back-compat);
        ``devices`` lists every configured southbound device (Tier 2)."""
        from .. import __version__
        mqtt_publisher, influxdb_publisher = ctx.mqtt_publisher, ctx.influxdb_publisher
        out = {
            "version": __version__,
            "modbus": modbus_client.get_stats() if modbus_client else {},
            "mqtt": mqtt_publisher.get_stats() if mqtt_publisher else {},
            "influxdb": influxdb_publisher.get_stats() if influxdb_publisher else {},
            "websocket_clients": len(ws_manager.active_connections),
            "last_update": last_update['timestamp'],
        }
        if registry:
            out["devices"] = []
            for dev_cfg, client in registry:
                entry = dev_cfg.summary()
                if client:
                    stats = client.get_stats()
                    entry.update({
                        "connected": stats.get("connected"),
                        "successful_reads": stats.get("successful_reads"),
                        "failed_reads": stats.get("failed_reads"),
                        # per-device poll rate — its absence made the Status page
                        # show 0.00/s per device while the pipeline header said
                        # 4.2/s (two contradictory numbers on one screen).
                        # None for push-driven sources (MQTT-in has no rate).
                        "poll_rate": stats.get("poll_rate"),
                        "error_counts": stats.get("error_counts"),
                        "staleness_age_s": stats.get("staleness_age_s"),
                        "last_latency_ms": stats.get("last_latency_ms"),
                        "data_health": client.data_health().get("status"),
                    })
                else:
                    entry.update({"connected": False,
                                  "data_health": "idle",
                                  "note": "transport not available yet (rtu = Tier 3)"})
                out["devices"].append(entry)
        return out

    @r.get("/api/status/resources")
    async def get_status_resources():
        """Process resource footprint for the Status page (CPU%, RSS, threads,
        open FDs, established TCP connections, uptime). Read from /proc/self."""
        from ..api import _read_self_resources
        return _read_self_resources()

    @r.get("/health")
    async def health():
        """Health for the container probe + external monitors.

        Body ``status`` = worst of (virtual-meter health, Modbus acquisition
        health) and includes a ``modbus`` block (freshness of the upstream data).
        The HTTP CODE is deliberately 503 ONLY when an enabled virtual meter is
        genuinely ``down`` (a real fault a restart may clear). A stale/dead
        Modbus source degrades the body ``status`` but returns HTTP 200 —
        restarting the container cannot fix an unreachable meter, and we must not
        restart-loop on an upstream-device problem (the vmeter freshness watchdog
        already fail-safes the consumers)."""
        rank = {"ok": 0, "degraded": 1, "down": 2}
        mgr = getattr(ctx.app.state, "vmeter_manager", None)
        vh = mgr.health() if mgr else {"status": "ok", "enabled_meters": 0, "meters": []}
        threshold = getattr(config.modbus, "stale_after_s", 30)
        mh = modbus_client.data_health(threshold) if modbus_client else {"status": "ok"}
        body = dict(vh)
        body["modbus"] = mh
        body["status"] = max([vh.get("status", "ok"), mh.get("status", "ok")],
                             key=lambda s: rank.get(s, 0))
        vmeter_down = vh.get("status") == "down"
        return JSONResponse(content=body, status_code=503 if vmeter_down else 200)

    return r
