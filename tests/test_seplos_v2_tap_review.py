# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The independent review of the Seplos V2 inter-pack tap (findings SV2-6..14).

Each test failed on the branch before its fix. No real port is opened: the
transport is the in-memory FakePack, and pyserial is replaced where the
listen port's control lines are checked."""
import ast
import json
import pathlib
import time
from types import SimpleNamespace

import pytest

from multibus import seplos_v2 as sv
from multibus.seplos_v2 import CMD_PARALLEL_SUMMARY, SeplosV2Client
from tests.test_seplos_v2 import CELLS16, TEMPS6, FakePack, conn, frame, reg, telemetry_info
from tests.test_seplos_v2_tap import capture

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def fake_port(monkeypatch):
    holder = {}

    def opener(c):
        if holder.get('port') is None:
            raise FileNotFoundError("no port")
        return holder['port']
    monkeypatch.setattr(sv, 'open_port', opener)
    monkeypatch.setattr(sv, '_BUSES', {})
    monkeypatch.setattr(sv, '_LISTENERS', {})
    return holder


def _first_answer_info():
    return sv.parse(capture()[1][1]).info


def _attached(fake_port, regs, adr=1, **kw):
    fake_port.setdefault('port', FakePack())
    c = SeplosV2Client(conn(unit_id=adr, mode='listen', baudrate=19200, **kw), regs, {}, device_id=f'p{adr}')
    c.publish_callback = lambda g, d: None
    c._listener._clients[adr] = c
    c._registered = True
    c._listener._port = fake_port['port']
    return c


# ── SV2-6 (H1): the ad-hoc Test never transmits for a listen-only template ──

@pytest.mark.parametrize("extra", [
    {"protocol": "seplos_v2", "serial_port": "/dev/ttySC0", "baudrate": 19200, "parity": "N"},
    {"protocol": "seplos_v2-tcp", "host": "192.168.1.50", "port": 4196},
])
def test_sv2_6_the_adhoc_test_of_a_tap_template_sends_nothing(tmp_path, monkeypatch, extra):
    pytest.importorskip('fastapi')
    from tests.test_devices_api import make_app
    port = FakePack()
    monkeypatch.setattr(sv, 'open_port', lambda c: port)
    monkeypatch.setattr(sv, '_BUSES', {})
    monkeypatch.setattr(sv, '_LISTENERS', {})
    _cfg, client = make_app(tmp_path)
    for mode in ("poll", "listen", None):
        c = dict(extra, unit_id=1, timeout=0.2)
        if mode:
            c["mode"] = mode
        r = client.post("/api/devices/test", json={"template": "seplos_bms_v2_tap", "connection": c})
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is None and "listen" in r.json()["message"], r.text
    assert port.written == []


def test_sv2_6_the_test_of_a_saved_tap_device_sends_nothing(tmp_path, monkeypatch):
    pytest.importorskip('fastapi')
    from tests.test_devices_api import make_app
    port = FakePack()
    monkeypatch.setattr(sv, 'open_port', lambda c: port)
    monkeypatch.setattr(sv, '_BUSES', {})
    monkeypatch.setattr(sv, '_LISTENERS', {})
    _cfg, client = make_app(tmp_path)
    r = client.post("/api/devices", json={"id": "adr1", "enabled": False, "template": "seplos_bms_v2_tap",
                                          "connection": {"protocol": "seplos_v2", "serial_port": "/dev/ttySC0",
                                                         "baudrate": 19200, "unit_id": 1, "mode": "listen"}})
    assert r.status_code == 200, r.text
    # the form switched to poll, the template comes from the stored device
    r = client.post("/api/devices/test", json={"device_id": "adr1", "connection": {"mode": "poll", "timeout": 0.2}})
    assert r.status_code == 200 and r.json()["ok"] is None, r.text
    assert port.written == []


# ── SV2-7 (M1): freshness counts only the commands the map reads ────────────

