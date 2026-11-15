# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""PQ-STALL-1: the waveform archive (one mk_hww reconstruction per channel on the
meter's own web server) is paced and deferred so it cannot starve the meter's
realtime Modbus poll. 2026-11-14 07:50:54Z: an L3 dip → archive of ~3001 samples
fetched at once while the meter was busy; the umg512 realtime read took 25.5 s.

The archive never runs inside the recorder's ring poll; it waits for the capture
window to settle, does ONE request per step with a gap between steps, and defers
while the device's realtime poll is behind (bounded, so the trace — the meter
keeps only its newest window — is not lost)."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

from multibus.pq_recorder import PqRecorder, PqRecorderManager

EV1 = [1000.0, 1000.1, 20.0, 25.0, 1.0, 230.0, 0, 0x200000]
# under_voltage_ln on L1 + L3 → channels UL1, UL3 (+ currents? see channels_for)
EV2 = [2000.0, 2000.5, 20.0, 25.0, 0.5, 231.0, 0x50, 0]
WINDOW = 1990.0


class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class _Influx:
    def __init__(self):
        self.points = []

    def is_enabled(self):
        return True

    def write_point(self, p, bucket=None):
        if p._name == "pq_waveforms":           # events/counters are not the archive
            self.points.append(p)


def _rec(tmp_path: Path, stats=None, **kw) -> tuple:
    influx = _Influx()
    calls = []
    payloads = {
        "/lib/events/getevt.html": {"events": [EV1]},
        "/json.do?_EVT_COUNT%2C_FLAG_COUNT%2C_TRANS_COUNT%2C":
            {"_EVT_COUNT": [1, ""], "_FLAG_COUNT": [0, ""], "_TRANS_COUNT": [0, ""]},
        "/lib/events/hww.html": {"hww": [[WINDOW, WINDOW + 50.0]]},
    }

    def fetch(path, timeout=15.0):
        calls.append(path)
        if path.startswith("/lib/events/mk_hww.html"):
            return {"data": [[WINDOW + i * 0.01, 230.0] for i in range(1000)]}
        return json.loads(json.dumps(payloads[path]))

    rec = PqRecorder(
        device_id="jz", base_url="http://meter", poll_s=60,
        archive_waveforms=True, influx_bucket="janitza_pq",
        influx_device_tag="janitza_umg512", mqtt_topic_prefix="",
        get_influx=lambda: influx, get_mqtt=lambda: None,
        event_log=MagicMock(), state_dir=tmp_path,
        get_realtime_stats=(lambda: stats[0]) if stats is not None else (lambda: None),
        **kw)
    rec._fetch_json = fetch
    clock = _Clock()
    rec._clock = clock
    rec._poll_once()                                  # first sync: history, no archive
    payloads["/lib/events/getevt.html"] = {"events": [EV1, EV2]}
    return rec, calls, influx, clock


def _archive_calls(calls):
    return [c for c in calls if c.startswith(("/lib/events/hww", "/lib/events/mk_hww"))]


def test_ring_poll_does_not_fetch_waveforms_inline(tmp_path):
    rec, calls, _influx, _clock = _rec(tmp_path)
    rec._poll_once()                                  # EV2 is new
    assert _archive_calls(calls) == []                # queued, not fetched in the poll
    assert rec.status()["archive_pending"] == 1


def test_archive_waits_to_settle_then_one_request_per_gap(tmp_path):
    rec, calls, influx, clock = _rec(tmp_path, archive_settle_s=60, archive_gap_s=5)
    rec._poll_once()
    rec._archive_step()
    assert _archive_calls(calls) == []                # still settling
    clock.t += 60
    rec._archive_step()
    assert _archive_calls(calls) == ["/lib/events/hww.html"]
    rec._archive_step()                               # inside the gap → nothing
    assert len(_archive_calls(calls)) == 1
    clock.t += 5
    rec._archive_step()
    assert len(_archive_calls(calls)) == 2 and _archive_calls(calls)[1].startswith("/lib/events/mk_hww")
    clock.t += 5
    rec._archive_step()
    assert len(_archive_calls(calls)) == 3            # second channel, its own step
    clock.t += 5
    rec._archive_step()
    assert len(_archive_calls(calls)) == 3            # done: UL1 + UL3 only
    assert len(influx.points) == 2000
    assert rec.status()["archive_pending"] == 0


