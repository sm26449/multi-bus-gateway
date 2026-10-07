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

from ..threshold_engine import gate_open


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["status"])
    config, registry = ctx.config, ctx.registry

    def _display_of(dev) -> Dict:
        from ..display import display_of
        return display_of(getattr(ctx, "template_registry", None), dev)
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
            "config": ctx.config.config_status() if hasattr(ctx.config, "config_status") else {"healthy": True},
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
                if getattr(dev_cfg, 'endpoint_id', ''):
                    # a unit is read through its sources — the status table
                    # names their protocols and judges latency against their
                    # own timeouts, not the fallback connection's
                    entry["read_via"] = [{
                        "id": s.id, "protocol": str(s.protocol or 'tcp').lower(),
                        "timeout_s": ((s.http or {}).get('timeout') if s.protocol == 'http'
                                      else getattr(s.connection, 'timeout', None)),
                    } for s in (dev_cfg.sources or [])]
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
        # Endpoints are entities in their own right, not just a grouping of rows:
        # a status page that lists four healthy units while the endpoint sits at
        # 2/4 producing tells the operator nothing about the ENDPOINT.
        endpoints = getattr(config, "endpoints", None) or []
        if endpoints and registry:
            from ..endpoint_aggregator import compute_endpoint_aggregates
            out["endpoints"] = []
            for p in endpoints:
                pid = p.get("id")
                if not pid:
                    continue
                try:
                    agg = compute_endpoint_aggregates(config, registry, pid)
                except Exception:  # noqa: BLE001 — status must never 500
                    agg = {}
                out["endpoints"].append({
                    "id": pid, "name": p.get("name") or pid,
                    "enabled": bool(p.get("enabled", True)),
                    "status": agg.get("status", ""),
                    "units_online": agg.get("units_online", 0),
                    "units_total": agg.get("units_total", 0),
                    "power_active_total": agg.get("power_active_total"),
                    "aggregates_enabled": bool(p.get("aggregates", True)),
                })
        return out

    # ── Fleet overview ──────────────────────────────────────────────────
    # One call serves the whole fleet grid: at 100 devices the UI must not
    # fan out 100 /api/values fetches to paint an overview.

    def _threshold_band(value, t, value_of=None) -> str:
        """Band for EXPLICIT thresholds only. The UI's implicit grid
        templates are display sugar; a fleet alarm must come from a limit
        someone actually configured."""
        if not isinstance(t, dict) or not t.get("enabled"):
            return ""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return ""
        if not gate_open(t, value_of or (lambda _n: None)):
            return ""
        def _f(key):
            x = t.get(key)
            if x is None or x == "":
                return None
            try:
                return float(x)
            except (TypeError, ValueError):
                return None
        dl, wl = _f("dangerLow"), _f("warningLow")
        wh, dh = _f("warningHigh"), _f("dangerHigh")
        if (dl is not None and v < dl) or (dh is not None and v > dh):
            return "danger"
        if (wl is not None and v < wl) or (wh is not None and v > wh):
            return "warning"
        return ""

    @r.get("/api/fleet")
    async def get_fleet():
        """Fleet overview: one compact row per device — health, staleness,
        explicit-threshold alarm counts, and up to three hero metrics (the
        device's first dashboard registers by dashboard_order)."""
        devices_out = []
        for dev_cfg, client in (registry or []):
            try:
                # a multi-source unit keeps every register in per-source
                # files — its root file is empty; the union is what it reads.
                # (Plain devices carry a synthetic 'default' source with no
                # file, and unit_registers falls back to the root file.)
                if any(config.source_registers_path(dev_cfg.id, s.id).exists()
                       for s in (dev_cfg.sources or [])):
                    regs = config.unit_registers(dev_cfg)
                else:
                    regs, _groups = config.load_device_registers(dev_cfg)
            except Exception:  # noqa: BLE001 — overview must never 500
                regs = []
            store = (registry.store_for(dev_cfg.id) or {}) if registry else {}
            danger = warning = 0
            from ..display import active_alarms, alarm_fields
            _disp = _display_of(dev_cfg)
            _declared_alarms = alarm_fields(_disp)
            _vals = {e.get("name"): e.get("value") for e in list(store.values())}
            for x in regs:
                item = store.get(x.address)
                # a field the template declares as an alarm is counted below,
                # once — not again by a threshold on it
                if item is None or getattr(x, "name", None) in _declared_alarms:
                    continue
                band = _threshold_band(item.get("value"), x.thresholds, _vals.get)
                if band == "danger":
                    danger += 1
                elif band == "warning":
                    warning += 1
            # plus what the device itself declares as wrong (a BMS's alarm,
            # protection and failure counts) — no threshold needed
            _decl = active_alarms(_disp, list(store.values()))
            danger += _decl["danger"]
            warning += _decl["warning"]
            hero = []
            declared = (_disp.get("hero") or [])
            if declared:
                # the template says what a fleet row of this kind shows —
                # calculated fields included (a pack's power is one)
                by_name = {e.get("name"): (addr, e) for addr, e in list(store.items())}
                for n in declared[:3]:
                    addr, e = by_name.get(n, (None, {}))
                    hero.append({"address": addr, "name": n,
                                 "label": e.get("label") or n, "unit": e.get("unit") or "",
                                 "value": e.get("value")})
            else:
                dash = sorted(
                    (x for x in regs if x.ui_show_on_dashboard),
                    key=lambda x: (x.ui_config or {}).get("dashboard_order", 999))
                for x in dash[:3]:
                    item = store.get(x.address) or {}
                    hero.append({
                        "address": x.address, "name": x.name,
                        "label": getattr(x, "label", "") or x.name,
                        "unit": getattr(x, "unit", "") or "",
                        "value": item.get("value"),
                    })
            health, stale_s = "idle", None
            if client:
                health = client.data_health().get("status")
                stale_s = client.get_stats().get("staleness_age_s")
            devices_out.append({
                "id": dev_cfg.id, "name": dev_cfg.name,
                "enabled": dev_cfg.enabled, "protocol": dev_cfg.protocol,
                "endpoint_id": getattr(dev_cfg, "endpoint_id", "") or "",
                "health": health, "staleness_age_s": stale_s,
                "alarms": {"danger": danger, "warning": warning},
                "hero": hero,
            })
        # endpoints carry their live aggregates too: the fleet header's site
        # cards (PV power now, units online) come from the same computation
        # the status page and the publisher already share
        endpoints = []
        for p in (getattr(config, "endpoints", None) or []):
            pid = p.get("id")
            if not pid:
                continue
            agg = {}
            if registry:
                try:
                    from ..endpoint_aggregator import compute_endpoint_aggregates
                    agg = compute_endpoint_aggregates(config, registry, pid)
                except Exception:  # noqa: BLE001 — overview must never 500
                    agg = {}
            # the installation card's metric: the template's headline when
            # the units declare one (a battery bank's power, not "PV power")
            card = None
            udevs = config.endpoint_devices(pid) if hasattr(config, "endpoint_devices") else []
            head = (_display_of(udevs[0]).get("headline") or []) if udevs else []
            # the first headline total that exists right now
            h0 = next((h for h in head if h.get("field") in agg), None)
            if h0:
                f = h0["field"]
                src_unit = ""
                for d in udevs:
                    for e in list(((registry.store_for(d.id) if registry else None) or {}).values()):
                        if f in (e.get("aggregates") or {}):
                            src_unit = e.get("unit") or ""
                            break
                    if src_unit:
                        break
                card = {"field": f, "label": h0.get("label") or f,
                        "hint": h0.get("hint", ""), "unit": src_unit, "value": agg[f]}
            endpoints.append({
                "id": pid, "name": p.get("name") or pid,
                "enabled": bool(p.get("enabled", True)),
                "status": agg.get("status", ""),
                "units_online": agg.get("units_online", 0),
                "units_total": agg.get("units_total", 0),
                "power_active_total": agg.get("power_active_total"),
                "card": card,
            })
        return {"devices": devices_out, "endpoints": endpoints}

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
