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
    monkeypatch.setattr(backfill, "influx_interior_gap_utc", lambda m: None)
    called = []
    monkeypatch.setattr(backfill, "backfill", lambda *a, **k: called.append(1) or 0)
    assert backfill.main([]) == 0
    assert not called          # fresh tail + no interior hole → nothing to heal


def test_main_auto_heals_interior_hole(monkeypatch):
    """DP-14: a fresh tail must no longer hide an already-recovered outage —
    the interior hole is detected and backfilled with padded bounds."""
    import time
    now = time.time()
    hole = (now - 7200, now - 3600)          # a past one-hour outage
    monkeypatch.setattr(backfill, "INFLUX_TOKEN", "tok")
    monkeypatch.setattr(backfill, "influx_latest_voltage_utc", lambda: now - 5)
    monkeypatch.setattr(backfill, "influx_interior_gap_utc", lambda m: hole)
    windows = []
    monkeypatch.setattr(backfill, "backfill",
                        lambda s, e, dry, v: windows.append((s, e)) or 7)
    assert backfill.main([]) == 0
    assert len(windows) == 1
    s, e = windows[0]
    assert s == hole[0] - 120 and e == hole[1] + 120


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


def _fake_regs():
    from multibus.config import SelectedRegister
    return {addr: SelectedRegister(address=addr, name=f"voltage_x{addr}",
                                   label=f"x{addr}", unit="V", data_type="float",
                                   poll_group="realtime")
            for _p, addr in backfill.PARAMS}


def test_backfill_dry_run_fetches_but_makes_no_client(monkeypatch):
    import influxdb_client
    monkeypatch.setattr(backfill, "meter_tz_offset", lambda: 0)
    monkeypatch.setattr(backfill, "load_registers", _fake_regs)
    calls = {"fetch": 0}

    def _fetch(*a, **k):
        calls["fetch"] += 1
        return [(230.0, 1000.0)]

    monkeypatch.setattr(backfill, "fetch_hist", _fetch)

    def _boom(*a, **k):
        raise AssertionError("dry-run must not construct an InfluxDB client")

    monkeypatch.setattr(influxdb_client, "InfluxDBClient", _boom)
    # dry → does the real fetch work (so a --dry-run report is accurate) but
    # constructs no client and writes nothing
    assert backfill.backfill(0, 10_000, dry=True, verbose=True) == 0
    assert calls["fetch"] == len(backfill.PARAMS)


def test_backfill_emits_the_live_publisher_schema(monkeypatch, tmp_path):
    """B3 (external audit): the backfilled line protocol must be BYTE-equal to
    the live publisher's for the same register/value/timestamp, modulo the
    `backfilled` marker field — no more hand-transcribed parallel series."""
    from multibus.config import SelectedRegister
    from multibus.influxdb_publisher import build_point

    reg = SelectedRegister(address=19000, name="voltage_l1_n", label="L1",
                           unit="V", data_type="float", poll_group="realtime",
                           influxdb_tags={"phase": "L1", "type": "line_neutral"})
    monkeypatch.setattr(backfill, "meter_tz_offset", lambda: 0)
    monkeypatch.setattr(backfill, "load_registers", lambda: {19000: reg})
    monkeypatch.setattr(backfill, "PARAMS", [("_ULN[0]", 19000)])
    monkeypatch.setattr(backfill, "fetch_hist", lambda *a, **k: [(230.5, 1000.0)])

    written = []

    class _WApi:
        def write(self, bucket, record): written.append(record)
        def close(self): pass

    class _Client:
        def __init__(self, *a, **k): pass
        def write_api(self, **k): return _WApi()
        def close(self): pass
    import influxdb_client
    monkeypatch.setattr(influxdb_client, "InfluxDBClient", _Client)

    assert backfill.backfill(0, 10_000, dry=False, verbose=False) == 1
    got = written[0].to_line_protocol()
    live = build_point(reg, 230.5, 1000.0, poll_group="realtime").to_line_protocol()
    # strip the marker field, timestamps normalized (S vs NS precision)
    assert got.replace(",backfilled=1i", "").replace("backfilled=1i,", "").split()[0:2] == live.split()[0:2]
    assert "backfilled=1" in got


def test_backfill_skips_deselected_addresses(monkeypatch, capsys):
    monkeypatch.setattr(backfill, "meter_tz_offset", lambda: 0)
    monkeypatch.setattr(backfill, "load_registers", lambda: {})
    monkeypatch.setattr(backfill, "fetch_hist",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch")))
    import influxdb_client

    class _Client:
        def __init__(self, *a, **k): pass
        def write_api(self, **k): return None
        def close(self): pass
    monkeypatch.setattr(influxdb_client, "InfluxDBClient", _Client)
    assert backfill.backfill(0, 10_000, dry=False, verbose=False) == 0
    assert "not in the live selection" in capsys.readouterr().out
