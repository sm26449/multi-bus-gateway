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
"""rtu_tap — the listen-only Modbus RTU observer.

Everything here runs against synthetic byte streams: the framer is pure
(bytes + timestamps in, frames out) and the reader's frame handler is called
directly, so no serial port is involved."""
from types import SimpleNamespace

from multibus.rtu_tap import (RtuFramer, RtuTapClient, TapReader, crc16,
                              frame_crc_ok)


def adu(body: bytes) -> bytes:
    c = crc16(body)
    return body + bytes([c & 0xFF, (c >> 8) & 0xFF])


def req_read(unit, fc, addr, count):
    return adu(bytes([unit, fc, addr >> 8, addr & 0xFF, count >> 8, count & 0xFF]))


def resp_read(unit, fc, words):
    body = bytes([unit, fc, 2 * len(words)])
    for w in words:
        body += bytes([w >> 8, w & 0xFF])
    return adu(body)


def conn(port='/dev/null-tap', unit=1, **kw):
    return SimpleNamespace(serial_port=port, baudrate=9600, parity='N',
                           stopbits=1, bytesize=8, unit_id=unit,
                           stale_after_s=kw.get('stale_after_s', 30))


def make_pair(port, unit=1, registers=(), byte_order='big'):
    """A TapReader + attached client WITHOUT opening any serial port."""
    c = conn(port=port, unit=unit)
    reader = TapReader(c)
    client = RtuTapClient(conn_cfg=c, registers=list(registers), device_id='tap-dev')
    client._reader = reader                     # bypass the shared-port registry
    reader._clients[unit] = client
    client.connected = True
    return reader, client


def reg(address, name, data_type='uint16', **kw):
    d = {'address': address, 'name': name, 'label': name, 'unit': '',
         'data_type': data_type, 'poll_group': 'normal', 'scale': 1.0,
         'offset': 0.0, 'nan': None, 'monotonic': False, 'enum': None,
         'bits': None, 'mask': None, 'shift': None, 'scale_from': ''}
    d.update(kw)
    return SimpleNamespace(**d)


def test_crc_roundtrip_and_reject():
    f = req_read(1, 3, 100, 2)
    assert frame_crc_ok(f)
    assert not frame_crc_ok(f[:-1] + bytes([f[-1] ^ 0xFF]))
    assert not frame_crc_ok(b'\x01\x03')


def test_framer_splits_on_silence_not_on_chunk_boundaries():
    fr = RtuFramer(baudrate=9600)
    f1, f2 = req_read(1, 3, 0, 1), resp_read(1, 3, [7])
    # frame 1 arrives in two chunks 1 ms apart (below the 3.5-char gap)
    assert fr.feed(f1[:3], now=0.000) == []
    assert fr.feed(f1[3:], now=0.001) == []
    # frame 2 starts after a real silent gap -> frame 1 closes
    out = fr.feed(f2, now=0.100)
    assert out == [f1]
    # silence closes the pending frame on flush
    assert fr.flush(now=0.200) == [f2]


def test_request_response_pairing_dispatches_decoded_values():
    got = {}
    r, c = make_pair('/p1', unit=5, registers=[
        reg(100, 'voltage', 'uint16', scale=10.0, unit='V'),   # raw/10
        reg(101, 'current', 'int16'),
    ])
    c.publish_callback = lambda g, data: got.update(data)
    r._on_frame(req_read(5, 3, 100, 2), now=1.0)
    r._on_frame(resp_read(5, 3, [2315, 0xFFFE]), now=1.05)
    assert got[100]['value'] == 231.5
    assert got[101]['value'] == -2
    assert c.windows == 1 and c.updates == 2
    assert r.frames == 2 and r.crc_errors == 0 and r.orphans == 0


def test_int32_spans_two_words_and_partial_windows_are_skipped():
    got = {}
    r, c = make_pair('/p2', unit=1, registers=[
        reg(200, 'energy', 'uint32'),
        reg(202, 'outside', 'uint16'),          # not in this window
    ])
    c.publish_callback = lambda g, data: got.update(data)
    r._on_frame(req_read(1, 4, 200, 2), now=0.0)
    r._on_frame(resp_read(1, 4, [0x0001, 0x86A0]), now=0.01)   # 100000
    assert got[200]['value'] == 100000
    assert 202 not in got


def test_unpaired_response_is_an_orphan_and_bad_crc_is_counted():
    r, c = make_pair('/p3', unit=1, registers=[reg(0, 'x')])
    c.publish_callback = lambda g, data: None
    r._on_frame(resp_read(1, 3, [1]), now=0.0)        # response with no request
    assert r.orphans == 1
    bad = bytearray(req_read(1, 3, 0, 1)); bad[-1] ^= 0xFF
    r._on_frame(bytes(bad), now=0.1)
    assert r.crc_errors == 1


def test_late_response_does_not_pair():
    got = {}
    r, c = make_pair('/p4', unit=1, registers=[reg(10, 'v')])
    c.publish_callback = lambda g, data: got.update(data)
    r._on_frame(req_read(1, 3, 10, 1), now=0.0)
    r._on_frame(resp_read(1, 3, [9]), now=5.0)        # way past the window
    assert got == {} and r.orphans == 1