def test_sv2_7_a_0x42_map_is_not_kept_fresh_by_0x5a(fake_port):
    c = _attached(fake_port, [reg(0x31, 'v42', scale=100)])
    lst = c._listener
    now = time.monotonic()
    for i, (_t, raw) in enumerate(capture()):
        lst.on_frame(raw, now + i * 0.001)
    st = c.get_stats()
    assert st['successful_reads'] == 0 and st['staleness_age_s'] is None
    assert c.data_health()['status'] == 'stale'


def test_sv2_7_a_mixed_master_does_not_hide_a_frozen_0x42(fake_port):
    c = _attached(fake_port, [reg(0x31, 'v42', scale=100), reg(sv.MAP_SUM_SOC, 'soc', scale=10)],
                  stale_after_s=10)
    lst = c._listener
    now = time.monotonic()
    lst.on_frame(sv.request(1, sv.CMD_TELEMETRY), now - 100)
    lst.on_frame(frame(1, 0, telemetry_info(CELLS16, TEMPS6)), now - 99.9)
    (_t0, q1), (_t1, a1) = capture()[:2]
    lst.on_frame(q1, now - 0.2)
    lst.on_frame(a1, now - 0.1)                    # 0x5A is fresh, 0x42 is 100 s old
    h = c.data_health()
    assert h['status'] == 'stale' and '0x42' in h['reason'], h


# ── SV2-8 (M2): only the bench's 32-byte layout is decoded by name ─────────

@pytest.mark.parametrize("mutate", [
    lambda b: bytes(range(40)),                                    # another length
    lambda b: b[:20],                                              # shorter
    lambda b: b'\x00\x02' + b[2:],                                 # another head
    lambda b: b[:2] + b'\x0c\xa0\x0c\xff' + b[6:],                 # cell min > cell max
    lambda b: b[:18] + b'\x04\xb0' + b[20:],                       # SOC 120 %
    lambda b: b[:14] + b'\x7f\xff' + b[16:],                       # remaining > full
    lambda b: b[:6] + b'\x10\x00' + b[8:],                         # 136.9 °C
])
def test_sv2_8_an_unknown_or_implausible_summary_publishes_only_raw_words(fake_port, mutate):
    info = mutate(_first_answer_info())
    d = sv.decode_summary(info)
    w = sv.summary_words(d)
    assert d['problem']
    for a in (sv.MAP_SUM_SOC, sv.MAP_SUM_VOLTAGE, sv.MAP_SUM_CURRENT, sv.MAP_SUM_CELL_MAX,
              sv.MAP_SUM_TEMP_MAX, sv.MAP_SUM_REMAINING):
        assert a not in w, hex(a)
    assert w[sv.MAP_SUM_LENGTH] == len(info) and w[sv.MAP_SUM_RAW] == (info[0] << 8) | info[1]
    # through the client: counted as a decode error, not as a good answer
    c = _attached(fake_port, [reg(sv.MAP_SUM_SOC, 'soc', scale=10), reg(sv.MAP_SUM_LENGTH, 'summary_length')])
    got = []
    c.publish_callback = lambda g, dd: got.append({v['register'].name: v['value'] for v in dd.values()})
    c._listener.on_frame(capture()[0][1], 1.0)
    c._listener.on_frame(frame(1, CMD_PARALLEL_SUMMARY, info), 1.1)
    assert c.stats.decode_errors == 1 and c.stats.good == 0
    assert got and 'soc' not in got[-1] and got[-1]['summary_length'] == len(info)


def test_sv2_8_the_bench_layout_still_decodes():
    d = sv.decode_summary(_first_answer_info())
    assert not d['problem'] and sv.summary_words(d)[sv.MAP_SUM_SOC] == 334


# ── SV2-9 (M3): a tap map listens whatever path the config took ─────────────

