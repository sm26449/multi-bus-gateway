# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""rtu_tap data integrity — the 7-day prod analysis (2026-11, ADR-0048).

Five times in a week a whole Seplos 0x1000 window was stored for the wrong
pack/offset (SOC 4098.9, pack 10.32 V, max cell 0.012 V) and reached the bank
aggregation. These tests hold the four fixes:

  TAP-PAIR-1   a response pairs only with the request that is the frame right
               before it on the bus, inside a 19200-baud-sized window;
  TAP-PLAUS-1  per-row valid_min/valid_max: an implausible value is dropped and
               counted, a mostly-implausible window is discarded whole;
  TAP-METRIC-1 bus counters are exported once per bus, not once per device;
  TAP-DIAG-1   a ring of the discarded byte runs; a time-based stats log line.
"""
import json
import logging
from types import SimpleNamespace

from multibus.rtu_tap import RtuFramer
from tests.test_rtu_tap import make_pair, reg, req_read, resp_read

TPL_PATH = 'multibus/device_templates/seplos_bms_v3_rtu_tap.json'


def _collect(client):
    got = {}
    client.publish_callback = lambda g, data: got.update(
        {data[a]['register'].name: data[a]['value'] for a in data})
    return got


# ── TAP-PAIR-1 — strict request/response pairing ──────────────────────────

def test_a_frame_in_between_unpairs_the_old_request():
    """unit 7's PIA request goes unanswered, the master asks unit 8, then a
    7-shaped response shows up: it must NOT be stored at 7's old address."""
    r, c = make_pair('/pair-a', unit=7, registers=[reg(0x1000, 'v', register_type='input')])
    got = _collect(c)
    r._ingest_bytes(req_read(7, 4, 0x1000, 2), now=0.00)
    r._ingest_bytes(req_read(8, 4, 0x1100, 2), now=0.05)
    r._ingest_bytes(resp_read(7, 4, [1, 2]), now=0.10)
    assert got == {} and c.windows == 0
    assert r.orphans == 1 and r.unpaired_responses == 1


def test_a_lost_request_does_not_pair_the_response_with_an_older_one():
    """the request that WOULD have paired was garbled (resync drops it) after
    the master waited out its timeout on the previous one: the response must
    not fall back to that older request of the same shape — the window
    refuses it."""
    r, c = make_pair('/pair-a2', unit=7, registers=[reg(0x1000, 'v', register_type='input')])
    got = _collect(c)
    r._ingest_bytes(req_read(7, 4, 0x1000, 2), now=0.00)       # its response never came
    bad = bytearray(req_read(7, 4, 0x1100, 2))
    bad[3] ^= 0x5A                                              # a corrupted request
    r._ingest_bytes(bytes(bad) + resp_read(7, 4, [4098, 9]), now=0.50)
    r._ingest_bytes(b'', now=0.55)                              # bus silence: resync
    assert got == {} and c.windows == 0
    assert r.orphans == 1 and r.unpaired_responses == 1
    assert r._framer.dropped_bytes > 0


def test_junk_in_the_gap_does_not_break_a_valid_pair():
    """line-turnaround junk between a request and its response (~7 B per
    frame on prod): still paired — the request is the last complete frame —
    and counted, so the rate can be watched."""
    r, c = make_pair('/pair-j', unit=7, registers=[reg(0x1000, 'v', register_type='input')])
    got = _collect(c)
    r._ingest_bytes(req_read(7, 4, 0x1000, 1), now=0.00)
    r._ingest_bytes(b'\xff\xfe\x00' + resp_read(7, 4, [321]), now=0.05)
    r._ingest_bytes(b'', now=0.10)
    assert r._framer.dropped_bytes == 3
    assert got == {'v': 321} and c.windows == 1
    assert r.paired_across_junk == 1 and r.unpaired_responses == 0 and r.orphans == 0
    st = c.get_stats()['bus']
    assert st['paired_across_junk'] == 1 and st['unpaired_responses'] == 0


def test_a_normal_exchange_still_decodes_from_a_byte_stream():
    r, c = make_pair('/pair-b', unit=3, registers=[reg(0x1000, 'v', register_type='input')])
    got = _collect(c)
    r._ingest_bytes(req_read(3, 4, 0x1000, 1) + resp_read(3, 4, [321]), now=0.0)
    assert got == {'v': 321} and r.orphans == 0
    # and again, request and response in successive read chunks
    r._ingest_bytes(req_read(3, 4, 0x1000, 1), now=1.00)
    r._ingest_bytes(resp_read(3, 4, [322]), now=1.05)
    assert got == {'v': 322} and c.windows == 2


