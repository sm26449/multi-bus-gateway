"""Prometheus metrics — GET /metrics, text exposition format 0.0.4.

Hand-rolled (the format is trivial) so no new dependency enters the image.
Everything is read from the live singletons at scrape time — no background
collector, no state, no hot-path involvement. Reachable without a login
session (a scraper can't log in), like /health; the IP allowlist still
applies, and nothing here exposes configuration or secrets — counters and
health only.
"""
from __future__ import annotations

import re
import time

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

_LABEL_BAD = re.compile(r'["\\\n]')


def _esc(v) -> str:
    """Escape a label value per the exposition format."""
    return _LABEL_BAD.sub(lambda m: {'"': r'\"', '\\': r'\\', '\n': r'\n'}[m.group()], str(v))


class _Fmt:
    """Tiny exposition-format builder: HELP/TYPE once per metric, then samples."""

    def __init__(self):
        self.lines: list[str] = []
        self._seen: set[str] = set()

    def add(self, name: str, help_: str, mtype: str, value, labels: dict | None = None):
        if value is None:
            return
        if name not in self._seen:
            self._seen.add(name)
            self.lines.append(f"# HELP {name} {help_}")
            self.lines.append(f"# TYPE {name} {mtype}")
        lab = ""
        if labels:
            lab = "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items()) + "}"
        try:
            num = float(value)
        except (TypeError, ValueError):
            return
        # ints render without the trailing .0 — cosmetic but conventional
        out = str(int(num)) if num == int(num) else repr(num)
        self.lines.append(f"{name}{lab} {out}")

    def render(self) -> str:
        return "\n".join(self.lines) + "\n"


_HEALTH_NUM = {"ok": 1, "degraded": 0.5, "stale": 0.5, "down": 0, "idle": 0}


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["metrics"])
    registry = ctx.registry

    @r.get("/metrics", response_class=PlainTextResponse)
    async def metrics():
        f = _Fmt()
        now = time.time()

        # ── devices (southbound sources) ────────────────────────────────────
        for dev_cfg, client in registry:
            lbl = {"device": dev_cfg.id}
            if client is None or not hasattr(client, "get_stats"):
                f.add("gateway_device_up", "Source device connected (1/0)", "gauge", 0, lbl)
                continue
            try:
                st = client.get_stats()
            except Exception:  # noqa: BLE001 — one sick client must not kill the scrape
                continue
            f.add("gateway_device_up", "Source device connected (1/0)", "gauge",
                  1 if st.get("connected") else 0, lbl)
            f.add("gateway_device_poll_rate", "Configured polls per second", "gauge",
                  st.get("poll_rate"), lbl)
            f.add("gateway_device_reads_total", "Successful reads since start", "counter",
                  st.get("successful_reads"), {**lbl, "result": "ok"})
            f.add("gateway_device_reads_total", "Failed reads since start", "counter",
                  st.get("failed_reads"), {**lbl, "result": "error"})
            for kind, n in (st.get("error_counts") or {}).items():
                f.add("gateway_device_errors_total",
                      "Failed attempts by kind (timeout/exception_N/connection)",
                      "counter", n, {**lbl, "kind": kind})
            f.add("gateway_device_read_latency_ms", "Last read latency (ms)", "gauge",
                  st.get("last_latency_ms"), lbl)
            f.add("gateway_device_staleness_seconds", "Age of the last successful read", "gauge",
                  st.get("staleness_age_s"), lbl)
            try:
                h = client.data_health().get("status")
                f.add("gateway_device_health", "Data health (1 ok, 0.5 degraded/stale, 0 down)",
                      "gauge", _HEALTH_NUM.get(h), lbl)
            except Exception:  # noqa: BLE001
                pass

        # ── sinks (read at scrape time — /api/config/apply may rebind them) ──
        mqtt = ctx.mqtt_publisher
        if mqtt is not None:
            try:
                st = mqtt.get_stats()
                f.add("gateway_mqtt_connected", "MQTT broker link up (1/0)", "gauge",
                      1 if st.get("connected") else 0)
                f.add("gateway_mqtt_published_total", "MQTT messages published", "counter",
                      st.get("messages_published"))
            except Exception:  # noqa: BLE001
                pass
        influx = ctx.influxdb_publisher
        if influx is not None:
            try:
                st = influx.get_stats()
                f.add("gateway_influx_connected", "InfluxDB link up (1/0)", "gauge",
                      1 if st.get("connected") else 0)
                f.add("gateway_influx_written_total", "InfluxDB points written", "counter",
                      st.get("writes_total"))
                f.add("gateway_influx_buffer_points", "Store-and-forward buffer size", "gauge",
                      st.get("buffer_points"))
                f.add("gateway_influx_dropped_total", "Points dropped (buffer overflow)", "counter",
                      st.get("dropped_total"))
            except Exception:  # noqa: BLE001
                pass

        # ── virtual meters (northbound Modbus servers) ───────────────────────
        mgr = getattr(ctx.app.state, "vmeter_manager", None)
        if mgr is not None:
            try:
                for inst in mgr.overview():
                    lbl = {"meter": inst.get("template") or ""}
                    f.add("gateway_vmeter_up", "Virtual meter serving (1/0)", "gauge",
                          1 if inst.get("running") else 0, lbl)
                    f.add("gateway_vmeter_requests_total", "Modbus requests served", "counter",
                          inst.get("requests"), lbl)
                    f.add("gateway_vmeter_request_rate", "Requests per second (10s avg)", "gauge",
                          inst.get("req_rate"), lbl)
                    f.add("gateway_vmeter_errors_total", "Refused/illegal reads", "counter",
                          inst.get("errors"), lbl)
                    f.add("gateway_vmeter_connections", "Active consumer connections", "gauge",
                          inst.get("conn_count"), lbl)
                    for state, n in (inst.get("quality") or {}).items():
                        f.add("gateway_vmeter_quality", "Per-register quality (composite meters)",
                              "gauge", n, {**lbl, "state": state})
            except Exception:  # noqa: BLE001
                pass

        f.add("gateway_scrape_timestamp_seconds", "Unix time of this scrape", "gauge", now)
        return PlainTextResponse(f.render(),
                                 media_type="text/plain; version=0.0.4; charset=utf-8")

    return r
