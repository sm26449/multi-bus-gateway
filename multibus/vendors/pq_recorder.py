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
"""Power-quality event recorder acquisition for Janitza (Jasic web firmware).

Janitza UMG-series analyzers (604/605/508/511/512) keep an on-device PQ event
recorder — voltage dips/swells/outages, rapid voltage changes, frequency
excursions — but only a small ring (32 entries on the UMG512): on an agitated
grid day that covers just a few hours, so by the time an operator looks, the
interesting events are gone. The device's Modbus map exposes only lifetime
COUNTERS (e.g. UMG512 addresses 6634/6636/6638); the event records themselves
are served by the Jasic web firmware over plain unauthenticated HTTP:

- ``GET /lib/events/getevt.html`` → ``{"events": [[start, end, bound, max,
  min, avg, reason_lo, reason_hi], ...]}`` — device clock is UTC epoch.
- ``GET /json.do?_EVT_COUNT,_FLAG_COUNT,_TRANS_COUNT,`` → named counters.
- ``GET /lib/events/hww.html`` → half-wave-RMS recording index
  (``[[start, end, trigger, ...], ...]``, ~50 s windows, retained ~days).
- ``GET /lib/events/mk_hww.html?_hww_nr=<start>&_val_nr=<ch>`` →
  ``{"data": [[ts, value], ...]}`` — one channel's RMS trace, 10 ms steps.

This module polls those endpoints per enabled device and persists everything
through the gateway's existing sinks:

- InfluxDB measurement ``pq_events``  — one point per event cause+channel,
  timestamped at event start (ms). Point identity = timestamp + tags, so
  re-reading the same ring is idempotent (overwrite-in-place).
- InfluxDB measurement ``pq_counters`` — lifetime counters at poll time.
- InfluxDB measurement ``pq_waveforms`` — archived RMS traces of the channels
  implicated in each NEW event (tag ``event`` = event-start ms), so the
  waveform survives the device's own few-day retention.
- MQTT ``<device prefix>/pq/event`` — the latest new event as JSON (retained).
- The gateway event log — one line per new PQ event.

The reason bitmask decoding was validated against the firmware's own
``lib/events/events.js``: the 64-bit reason is ``HI<<32 | LO`` with one
nibble per cause and one bit per channel inside the nibble (L1..L4 for L/N
causes, the three pairs for L/L causes).
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Waveform archive pacing defaults (PQ-STALL-1) — per device in its
# pq_recorder block, see docs/config-reference.md.
ARCHIVE_SETTLE_S = 60.0          # after the event is seen: let the capture close
ARCHIVE_GAP_S = 5.0              # between two archive requests to the meter
ARCHIVE_DEFER_STALE_S = 5.0      # realtime poll older than this → hold off
ARCHIVE_DEFER_LATENCY_MS = 2000.0  # realtime read slower than this → hold off
ARCHIVE_MAX_DEFER_S = 600.0      # never hold an event longer (newest window only)
ARCHIVE_PACING_KEYS = ("archive_settle_s", "archive_gap_s", "archive_defer_stale_s",
                       "archive_defer_latency_ms", "archive_max_defer_s")


def _pacing_number(v) -> Optional[float]:
    """A pacing key's value as a non-negative number, else None (→ default)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v < 0:
        return None
    return float(v)

# Template ids whose devices run the Jasic web firmware with the PQ recorder.
# Extend as templates for other UMG models land.
JASIC_PQ_TEMPLATES = {
    "janitza_umg512_pro",
    "janitza_umg604",
    "janitza_umg605",
    "janitza_umg508",
    "janitza_umg511",
}