def test_sv2_9_the_runtime_makes_a_tap_template_listen():
    from multibus.config import ModbusConfig
    from multibus.device_runtime import driver_for
    from multibus.device_template import TemplateRegistry
    tr = TemplateRegistry()
    for mode in ('poll', ''):
        cfg = ModbusConfig(protocol='seplos_v2', serial_port='/dev/ttyNOPE', unit_id=1, mode=mode)
        src = SimpleNamespace(protocol='seplos_v2', template='seplos_bms_v2_tap', id='default', connection=cfg)
        dev = SimpleNamespace(id='adr1', template='seplos_bms_v2_tap', sources=[src], component_of='')
        drv = driver_for(None, tr, dev, src, [], {}, False)
        assert drv.mode == 'listen' and drv._bus is None and drv._listener is not None
        assert cfg.mode == mode                    # the stored config is not rewritten


def test_sv2_9_a_client_whose_rows_are_all_summary_listens(fake_port):
    c = SeplosV2Client(conn(unit_id=1, mode='poll'),
                       [reg(sv.MAP_SUM_SOC, 'soc'), reg(sv.MAP_DIAG_GOOD, 'good')], {}, device_id='p1')
    assert c.mode == 'listen' and c._bus is None
    c2 = SeplosV2Client(conn(unit_id=2, mode='poll'), [reg(0x31, 'v'), reg(sv.MAP_SUM_SOC, 'soc')], {},
                        device_id='p2')
    assert c2.mode == 'poll'                       # a 0x42 row: polling stays the operator's choice


# ── SV2-10 (L1/L2): unpaired summaries are said, not only counted ──────────

def test_sv2_10_answers_heard_but_never_paired_raise_a_warning(fake_port):
    c = _attached(fake_port, [reg(sv.MAP_SUM_SOC, 'soc')])
    lst = c._listener
    a1 = capture()[1][1]
    for i in range(5):
        lst.on_frame(a1, float(i))                 # no question heard (or one sent with INFO)
    st = c.get_stats()
    assert st['bus']['summary_unpaired'] == 5
    assert '0x5A' in st['bus']['warning'] and st['successful_reads'] == 0
    assert '0x5A' in c.data_health()['reason']


# ── SV2-11 (L3): impossible temperatures are absent ─────────────────────────

@pytest.mark.parametrize("word", [0x0000, 0x0001, 0x1000, 0xFFFF])
def test_sv2_11_a_zero_or_absurd_temperature_word_is_absent(word):
    assert sv._kelvin10(word) is None
    assert sv._kelvin10(2976) == 245 and sv._kelvin10(2731 - 200) == -200


# ── SV2-12 (L4): template names, freshness, cell delta ─────────────────────

def test_sv2_12_the_tap_template_claims_no_cell_sensor_and_clamps_the_delta():
    from multibus.expressions import evaluate
    t = json.loads((ROOT / "multibus/device_templates/seplos_bms_v2_tap.json").read_text())['device_template']
    names = {r['name'] for r in t['registers']} | {c['name'] for c in t['calculated']}
    assert not names & {'max_cell_temp', 'min_cell_temp'}
    delta = next(c['expr'] for c in t['calculated'] if c['name'] == 'cell_delta')
    assert evaluate(delta, {'max_cell_voltage': 3.20, 'min_cell_voltage': 3.25}.get) == 0
    assert round(evaluate(delta, {'max_cell_voltage': 3.278, 'min_cell_voltage': 3.241}.get)) == 37
    assert any('stale_after_s' in n['text'] for n in t['notes'])


# ── SV2-13 (L5): a listen port never raises RTS/DTR; no transmit on the path ─

def test_sv2_13_a_listen_port_opens_with_rts_and_dtr_low(monkeypatch):
    import serial
    seen = []

    class FakeSerial:
        def __init__(self, *a, **kw):
            seen.append(('init', dict(kw)))
            self.is_open = bool(kw.get('port'))

        def __setattr__(self, k, v):
            if k in ('rts', 'dtr', 'port'):
                seen.append((k, v, self.__dict__.get('is_open')))
            object.__setattr__(self, k, v)

        def open(self):
            seen.append(('open',))
            object.__setattr__(self, 'is_open', True)

    monkeypatch.setattr(serial, 'Serial', FakeSerial)
    sv.open_port(conn(mode='listen', serial_port='/dev/ttySC0'))
    kinds = [s[0] for s in seen]
    assert 'open' in kinds and seen[0][1].get('port') is None, seen   # not opened by the constructor
    i_open = kinds.index('open')
    assert ('rts', False, False) in seen[:i_open] and ('dtr', False, False) in seen[:i_open], seen


