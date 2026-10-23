# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""What a high-availability pair needs from one gateway around a take-over:
a standby listens to the RTU buses it would poll (its partner is the master
there) and says when it last heard them; a standby that takes over carries
on its partner's write leases, says which sequence was cut, and lets its
rules act only on fresh readings."""
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from multibus import role as gateway_role
from multibus.role import GatewayRole

from tests.test_devices_api import needs_tc


class _Drv:
    def __init__(self, log, name):
        self.log, self.name, self.publish_callback = log, name, None

    def connect(self):
        self.log.append(f'{self.name}:connect')
        return True

    def start_polling(self):
        self.log.append(f'{self.name}:poll')

    def disconnect(self):
        self.log.append(f'{self.name}:stop')


def test_a_standby_listens_to_the_rtu_bus_it_would_poll(tmp_path):
    from multibus.multi_source import MultiSourceClient
    log = []
    src = SimpleNamespace(id='rtu', protocol='rtu', stale_after_s=0)
    client = MultiSourceClient('meter', [(src, _Drv(log, 'poller'))], tap_factories={'rtu': lambda: _Drv(log, 'tap')})
    r = GatewayRole(str(tmp_path / 'role.json'))
    gateway_role.install(r)
    r.set('standby', 1, by='t')
    client.connect()
    client.start_polling()                                               # booting as a standby
    assert log == ['tap:connect'] and client._standby_taps
    r.set('active', 2, by='t')
    client.resume()
    assert log[-3:] == ['tap:stop', 'poller:connect', 'poller:poll']       # the port is the poller's again, in order
    r.set('standby', 3, by='t')
    client.standby()
    assert log[-2:] == ['poller:stop', 'tap:connect']
    assert client._standby_taps['rtu'].publish_callback is not None        # its values go through the arbiter


def test_the_tap_says_when_it_last_heard_its_bus():
    from multibus import rtu_tap
    reader = rtu_tap.TapReader(SimpleNamespace(serial_port='/dev/ttyWITNESS', baudrate=9600, parity='N', stopbits=1,
                                               bytesize=8, host='', port=0))
    with rtu_tap._READERS_LOCK:
        rtu_tap._READERS['/dev/ttyWITNESS'] = reader
    try:
        body = bytes([1, 3, 0, 0, 0, 2])
        crc = rtu_tap.crc16(body) if hasattr(rtu_tap, 'crc16') else None
        frame = body + (crc.to_bytes(2, 'little') if crc is not None else b'\xc4\x0b')
        reader._on_frame(frame, time.monotonic() - 1.5)
        bus = next(b for b in rtu_tap.bus_activity() if b['port'] == '/dev/ttyWITNESS')
        assert bus['frames'] == 1 and 1.4 < bus['last_frame_age_s'] < 3
    finally:
        with rtu_tap._READERS_LOCK:
            rtu_tap._READERS.pop('/dev/ttyWITNESS', None)


@needs_tc
def test_taking_over_carries_on_the_partners_leases_says_the_cut_sequence_and_waits(tmp_path):
    from tests.test_operator_role import _build_clients
    c = _build_clients(tmp_path)
    ha = {"Authorization": f"Bearer {c.admin.post('/api/auth/service-tokens', json={'name': 'mbg-ha', 'role': 'ha'}).json()['token']}"}
    svc = TestClient(c.app, raise_server_exceptions=False)
    assert svc.put('/api/role', json={'role': 'standby', 'epoch': 1}, headers=ha).status_code == 200
    lease = {'device': 'umg512', 'register_type': 'holding', 'address': 1234, 'data_type': 'uint16', 'scale': 1.0,
             'offset': 0.0, 'safe_value': 0, 'lease_ms': 60000, 'remaining_s': 45.0}
    seq = {'sequence': 'pump-start', 'step': 2, 'by': 'admin', 'status': 'running'}
    r = svc.post('/api/state/mirror', json={'t': time.time(), 'values': {}, 'rules': {}, 'leases': [lease],
                                            'sequences': [seq]}, headers=ha)
    assert r.status_code == 200
    bus = svc.get('/api/role/bus', headers=ha).json()
    assert bus['role'] == 'standby' and isinstance(bus['listened'], list)
    assert svc.put('/api/role', json={'role': 'active', 'epoch': 2}, headers=ha).status_code == 200
    lm = c.app.state.lease_mgr
    for _ in range(60):
        if lm.snapshot():
            break
        time.sleep(0.05)
    snap = lm.snapshot()
    assert snap and snap[0]['address'] == 1234 and 40 < snap[0]['remaining_s'] <= 45   # the time it had left
    events = str(c.admin.get('/api/events?limit=50').json())
    assert 'pump-start' in events and 'NOT resumed' in events
    assert c.app.state.role_warm_until > time.monotonic()                 # rules wait for fresh readings
    assert c.app.state.peer_mirror == {}                                   # used once