# (mask, cause tag, channel name per bit) — one nibble per cause, one bit per
# channel. Semantics from the firmware's events.js (64-bit = HI<<32 | LO).
_CAUSES: List[Tuple[int, str, Tuple[str, str, str, str]]] = [
    (0x0000000F, "over_voltage_ln", ("L1", "L2", "L3", "L4")),
    (0x000000F0, "under_voltage_ln", ("L1", "L2", "L3", "L4")),
    (0x00000F00, "voltage_outage_ln", ("L1", "L2", "L3", "L4")),
    (0x0000F000, "over_current", ("L1", "L2", "L3", "L4")),
    (0x00F00000, "over_voltage_ll", ("L1-L2", "L2-L3", "L3-L1", "L4")),
    (0x0F000000, "under_voltage_ll", ("L1-L2", "L2-L3", "L3-L1", "L4")),
    (0xF0000000, "voltage_outage_ll", ("L1-L2", "L2-L3", "L3-L1", "L4")),
    (0x00000F00 << 32, "over_frequency", ("", "", "", "")),
    (0x0000F000 << 32, "under_frequency", ("", "", "", "")),
    (0x000F0000 << 32, "dt_frequency", ("", "", "", "")),
    (0x00F00000 << 32, "rapid_voltage_change_ln", ("L1", "L2", "L3", "L4")),
    (0x0F000000 << 32, "rapid_voltage_change_ll",
     ("L1-L2", "L2-L3", "L3-L1", "L4")),
    (0xF0000000 << 32, "rapid_voltage_change_multi", ("", "", "", "")),
]

# Half-wave-RMS channel index (mk_hww ``_val_nr``) per trace name — from the
# firmware's channel table: 0-3 voltage L1..L4, 4-7 current L1..L4,
# 16-18 voltage L-L pairs.
WAVEFORM_CHANNELS: Dict[str, int] = {
    "UL1": 0, "UL2": 1, "UL3": 2, "UL4": 3,
    "IL1": 4, "IL2": 5, "IL3": 6, "IL4": 7,
    "UL1-L2": 16, "UL2-L3": 17, "UL3-L1": 18,
}


def fetch_json(base_url: str, path: str, timeout: float = 15.0) -> Any:
    """GET a JSON document from the meter's Jasic web firmware."""
    with urllib.request.urlopen(f"{base_url.rstrip('/')}{path}",
                                timeout=timeout) as resp:
        return json.load(resp)


def device_base_url(device: Any) -> str:
    """Resolve a device's PQ recorder base URL: explicit ``base_url`` from
    its pq_recorder block, else ``http://<connection.host>``."""
    cfg = dict(getattr(device, "pq_recorder", {}) or {})
    base = (cfg.get("base_url") or "").strip()
    if base:
        return base
    host = getattr(getattr(device, "connection", None), "host", "") or ""
    return f"http://{host}" if host else ""


def fetch_waveform_live(base_url: str, event_s: float,
                        channel: str) -> List[Tuple[float, float]]:
    """Fetch one channel's half-wave-RMS trace for an event straight from the
    meter (used as a read-through fallback while the trace still exists in
    the device's own few-day retention — e.g. ring events that predate the
    recorder being enabled). Returns ``[(ts_seconds, value), ...]`` or []
    when the meter no longer has a capture window for the event."""
    val_nr = WAVEFORM_CHANNELS.get(channel)
    if val_nr is None:
        return []
    index = fetch_json(base_url, "/lib/events/hww.html").get("hww", [])
    window = None
    for w in index:
        if float(w[0]) - 1.0 <= event_s <= float(w[1]) + 1.0:
            window = float(w[0])
            break
    if window is None:
        return []
    # The meter reconstructs the trace from its recording memory on demand —
    # older windows can take tens of seconds, and a busy meter occasionally
    # times out once. Generous timeout + one retry.
    path = (f"/lib/events/mk_hww.html?_hww_nr={window:.2f}"
            f"&_val_nr={val_nr}")
    try:
        data = fetch_json(base_url, path, timeout=60.0).get("data", [])
    except (OSError, ValueError):
        data = fetch_json(base_url, path, timeout=60.0).get("data", [])
    out = [(float(ts), float(v)) for ts, v in data]
    # CRITICAL validation: a busy/rotating meter has been observed answering
    # a request for an old window with the data of its NEWEST window (same
    # 200/JSON shape, wrong timestamps). Never return — and never let a
    # caller archive — samples that don't belong to the requested window.
    out = [(ts, v) for ts, v in out if window - 1.0 <= ts <= window + 120.0]
    return out


def supports_pq_recorder(template_id: str) -> bool:
    """Legacy id-based check — fallback for templates that predate the
    ``pq_recorder`` template field. Prefer :func:`template_supports_pq`."""
    return (template_id or "").strip().lower() in JASIC_PQ_TEMPLATES


def template_supports_pq(template: Any, template_id: str = "") -> bool:
    """Whether a device has an on-device PQ recorder this module can read.

    The capability is declared IN the device template (top-level
    ``pq_recorder: "jasic"`` — the professional source of truth, so
    community templates for other Jasic-family meters enable the feature
    without code changes). Templates that predate the field fall back to
    the known-id set."""
    fam = str(getattr(template, "pq_recorder", "") or "").strip().lower()
    if fam:
        return fam == "jasic"
    return supports_pq_recorder(template_id)


