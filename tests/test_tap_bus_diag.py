# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""TAP-DIAG-2 — a tapped bus's counters in the diagnostics, once per bus.

Eight Seplos packs share one listen-only reader. Its CRC errors, resync drops,
orphans and exception responses are the BUS's; every pack's get_stats()
repeats them. TAP-METRIC-1 fixed /metrics; the bridge diagnostics (the
Devices page's Line health, its diagnosis and alerts) still summed them over
every device on the bus, so a bus with 3 CRC errors read 24. These tests hold
the fix: once per bus, per device only what is the device's own.
"""
from types import SimpleNamespace

from multibus import bridge_diag as bd
from multibus.transports.modbus.rtu_tap import RtuTapClient, TapReader, unique_buses
from tests.test_bridge_diag import Bridge, FakeClient, _adapter, _diag, _stats
from tests.test_rtu_tap import req_read, resp_read

N = 8


def _bus(n=N, host="192.0.2.9", port=7001):
    """One reader on a bridge's bus and n tap clients on it (units 1..n)."""
    c0 = SimpleNamespace(serial_port="", host=host, port=port, baudrate=9600, parity="N",
                         stopbits=1, bytesize=8, unit_id=1, stale_after_s=30)
    reader = TapReader(c0)
    reader._serial = SimpleNamespace(close=lambda: None)
    clients = []
    for u in range(1, n + 1):
        cc = SimpleNamespace(**{**vars(c0), "unit_id": u})
        c = RtuTapClient(conn_cfg=cc, registers=[], device_id=f"seplos-tap-p{u}")
        c._reader = reader
        reader._clients[u] = c
        c._registered = True
        clients.append(c)
    return reader, clients


def test_get_stats_names_the_device_own_errors_apart_from_the_bus():
    reader, (c1, c2) = _bus(2)
    reader.crc_errors, reader._framer.dropped_bytes = 3, 1234
    c1.implausible, c1.implausible_windows = 2, 1
    st = c1.get_stats()
    assert st["device_error_counts"] == {"implausible": 2, "implausible_windows": 1}
    assert st["error_counts"]["crc_errors"] == 3          # kept: the REST shape every reader has
    assert c2.get_stats()["device_error_counts"] == {"implausible": 0, "implausible_windows": 0}


def test_unique_buses_counts_a_reader_once():
    reader, clients = _bus()
    reader.crc_errors = 3
    stats = [c.get_stats() for c in clients] + [_stats(ok=5), None]
    rows = unique_buses(stats)
    assert len(rows) == 1 and rows[0]["crc_errors"] == 3


def test_mbg_counts_take_a_tapped_bus_crc_once_not_per_device():
    reader, clients = _bus()
    reader.crc_errors, reader.exceptions = 3, 2
    c = bd.mbg_counts([cl.get_stats() for cl in clients])
    assert c["crc_deduped"] == 3 and c["exceptions_deduped"] == 2
    assert c["tap_buses"] == 1 and c["tap_devices"] == N and c["devices"] == N
    # the legacy per-device sums stay as they were (documented: N × the bus's)
    assert c["crc"] == 3 * N


def test_mbg_counts_polled_devices_still_sum_their_own_transactions():
    reader, clients = _bus(2)
    reader.crc_errors = 4
    polled = {"successful_reads": 9, "error_counts": {"crc_errors": 2, "exception_2": 1}}
    c = bd.mbg_counts([clients[0].get_stats(), clients[1].get_stats(), polled, dict(polled)])
    assert c["crc_deduped"] == 4 + 2 + 2
    assert c["exceptions_deduped"] == 2
    assert c["tap_buses"] == 1 and c["tap_devices"] == 2


def _tap_devices(clients, host="192.0.2.9", port=7001):
    return [SimpleNamespace(id=c.device_id, bridge="pi", enabled=True,
                            connection=SimpleNamespace(host=host, port=port)) for c in clients]


def test_the_diagnosis_reads_a_tapped_bus_crc_once():
    """3 CRC errors on a bus of 8 taps over the window: the evidence says 3."""
    br, clock = Bridge(), [1000.0]
    reader, clients = _bus()
    devs = _tap_devices(clients)
    d, _mq, _ix = _diag(br, clock, devices=devs, pairs=list(zip(devs, clients)))
    d.poll_once()
    clock[0] = 1060.0
    reader.crc_errors = 3
    br.adapters = [_adapter(frame=2, rx=500, tx=400, connects=1)]
    d.poll_once()
    v = d.port_view("pi", 7001)
    assert v["mbg"]["crc_deduped"] == 3
    assert v["diagnosis"]["evidence"]["garbled"] == 3, v["diagnosis"]
    assert v["diagnosis"]["code"] == "wiring"


def test_port_view_shows_the_tapped_bus_once_and_each_unit_its_own():
    br, clock = Bridge(), [1000.0]
    reader, clients = _bus()
    reader.crc_errors, reader.orphans = 3, 1
    reader.unpaired_responses, reader.paired_across_junk = 4, 2
    # a real junk run: 12 bytes of noise, then a good exchange
    reader._ingest_bytes(b"\x00" * 12 + req_read(1, 3, 0, 1) + resp_read(1, 3, [1]), now=0.0)
    clients[2].implausible, clients[2].implausible_windows = 5, 1
    devs = _tap_devices(clients)
    d, _mq, _ix = _diag(br, clock, devices=devs, pairs=list(zip(devs, clients)))
    d.poll_once()
    tb = d.port_view("pi", 7001)["tap_bus"]
    assert tb["crc_errors"] == 3 and tb["orphan_frames"] == 1
    assert tb["unpaired_responses"] == 4 and tb["paired_across_junk"] == 2
    assert tb["resync_dropped_bytes"] == 12 and tb["frames"] == 2
    assert len(tb["junk_samples"]) == 1 and tb["junk_samples"][0]["bytes"] == 12
    assert tb["junk_samples"][0]["hex"].startswith("0000")
    assert [x["id"] for x in tb["devices"]] == [f"seplos-tap-p{u}" for u in range(1, N + 1)]
    p3 = tb["devices"][2]
    assert p3["implausible"] == 5 and p3["implausible_windows"] == 1
    assert "crc_errors" not in p3                        # a bus counter is never a unit's
    assert all(x["implausible"] == 0 for i, x in enumerate(tb["devices"]) if i != 2)


def test_a_polled_bus_has_no_tap_view():
    br, clock = Bridge(), [1000.0]
    dev = SimpleNamespace(id="m1", bridge="pi", enabled=True,
                          connection=SimpleNamespace(host="192.0.2.9", port=7001))
    d, _mq, _ix = _diag(br, clock, devices=[dev], pairs=[(dev, FakeClient(_stats(ok=10)))])
    d.poll_once()
    v = d.port_view("pi", 7001)
    assert v["tap_bus"] is None
    assert v["mbg"]["crc_deduped"] == 0 and v["mbg"]["ok"] == 0


def test_status_rows_carry_a_tap_unit_own_errors(tmp_path, monkeypatch):
    """The Status page lists units: a tap's row says its own errors in
    ``device_error_counts`` (the bus's stay in ``error_counts``, unchanged)."""
    import pytest
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient
    from multibus.api import create_api
    from tests.test_devices import write_config
    reader, (tapc,) = _bus(1)
    reader.crc_errors = 7
    tapc.implausible = 2

    class _Tap(SimpleNamespace):
        def get_stats(self):
            return tapc.get_stats()

        def data_health(self, *a):
            return {"status": "ok"}

    cfg = write_config(tmp_path)
    fake = _Tap(publish_callback=None, connected=True)
    app, _ = create_api(cfg, fake, None, None, devices=[(d, fake) for d in cfg.devices])
    rows = TestClient(app, client=("127.0.0.1", 50000)).get("/api/status").json()["devices"]
    assert rows and all(r["device_error_counts"] == {"implausible": 2, "implausible_windows": 0}
                        for r in rows)
    assert all(r["error_counts"]["crc_errors"] == 7 for r in rows)
