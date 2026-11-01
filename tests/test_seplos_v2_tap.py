# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Seplos V2 inter-pack tap: the master pack's 0x5A summary exchange, heard.

The frames are the bench's: a 60 s passive capture of the inter-pack RS485
bus of a three-pack Seplos V2 bank (master + ADR 1 + ADR 2, 19200 Bd,
2026-10-26), one line per frame, ``<t> <frame>`` (the ``\\r`` stripped by
the capture tool). Nothing here ever opens a real port, except the PTY
replay, which opens a pseudo-terminal of its own."""
import ast
import json
import os
import pathlib
import threading
import time
from types import SimpleNamespace

import pytest

from multibus import seplos_v2 as sv
from multibus.seplos_v2 import CMD_PARALLEL_SUMMARY, CommandNotAllowed, SeplosV2Client, encode, parse
from tests.test_seplos_v2 import FakePack, conn, frame, reg

ROOT = pathlib.Path(__file__).resolve().parent.parent
CAPTURE = ROOT / "tests" / "fixtures" / "seplos_v2" / "ttySC0-interpack-20261026.capture"
TPL = ROOT / "multibus" / "device_templates" / "seplos_bms_v2_tap.json"


@pytest.fixture
def fake_port(monkeypatch):
    """Every bus opens the one fake in ``holder['port']``; registries start empty."""
    holder = {}

    def opener(c):
        if holder.get('port') is None:
            raise FileNotFoundError(f"could not open port {c.serial_port}")
        return holder['port']
    monkeypatch.setattr(sv, 'open_port', opener)
    monkeypatch.setattr(sv, '_BUSES', {})
    monkeypatch.setattr(sv, '_LISTENERS', {})
    return holder


def capture():
    """[(t, raw frame with its \\r)] in capture order."""
    out = []
    for line in CAPTURE.read_text().splitlines():
        t, f = line.split()
        out.append((float(t), f.encode('ascii') + b'\r'))
    return out


def answers():
    return [(t, parse(raw)) for t, raw in capture() if len(raw) > 20]


# ── the capture itself ──────────────────────────────────────────────────────

def test_the_capture_is_what_the_bench_logged():
    frames = capture()
    assert len(frames) == 240
    parsed = [parse(raw) for _t, raw in frames]                 # every frame passes every check
    assert {(f.adr, f.cid2, len(f.info)) for f in parsed} == {(1, 0x5A, 0), (1, 0x5A, 32),
                                                               (2, 0x5A, 0), (2, 0x5A, 32)}
    # 0x5A echoes the command in an ANSWER: CID2 alone cannot tell the two apart
    assert all(f.is_request for f in parsed)


# ── the decoder ─────────────────────────────────────────────────────────────

def test_the_first_answers_decode_to_the_bench_values():
    a = {f.adr: sv.decode_summary(f.info) for _t, f in answers()[:2]}
    one, two = a[1], a[2]
    assert (one['cell_max'], one['cell_min'], two['cell_max'], two['cell_min']) == (3274, 3269, 3278, 3241)
    assert (one['temp_max'] - 2731, one['temp_min'] - 2731) == (245, 237)
    assert (two['temp_max'] - 2731, two['temp_min'] - 2731) == (238, 227)
    assert (one['current'], two['current']) == (-47, -45)
    assert (one['voltage'], two['voltage']) == (5236, 5231)
    assert (one['remaining_capacity'], two['remaining_capacity']) == (9371, 5741)
    assert one['full_capacity'] == two['full_capacity'] == 28000
    assert (one['soc'], two['soc']) == (334, 205)
    assert (one['port_voltage'], two['port_voltage']) == (5241, 5237)
    assert one['head'] == two['head'] == 0x0001
    assert one["words"][11:] == [0, 0, 0, 0x0302, 0x8010]
    assert two["words"][11:] == [0, 0, 0, 0x0302, 0x8001]
    assert one['length'] == 32 and one['trailing'] == 0


def test_every_answer_obeys_the_physics_of_that_evening():
    """The cross-checks against the CAN link of the same bank (the 0x5A summary words)."""
    for _t, f in answers():
        d = sv.decode_summary(f.info)
        assert 3230 <= d['cell_min'] <= d['cell_max'] <= 3290            # CAN 0x370 range
        for k in ('temp_max', 'temp_min'):
            assert 200 <= d[k] - 2731 <= 260                            # about 24 °C
        assert d['temp_min'] <= d['temp_max']
        assert -50 <= d['current'] <= 400                                # −0.5…+4 A
        assert 5200 <= d['voltage'] <= 5260                              # about 52.3 V
        # 16 cells in series: the pack voltage lies between 16 × lowest and 16 × highest cell
        assert 16 * d['cell_min'] - 10 <= d['voltage'] * 10 <= 16 * d['cell_max'] + 10
        assert d['full_capacity'] == 28000                               # 3 × 280 Ah = 840 Ah
        # SOC is remaining ÷ full, truncated to 0.1 % (33.468 % → 33.4)
        assert d['soc'] == 1000 * d['remaining_capacity'] // d['full_capacity']
    # the bank SOC (CAN 0x355) was 29 %: the two slaves bracket it
    socs = {f.adr: sv.decode_summary(f.info)['soc'] for _t, f in answers()}
    assert socs[2] < 290 < socs[1]


def test_the_summary_lands_in_its_own_page_of_the_map():
    _t, f = answers()[0]
    w = sv.words_for(CMD_PARALLEL_SUMMARY, f.info)
    assert w[sv.MAP_SUM_CELL_MAX] == 3274 and w[sv.MAP_SUM_CELL_MIN] == 3269 and w[sv.MAP_SUM_CELL_DELTA] == 5
    assert w[sv.MAP_SUM_TEMP_MAX] == 245 and w[sv.MAP_SUM_TEMP_MIN] == 237
    assert w[sv.MAP_SUM_CURRENT] == (-47) & 0xFFFF
    assert w[sv.MAP_SUM_SOC] == 334 and w[sv.MAP_SUM_LENGTH] == 32
    assert w[sv.MAP_SUM_RAW + 15] == 0x8010
    assert all(sv.area_of(a) == CMD_PARALLEL_SUMMARY for a in w)


def test_only_the_bench_layout_is_decoded_by_name():
    """A guard (SV2-8): another length is never read by position."""
    _t, f = answers()[0]
    for info in (f.info[:19], f.info[:20], f.info + b'\x07', f.info + b'\x00\x00'):
        d = sv.decode_summary(info)
        assert d['problem'] and 'soc' not in d
        w = sv.summary_words(d)
        assert sv.MAP_SUM_SOC not in w and w[sv.MAP_SUM_LENGTH] == len(info)
    d = sv.decode_summary(f.info + b'\x07')
    assert d['trailing'] == 1 and len(d['words']) == 16
    na = bytearray(f.info)
    na[2:4] = b'\xff\xff'                                                # cell max "not available"
    w = sv.words_for(CMD_PARALLEL_SUMMARY, bytes(na))
    assert sv.MAP_SUM_CELL_MAX not in w and sv.MAP_SUM_CELL_DELTA not in w


# ── pairing in listen mode ──────────────────────────────────────────────────

def _listening(fake_port, adrs=(1, 2), regs=None):
    fake_port['port'] = FakePack()
    regs = regs or [reg(sv.MAP_SUM_SOC, 'soc', scale=10), reg(sv.MAP_SUM_VOLTAGE, 'pack_voltage', scale=100)]
    got = {a: [] for a in adrs}
    clients = {}
    for a in adrs:
        c = SeplosV2Client(conn(unit_id=a, mode='listen', baudrate=19200), regs, {}, device_id=f'p{a}')
        c.publish_callback = (lambda a: lambda g, d: got[a].append(
            (g, {v['register'].name: v['value'] for v in d.values()})))(a)
        c._listener._clients[a] = c                                    # attached without a thread
        clients[a] = c
    return clients, got, clients[adrs[0]]._listener


def test_the_capture_feeds_both_slaves_every_second(fake_port):
    clients, got, lst = _listening(fake_port)
    for t, raw in capture():
        lst.on_frame(raw, t)
    assert len(got[1]) == 60 and len(got[2]) == 60
    assert got[1][0] == ('tap', {'soc': 33.4, 'pack_voltage': 52.36})
    assert got[2][0] == ('tap', {'soc': 20.5, 'pack_voltage': 52.31})
    assert lst.orphans == 0 and lst.bad_frames == 0 and lst.commands_seen == 0
    for c in clients.values():
        s = c.get_stats()
        assert s['successful_reads'] == 60 and s['failed_reads'] == 0
        assert s['answers_by_command'] == {'0x5A': 60}
    assert fake_port['port'].written == []


def test_an_answer_is_taken_only_after_its_own_question(fake_port):
    clients, got, lst = _listening(fake_port)
    (_t0, q1), (_t1, a1), (_t2, q2), (_t3, a2) = capture()[:4]
    lst.on_frame(a1, 0.0)                         # its question was lost (a collision)
    lst.on_frame(q1, 1.0)
    lst.on_frame(a2, 1.1)                         # pack 2's answer to pack 1's question
    lst.on_frame(q2, 2.0)
    lst.on_frame(a2, 4.5)                         # outside the 2 s window
    assert got == {1: [], 2: []} and lst.orphans == 3
    lst.on_frame(q1, 5.0)
    lst.on_frame(a1, 5.1)
    assert len(got[1]) == 1 and lst.orphans == 3
    lst.on_frame(a1, 5.2)                         # the same answer twice: the question is spent
    assert len(got[1]) == 1 and lst.orphans == 4


def test_the_read_pairing_is_unchanged_next_to_the_summary(fake_port):
    from tests.test_seplos_v2 import CELLS16, TEMPS6, telemetry_info
    clients, got, lst = _listening(fake_port, regs=[reg(0x31, 'v42', scale=100), reg(sv.MAP_SUM_SOC, 'soc', scale=10)])
    (_t0, q1), (_t1, a1) = capture()[:2]
    lst.on_frame(sv.request(1, sv.CMD_TELEMETRY), 0.0)
    lst.on_frame(frame(1, 0, telemetry_info(CELLS16, TEMPS6, voltage=5300)), 0.1)
    lst.on_frame(q1, 0.5)
    lst.on_frame(a1, 0.6)
    assert [g[1] for g in got[1]] == [{'v42': 53.0}, {'soc': 33.4}]
    lst.on_frame(sv.request(1, sv.CMD_TELEMETRY), 1.0)
    lst.on_frame(frame(1, 0x02), 1.1)             # RTN 0x02 still counted as an error answer
    assert clients[1].stats.rtn_errors == 1 and lst.commands_seen == 0


def test_a_summary_rtn_error_is_counted(fake_port):
    clients, got, lst = _listening(fake_port)
    (_t0, q1), _ = capture()[:2]
    lst.on_frame(q1, 0.0)
    lst.on_frame(frame(1, 0x04), 0.1)              # "CID2 invalid", in the RTN position
    assert clients[1].stats.rtn_errors == 1 and clients[1].stats.last_rtn == 0x04 and got[1] == []


def test_garbage_collisions_and_torn_frames_on_the_line(fake_port):
    """The stream as the reader gets it: junk, torn frames, a corrupted
    checksum, a bad LCHKSUM, a frame cut off at the end — through the framer."""
    clients, got, lst = _listening(fake_port)
    (_t0, q1), (_t1, a1), (_t2, q2), (_t3, a2) = capture()[:4]
    bad_sum = a1.replace(b'~2001', b'~2002')      # pack 1's checksum on pack 2's head
    bad_len = a2[:9] + b'D' + a2[10:]             # LCHKSUM nibble C → D
    stream = (b'\x00\xff junk ' + q1 + a1                  # good pair after junk
              + q2 + a2[:30] + q1 + a1                     # pack 2's answer torn by the next frame
              + q2 + bad_sum                               # CHKSUM error: charged to pack 2, awaited
              + q1 + bad_len.replace(b'~2002', b'~2001')   # LCHKSUM error: charged to pack 1
              + b'\x11\x22' + q2 + a2                      # good pair after junk
              + q1 + a1[:40])                              # cut off: never completed
    for i in range(0, len(stream), 13):                    # in 13-byte reads, as a UART delivers
        for raw in lst.framer.feed(stream[i:i + 13]):
            lst.on_frame(raw, 1.0)
    assert len(got[1]) == 2 and len(got[2]) == 1
    assert clients[2].stats.checksum_errors == 1 and clients[1].stats.checksum_errors == 1
    assert lst.bad_frames == 2 and lst.orphans == 0
    assert lst.framer.dropped_bytes > 0
    assert fake_port['port'].written == []


def test_a_listener_running_on_its_thread_never_writes(fake_port):
    """The real reader thread over the whole capture, in tiny reads."""
    stream = b''.join(raw for _t, raw in capture())
    port = FakePack(stream=stream)
    port.write = lambda data: (_ for _ in ()).throw(AssertionError(f"wrote {data!r}"))
    fake_port['port'] = port
    got = []
    c = SeplosV2Client(conn(unit_id=2, mode='listen', baudrate=19200),
                       [reg(sv.MAP_SUM_SOC, 'soc', scale=10)], {}, device_id='p2')
    c.publish_callback = lambda g, d: got.append(d)
    assert c.connect()
    c.start_polling()                                      # listen: nothing to start
    deadline = time.time() + 10
    while time.time() < deadline and len(got) < 60:
        time.sleep(0.02)
    lst = c._listener
    c.disconnect()
    assert len(got) == 60 and lst.orphans == 0 and lst.bad_frames == 0 and port.closed


# ── nothing can transmit 0x5A, and listening has no transmit path ───────────

def test_0x5a_is_heard_never_encoded():
    assert CMD_PARALLEL_SUMMARY not in sv.READ_COMMANDS and CMD_PARALLEL_SUMMARY in sv.LISTEN_COMMANDS
    with pytest.raises(CommandNotAllowed):
        encode(1, CMD_PARALLEL_SUMMARY)
    with pytest.raises(CommandNotAllowed):
        sv.request(1, CMD_PARALLEL_SUMMARY)


def test_the_listener_has_no_transmit_path():
    """As the CAN lot's check: no call in _Listener that could put a byte on a wire."""
    tree = ast.parse((ROOT / "multibus" / "seplos_v2.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == '_Listener')
    calls = {n.func.attr for n in ast.walk(cls) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    names = {n.func.id for n in ast.walk(cls) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not calls & {'write', 'writelines', 'send', 'sendall', 'sendto', 'flush', 'reset_input_buffer',
                        'exchange', 'poll_once'}, calls
    assert not names & {'encode', 'request', 'probe'}, names


def test_a_polled_pack_with_only_summary_rows_asks_nothing(fake_port):
    """A tap map on a polled device (a YAML import that bypassed the API):
    0x5A is not in the poll plan, so nothing is ever sent."""
    port = FakePack(answer=lambda fr: (_ for _ in ()).throw(AssertionError("asked")))
    fake_port['port'] = port
    c = SeplosV2Client(conn(unit_id=1, mode='poll'), [reg(sv.MAP_SUM_SOC, 'soc', group='realtime')],
                       {'realtime': SimpleNamespace(interval=0.05)}, device_id='p1')
    assert [p[3] for p in c._plan()] == [[]]
    c.connect()
    c.start_polling()
    time.sleep(0.3)
    c.disconnect()
    assert port.written == []


# ── the template ────────────────────────────────────────────────────────────

def _tpl():
    return json.loads(TPL.read_text(encoding='utf-8'))


def _rows(t):
    from multibus.device_template import parse_template
    return [SimpleNamespace(address=r.address, name=r.name, label=r.name, unit=r.unit, data_type=r.data_type,
                            register_type=r.register_type, poll_group=r.poll_group, scale=r.scale,
                            offset=r.offset, enum=r.enum, bits=getattr(r, 'bits', None), mask=r.mask,
                            shift=getattr(r, 'shift', None), nan=getattr(r, 'nan', None),
                            monotonic=False, scale_from='')
            for r in parse_template(t, builtin=True).registers]


def test_the_template_is_valid_listen_only_and_of_the_seplos_family():
    from multibus.device_template import (TemplateRegistry, extra_errors, template_listens_only,
                                          template_transport, validate_template)
    data = _tpl()
    assert validate_template(data) == [] and extra_errors(data) == []
    reg_ = TemplateRegistry()
    t = reg_.get('seplos_bms_v2_tap')
    assert t is not None and 'seplos_bms_v2_tap.json' not in reg_.load_errors
    assert 'seplos_bms_v2_tap.json' not in reg_.load_warnings
    assert template_transport(t) == 'seplos_v2' and template_listens_only(t) and t.kind == 'battery'


def test_listen_only_is_only_for_a_virtual_map_bus():
    from multibus.device_template import validate_template
    data = _tpl()
    data['device_template']['protocol']['listen_only'] = 'yes'
    assert any('listen_only' in e for e in validate_template(data))
    data['device_template']['protocol'].update(listen_only=True, transports=['rtu'])
    assert any('listen_only' in e for e in validate_template(data))


def test_every_row_reads_the_summary_or_the_diagnostics_and_names_are_canonical():
    from multibus.canonical_fields import is_canonical
    tpl = _tpl()['device_template']
    for r in tpl['registers']:
        assert r['register_type'] == 'input' and r['poll_group'] == 'tap', r['name']
        assert sv.area_of(r['address']) in (CMD_PARALLEL_SUMMARY, 'diag'), (r['name'], hex(r['address']))
        # port voltage and the temperature extremes are ASSUMED: no canonical name claimed
        if r['defaults']['mqtt']['enabled'] and r['name'] not in ('port_voltage', 'temp_max', 'temp_min'):
            assert is_canonical(r['name']), r['name']
        assert 'verified' in r['description'].lower() or 'assumed' in r['description'].lower() \
            or r['category'] == 'diagnostics', r['name']
    for c in tpl['calculated']:
        assert is_canonical(c['name']), c['name']


def test_the_template_decodes_the_capture_end_to_end(fake_port):
    """The template's own rows and calculated fields, over the captured answers."""
    from multibus.device_template import parse_template
    from multibus.expressions import evaluate
    t = _tpl()
    regs = _rows(t)
    clients, got, lst = _listening(fake_port, regs=regs)
    for tt, raw in capture()[:4]:
        lst.on_frame(raw, tt)
    v1, v2 = got[1][0][1], got[2][0][1]
    assert v1['pack_voltage'] == 52.36 and v1['battery_current'] == -0.47 and v1['soc'] == 33.4
    assert v1['remaining_capacity'] == 93.71 and v1['total_capacity'] == 280.0
    assert v1['max_cell_voltage'] == 3.274 and v1['min_cell_voltage'] == 3.269
    assert v1['temp_max'] == 24.5 and v1['temp_min'] == 23.7 and v1['port_voltage'] == 52.41
    assert v1['summary_word_15'] == 0x8010 and v1['summary_length'] == 32
    assert v2['min_cell_voltage'] == 3.241 and v2['temp_min'] == 22.7 and v2['soc'] == 20.5
    assert v2['battery_current'] == -0.45
    # the calculated fields evaluate on those names
    calc = {c.name: c.expr for c in parse_template(t, builtin=True).calculated}
    assert round(evaluate(calc['battery_power'], v1.get), 1) == round(-0.47 * 52.36, 1)
    assert round(evaluate(calc['energy_remaining'], v1.get)) == round(93.71 * 52.36)
    assert round(evaluate(calc['cell_delta'], v2.get)) == 37


# ── a PTY replay: the real serial path, a pseudo-terminal for the bus ───────

@pytest.mark.skipif(not hasattr(os, 'openpty'), reason="no PTY on this platform")
def test_pty_replay_of_the_capture_through_the_listen_transport(monkeypatch):
    """The capture written into a PTY at 19200 Bd pacing; the gateway opens the
    other end with pyserial like a USB adapter, listens, and writes nothing."""
    pytest.importorskip('serial')
    monkeypatch.setattr(sv, '_BUSES', {})
    monkeypatch.setattr(sv, '_LISTENERS', {})
    master, slave = os.openpty()
    path = os.ttyname(slave)
    got = {1: 0, 2: 0}
    clients = []
    for a in (1, 2):
        c = SeplosV2Client(conn(serial_port=path, unit_id=a, mode='listen', baudrate=19200),
                           [reg(sv.MAP_SUM_SOC, 'soc', scale=10)], {}, device_id=f'pty{a}')
        c.publish_callback = (lambda a: lambda g, d: got.__setitem__(a, got[a] + 1))(a)
        clients.append(c)
    try:
        assert all(c.connect() for c in clients)
        frames = capture()
        stop = threading.Event()

        def writer():
            for _t, raw in frames:
                if stop.is_set():
                    return
                os.write(master, raw)
                time.sleep(len(raw) * 10 / 19200)      # the line's own pace
        th = threading.Thread(target=writer, daemon=True)
        th.start()
        deadline = time.time() + 20
        while time.time() < deadline and (got[1] < 60 or got[2] < 60):
            time.sleep(0.05)
        stop.set()
        th.join(timeout=5)
        lst = clients[0]._listener
        assert got == {1: 60, 2: 60}, (got, lst.orphans, lst.bad_frames)
        assert lst.orphans == 0 and lst.bad_frames == 0
        # nothing came back up the line: the master side reads no byte
        os.set_blocking(master, False)
        try:
            back = os.read(master, 4096)
        except BlockingIOError:
            back = b''
        assert back == b''
    finally:
        for c in clients:
            c.disconnect()
        os.close(master)
        os.close(slave)


# ── the API refuses polling with this template ──────────────────────────────

def test_the_api_refuses_a_polled_tap_and_keeps_a_listening_one(tmp_path, monkeypatch):
    pytest.importorskip('fastapi')
    from tests.test_devices_api import make_app

    def no_port(c):
        raise FileNotFoundError("no port")
    monkeypatch.setattr(sv, 'open_port', no_port)
    monkeypatch.setattr(sv, '_BUSES', {})
    monkeypatch.setattr(sv, '_LISTENERS', {})
    _cfg, client = make_app(tmp_path)

    def body(did, **c):
        cc = {"protocol": "seplos_v2", "serial_port": "/dev/ttySC0", "baudrate": 19200, "parity": "N",
              "unit_id": 1, "mode": "listen", "stale_after_s": 10}
        cc.update(c)
        return {"id": did, "enabled": False, "template": "seplos_bms_v2_tap", "connection": cc}
    r = client.post("/api/devices", json=body("pack-1", mode="poll"))
    assert r.status_code == 422 and "choose listen" in r.text, r.text
    r = client.post("/api/devices", json=body("pack-1"))
    assert r.status_code == 200, r.text
    r = client.post("/api/devices", json=body("pack-2", unit_id=2))
    assert r.status_code == 200, r.text
    from multibus.config import Config
    d = Config(str(tmp_path / "config.yaml")).get_device("pack-2")
    assert d.connection.mode == "listen" and d.connection.stale_after_s == 10   # freshness kept
    r = client.put("/api/devices/pack-2", json=body("pack-2", unit_id=2, mode="poll"))
    assert r.status_code == 422 and "choose listen" in r.text, r.text