def decode_reason(mask: int) -> List[Tuple[str, str]]:
    """Decode a 64-bit event reason into ``[(cause, channel), ...]``."""
    out: List[Tuple[str, str]] = []
    for field, cause, chans in _CAUSES:
        bits = mask & field
        if not bits:
            continue
        shift = (field & -field).bit_length() - 1
        nib = bits >> shift
        hit = [chans[i] for i in range(4) if nib & (1 << i)]
        out.extend((cause, ch or "all") for ch in (hit or ["all"]))
    return out or [(f"unknown_0x{mask:x}", "all")]


def alert_severity_for(causes: List[Tuple[str, str]]) -> str:
    """Alert severity for an event's decoded causes: an outage is critical,
    any voltage/current/frequency excursion is a warning, a bare rapid
    voltage change (or unknown) is informational."""
    worst = "info"
    for cause, _ch in causes:
        if "outage" in cause:
            return "critical"
        if cause.startswith(("over_", "under_", "dt_")):
            worst = "warning"
    return worst


def waveform_channels_for(causes: List[Tuple[str, str]]) -> List[str]:
    """Which RMS traces to archive for an event's decoded causes.

    Voltage L/N causes → that phase's voltage trace; L/L causes → that pair's
    trace; over-current → that phase's current trace; frequency/multi-phase
    causes → the three phase voltages (the most informative default).
    """
    chans: List[str] = []

    def _add(name: str) -> None:
        if name in WAVEFORM_CHANNELS and name not in chans:
            chans.append(name)

    for cause, channel in causes:
        if cause == "over_current":
            _add("I" + channel)
        elif cause.endswith("_ll") and channel != "all":
            _add("U" + channel)
        elif channel != "all":
            _add("U" + channel)
        else:
            for ph in ("UL1", "UL2", "UL3"):
                _add(ph)
    return chans