def test_a_response_after_the_window_is_an_orphan():
    from multibus.transports.modbus import rtu_tap
    assert 0.1 <= rtu_tap._RESPONSE_WINDOW_S <= 0.3
    r, c = make_pair('/pair-c', unit=1, registers=[reg(10, 'v')])
    got = _collect(c)
    r._ingest_bytes(req_read(1, 3, 10, 1), now=0.0)
    r._ingest_bytes(resp_read(1, 3, [9]), now=0.5)     # paired before: inside 1 s
    assert got == {} and r.orphans == 1


def test_a_unit_that_is_asked_is_never_inferred():
    """inference is for the bus master (nobody asks it anything). A unit we
    have seen asked whose response is unpaired lost its request — orphan."""
    r, c = make_pair('/pair-d', unit=1, registers=[reg(0x1000, 'v', register_type='input')])
    got = _collect(c)
    r._ingest_bytes(req_read(2, 4, 0x1000, 1) + resp_read(2, 4, [111]), now=0.0)  # teaches the shape
    r._ingest_bytes(resp_read(1, 4, [222]), now=1.0)                             # master: inferred
    assert got == {'v': 222} and r.inferred == 1
    r2, c2 = make_pair('/pair-d2', unit=7, registers=[reg(0x1000, 'v', register_type='input')])
    got2 = _collect(c2)
    r2._ingest_bytes(req_read(2, 4, 0x1000, 1) + resp_read(2, 4, [111]), now=0.0)
    r2._ingest_bytes(req_read(7, 4, 0x1000, 1) + resp_read(7, 4, [5]), now=0.5)  # 7 is a slave
    got2.clear()
    r2._ingest_bytes(resp_read(7, 4, [4098]), now=1.0)      # its request was lost
    assert got2 == {} and r2.inferred == 0 and r2.orphans == 1


def test_no_inference_right_after_dropped_bytes():
    r, c = make_pair('/pair-e', unit=1, registers=[reg(0x1000, 'v', register_type='input')])
    got = _collect(c)
    r._ingest_bytes(req_read(2, 4, 0x1000, 1) + resp_read(2, 4, [111]), now=0.0)
    r._ingest_bytes(b'\x00\x00\x00\x00\x00\x00\x00\x00\x00' + resp_read(1, 4, [222]), now=1.0)
    r._ingest_bytes(b'', now=1.05)                     # silence: the junk head slides out
    assert r._framer.dropped_bytes == 9
    assert got == {} and r.inferred == 0 and r.orphans == 1


# ── TAP-PLAUS-1 — per-row plausibility ────────────────────────────────────

def test_apply_corrections_drops_a_value_outside_valid_range():
    from multibus.codec.value_decode import apply_corrections
    row = SimpleNamespace(scale=10.0, offset=0.0, valid_min=0.0, valid_max=100.0)
    info = {}
    assert apply_corrections(40989, row, info=info) is None
    assert info['stage'] == 'implausible'
    assert apply_corrections(345, row) == 34.5
    assert apply_corrections(1000, row) == 100.0                     # bounds inclusive
    assert apply_corrections(40989, SimpleNamespace(scale=10.0)) == 4098.9   # undeclared: unchanged
    assert apply_corrections(-5, {'valid_min': 0}) is None              # dict rows too


def test_template_rows_carry_valid_range_and_validate_it():
    from multibus.catalog.device_template import parse_template, validate_template
    data = json.load(open(TPL_PATH))
    tpl = parse_template(data, builtin=True)
    soc = next(r for r in tpl.registers if r.name == 'soc')
    assert (soc.valid_min, soc.valid_max) == (0.0, 100.0)
    assert soc.to_dict()['valid_min'] == 0.0 and soc.to_dict()['valid_max'] == 100.0
    bad = json.loads(json.dumps(data))
    row = next(r for r in bad['device_template']['registers'] if r['name'] == 'soc')
    row['valid_min'], row['valid_max'] = 100, 0
    assert any('valid_min' in e for e in validate_template(bad))
    row['valid_min'], row['valid_max'] = 'x', None
    assert any('valid_min' in e for e in validate_template(bad))


