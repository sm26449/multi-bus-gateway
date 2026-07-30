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
"""System status + resource footprint + container health probe.

Moved verbatim from create_api(). Publishers are read from ctx at request time
(rebindable via /api/config/apply).
"""
from __future__ import annotations

from typing import Dict, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["status"])
    config, registry = ctx.config, ctx.registry
    modbus_client, ws_manager, last_update = ctx.modbus_client, ctx.ws_manager, ctx.last_update

    # Connection-uptime tracking: device id -> (last health, monotonic since).
    # Sampled on each /api/status call (the page polls every few seconds) and
    # measured on the MONOTONIC clock, so an NTP step cannot fake or wipe it.
    # In-memory by design: a gateway restart legitimately resets "up for".
    _health_since: Dict[str, tuple] = {}

    def _up_since_s(dev_id: str, health: str) -> Optional[int]:
        import time as _t
        now = _t.monotonic()
        prev = _health_since.get(dev_id)
        if prev is None or prev[0] != health:
            _health_since[dev_id] = (health, now)
            return 0
        return int(now - prev[1])

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
                # summary() carries http_url raw; a status view never needs the
                # editable credential URL, so redact it for every role (userinfo
                # or a ?token= would otherwise leak to a viewer here).
                if entry.get('http_url'):
                    from ..redact import redact_url
                    entry['http_url'] = redact_url(entry['http_url'])
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
                    entry["up_since_s"] = _up_since_s(
                        dev_cfg.id, str(entry.get("data_health") or ""))
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