def test_write_observations_fc6_deduped_echo_and_fc16():
    got = {}
    r, c = make_pair('/p5', unit=2, registers=[
        reg(50, 'setpoint', 'uint16'), reg(60, 'a'), reg(61, 'b')])
    c.publish_callback = lambda g, data: got.update(data)
    w6 = adu(bytes([2, 6, 0, 50, 0x01, 0x2C]))        # write 300 @50
    r._on_frame(w6, now=0.0)
    r._on_frame(w6, now=0.01)                         # the slave's echo
    assert got[50]['value'] == 300
    assert r.writes_seen == 1                         # echo deduped
    body = bytes([2, 16, 0, 60, 0, 2, 4, 0, 7, 0, 8])
    r._on_frame(adu(body), now=1.0)
    assert got[60]['value'] == 7 and got[61]['value'] == 8


def test_exception_responses_are_counted_not_dispatched():
    got = {}
    r, c = make_pair('/p6', unit=1, registers=[reg(0, 'x')])
    c.publish_callback = lambda g, data: got.update(data)
    r._on_frame(req_read(1, 3, 0, 1), now=0.0)
    r._on_frame(adu(bytes([1, 0x83, 2])), now=0.01)   # illegal data address
    assert r.exceptions == 1 and got == {}


def test_health_and_stats_shape():
    r, c = make_pair('/p7', unit=1, registers=[reg(0, 'x')])
    c.publish_callback = lambda g, data: None
    assert c.data_health()['status'] == 'stale'        # attached, nothing seen
    import time as _t
    t0 = _t.monotonic()
    r._on_frame(req_read(1, 3, 0, 1), now=t0)
    r._on_frame(resp_read(1, 3, [1]), now=t0 + 0.05)
    assert c.data_health()['status'] == 'ok'
    s = c.get_stats()
    assert s['mode'].startswith('rtu_tap') and s['successful_reads'] == 1
    assert s['bus']['port'] == '/p7'
    # the debug ring remembers the exchange
    kinds = [e['kind'] for e in c.recent_frames()]
    assert kinds[-2:] == ['request', 'response']


def test_driver_factory_builds_tap_client(tmp_path):
    """device_runtime routes protocol rtu_tap to RtuTapClient (template byte
    order resolved like any Modbus source)."""
    from multibus.device_runtime import driver_for
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml="""
devices:
  - id: tapped
    name: Tapped meter
    connection: { protocol: rtu_tap, serial_port: /dev/ttyTAP0, unit_id: 7 }
""")
    dev = next(d for d in cfg.devices if d.id == 'tapped')
    src = dev.sources[0]

    class _TplReg:                                     # no template declared
        def get(self, tid): return None
        def byte_order_for(self, tid): return 'big'

    regs, groups = cfg.load_device_registers(dev)
    drv = driver_for(cfg, _TplReg(), dev, src, regs, groups, allow_nonlan=False)
    from multibus.rtu_tap import RtuTapClient as _C
    assert isinstance(drv, _C) and drv.unit_id == 7
    assert dev.protocol == 'rtu_tap'


def test_seplos_v3_template_decodes_pia_like_the_collector():
    """The bundled seplos_bms_v3_rtu_tap template, fed a synthetic PIA frame,
    must land on the collector's math: V/100, A/100 signed, SOC/10,
    cell V/1000, Kelvin/10 - 273.15."""
    import json
    tpl = json.load(open('multibus/device_templates/seplos_bms_v3_rtu_tap.json'))
    regs = [reg(x['address'], x['name'], x.get('data_type', 'uint16'),
                scale=x.get('scale', 1.0), offset=x.get('offset', 0.0),
                unit=x.get('unit', ''))
            for x in tpl['device_template']['registers']]
    r, c = make_pair('/seplos', unit=1, registers=regs)
    got = {}
    c.publish_callback = lambda g, data: got.update(
        {data[a]['register'].name: data[a]['value'] for a in data})
    # PIA: 18 words — 52.29 V, -12.34 A, SOC 34.5 %, avg cell 3.268 V,
    # avg cell temp 21.45 °C (2946 raw Kelvin*10)
    words = [5229, 65536 - 1234, 7730, 28000, 1234, 345, 1000, 42,
             3268, 2946, 3270, 3261, 2950, 2940, 0, 180, 80, 0]
    r._on_frame(req_read(1, 4, 0x1000, 18), now=0.0)
    r._on_frame(resp_read(1, 4, words), now=0.05)
    assert got['pack_voltage'] == 52.29
    assert got['current'] == -12.34
    assert got['soc'] == 34.5
    assert got['average_cell_voltage'] == 3.268
    assert abs(got['average_cell_temp'] - 21.45) < 0.01
    assert got['maxdiscurt'] == 180 and got['maxchgcurt'] == 80
    # PIB: 16 cells + 4 temps + skip 4 + ambient/mosfet = 26 words
    pib = [3265 + i for i in range(16)] + [2950, 2951, 2952, 2953] + [0]*4 + [2990, 3050]
    r._on_frame(req_read(1, 4, 0x1100, 26), now=1.0)
    r._on_frame(resp_read(1, 4, pib), now=1.05)
    assert got['cell_1'] == 3.265 and got['cell_16'] == 3.280
    assert abs(got['mosfet_temp'] - 31.85) < 0.01
