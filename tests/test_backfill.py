# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Unit tests for the InfluxDB gap-backfill tool (multibus/backfill.py).

Pure logic only — every network call (meter HTTP, InfluxDB) is mocked, so these
never touch a real meter or database.
"""
from datetime import datetime, timezone

from multibus import backfill


def test_fetch_hist_filters_window_converts_tz_and_skips_junk(monkeypatch):
    tz = 3600  # device-local = UTC + 1h

    def local(utc):
        return float(utc + tz)

    # meter returns HIST_DATA as {param: [[ [value, local_ts], ... ]]}
    arr = [
        [231.0, local(1000)],   # utc 1000 — before window → dropped
        [232.0, local(1060)],   # utc 1060 — IN window → kept
        [233.0, local(1120)],   # utc 1120 — after window → dropped
        None,                    # junk → skipped
        [None, local(1060)],     # null value → skipped
        [5.0],                   # too short → skipped
    ]
    monkeypatch.setattr(backfill, "_http_json", lambda url, timeout=12: {"_ULN[0]": [arr]})
    out = backfill.fetch_hist("_ULN[0]", 1050, 1100, tz)
    assert out == [(232.0, 1060.0)]        # only the in-window point, tz-converted to UTC


def test_meter_tz_offset(monkeypatch):
    monkeypatch.setattr(backfill, "_http_json",
                        lambda url, timeout=8: {"_SYSTIME": [8200], "_UTCTIME": [1000]})
    assert backfill.meter_tz_offset() == 7200


def test_main_requires_token(monkeypatch, capsys):
    monkeypatch.setattr(backfill, "INFLUX_TOKEN", "")
    assert backfill.main([]) == 2
    assert "INFLUXDB_TOKEN not set" in capsys.readouterr().err


def test_main_window_treats_naive_as_utc_and_honours_dry_run(monkeypatch):
    monkeypatch.setattr(backfill, "INFLUX_TOKEN", "tok")
    seen = {}
    monkeypatch.setattr(backfill, "backfill",
                        lambda s, e, dry, verbose: seen.update(s=s, e=e, dry=dry) or 5)
    rc = backfill.main(["--window", "2026-06-03T11:00", "2026-06-03T12:00", "--dry-run"])
    assert rc == 0
    assert seen["dry"] is True
    assert seen["s"] == datetime(2026, 6, 3, 11, 0, tzinfo=timezone.utc).timestamp()
    assert seen["e"] == datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc).timestamp()


def test_main_auto_skips_when_no_gap(monkeypatch):
    import time
    monkeypatch.setattr(backfill, "INFLUX_TOKEN", "tok")
    monkeypatch.setattr(backfill, "influx_latest_voltage_utc", lambda: time.time() - 5)  # 5s ago
    called = []
    monkeypatch.setattr(backfill, "backfill", lambda *a, **k: called.append(1) or 0)
    assert backfill.main([]) == 0
    assert not called                       # a fresh last-point → nothing to heal


def test_main_auto_backfills_on_gap(monkeypatch):
    import time
    monkeypatch.setattr(backfill, "INFLUX_TOKEN", "tok")
    # last point an hour ago → a gap well past MIN_GAP_SEC
    monkeypatch.setattr(backfill, "influx_latest_voltage_utc", lambda: time.time() - 3600)
    seen = {}
    monkeypatch.setattr(backfill, "backfill",
                        lambda s, e, dry, verbose: seen.update(s=s, e=e) or 42)
    assert backfill.main([]) == 0
    assert seen and seen["e"] > seen["s"]   # a real window was passed to backfill


def test_backfill_dry_run_writes_nothing(monkeypatch):
    monkeypatch.setattr(backfill, "meter_tz_offset", lambda: 0)
    monkeypatch.setattr(backfill, "fetch_hist", lambda *a, **k: [(230.0, 1000.0)])
    # dry=True → no InfluxDB client is created and nothing is written
    assert backfill.backfill(0, 10_000, dry=True, verbose=True) == 0
