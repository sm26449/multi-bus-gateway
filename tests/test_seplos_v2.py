# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""seplos_v2 — the Seplos V2 ASCII protocol, read-only, into a fixed map.

Golden frames are built here from the published layout (V2.0 document and the
community decoders); none comes from a real pack yet — the bench will add
captured ones. No serial port is opened: the transport is replaced by an
in-memory fake that answers like a pack and records every byte written."""
import threading
import time
from types import SimpleNamespace

import pytest

from multibus import seplos_v2 as sv
from multibus.seplos_v2 import (CMD_ALARMS, CMD_MANUFACTURER, CMD_TELEMETRY, CommandNotAllowed,
                                Framer, SeplosV2Client, chksum, encode, lchksum, parse)


# ── helpers: an answer frame, built outside the module (it has no such encoder) ──

def frame(adr, cid2, info=b'', ver=0x20, cid1=0x46):
    info_hex = info.hex().upper().encode()
    n = len(info_hex)
    body = (f'{ver:02X}{adr:02X}{cid1:02X}{cid2:02X}'.encode()
            + f'{(lchksum(n) << 12) | n:04X}'.encode() + info_hex)
    return b'~' + body + f'{chksum(body):04X}'.encode() + b'\r'


def w16(v):
    v &= 0xFFFF
    return bytes([v >> 8, v & 0xFF])


def telemetry_info(cells, temps_c10, current=-1234, voltage=5223, remaining=2745, full=3900,
                   soc=946, rated=20000, cycles=70, soh=1000, port=5223, pack=1, custom=10):
    b = bytes([0x00, pack, len(cells)]) + b''.join(w16(c) for c in cells)
    b += bytes([len(temps_c10)]) + b''.join(w16(t + 2731) for t in temps_c10)
    b += w16(current) + w16(voltage) + w16(remaining) + bytes([custom])
    words = [full, soc, rated, cycles, soh, port, 0, 0, 0, 0][:custom]
    return b + b''.join(w16(x) for x in words)


CELLS16 = [3287, 3290, 3301, 3295, 3288, 3299, 3300, 3297, 3292, 3291, 3289, 3296, 3298, 3294, 3293, 3305]
TEMPS6 = [251, 249, 250, 252, 230, 287]          # 4 cells, ambient, power — 0.1 °C


def alarm_info(n=16, m=6, cell_alarm=None, temp_alarm=None, k=2, custom=None, pack=1):
    ca = list(cell_alarm or [0] * n)
    ta = list(temp_alarm or [0] * m)
    cu = list(custom if custom is not None else [0] * 20)
    return (bytes([0x00, pack, n]) + bytes(ca) + bytes([m]) + bytes(ta)
            + bytes([0] * k) + bytes([len(cu)]) + bytes(cu))


def reg(address, name, data_type='uint16', group='realtime', **kw):
    d = {'address': address, 'name': name, 'label': name, 'unit': '', 'data_type': data_type,
         'register_type': 'input', 'poll_group': group, 'scale': 1.0, 'offset': 0.0, 'nan': None,
         'monotonic': False, 'enum': None, 'bits': None, 'mask': None, 'shift': None, 'scale_from': ''}
    d.update(kw)
    return SimpleNamespace(**d)


def conn(**kw):
    c = dict(protocol='seplos_v2', serial_port='/dev/fake-seplos', host='', port=0, baudrate=9600,
             parity='N', stopbits=1, bytesize=8, unit_id=1, timeout=0.3, retry_attempts=1,
             retry_delay=0, stale_after_s=30, mode='poll')
    c.update(kw)
    return SimpleNamespace(**c)


class FakePack:
    """A port: what is written is recorded; each request is answered by
    ``answer(request_frame) -> bytes`` (b'' = silence)."""

    def __init__(self, answer=None, stream=b''):
        self.answer = answer or (lambda fr: b'')
        self.written = []
        self._out = bytearray(stream)
        self.closed = False
        self.lock = threading.Lock()

    def write(self, data):
        self.written.append(bytes(data))
        with self.lock:
            self._out.extend(self.answer(parse(bytes(data))))

    def read(self, n):
        with self.lock:
            chunk = bytes(self._out[:7])          # tiny chunks: the framer must reassemble
            del self._out[:7]
        if not chunk:
            time.sleep(0.005)
        return chunk

    def feed(self, data):
        with self.lock:
            self._out.extend(data)

    def reset_input_buffer(self):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def fake_port(monkeypatch):
    """Every bus opens this one fake; registries start empty."""
    holder = {}

    def opener(c):
        p = holder.get('port')
        if p is None:
            raise FileNotFoundError(f"could not open port {c.serial_port}")
        return p
    monkeypatch.setattr(sv, 'open_port', opener)
    monkeypatch.setattr(sv, '_BUSES', {})
    monkeypatch.setattr(sv, '_LISTENERS', {})
    return holder


# ── checksums and the encoder ───────────────────────────────────────────────

def test_lchksum_vectors():
    assert lchksum(0) == 0
    assert lchksum(2) == 0xE            # the "E002" of every one-byte request
    assert lchksum(0x96) == 0x1          # 150 chars: 0+9+6=15 → 1
    assert lchksum(0x12) == 0xD
    assert lchksum(0xFFF) == ((~45 + 1) & 0xF)


def test_chksum_vectors():
    assert chksum(b'20004642E00200') == 0xFD37
    assert chksum(b'200046510000') == 0xFDAE


def test_encoder_reproduces_the_known_telemetry_request():
    assert sv.request(0, CMD_TELEMETRY) == b'~20004642E00200FD37\r'
    assert encode(0, 0x42, b'\x00') == b'~20004642E00200FD37\r'


def test_encoder_builds_every_allowed_read():
    assert sv.request(0, CMD_MANUFACTURER) == b'~200046510000FDAE\r'
    for adr in (0, 1, 15):
        for cid2 in sorted(sv.READ_COMMANDS):
            f = parse(sv.request(adr, cid2))
            assert (f.ver, f.adr, f.cid1, f.cid2) == (0x20, adr, 0x46, cid2)


@pytest.mark.parametrize('cid2', [0x45, 0x49, 0x4F, 0x4D, 0x80, 0x00, 0xFF, 0x41, 0x43, 0x48])
def test_encoder_refuses_every_command_outside_the_allow_list(cid2):
    with pytest.raises(CommandNotAllowed):
        encode(0, cid2, b'\x00')


def test_the_poll_transport_cannot_send_a_write_either(fake_port):
    fake_port['port'] = FakePack()
    bus = sv.bus_for(conn())
    with pytest.raises(CommandNotAllowed):
        bus.exchange(1, 0x49, 0.1)
    assert fake_port['port'].written == []


# ── frame validation ────────────────────────────────────────────────────────

def test_parse_validates_every_layer():
    good = frame(1, 0x00, b'\x01\x02')
    f = parse(good)
    assert (f.adr, f.cid2, f.info) == (1, 0, b'\x01\x02')
    bad_sum = good[:-5] + (b'0' if good[-5:-4] != b'0' else b'1') + good[-4:]
    with pytest.raises(sv.ChecksumError):
        parse(bad_sum)
    # LCHKSUM nibble wrong (and the CHKSUM recomputed so only LCHKSUM is off)
    body = bytearray(good[1:-5])
    body[8:9] = b'0' if body[8:9] != b'0' else b'1'
    tampered = b'~' + bytes(body) + f'{chksum(bytes(body)):04X}'.encode() + b'\r'
    with pytest.raises(sv.LengthChecksumError):
        parse(tampered)
    with pytest.raises(sv.LengthError):
        parse(good[:-6] + good[-5:])           # one INFO char short
    with pytest.raises(sv.FormatError):
        parse(good[:-1] + b'\n')               # wrong EOI
    with pytest.raises(sv.FormatError):
        parse(b'~2000464G' + good[9:])         # not hex


def test_framer_reassembles_split_frames_and_skips_garbage():
    a, b = frame(1, 0, b'\x05'), frame(2, 0, b'\x06\x07')
    stream = b'\x00\xffjunk' + a + b'~2000' + b'garbage\r' + b + b'tail'
    fr = Framer()
    out = []
    for i in range(0, len(stream), 3):
        out += fr.feed(stream[i:i + 3])
    parsed = []
    for raw in out:
        try:
            parsed.append(parse(raw))
        except sv.FrameError:
            pass
    assert [p.adr for p in parsed] == [1, 2]
    assert fr.dropped_bytes >= len(b'\x00\xffjunk')


def test_framer_resyncs_on_a_torn_head():
    a = frame(3, 0, b'\x01')
    fr = Framer()
    assert fr.feed(b'~200146' + a) == [a]      # the torn head is dropped at the next '~'
    assert fr.dropped_bytes == len(b'~200146')


def test_framer_drops_a_head_that_never_ends():
    fr = Framer(max_frame=40)
    assert fr.feed(b'~' + b'0' * 60) == []
    a = frame(1, 0)
    assert fr.feed(a) == [a]


# ── semantic decode into the virtual map ────────────────────────────────────

def test_telemetry_16_cells_into_the_map():
    w = sv.words_for(CMD_TELEMETRY, telemetry_info(CELLS16, TEMPS6))
    assert [w[i] for i in range(16)] == CELLS16
    assert all(i not in w for i in range(16, 32))       # absent, never 0
    assert w[0x20] == 16 and w[0x21] == 6
    assert [w[0x22 + i] for i in range(6)] == TEMPS6    # Kelvin offset removed
    assert 0x28 not in w and 0x29 not in w
    assert w[0x30] == (-1234) & 0xFFFF                  # int16, 0.01 A
    assert (w[0x31], w[0x32], w[0x33], w[0x34], w[0x35], w[0x36], w[0x37], w[0x38]) == \
        (5223, 2745, 3900, 946, 20000, 70, 1000, 5223)
    assert w[0x40] == 3305 and w[0x49] == 16             # max and which cell
    assert w[0x41] == 3287 and w[0x4A] == 1
    assert w[0x43] == 3305 - 3287
    assert w[0x44] == 252 and w[0x45] == 249             # cell sensors 1–4 only
    assert w[0x47] == 230 and w[0x48] == 287             # ambient, power (assumed order)


def test_telemetry_15_cells_into_the_same_map():
    w = sv.words_for(CMD_TELEMETRY, telemetry_info(CELLS16[:15], TEMPS6))
    assert w[0x20] == 15 and [w[i] for i in range(15)] == CELLS16[:15]
    assert 15 not in w                                   # cell 16 absent
    assert w[0x31] == 5223                               # the rest did not shift


def test_negative_temperatures_and_not_available_values():
    info = telemetry_info([3300, 0xFFFF], [-105, 0xFFFF - 2731], soc=0xFFFF)
    w = sv.words_for(CMD_TELEMETRY, info)
    assert w[0] == 3300 and 1 not in w                   # 0xFFFF cell: absent
    assert w[0x22] == (-105) & 0xFFFF and 0x23 not in w  # −10.5 °C; the 0xFFFF sensor absent
    assert 0x34 not in w                                 # SOC not available: absent, not 0


def test_a_short_custom_block_decodes_as_far_as_it_goes():
    info = telemetry_info(CELLS16, TEMPS6, custom=10)
    cut = info[:-2 * 7]                                  # only full, SOC, rated left
    w = sv.words_for(CMD_TELEMETRY, cut)
    assert w[0x33] == 3900 and w[0x34] == 946 and w[0x35] == 20000
    assert 0x36 not in w and 0x37 not in w


def test_a_truncated_fixed_part_is_a_decode_error():
    with pytest.raises(sv.DecodeError):
        sv.words_for(CMD_TELEMETRY, telemetry_info(CELLS16, TEMPS6)[:20])


def test_alarms_into_the_map_two_byte_layout():
    custom = [0] * 20
    custom[0] = 0b0000_0010          # alarm event 1: temperature sensor fault
    custom[1] = 0b0000_0001          # event 2: cell high voltage alarm
    custom[7], custom[8] = 0b1000_0001, 0b0000_0010   # balancing cells 1, 8, 10
    custom[9] = 0b0000_0010          # system state: charge
    cell = [0] * 16
    cell[3], cell[9] = 0x01, 0x02
    w = sv.words_for(CMD_ALARMS, alarm_info(cell_alarm=cell, temp_alarm=[0, 0, 0, 0, 0, 2], custom=custom))
    assert w[0x103] == 1 and w[0x109] == 2 and 0x110 not in w
    assert w[0x120] == 16 and w[0x121] == 6 and w[0x127] == 2
    assert w[0x133] == 20 and w[0x134] == 2 and w[0x135] == 1
    assert w[0x160 + 1] == 1 and w[0x160] == 0           # event 1 bit 1
    assert w[0x168] == 1                                 # event 2 bit 0
    assert w[0x1E0] == (0b1000_0001 | (0b10 << 8))
    assert w[0x1E2] == 1 << 3 and w[0x1E3] == 1 << 9 and w[0x1E4] == 1 << 5
    assert w[0x13D] == 2                                 # system state byte
    assert w[0x1E5] == 0 and 0x132 not in w


def test_alarms_three_byte_layout_is_recognised_by_the_frame():
    w = sv.words_for(CMD_ALARMS, alarm_info(k=3))
    assert w[0x1E5] == 0 and w[0x132] == 0 and w[0x133] == 20


def test_alarms_with_an_inconsistent_count_are_flagged():
    info = alarm_info()[:-3]                             # P says 20, 17 bytes follow
    w = sv.words_for(CMD_ALARMS, info)
    assert w[0x1E5] == 1 and w[0x133] == 20
    assert 0x134 + 17 not in w


def test_manufacturer_strings():
    info = b'SEPLOS-BMS' + bytes([1, 6]) + b'SEPLOS Technology'.ljust(20, b'\x00')
    w = sv.words_for(CMD_MANUFACTURER, info)
    assert w[0x205] == 0x0106
    p = sv.RegisterParser('big')
    assert p.parse_value([w[0x200 + i] for i in range(5)], 'string:5') == 'SEPLOS-BMS'
    assert p.parse_value([w[0x206 + i] for i in range(10)], 'string:10') == 'SEPLOS Technology'


def test_map_areas():
    assert sv.area_of(0x0000) == CMD_TELEMETRY and sv.area_of(0x004A) == CMD_TELEMETRY
    assert sv.area_of(0x01E5) == CMD_ALARMS and sv.area_of(0x0210) == CMD_MANUFACTURER
    assert sv.area_of(0x0F03) == 'diag' and sv.area_of(0x0500) is None


# ── poll mode over a fake port ──────────────────────────────────────────────

def pack_answer(adr=1, cells=CELLS16, bad_sum_on=(), silent_on=(), rtn=0):
    seen = {'n': 0}

    def answer(req):
        seen['n'] += 1
        if req.adr != adr or seen['n'] in silent_on:
            return b''
        info = {CMD_TELEMETRY: telemetry_info(cells, TEMPS6, pack=adr),
                CMD_ALARMS: alarm_info(pack=adr),
                CMD_MANUFACTURER: b'SEPLOS-BMS\x01\x06' + b'X' * 20}.get(req.cid2, b'')
        f = frame(adr, rtn, info if rtn == 0 else b'')
        if seen['n'] in bad_sum_on:
            f = f[:-2] + (b'0' if f[-2:-1] != b'0' else b'1') + b'\r'
        return f
    return answer


def test_poll_once_fills_the_map_and_counts(fake_port):
    fake_port['port'] = FakePack(pack_answer(adr=1))
    c = SeplosV2Client(conn(unit_id=1), [reg(0, 'cell_1', scale=1000)], {}, device_id='p1')
    assert c.connect()
    assert c.poll_once(CMD_TELEMETRY)
    assert c.words[0] == 3287 and c.stats.good == 1
    assert fake_port['port'].written == [b'~20014642E00201FD35\r']
    c.disconnect()
    assert fake_port['port'].closed


def test_poll_timeout_and_checksum_errors_are_counted(fake_port):
    fake_port['port'] = FakePack(pack_answer(adr=1, silent_on=(1,), bad_sum_on=(2,)))
    c = SeplosV2Client(conn(unit_id=1, retry_attempts=3, timeout=0.15), [], {}, device_id='p1')
    c.connect()
    assert c.poll_once(CMD_TELEMETRY)                   # 1: silence, 2: bad sum, 3: good
    st = c.get_stats()
    assert st['error_counts']['timeouts'] == 1
    assert st['error_counts']['checksum_errors'] == 1
    assert st['successful_reads'] == 1
    assert c.words[sv.MAP_DIAG_TIMEOUTS] == 1 and c.words[sv.MAP_DIAG_CHECKSUM_ERRORS] == 1
    c.disconnect()


def test_poll_rtn_error_is_counted_not_retried(fake_port):
    fake_port['port'] = FakePack(pack_answer(adr=1, rtn=0x04))
    c = SeplosV2Client(conn(unit_id=1, retry_attempts=3), [], {}, device_id='p1')
    c.connect()
    assert not c.poll_once(CMD_MANUFACTURER)
    assert c.stats.rtn_errors == 1 and c.stats.last_rtn == 0x04
    assert len(fake_port['port'].written) == 1
    assert '0x04' in c.get_stats()['last_rtn']
    c.disconnect()


def test_an_echoing_adapter_and_other_packs_traffic_are_ignored(fake_port):
    ans = pack_answer(adr=2)

    def echo_then_answer(req):
        return sv.request(req.adr, req.cid2) + frame(5, 0, b'\x00') + ans(req)
    fake_port['port'] = FakePack(echo_then_answer)
    c = SeplosV2Client(conn(unit_id=2), [], {}, device_id='p2')
    c.connect()
    assert c.poll_once(CMD_TELEMETRY) and c.words[0x20] == 16
    c.disconnect()


def test_the_poll_loop_publishes_by_group(fake_port):
    from multibus.config import PollGroup
    fake_port['port'] = FakePack(pack_answer(adr=1))
    regs = [reg(0x31, 'pack_voltage', scale=100), reg(0x30, 'current', 'int16', scale=100),
            reg(0x1E0, 'balancing_bits', group='normal'), reg(15, 'cell_16', scale=1000),
            reg(0x200, 'device_name', 'string:5', group='slow')]
    got = []
    c = SeplosV2Client(conn(unit_id=1), regs, {'realtime': PollGroup(interval=0.1),
                                               'normal': PollGroup(interval=0.2),
                                               'slow': PollGroup(interval=60)}, device_id='p1')
    c.publish_callback = lambda g, d: got.append((g, {v['register'].name: v['value'] for v in d.values()}))
    c.connect()
    c.start_polling()
    deadline = time.time() + 3
    while time.time() < deadline and len({g for g, _ in got}) < 3:
        time.sleep(0.02)
    c.disconnect()
    by = {}
    for g, vals in got:
        by.setdefault(g, {}).update(vals)
    assert by['realtime'] == {'pack_voltage': 52.23, 'current': -12.34, 'cell_16': 3.305}
    assert by['normal'] == {'balancing_bits': 0}
    assert by['slow'] == {'device_name': 'SEPLOS-BMS'}
    sent = {parse(w).cid2 for w in fake_port['port'].written}
    assert sent <= sv.READ_COMMANDS and {0x42, 0x44, 0x51} <= sent
    assert sum(1 for w in fake_port['port'].written if parse(w).cid2 == 0x51) == 1


def test_a_cell_that_disappears_is_not_published_from_memory(fake_port):
    state = {'cells': CELLS16}
    fake_port['port'] = FakePack(lambda req: frame(1, 0, telemetry_info(state['cells'], TEMPS6)))
    c = SeplosV2Client(conn(unit_id=1), [reg(15, 'cell_16')], {}, device_id='p1')
    c.connect()
    assert c.poll_once(CMD_TELEMETRY) and 15 in c.words
    state['cells'] = CELLS16[:15]
    assert c.poll_once(CMD_TELEMETRY) and 15 not in c.words
    assert c._values(c.registers, time.monotonic()) == {}
    c.disconnect()


def test_a_missing_port_is_down_and_retried(fake_port):
    c = SeplosV2Client(conn(unit_id=1), [reg(0, 'cell_1')], {}, device_id='p1')
    assert not c.connect()
    assert c.data_health()['status'] == 'down'
    assert not c.poll_once(CMD_TELEMETRY) and c.stats.io_errors == 1
    fake_port['port'] = FakePack(pack_answer(adr=1))
    assert c.poll_once(CMD_TELEMETRY)                   # opened on the next try
    assert c.connected and c.data_health()['status'] == 'ok'
    c.disconnect()


def test_two_packs_share_one_port_and_one_lock(fake_port):
    def two(req):
        return pack_answer(adr=req.adr)(req)
    fake_port['port'] = FakePack(two)
    a = SeplosV2Client(conn(unit_id=1), [], {}, device_id='a')
    b = SeplosV2Client(conn(unit_id=2), [], {}, device_id='b')
    assert a._bus is b._bus
    a.connect(), b.connect()
    assert a.poll_once(CMD_TELEMETRY) and b.poll_once(CMD_TELEMETRY)
    a.disconnect()
    assert not fake_port['port'].closed                 # b still uses it
    b.disconnect()
    assert fake_port['port'].closed


# ── listen mode ─────────────────────────────────────────────────────────────

def test_listen_pairs_requests_with_answers_and_never_writes(fake_port):
    port = FakePack()
    fake_port['port'] = port
    regs = [reg(0x31, 'pack_voltage', scale=100), reg(0x13D, 'system_state')]
    got = []
    c1 = SeplosV2Client(conn(unit_id=1, mode='listen'), regs, {}, device_id='l1')
    c2 = SeplosV2Client(conn(unit_id=2, mode='listen'), regs, {}, device_id='l2')
    assert c1._listener is c2._listener and c1._bus is None
    c1.publish_callback = lambda g, d: got.append(('l1', {v['register'].name: v['value'] for v in d.values()}))
    c2.publish_callback = lambda g, d: got.append(('l2', {v['register'].name: v['value'] for v in d.values()}))
    assert c1.connect() and c2.connect()
    stream = (sv.request(1, CMD_TELEMETRY) + frame(1, 0, telemetry_info(CELLS16, TEMPS6, voltage=5300))
              + b'\x00noise' + sv.request(2, CMD_ALARMS) + frame(2, 0, alarm_info(custom=[0] * 9 + [1] + [0] * 10))
              + frame(3, 0, b'\x00')                                    # an answer nobody asked for
              + frame(2, 0x49, b'\x02\x01')                           # a master's set command: only noted
              + sv.request(1, CMD_TELEMETRY) + frame(1, 0x02))          # RTN 0x02 from pack 1
    port.feed(stream)
    deadline = time.time() + 3
    while time.time() < deadline and (len(got) < 2 or c1.stats.rtn_errors < 1):
        time.sleep(0.02)
    lst = c1._listener
    c1.disconnect()
    c2.disconnect()
    assert ('l1', {'pack_voltage': 53.0}) in got
    assert ('l2', {'system_state': 1}) in got
    assert c1.stats.rtn_errors == 1 and c1.stats.last_rtn == 0x02
    assert lst.orphans == 1 and lst.commands_seen == 1
    assert port.written == []                           # not one byte, ever
    assert port.closed


def test_listen_reopens_a_port_that_appears_later(fake_port, monkeypatch):
    monkeypatch.setattr(sv._Listener, 'REOPEN_S', 0.05)
    c = SeplosV2Client(conn(unit_id=1, mode='listen'), [reg(0x20, 'cells')], {}, device_id='l1')
    got = []
    c.publish_callback = lambda g, d: got.append(d)
    assert not c.connect()                              # no port yet
    assert c.data_health()['status'] == 'down'
    port = FakePack(stream=sv.request(1, CMD_TELEMETRY) + frame(1, 0, telemetry_info(CELLS16, TEMPS6)))
    fake_port['port'] = port
    deadline = time.time() + 3
    while time.time() < deadline and not got:
        time.sleep(0.02)
    c.disconnect()
    assert got and port.written == []


def test_listen_charges_a_corrupted_answer_to_the_pack_awaited(fake_port):
    fake_port['port'] = FakePack()
    c = SeplosV2Client(conn(unit_id=4, mode='listen'), [], {}, device_id='l4')
    lst = c._listener
    lst._clients[4] = c                                 # attached without a thread
    bad = frame(4, 0, b'\x00\x01')
    bad = bad[:-2] + (b'0' if bad[-2:-1] != b'0' else b'1') + b'\r'
    lst.on_frame(sv.request(4, CMD_TELEMETRY), 1.0)
    lst.on_frame(bad, 1.1)
    assert c.stats.checksum_errors == 1 and lst.bad_frames == 1


# ── writes are impossible ───────────────────────────────────────────────────

def test_the_client_has_no_write_path_and_the_device_refuses():
    from multibus.multi_source import MultiSourceClient
    assert not hasattr(SeplosV2Client, 'write_value')
    drv = SimpleNamespace(registers=[], connected=True)   # stands for the client: no write_value
    ms = MultiSourceClient('bms', [(SimpleNamespace(id='default', protocol='seplos_v2', enabled=True), drv)])
    ok, err, _ = ms.write_value(0x30, 'holding', 'uint16', 1)
    assert ok is False and 'no source supports writing' in err


def test_probe_in_listen_mode_asks_nothing(fake_port):
    fake_port['port'] = FakePack()
    r = sv.probe(conn(mode='listen'))
    assert r['ok'] is None and fake_port['port'].written == []


def test_probe_reads_one_telemetry_answer(fake_port):
    fake_port['port'] = FakePack(pack_answer(adr=1))
    r = sv.probe(conn(unit_id=1), timeout=0.3)
    assert r['ok'] is True and '16 cells' in r['message'] and '52.23 V' in r['message']
    assert fake_port['port'].closed                     # nobody else held the port


# ── the template ────────────────────────────────────────────────────────────

def _tpl():
    import json
    from pathlib import Path
    return json.loads(Path("multibus/device_templates/seplos_bms_v2.json").read_text())


def test_the_template_is_valid_and_of_its_own_family():
    from multibus.device_template import TemplateRegistry, extra_errors, template_transport, validate_template
    data = _tpl()
    assert validate_template(data) == [] and extra_errors(data) == []
    reg_ = TemplateRegistry()
    t = reg_.get('seplos_bms_v2')
    assert t is not None and 'seplos_bms_v2.json' not in reg_.load_errors
    assert template_transport(t) == 'seplos_v2' and t.summary()['transport'] == 'seplos_v2'


def test_every_row_reads_a_decoded_part_of_the_map():
    tpl = _tpl()['device_template']
    decoded = {CMD_TELEMETRY, CMD_ALARMS, CMD_MANUFACTURER, 'diag'}
    for r in tpl['registers']:
        assert r['register_type'] == 'input', r['name']
        assert sv.area_of(r['address']) in decoded, (r['name'], hex(r['address']))


def test_names_follow_the_v3_template_where_the_meaning_is_the_same():
    import json
    from pathlib import Path
    v3 = json.loads(Path("multibus/device_templates/seplos_bms_v3_rtu_tap.json").read_text())['device_template']
    v2 = _tpl()['device_template']
    names2 = {r['name'] for r in v2['registers']} | {c['name'] for c in v2['calculated']}
    shared = {'pack_voltage', 'current', 'remaining_capacity', 'total_capacity', 'soc', 'soh', 'cycles',
              'average_cell_voltage', 'max_cell_voltage', 'min_cell_voltage', 'max_cell_temp', 'min_cell_temp',
              'average_cell_temp', 'ambient_temp', 'mosfet_temp', 'status', 'balancing_bits', 'power',
              'cell_delta', 'alarm_count', 'warning_count', 'protection_count', 'failure_count',
              'energy_remaining', 'energy_to_full', 'fet_charge', 'fet_discharge', 'alarm_cell_high_v',
              'alarm_short_circuit_prot', 'alarm_soc_low'} | {f'cell_{i}' for i in range(1, 17)} \
        | {f'cell_temp_{i}' for i in range(1, 5)}
    names3 = {r['name'] for r in v3['registers']} | {c['name'] for c in v3['calculated']}
    assert shared <= names2 and shared <= names3
    # the same scale where both read the same unit
    s2 = {r['name']: r.get('scale') for r in v2['registers']}
    s3 = {r['name']: r.get('scale') for r in v3['registers']}
    for n in ('pack_voltage', 'current', 'soc', 'soh', 'cell_1', 'remaining_capacity'):
        assert s2[n] == s3[n], n


def test_the_template_decodes_a_golden_frame_end_to_end(fake_port):
    """The template's rows against the driver's map, as a device runs them."""
    from multibus.config import PollGroup
    from multibus.device_template import parse_template
    t = parse_template(_tpl(), builtin=True)
    regs = [SimpleNamespace(address=r.address, name=r.name, label=r.name, unit=r.unit,
                            data_type=r.data_type, register_type=r.register_type, poll_group=r.poll_group,
                            scale=r.scale, offset=r.offset, enum=r.enum, bits=getattr(r, 'bits', None),
                            mask=r.mask, shift=getattr(r, 'shift', None), nan=getattr(r, 'nan', None),
                            monotonic=False, scale_from='') for r in t.registers]
    fake_port['port'] = FakePack(pack_answer(adr=1))
    c = SeplosV2Client(conn(unit_id=1), regs, {'realtime': PollGroup(interval=5)}, device_id='p1')
    c.connect()
    assert c.poll_once(CMD_TELEMETRY) and c.poll_once(CMD_ALARMS)
    v = {d['register'].name: d['value'] for d in c._values(regs, time.monotonic()).values()}
    c.disconnect()
    assert v['pack_voltage'] == 52.23 and v['current'] == -12.34 and v['soc'] == 94.6
    assert v['cell_16'] == 3.305 and v['max_cell_voltage'] == 3.305 and v['min_cell_voltage'] == 3.287
    assert v['cell_temp_1'] == 25.1 and v['ambient_temp'] == 23.0 and v['mosfet_temp'] == 28.7
    assert v['max_cell_temp'] == 25.2 and v['total_capacity'] == 39.0 and v['cycles'] == 70
    assert v['status'] == 'Unknown' and v['alarm_cell_high_v'] == 0
    assert 'device_name' not in v                       # 0x51 not asked: absent, not empty
