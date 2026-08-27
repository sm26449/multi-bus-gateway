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


def supports_pq_recorder(template_id: str) -> bool:
    """Whether a device template's hardware family has the Jasic PQ recorder."""
    return (template_id or "").strip().lower() in JASIC_PQ_TEMPLATES


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
                 state_dir: Path):
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
        self._event_log = event_log
        self._state_path = state_dir / f"pq_state_{device_id}.json"
        self._stop = threading.Event()
        self._high_water = 0.0          # newest event start ts already handled
        self._last_poll: float = 0.0
        self._last_error: str = ""
        self._error_streak = 0
        self._counters: Dict[str, int] = {}
        self._ring: List[dict] = []     # decoded ring cache for the API
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
        with urllib.request.urlopen(f"{self.base_url}{path}",
                                    timeout=timeout) as resp:
            return json.load(resp)

    # -- polling loop --------------------------------------------------------

    def run(self) -> None:
        logger.info("pq[%s]: recorder started (%s, every %.0fs)",
                    self.device_id, self.base_url, self.poll_s)
        while not self._stop.is_set():
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
            self._stop.wait(self.poll_s)
        logger.info("pq[%s]: recorder stopped", self.device_id)

    def stop(self) -> None:
        self._stop.set()

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
                try:
                    self._archive_waveforms(ev)
                except Exception as e:  # noqa: BLE001
                    logger.warning("pq[%s]: waveform archive failed for "
                                   "event %.3f: %s", self.device_id,
                                   ev["start"], e)

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
        if self._event_log is not None:
            try:
                self._event_log.add(
                    "warning", "pq",
                    f"{self.device_id}: PQ event {summary} "
                    f"({ev['duration_ms']:.0f} ms, min {ev['vmin']:.1f})")
            except Exception:  # noqa: BLE001
                pass
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

    def _archive_waveforms(self, ev: dict) -> None:
        """Fetch and persist the RMS traces of the channels implicated in one
        event. The recording index maps event → its ~50 s capture window."""
        influx = self._get_influx()
        if influx is None or not getattr(influx, "is_enabled", lambda: False)():
            return
        index = self._fetch_json("/lib/events/hww.html").get("hww", [])
        window = None
        for w in index:
            if float(w[0]) - 1.0 <= ev["start"] <= float(w[1]) + 1.0:
                window = float(w[0])
                break
        if window is None:
            logger.info("pq[%s]: no capture window for event %.3f",
                        self.device_id, ev["start"])
            return
        causes = [(c["cause"], c["channel"]) for c in ev["causes"]]
        from influxdb_client import Point, WritePrecision
        event_tag = str(int(ev["start"] * 1000))
        for name in waveform_channels_for(causes):
            val_nr = WAVEFORM_CHANNELS[name]
            data = self._fetch_json(
                f"/lib/events/mk_hww.html?_hww_nr={window:.2f}"
                f"&_val_nr={val_nr}", timeout=30.0).get("data", [])
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
        }

    def ring(self) -> List[dict]:
        return list(self._ring)


class PqRecorderManager:
    """Build/start/stop one :class:`PqRecorder` per PQ-enabled device."""

    def __init__(self, *, config: Any,
                 get_influx: Callable[[], Any],
                 get_mqtt: Callable[[], Any],
                 event_log: Any,
                 state_dir: Path):
        self._config = config
        self._get_influx = get_influx
        self._get_mqtt = get_mqtt
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
            influx_bucket=device.influxdb_bucket,
            influx_device_tag=device.influxdb_device_tag or device.id,
            mqtt_topic_prefix=device.mqtt_topic_prefix,
            get_influx=self._get_influx,
            get_mqtt=self._get_mqtt,
            event_log=self._event_log,
            state_dir=self._state_dir,
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
                "supported": supports_pq_recorder(device.template),
                "enabled": bool((getattr(device, "pq_recorder", {}) or {})
                                .get("enabled")),
                "running": rec is not None and rec.is_alive(),
            }
            if rec is not None:
                entry.update(rec.status())
            out.append(entry)
        return out