def test_seplos_tap_template_guards_its_telemetry_and_is_bumped():
    t = json.load(open(TPL_PATH))['device_template']
    assert tuple(int(x) for x in t['version'].split('.')) > (1, 9, 0)
    rows = {r['name']: r for r in t['registers']}
    for n in ('soc', 'soh'):
        assert (rows[n]['valid_min'], rows[n]['valid_max']) == (0, 100)
    for n in ('average_cell_voltage', 'max_cell_voltage', 'min_cell_voltage'):
        assert (rows[n]['valid_min'], rows[n]['valid_max']) == (1.5, 4.5)
    for n in ('average_cell_temp', 'max_cell_temp', 'min_cell_temp', 'mosfet_temp', 'cell_temp_1'):
        assert (rows[n]['valid_min'], rows[n]['valid_max']) == (-40, 100)
    assert rows['pack_voltage']['valid_min'] > 10.32
    assert 'valid_min' in rows['current'] and 'valid_max' in rows['cell_16']
    # coils / status rows carry no range
    assert all('valid_min' not in r for r in t['registers'] if r.get('register_type') == 'coil')


def _template_regs():
    t = json.load(open(TPL_PATH))['device_template']
    return [reg(x['address'], x['name'], x.get('data_type', 'uint16'),
                scale=x.get('scale', 1.0), offset=x.get('offset', 0.0),
                unit=x.get('unit', ''), register_type=x.get('register_type', 'holding'),
                valid_min=x.get('valid_min'), valid_max=x.get('valid_max'))
            for x in t['registers']]


GOOD_PIA = [5229, 65536 - 1234, 7730, 28000, 1234, 345, 1000, 42,
            3268, 2946, 3270, 3261, 2950, 2940, 0, 180, 80, 0]


def test_one_implausible_row_is_dropped_and_counted():
    r, c = make_pair('/plaus-a', unit=1, registers=_template_regs())
    got = _collect(c)
    words = list(GOOD_PIA)
    words[5] = 40989                                       # SOC 4098.9 %
    r._ingest_bytes(req_read(1, 4, 0x1000, 18) + resp_read(1, 4, words), now=0.0)
    assert 'soc' not in got and got['pack_voltage'] == 52.29
    st = c.get_stats()
    assert st['error_counts']['implausible'] == 1
    assert st['error_counts']['implausible_windows'] == 0
    assert c.windows == 1


def test_the_prod_mis_paired_window_is_discarded_whole():
    """battery_7, prod: SOC 2560.3, pack 10.32 V, max cell 0.012 V — a window
    of somebody else's bytes. Nothing of it is published, it is counted once,
    and its raw values do not leak into the scale-factor siblings."""
    r, c = make_pair('/plaus-b', unit=7, registers=_template_regs())
    got = _collect(c)
    junk = [1032, 4455, 29197, 54784, 1234, 25603, 44557, 42,
            29197, 16496, 12, 55040, 16496, 16496, 0, 180, 80, 0]
    r._ingest_bytes(req_read(7, 4, 0x1000, 18) + resp_read(7, 4, junk), now=0.0)
    assert got == {}
    assert c.windows == 0 and c.updates == 0
    st = c.get_stats()
    assert st['error_counts']['implausible_windows'] == 1
    assert 'soc' not in c._sibling_raw
    # the next good window decodes normally
    r._ingest_bytes(req_read(7, 4, 0x1000, 18) + resp_read(7, 4, GOOD_PIA), now=1.0)
    assert got['soc'] == 34.5 and c.windows == 1


def test_template_hygiene_brings_a_range_to_an_older_selection():
    from multibus.config import SelectedRegister
    from multibus.device_runtime import apply_template_hygiene
    sel = SelectedRegister(address=0x1005, name='soc', label='soc', unit='%',
                           data_type='uint16', poll_group='normal', register_type='input')
    tpl = SimpleNamespace(registers=[SimpleNamespace(
        name='soc', address=0x1005, register_type='input', monotonic=False, daily=False,
        valid_min=0.0, valid_max=100.0)])
    _, changed = apply_template_hygiene([sel], tpl)
    assert (sel.valid_min, sel.valid_max) == (0.0, 100.0)
    assert changed == ['soc:valid_range']
    # a selection that declares its own range keeps it
    own = SelectedRegister(address=0x1005, name='soc', label='soc', unit='%',
                           data_type='uint16', poll_group='normal', register_type='input',
                           valid_min=5.0, valid_max=95.0)
    apply_template_hygiene([own], tpl)
    assert (own.valid_min, own.valid_max) == (5.0, 95.0)


def test_a_selection_file_carries_valid_range():
    from multibus.config import Config
    out = Config._parse_selected_payload({'registers': [
        {'address': 1, 'name': 'soc', 'data_type': 'uint16', 'valid_min': 0, 'valid_max': 100},
        {'address': 2, 'name': 'x', 'data_type': 'uint16'}]})
    assert (out[0].valid_min, out[0].valid_max) == (0.0, 100.0)
    assert (out[1].valid_min, out[1].valid_max) == (None, None)