class PqRecorder(threading.Thread):
    """Poll one device's Jasic PQ recorder and route events to the sinks.

    Publisher references are resolved through getter callables at use time —
    ``/api/config/apply`` can rebind the gateway's publishers, and a captured
    stale reference would silently write into a closed client.
    """

    def __init__(self, *,
                 device_id: str,
                 base_url: str,
                 poll_s: float,
                 archive_waveforms: bool,
                 influx_bucket: str,
                 influx_device_tag: str,
                 mqtt_topic_prefix: str,
                 get_influx: Callable[[], Any],
                 get_mqtt: Callable[[], Any],
                 event_log: Any,
                 state_dir: Path,
                 get_alerts: Callable[[], Any] = lambda: None,
                 get_realtime_stats: Callable[[], Optional[dict]] = lambda: None,
                 archive_settle_s: float = ARCHIVE_SETTLE_S,
                 archive_gap_s: float = ARCHIVE_GAP_S,
                 archive_defer_stale_s: float = ARCHIVE_DEFER_STALE_S,
                 archive_defer_latency_ms: float = ARCHIVE_DEFER_LATENCY_MS,
                 archive_max_defer_s: float = ARCHIVE_MAX_DEFER_S):
        super().__init__(daemon=True, name=f"PQRecorder-{device_id}")
        self.device_id = device_id
        self.base_url = base_url.rstrip("/")
        self.poll_s = max(30.0, float(poll_s or 300))
        self.archive_waveforms = bool(archive_waveforms)
        self.influx_bucket = influx_bucket
        self.influx_device_tag = influx_device_tag
        self.mqtt_topic_prefix = (mqtt_topic_prefix or "").rstrip("/")
        self._get_influx = get_influx
        self._get_mqtt = get_mqtt
        self._get_alerts = get_alerts
        self._event_log = event_log
        self._state_path = state_dir / f"pq_state_{device_id}.json"
        # not `_stop`: that name is threading.Thread._stop(), which join() and
        # is_alive() call — an Event there made both raise TypeError
        self._stop_evt = threading.Event()
        self._high_water = 0.0          # newest event start ts already handled
        self._last_poll: float = 0.0
        self._last_error: str = ""
        self._error_streak = 0
        self._counters: Dict[str, int] = {}
        self._ring: List[dict] = []     # decoded ring cache for the API
        # Waveform archive pacing (PQ-STALL-1). Each mk_hww request makes the
        # meter rebuild a trace from its recording memory — work its web
        # server shares with its Modbus TCP server, so an archive fired the
        # moment an event is seen starved the realtime poll (25.5 s read).
        # The archive is a queue worked ONE request per step, off the ring
        # poll: not before the capture settled, a gap between requests, and
        # deferred while the device's realtime poll is behind — bounded,
        # because the meter keeps only its newest capture window.
        self._get_realtime_stats = get_realtime_stats
        self.archive_settle_s = max(0.0, float(archive_settle_s))
        self.archive_gap_s = max(0.0, float(archive_gap_s))
        self.archive_defer_stale_s = max(0.0, float(archive_defer_stale_s))
        self.archive_defer_latency_ms = max(0.0, float(archive_defer_latency_ms))
        self.archive_max_defer_s = max(0.0, float(archive_max_defer_s))
        self._archive_jobs: List[dict] = []
        self._clock: Callable[[], float] = time.monotonic
        self._load_state()

    # -- state ---------------------------------------------------------------

    def _load_state(self) -> None:
        try:
            data = json.loads(self._state_path.read_text())
            self._high_water = float(data.get("high_water", 0.0))
        except (OSError, ValueError):
            self._high_water = 0.0

    def _save_state(self) -> None:
        try:
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"high_water": self._high_water}))
            tmp.replace(self._state_path)
        except OSError as e:
            logger.warning("pq[%s]: state save failed: %s", self.device_id, e)

    # -- device HTTP ---------------------------------------------------------

    def _fetch_json(self, path: str, timeout: float = 15.0) -> Any:
        # thin instance wrapper over the module helper (patch point in tests)
        return fetch_json(self.base_url, path, timeout)

    # -- polling loop --------------------------------------------------------

    def run(self) -> None:
        logger.info("pq[%s]: recorder started (%s, every %.0fs, bucket %s)",
                    self.device_id, self.base_url, self.poll_s,
                    self.influx_bucket)
        # A dedicated PQ bucket may not exist yet — create it (infinite
        # retention) before the first write, best-effort.
        try:
            influx = self._get_influx()
            if influx is not None and hasattr(influx, "ensure_bucket"):
                influx.ensure_bucket(self.influx_bucket)
        except Exception as e:  # noqa: BLE001
            logger.debug("pq[%s]: ensure_bucket failed: %s",
                         self.device_id, e)
        next_poll = self._clock()
        while not self._stop_evt.is_set():
            if self._clock() >= next_poll:
                self._poll_guarded()
                next_poll = self._clock() + self.poll_s
            # at most ONE archive request per pass; the ring poll keeps its
            # own cadence around it
            wake = self._archive_step()
            until = next_poll if wake is None else min(next_poll, wake)
            self._stop_evt.wait(max(0.05, until - self._clock()))
        logger.info("pq[%s]: recorder stopped", self.device_id)

    def _poll_guarded(self) -> None:
        try:
            self._poll_once()
            if self._error_streak:
                logger.info("pq[%s]: recovered after %d failed polls",
                            self.device_id, self._error_streak)
            self._error_streak = 0
            self._last_error = ""
        except Exception as e:  # noqa: BLE001 — a device outage is normal
            self._error_streak += 1
            self._last_error = str(e)
            # First failure of a streak at info — during a grid outage the
            # meter itself is dark and this is expected, not alarming.
            if self._error_streak == 1:
                logger.info("pq[%s]: poll failed: %s", self.device_id, e)

    def stop(self) -> None:
        self._stop_evt.set()

    def _poll_once(self) -> None:
        events = self._fetch_json("/lib/events/getevt.html").get("events", [])
        counters = self._fetch_json(
            "/json.do?" + urllib.parse.quote(
                "_EVT_COUNT,_FLAG_COUNT,_TRANS_COUNT,"))
        self._last_poll = time.time()
        self._counters = {
            "events": int(counters.get("_EVT_COUNT", [0])[0]),
            "flags": int(counters.get("_FLAG_COUNT", [0])[0]),
            "transients": int(counters.get("_TRANS_COUNT", [0])[0]),
        }

        decoded: List[dict] = []
        for ev in events:
            try:
                start, end, bound, vmax, vmin, vavg, lo, hi = ev[:8]
            except (TypeError, ValueError):
                continue
            causes = decode_reason((int(hi) << 32) | int(lo))
            decoded.append({
                "start": float(start), "end": float(end),
                "duration_ms": (float(end) - float(start)) * 1000.0,
                "bound": float(bound), "vmax": float(vmax),
                "vmin": float(vmin), "vavg": float(vavg),
                "reason_lo": int(lo), "reason_hi": int(hi),
                "causes": [{"cause": c, "channel": ch} for c, ch in causes],
            })
        decoded.sort(key=lambda d: d["start"])
        self._ring = decoded

        # The whole ring is (re)written every poll — identical points
        # overwrite in place, so this is cheap and self-healing after an
        # InfluxDB outage.
        for ev in decoded:
            self._write_event(ev)
        self._write_counters()

        new = [ev for ev in decoded if ev["start"] > self._high_water]
        first_sync = self._high_water == 0.0
        if decoded:
            self._high_water = max(d["start"] for d in decoded)
            self._save_state()
        if first_sync:
            # First contact ever: the ring's history is archived above, but it
            # is not "news" — no notifications, no waveform burst.
            return
        for ev in new:
            self._announce(ev)
            if self.archive_waveforms:
                # queued, not fetched here: the run loop works it paced
                now = self._clock()
                self._archive_jobs.append({
                    "ev": ev, "due": now + self.archive_settle_s,
                    "ready_at": now + self.archive_settle_s,
                    "window": None, "channels": None, "deferred": False})

    # -- sinks ---------------------------------------------------------------

    def _write_event(self, ev: dict) -> None:
        influx = self._get_influx()
        if influx is None or not getattr(influx, "is_enabled", lambda: False)():
            return
        from influxdb_client import Point, WritePrecision
        ts_ms = int(ev["start"] * 1000)
        for c in ev["causes"]:
            p = (Point("pq_events")
                 .tag("device", self.influx_device_tag)
                 .tag("cause", c["cause"])
                 .tag("channel", c["channel"])
                 .field("duration_ms", float(ev["duration_ms"]))
                 .field("bound", float(ev["bound"]))
                 .field("vmax", float(ev["vmax"]))
                 .field("vmin", float(ev["vmin"]))
                 .field("vavg", float(ev["vavg"]))
                 .field("reason_lo", int(ev["reason_lo"]))
                 .field("reason_hi", int(ev["reason_hi"]))
                 .time(ts_ms, WritePrecision.MS))
            influx.write_point(p, bucket=self.influx_bucket)

    def _write_counters(self) -> None:
        influx = self._get_influx()
        if influx is None or not getattr(influx, "is_enabled", lambda: False)():
            return
        from influxdb_client import Point, WritePrecision
        p = (Point("pq_counters")
             .tag("device", self.influx_device_tag)
             .field("evt_count", int(self._counters.get("events", 0)))
             .field("flag_count", int(self._counters.get("flags", 0)))
             .field("trans_count", int(self._counters.get("transients", 0)))
             .time(int(time.time() * 1000), WritePrecision.MS))
        influx.write_point(p, bucket=self.influx_bucket)

    def _announce(self, ev: dict) -> None:
        summary = ", ".join(f'{c["cause"]}[{c["channel"]}]'
                            for c in ev["causes"])
        message = (f"{self.device_id}: PQ event {summary} "
                   f"({ev['duration_ms']:.0f} ms, min {ev['vmin']:.1f})")
        if self._event_log is not None:
            try:
                self._event_log.add("warning", "pq", message)
            except Exception:  # noqa: BLE001
                pass
        alerts = self._get_alerts()
        if alerts is not None:
            try:
                causes = [(c["cause"], c["channel"]) for c in ev["causes"]]
                # per-device alert key → the AlertManager's own min_interval
                # throttles a burst of events into one notification
                alerts.fire(alert_severity_for(causes),
                            f"pq_{self.device_id}", "pq", message)
            except Exception as e:  # noqa: BLE001
                logger.debug("pq[%s]: alert fire failed: %s",
                             self.device_id, e)
        mqtt = self._get_mqtt()
        if mqtt is not None and self.mqtt_topic_prefix:
            try:
                mqtt.publish_topic(
                    f"{self.mqtt_topic_prefix}/pq/event",
                    json.dumps({k: ev[k] for k in
                                ("start", "end", "duration_ms", "bound",
                                 "vmax", "vmin", "vavg", "causes")}),
                    retain=True)
            except Exception as e:  # noqa: BLE001
                logger.debug("pq[%s]: mqtt publish failed: %s",
                             self.device_id, e)

    def _realtime_behind(self) -> bool:
        """Is this device's realtime poll behind right now (stale, or its last
        read slow)? Unknown → not behind."""
        try:
            st = self._get_realtime_stats() or {}
        except Exception:  # noqa: BLE001
            return False
        age = st.get("staleness_age_s")
        lat = st.get("last_latency_ms")
        return bool((age is not None and self.archive_defer_stale_s
                     and float(age) > self.archive_defer_stale_s)
                    or (lat is not None and self.archive_defer_latency_ms
                        and float(lat) > self.archive_defer_latency_ms))

    def _archive_step(self) -> Optional[float]:
        """Do at most ONE archive request (the capture index, or one channel's
        trace) for the oldest queued event, when it is due. Returns when the
        next step is due (monotonic), or None when nothing is queued."""
        if not self._archive_jobs:
            return None
        job = self._archive_jobs[0]
        now = self._clock()
        if now < job["due"]:
            return job["due"]
        ev = job["ev"]
        if self._realtime_behind():
            if now - job["ready_at"] < self.archive_max_defer_s:
                if not job["deferred"]:
                    job["deferred"] = True
                    logger.info("pq[%s]: waveform archive for event %.3f deferred — "
                                "the device's realtime poll is behind",
                                self.device_id, ev["start"])
                job["due"] = now + max(1.0, self.archive_gap_s)
                return job["due"]
            logger.warning("pq[%s]: waveform archive for event %.3f proceeds after "
                           "%.0f s of deferral (the meter keeps only its newest window)",
                           self.device_id, ev["start"], now - job["ready_at"])
        try:
            more = self._archive_one_request(job)
        except Exception as e:  # noqa: BLE001
            logger.warning("pq[%s]: waveform archive failed for "
                           "event %.3f: %s", self.device_id, ev["start"], e)
            more = False
        if not more:
            self._archive_jobs.pop(0)
            if not self._archive_jobs:
                return None
        job = self._archive_jobs[0]
        job["due"] = max(job["due"], self._clock() + self.archive_gap_s)
        return job["due"]

    def _archive_one_request(self, job: dict) -> bool:
        """One request of an event's archive. True while the event has more."""
        ev = job["ev"]
        influx = self._get_influx()
        if influx is None or not getattr(influx, "is_enabled", lambda: False)():
            return False
        if job["window"] is None:
            # the recording index maps event → its ~50 s capture window
            index = self._fetch_json("/lib/events/hww.html").get("hww", [])
            for w in index:
                if float(w[0]) - 1.0 <= ev["start"] <= float(w[1]) + 1.0:
                    job["window"] = float(w[0])
                    break
            if job["window"] is None:
                logger.info("pq[%s]: no capture window for event %.3f",
                            self.device_id, ev["start"])
                return False
            causes = [(c["cause"], c["channel"]) for c in ev["causes"]]
            job["channels"] = list(waveform_channels_for(causes))
            return bool(job["channels"])
        window = job["window"]
        name = job["channels"].pop(0)
        self._archive_channel(influx, ev, window, name)
        return bool(job["channels"])

    def _archive_channel(self, influx, ev: dict, window: float, name: str) -> None:
        """Fetch and persist one channel's RMS trace for one event."""
        from influxdb_client import Point, WritePrecision
        event_tag = str(int(ev["start"] * 1000))
        val_nr = WAVEFORM_CHANNELS[name]
        data = self._fetch_json(
            f"/lib/events/mk_hww.html?_hww_nr={window:.2f}"
            f"&_val_nr={val_nr}", timeout=60.0).get("data", [])
        # same wrong-window guard as fetch_waveform_live: never archive
        # samples that don't belong to the requested capture window
        data = [(ts, v) for ts, v in data
                if window - 1.0 <= float(ts) <= window + 120.0]
        for ts, value in data:
            p = (Point("pq_waveforms")
                 .tag("device", self.influx_device_tag)
                 .tag("event", event_tag)
                 .tag("channel", name)
                 .field("value", float(value))
                 .time(int(float(ts) * 1000), WritePrecision.MS))
            influx.write_point(p, bucket=self.influx_bucket)
        logger.info("pq[%s]: archived %d samples of %s for event %s",
                    self.device_id, len(data), name, event_tag)

    # -- introspection -------------------------------------------------------

    def status(self) -> dict:
        return {
            "device": self.device_id,
            "base_url": self.base_url,
            "poll_s": self.poll_s,
            "archive_waveforms": self.archive_waveforms,
            "last_poll": self._last_poll or None,
            "last_error": self._last_error or None,
            "error_streak": self._error_streak,
            "counters": dict(self._counters),
            "ring_size": len(self._ring),
            "archive_pending": len(self._archive_jobs),
        }

    def ring(self) -> List[dict]:
        return list(self._ring)