def test_archive_defers_while_realtime_is_behind(tmp_path):
    stats = [{"staleness_age_s": 12.0, "last_latency_ms": 40}]
    rec, calls, _influx, clock = _rec(tmp_path, stats=stats, archive_settle_s=0,
                                      archive_gap_s=5, archive_max_defer_s=600)
    rec._poll_once()
    for _ in range(10):
        clock.t += 5
        rec._archive_step()
    assert _archive_calls(calls) == []                # realtime stale → hands off
    stats[0] = {"staleness_age_s": 0.4, "last_latency_ms": 3500}
    clock.t += 5
    rec._archive_step()
    assert _archive_calls(calls) == []                # slow reads → still hands off
    stats[0] = {"staleness_age_s": 0.4, "last_latency_ms": 40}
    clock.t += 5
    rec._archive_step()
    assert _archive_calls(calls) == ["/lib/events/hww.html"]


def test_archive_deferral_is_bounded(tmp_path):
    """The meter keeps only its newest capture window: a deferral without end
    would lose the trace. After archive_max_defer_s it proceeds anyway."""
    stats = [{"staleness_age_s": 99.0}]
    rec, calls, _influx, clock = _rec(tmp_path, stats=stats, archive_settle_s=0,
                                      archive_gap_s=5, archive_max_defer_s=60)
    rec._poll_once()
    rec._archive_step()
    assert _archive_calls(calls) == []
    clock.t += 61
    rec._archive_step()
    assert _archive_calls(calls) == ["/lib/events/hww.html"]


def test_manager_passes_pacing_keys_and_realtime_stats(tmp_path):
    class Conn:
        host = "192.168.1.9"

    class Dev:
        id = "jz"
        enabled = True
        template = "janitza_umg512_pro"
        connection = Conn()
        influxdb_bucket = "janitza"
        influxdb_device_tag = "janitza_umg512"
        mqtt_topic_prefix = "meters/jz"
        pq_recorder = {"enabled": True, "archive_settle_s": 90, "archive_gap_s": 2,
                       "archive_defer_stale_s": 4, "archive_defer_latency_ms": 1500,
                       "archive_max_defer_s": 300}

    class Cfg:
        devices = [Dev()]

    seen = []
    mgr = PqRecorderManager(config=Cfg(), get_influx=lambda: None, get_mqtt=lambda: None,
                            event_log=None, state_dir=tmp_path,
                            get_realtime_stats=lambda did: seen.append(did) or {"x": 1})
    rec = mgr._build_one(Cfg.devices[0])
    assert (rec.archive_settle_s, rec.archive_gap_s, rec.archive_defer_stale_s,
            rec.archive_defer_latency_ms, rec.archive_max_defer_s) == (90, 2, 4, 1500, 300)
    assert rec._get_realtime_stats() == {"x": 1} and seen == ["jz"]
    Dev.pq_recorder = {"enabled": True}
    rec = mgr._build_one(Cfg.devices[0])
    assert (rec.archive_settle_s, rec.archive_gap_s, rec.archive_defer_stale_s,
            rec.archive_defer_latency_ms, rec.archive_max_defer_s) == (60, 5, 5, 2000, 600)


def test_realtime_poller_keeps_cadence_during_a_slow_archive(tmp_path):
    """A slow archive (each mk_hww blocks 0.4 s, ~1000 samples written) runs on the
    recorder thread; a realtime poller on its own thread keeps its 20 ms cadence —
    nothing in the gateway serializes the two."""
    influx = _Influx()
    rec = PqRecorder(
        device_id="jz", base_url="http://meter", poll_s=30,
        archive_waveforms=True, influx_bucket="b", influx_device_tag="t",
        mqtt_topic_prefix="", get_influx=lambda: influx, get_mqtt=lambda: None,
        event_log=MagicMock(), state_dir=tmp_path, archive_settle_s=0, archive_gap_s=0)
    rec._high_water = 1.0                              # not a first sync
    in_archive = threading.Event()

    def fetch(path, timeout=15.0):
        if path == "/lib/events/getevt.html":
            return {"events": [EV2]}
        if path.startswith("/json.do"):
            return {}
        if path == "/lib/events/hww.html":
            return {"hww": [[WINDOW, WINDOW + 50.0]]}
        in_archive.set()
        time.sleep(0.4)
        return {"data": [[WINDOW + i * 0.01, 230.0] for i in range(1000)]}
    rec._fetch_json = fetch

    stop = threading.Event()
    ticks = []

    def realtime():
        while not stop.is_set():
            ticks.append(time.monotonic())
            time.sleep(0.02)
    rt = threading.Thread(target=realtime, daemon=True)
    rt.start()
    rec.start()
    try:
        assert in_archive.wait(5)
        deadline = time.monotonic() + 5
        while rec.status()["archive_pending"] and time.monotonic() < deadline:
            time.sleep(0.05)
        assert rec.status()["archive_pending"] == 0
    finally:
        rec.stop()
        stop.set()
        rt.join(2)
        rec.join(5)
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert len(influx.points) == 2000
    assert max(gaps) < 0.3, f"realtime stalled {max(gaps):.2f}s during the archive"
