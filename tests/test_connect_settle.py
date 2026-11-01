# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""A pause after connecting belongs to the transport (ADR-0015).

Some gateways ignore the first seconds after a TCP connect (a Huawei SDongle,
some serial bridges): the first batch would burn its whole retry budget. The
pause runs once per successful (re)connect, never per read, under the lock the
units sharing that socket queue on, and the most cautious declaration among
them wins. 0, the default, is the old behaviour exactly.
"""
import threading
from types import SimpleNamespace

import pytest

from multibus import modbus_client as mc
from multibus.modbus_client import (ModbusConfig, ModbusConnection, _TRANSPORTS,
                                    transport_for)


class _Result:
    def __init__(self, regs=None, exc=None):
        self.registers = regs or []
        self.exception_code = exc

    def isError(self):
        return self.exception_code is not None or not self.registers


class _Client:
    """A fake pymodbus client; ``script`` hands out answers in order."""
    script = []
    connects = 0

    def __init__(self):
        self.asked = []

    def is_socket_open(self): return True
    def close(self): pass

    def connect(self):
        _Client.connects += 1
        return True

    def read_holding_registers(self, address, count, device_id):
        self.asked.append(device_id)
        if _Client.script:
            return _Client.script.pop(0)
        return _Result([0] * count)

    read_input_registers = read_holding_registers


class _Refused(_Client):
    def connect(self):
        return False


def _conn(unit, host='192.0.2.110', settle_default=None, client=_Client, **kw):
    kw.setdefault('retry_attempts', 1)
    kw.setdefault('retry_delay', 0)
    cfg = ModbusConfig(host=host, port=502, unit_id=unit, **kw)
    c = (ModbusConnection(cfg, trace_label=f'u{unit}') if settle_default is None
         else ModbusConnection(cfg, trace_label=f'u{unit}', settle_default=settle_default))
    c._new_client = client
    return c


@pytest.fixture
def sleeps(monkeypatch):
    """Every sleep the client makes on this test's thread, recorded instead of
    slept. Only the client's own `_sleep` is patched, never the shared `time`
    module, and a leftover thread of an earlier test sleeps for real (FLK-3)."""
    _TRANSPORTS.clear()
    _Client.script, _Client.connects = [], 0
    got = []
    mine = threading.get_ident()
    real = mc._sleep

    def fake(s):
        if threading.get_ident() == mine:
            got.append(s)
        else:
            real(s)
    monkeypatch.setattr(mc, '_sleep', fake)
    return got


# ── 0 is today's behaviour ───────────────────────────────────────────────────

def test_default_is_no_pause(sleeps):
    assert ModbusConfig().connect_settle_s is None     # unset: template or 0
    c = _conn(1)
    assert c.connect() is True
    c.connected = False
    assert c.read_registers(0, 2) == [0, 0]            # reconnects inside the read
    assert sleeps == []


# ── once per successful connect ──────────────────────────────────────────────

def test_connect_pauses_once_after_a_successful_connect(sleeps):
    c = _conn(1, connect_settle_s=2.5)
    assert c.connect() is True
    assert sleeps == [2.5]
    assert c.read_registers(0, 2) == [0, 0]            # a read on the open socket
    assert sleeps == [2.5]                             # …costs nothing more


def test_a_refused_connect_does_not_pause(sleeps):
    c = _conn(1, connect_settle_s=2.5, client=_Refused)
    assert c.connect() is False
    assert sleeps == []


def test_the_reconnect_inside_a_read_pauses_before_the_first_request(sleeps, monkeypatch):
    c = _conn(1, connect_settle_s=1.5)
    order = []
    monkeypatch.setattr(mc, '_sleep', lambda s: (sleeps.append(s), order.append('settle')))

    class _Ordered(_Client):
        def read_holding_registers(self, address, count, device_id):
            order.append('read')
            return super().read_holding_registers(address, count, device_id)

    c._new_client = _Ordered
    assert c.read_registers(0, 2) == [0, 0]
    assert order == ['settle', 'read']
    assert c.read_registers(0, 2) == [0, 0]
    assert sleeps == [1.5]                             # not once per read


# ── held under the transport lock ────────────────────────────────────────────

def test_the_pause_holds_the_transport_lock_so_a_sibling_waits(sleeps, monkeypatch):
    a = _conn(1, connect_settle_s=3)
    b = _conn(2)
    assert a._tp is b._tp
    in_pause, go = threading.Event(), threading.Event()
    seen = {}

    def fake_sleep(s):
        if s != 3:
            return
        # another thread cannot take the transport lock while we settle
        probe = threading.Thread(target=lambda: seen.setdefault(
            'free', a._tp.lock.acquire(blocking=False)))
        probe.start(); probe.join()
        in_pause.set()
        go.wait(5)

    monkeypatch.setattr(mc, '_sleep', fake_sleep)
    t = threading.Thread(target=a.connect)
    t.start()
    assert in_pause.wait(5)
    sib = threading.Thread(target=lambda: seen.setdefault('b', b.read_registers(0, 1)))
    sib.start()
    sib.join(0.2)
    assert sib.is_alive()                              # the sibling is queued…
    assert a.client.asked == []                        # …and nothing went on the wire
    go.set()
    t.join(5); sib.join(5)
    assert seen['free'] is False
    assert seen['b'] == [0]
    assert a.client.asked == [2]


# ── the transport's value: the most cautious wins ────────────────────────────

def test_the_largest_value_on_a_shared_transport_wins(sleeps):
    a = _conn(1, connect_settle_s=1)
    b = _conn(2, connect_settle_s=4)
    c = _conn(3, connect_settle_s=0.5)                 # a later, smaller one
    assert a._tp is b._tp is c._tp
    assert a._tp.settle_s == 4
    c.connect()
    assert sleeps == [4]


def test_a_private_transport_keeps_its_own_value(sleeps):
    a = _conn(1, connect_settle_s=4)
    b = _conn(2, connect_settle_s=1, share_transport=False)
    assert b._tp is not a._tp
    b.connect()
    assert sleeps == [1]


def test_transport_for_takes_the_max():
    _TRANSPORTS.clear()
    tp = transport_for('192.0.2.111', 502, True, 0, 2.0)
    assert transport_for('192.0.2.111', 502, True, 0, 1.0).settle_s == 2.0
    assert transport_for('192.0.2.111', 502, True, 0, 5.0) is tp
    assert tp.settle_s == 5.0


# ── template default, connection override ────────────────────────────────────

def test_template_default_applies_when_the_connection_says_nothing(sleeps):
    c = _conn(1, settle_default=3)
    c.connect()
    assert sleeps == [3]


def test_the_connections_own_value_overrides_the_template(sleeps):
    c = _conn(1, settle_default=3, connect_settle_s=1)
    c.connect()
    d = _conn(1, host='192.0.2.112', settle_default=3, connect_settle_s=0)
    d.connect()                                        # an explicit 0 turns it off
    assert sleeps == [1]


def test_driver_for_hands_the_template_protocol_default_to_the_connection(sleeps):
    from multibus.device_runtime import driver_for
    tpl = SimpleNamespace(protocol={'connect_settle_s': 3}, setup=[])
    reg = SimpleNamespace(get=lambda tid: tpl, byte_order_for=lambda tid: 'big')
    dev = SimpleNamespace(id='hw', template='t', sources=[])
    cfg = SimpleNamespace(security=SimpleNamespace(allow_writes=False))
    for own, want in ((None, 3.0), (0.5, 0.5)):
        _TRANSPORTS.clear()
        src = SimpleNamespace(id='default', protocol='tcp', template='t',
                              connection=ModbusConfig(host='192.0.2.113', port=502,
                                                      connect_settle_s=own))
        client = driver_for(cfg, reg, dev, src, [], {}, False)
        assert client.connection.settle_s == want
        assert client.connection._tp.settle_s == want


def test_the_config_parses_and_clamps_it(tmp_path):
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml="""
endpoints:
  - id: dongle
    connection: { protocol: tcp, host: 192.0.2.114, connect_settle_s: 3 }
    units: [1]
  - id: silly
    connection: { protocol: tcp, host: 192.0.2.115, connect_settle_s: 600 }
    units: [1]
  - id: negative
    connection: { protocol: tcp, host: 192.0.2.116, connect_settle_s: -2 }
    units: [1]
  - id: unset
    connection: { protocol: tcp, host: 192.0.2.117 }
    units: [1]
""")
    got = {eid: cfg.endpoint_devices(eid)[0].connection.connect_settle_s
           for eid in ('dongle', 'silly', 'negative', 'unset')}
    assert got == {'dongle': 3.0, 'silly': 30.0, 'negative': 0.0, 'unset': None}


def test_the_template_validator_bounds_it():
    from multibus.device_template import validate_template
    base = {'id': 'x-settle', 'name': 'x', 'registers': []}
    ok = validate_template({'device_template': dict(base, protocol={'connect_settle_s': 3})})
    bad = validate_template({'device_template': dict(base, protocol={'connect_settle_s': 99})})
    assert not any('connect_settle_s' in e for e in ok)
    assert any('connect_settle_s' in e for e in bad)


# ── exception 0x06, slave device busy: retried like any other refusal ────────

def test_slave_busy_goes_through_the_normal_retry_path(sleeps):
    c = _conn(1, retry_attempts=3, retry_delay=0.7)
    c.connect()
    _Client.script = [_Result(exc=6), _Result([5, 6])]
    assert c.read_registers(0, 2) == [5, 6]
    assert sleeps == [0.7]                             # backed off, then asked again
    assert c.snapshot_errors() == {'exception_6': 1}   # and it stays counted
    assert c.batch_failures == 0