_FORBIDDEN_ATTR = {'write', 'writelines', 'send', 'sendall', 'sendto', 'sendmsg', 'flush',
                   'reset_input_buffer', 'exchange', 'poll_once'}
_FORBIDDEN_NAME = {'encode', 'request', 'probe', 'exec', 'eval', '__import__'}


def test_sv2_13_nothing_reachable_from_the_listener_can_transmit():
    """Every function the listener can reach in the module (by name, by
    self./cl. method, by constructor), walked transitively; and no dynamic
    getattr dispatch on that path."""
    tree = ast.parse((ROOT / "multibus" / "seplos_v2.py").read_text())
    funcs, classes = {}, {}
    for n in tree.body:
        if isinstance(n, ast.FunctionDef):
            funcs[n.name] = n
        elif isinstance(n, ast.ClassDef):
            classes[n.name] = {m.name: m for m in n.body if isinstance(m, ast.FunctionDef)}
    client_methods = classes['SeplosV2Client']
    todo = list(classes['_Listener'].values())
    seen, attrs, names, dyn = set(), set(), set(), []
    while todo:
        f = todo.pop()
        if id(f) in seen:
            continue
        seen.add(id(f))
        for n in ast.walk(f):
            if not isinstance(n, ast.Call):
                continue
            if isinstance(n.func, ast.Attribute):
                attrs.add(n.func.attr)
                for cls in (classes['_Listener'], client_methods):
                    if n.func.attr in cls:
                        todo.append(cls[n.func.attr])
            elif isinstance(n.func, ast.Name):
                names.add(n.func.id)
                if n.func.id in funcs:
                    todo.append(funcs[n.func.id])
                if n.func.id in classes and '__init__' in classes[n.func.id]:
                    todo.append(classes[n.func.id]['__init__'])
                if n.func.id == 'getattr' and len(n.args) > 1 and not isinstance(n.args[1], ast.Constant):
                    dyn.append(ast.unparse(n))
    assert len(seen) > 15                                 # the walk did reach the helpers
    assert not attrs & _FORBIDDEN_ATTR, attrs & _FORBIDDEN_ATTR
    assert not names & _FORBIDDEN_NAME, names & _FORBIDDEN_NAME
    assert not dyn, dyn


# ── SV2-14 (L6): reopen starts clean; one listener, one device per ADR ──────

def test_sv2_14_a_reopen_drops_half_a_frame_and_the_pending_question(fake_port):
    c = _attached(fake_port, [reg(sv.MAP_SUM_SOC, 'soc')])
    lst = c._listener
    (_t0, q1), (_t1, a1) = capture()[:2]
    lst.on_frame(q1, 1.0)
    lst.framer.feed(a1[:30])
    lst._close()
    assert lst._open()
    assert lst._pending == {} and lst.framer.feed(a1[30:]) == []


def test_sv2_14_two_devices_on_one_adr_the_second_is_refused_and_leaves_the_first(fake_port):
    fake_port['port'] = FakePack()
    a = SeplosV2Client(conn(unit_id=1, mode='listen'), [reg(sv.MAP_SUM_SOC, 'soc')], {}, device_id='a')
    b = SeplosV2Client(conn(unit_id=1, mode='listen'), [reg(sv.MAP_SUM_SOC, 'soc')], {}, device_id='b')
    try:
        assert a.connect()
        assert not b.connect()
        h = b.data_health()
        assert h['status'] == 'down' and "'a'" in h['reason']
        b.disconnect()
        assert a._listener._clients.get(1) is a and a._listener._thread is not None
    finally:
        a.disconnect()
        b.disconnect()