class PqRecorderManager:
    """Build/start/stop one :class:`PqRecorder` per PQ-enabled device."""

    def __init__(self, *, config: Any,
                 get_influx: Callable[[], Any],
                 get_mqtt: Callable[[], Any],
                 event_log: Any,
                 state_dir: Path,
                 get_alerts: Callable[[], Any] = lambda: None,
                 get_template: Callable[[str], Any] = lambda _tid: None,
                 get_realtime_stats: Callable[[str], Optional[dict]] = lambda _id: None):
        self._config = config
        # device id → that device's realtime client stats (get_stats()), so a
        # recorder can hold its waveform archive while the realtime poll lags
        self._get_realtime_stats = get_realtime_stats
        self._get_influx = get_influx
        self._get_mqtt = get_mqtt
        self._get_alerts = get_alerts
        self._get_template = get_template
        self._event_log = event_log
        self._state_dir = Path(state_dir)
        self.recorders: Dict[str, PqRecorder] = {}

    def _build_one(self, device: Any) -> Optional[PqRecorder]:
        cfg = dict(getattr(device, "pq_recorder", {}) or {})
        if not cfg.get("enabled"):
            return None
        base_url = (cfg.get("base_url") or "").strip()
        if not base_url:
            host = getattr(device.connection, "host", "") or ""
            if not host:
                logger.warning("pq[%s]: enabled but no host/base_url — "
                               "skipping", device.id)
                return None
            base_url = f"http://{host}"
        return PqRecorder(
            device_id=device.id,
            base_url=base_url,
            poll_s=cfg.get("poll_s", 300),
            archive_waveforms=cfg.get("archive_waveforms", True),
            # PQ history is small but forensically precious (grid disputes
            # run on months) — an optional dedicated bucket decouples its
            # retention from the device's high-frequency telemetry bucket.
            influx_bucket=(cfg.get("bucket") or "").strip()
                          or device.influxdb_bucket,
            influx_device_tag=device.influxdb_device_tag or device.id,
            mqtt_topic_prefix=device.mqtt_topic_prefix,
            get_influx=self._get_influx,
            get_mqtt=self._get_mqtt,
            get_alerts=self._get_alerts,
            event_log=self._event_log,
            state_dir=self._state_dir,
            get_realtime_stats=(lambda _id=device.id: self._get_realtime_stats(_id)),
            **{k: float(cfg[k]) for k in ARCHIVE_PACING_KEYS
               if _pacing_number(cfg.get(k)) is not None},
        )

    def start_all(self) -> None:
        for device in self._config.devices:
            if device.id in self.recorders or not device.enabled:
                continue
            rec = self._build_one(device)
            if rec is not None:
                self.recorders[device.id] = rec
                rec.start()

    def stop_all(self) -> None:
        for rec in self.recorders.values():
            rec.stop()
        self.recorders.clear()

    def apply_device(self, device_id: str) -> None:
        """Restart one device's recorder after its config changed."""
        old = self.recorders.pop(device_id, None)
        if old is not None:
            old.stop()
        for device in self._config.devices:
            if device.id == device_id and device.enabled:
                rec = self._build_one(device)
                if rec is not None:
                    self.recorders[device_id] = rec
                    rec.start()
                break

    def status(self) -> List[dict]:
        out = []
        for device in self._config.devices:
            rec = self.recorders.get(device.id)
            entry = {
                "device": device.id,
                "supported": template_supports_pq(
                    self._get_template(device.template), device.template),
                "enabled": bool((getattr(device, "pq_recorder", {}) or {})
                                .get("enabled")),
                "running": rec is not None and rec.is_alive(),
            }
            if rec is not None:
                entry.update(rec.status())
            out.append(entry)
        return out