# ── TAP-METRIC-1 — bus counters once per bus ──────────────────────────────

def test_bus_stats_are_one_row_per_reader(monkeypatch):
    from multibus.transports.modbus import rtu_tap
    r1, _ = make_pair('/bus-x', unit=1)
    r1._ingest_bytes(b'\x00' * 12 + req_read(1, 3, 0, 1) + resp_read(1, 3, [1]), now=0.0)
    monkeypatch.setattr(rtu_tap, '_READERS', {'/bus-x': r1})
    rows = rtu_tap.bus_stats()
    assert len(rows) == 1
    row = rows[0]
    assert row['port'] == '/bus-x' and row['frames'] == 2
    assert row['resync_dropped_bytes'] == 12
    assert {'crc_errors', 'orphan_frames', 'exception_responses', 'inferred_windows'} <= set(row)


def test_metrics_export_bus_counters_once_not_per_device(tmp_path, monkeypatch):
    import pytest
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient
    from multibus.api import create_api
    from multibus.transports.modbus import rtu_tap
    from tests.test_devices import write_config
    reader, tapc = make_pair('/dev/ttySEPLOS-test', unit=1)
    reader._framer.dropped_bytes = 19770000
    reader.frames = 2680000
    tapc.implausible, tapc.implausible_windows = 2, 1

    class _Tap(SimpleNamespace):
        def get_stats(self):
            return tapc.get_stats()

        def data_health(self, *a):
            return {"status": "ok"}

    monkeypatch.setattr(rtu_tap, '_READERS', {'/dev/ttySEPLOS-test': reader})
    cfg = write_config(tmp_path)
    fake = _Tap(publish_callback=None)
    app, _ = create_api(cfg, fake, None, None, devices=[(d, fake) for d in cfg.devices])
    body = TestClient(app, client=("127.0.0.1", 50000)).get("/metrics").text
    assert body.count('gateway_bus_resync_dropped_bytes_total{bus="/dev/ttySEPLOS-test"} 19770000') == 1
    assert 'gateway_bus_frames_total{bus="/dev/ttySEPLOS-test"} 2680000' in body
    assert 'gateway_bus_paired_across_junk_total{bus="/dev/ttySEPLOS-test"} 0' in body
    assert 'gateway_bus_unpaired_responses_total{bus="/dev/ttySEPLOS-test"} 0' in body
    assert 'kind="resync_dropped_bytes"' not in body
    assert 'kind="orphan_frames"' not in body
    # per-device facts stay per device
    assert 'kind="implausible"} 2' in body and 'kind="implausible_windows"} 1' in body


def test_device_stats_keep_their_error_counts_and_carry_bus_counters():
    r, c = make_pair('/bus-y', unit=1, registers=[reg(0, 'x')])
    st = c.get_stats()
    for k in ('crc_errors', 'orphan_frames', 'exception_responses', 'resync_dropped_bytes'):
        assert k in st['error_counts']            # REST contract unchanged
        assert k in st['bus']                     # and named as the bus's own


# ── TAP-DIAG-1 — junk samples + time-based stats log ──────────────────────

def test_discarded_byte_runs_are_sampled_in_a_ring():
    fr = RtuFramer(baudrate=19200)
    good = req_read(1, 3, 0, 1)
    for i in range(10):
        fr.feed(bytes([0xA0 + i]) * 40 + good, now=float(i))
    s = list(fr.junk_samples)
    assert len(s) == 8                                       # ring of the last 8 runs
    assert s[-1]['bytes'] == 40
    assert s[-1]['hex'] == 'a9' * 32                         # first 32 bytes, hex
    r, c = make_pair('/diag', unit=1, registers=[reg(0, 'x')])
    r._ingest_bytes(b'\xde\xad\xbe\xef\x01' + good, now=0.0)
    js = c.get_stats()['bus']['junk_samples']
    assert js and js[-1]['hex'].startswith('deadbeef')


def test_the_stats_line_is_time_based(caplog):
    r, _ = make_pair('/statlog', unit=1)
    caplog.set_level(logging.INFO, logger='multibus.transports.modbus.rtu_tap')
    for i in range(3000):                                   # 3000 frames in ~30 s
        r._on_frame(req_read(1, 3, 0, 1), now=i * 0.01)
    lines = [m for m in caplog.messages if 'frames (crc_err' in m]
    assert lines == []
    r._on_frame(req_read(1, 3, 0, 1), now=601.0)            # ten minutes on
    lines = [m for m in caplog.messages if 'frames (crc_err' in m]
    assert len(lines) == 1
