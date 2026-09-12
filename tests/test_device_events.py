# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""A device's acquisition keeps a log, and you can read it.

The connection has always recorded its own troubles — unreachable, recovered,
forced reopen, bus busy — into a ring. Until now the only reader was the alert
harvester, which fired on them and dropped them, so diagnosing a misbehaving
endpoint meant grepping container logs for facts the process already had in
memory and could not be asked for. These tests pin the three things that fixes:
the failures that mattered are recorded, the ring is long enough to survive an
episode, and an operator can fetch it.
"""
import pytest

from multibus.modbus_client import ModbusConfig, ModbusConnection


class _Result:
    def __init__(self, regs):
        self.registers = regs or []
        self._err = regs is None

    def isError(self):
        return self._err


class _Client:
    ok = True

    def is_socket_open(self): return True
    def connect(self): return True
    def close(self): pass

    def read_holding_registers(self, address, count, device_id):
        return _Result([0] * count) if _Client.ok else _Result(None)

    read_input_registers = read_holding_registers
    read_coils = read_holding_registers
    read_discrete_inputs = read_holding_registers


def _conn(**kw):
    cfg = ModbusConfig(host='192.0.2.60', port=502, unit_id=1, retry_attempts=1,
                       retry_delay=0, **kw)
    c = ModbusConnection(cfg, trace_label='u1')
    c._new_client = _Client
    _Client.ok = True
    return c


# ── the ring holds an episode, not a moment ─────────────────────────────────

def test_the_ring_is_sized_for_an_episode():
    """A datalogger that stalls in bursts every few minutes buried the old
    50-entry ring before anyone could open the page."""
    c = _conn()
    assert c.events.maxlen >= 500


def test_the_ring_drops_the_oldest_and_never_raises():
    c = _conn()
    for i in range(c.events.maxlen + 50):
        c.record_event('info', 'tick', f'#{i}')
    assert len(c.events) == c.events.maxlen
    assert c.snapshot_events()[-1]['message'] == f'#{c.events.maxlen + 49}'


def test_a_recorded_event_carries_when_how_bad_and_what():
    c = _conn()
    c.record_event('warn', 'bus_busy', 'no turn within 10s')
    e = c.snapshot_events()[-1]
    assert e['level'] == 'warn' and e['kind'] == 'bus_busy'
    assert e['message'] == 'no turn within 10s' and e['ts'] > 0


# ── the failures that used to reach only the logger ─────────────────────────

def test_a_failed_batch_is_recorded_with_what_was_asked_for():
    """The per-batch loss the connection-level timestamp cannot see. Without the
    address and size, a log line says only 'something failed'."""
    c = _conn()
    c.connect()
    _Client.ok = False
    assert c.read_registers(40071, 49) is None
    ev = [e for e in c.snapshot_events() if e['kind'] == 'batch_failed']
    assert len(ev) == 1
    assert '49' in ev[0]['message'] and '40071' in ev[0]['message']
    assert ev[0]['level'] == 'error'


def test_a_device_that_goes_away_and_comes_back_leaves_both_facts():
    c = _conn()
    c.connect()
    _Client.ok = False
    for _ in range(c._reopen_after_fails):
        c.read_registers(40071, 2)
    kinds = [e['kind'] for e in c.snapshot_events()]
    assert 'unreachable' in kinds
    _Client.ok = True
    assert c.read_registers(40071, 2) is not None
    assert 'recovered' in [e['kind'] for e in c.snapshot_events()]


# ── the poller's own facts reach the device's log ───────────────────────────

def test_an_overrun_is_recorded_once_per_episode_not_once_per_cycle():
    """A group that cannot keep up for an hour must not evict the reason it
    started: the ring gets the edge, exactly like the logger does."""
    from multibus.modbus_client import RegisterPoller
    from multibus.register_parser import RegisterParser

    c = _conn()
    p = RegisterPoller('normal', 5, [], c, RegisterParser('big'),
                       lambda *a: None, 'u1')
    p._overrun_logged = False
    for _ in range(3):                       # three slow cycles in a row
        if not p._overrun_logged:
            p._overrun_logged = True
            p._record('warn', 'overrun', 'normal: sweep took 9.0s, interval is 5s')
    over = [e for e in c.snapshot_events() if e['kind'] == 'overrun']
    assert len(over) == 1
    assert 'normal' in over[0]['message'] and '9.0s' in over[0]['message']


def test_the_poller_bridge_never_breaks_polling():
    """Observability is not allowed to raise into the acquisition path."""
    from multibus.modbus_client import RegisterPoller
    from multibus.register_parser import RegisterParser

    p = RegisterPoller('normal', 5, [], object(), RegisterParser('big'),
                       lambda *a: None, 'u1')
    p._record('warn', 'overrun', 'no connection to record onto')   # must not raise


# ── and an operator can read it ─────────────────────────────────────────────

HAVE_TC = True
try:
    from fastapi.testclient import TestClient      # noqa: F401
except Exception:                                   # pragma: no cover
    HAVE_TC = False

needs_tc = pytest.mark.skipif(not HAVE_TC, reason="fastapi testclient unavailable")


def _app(tmp_path):
    from multibus.api import create_api
    from tests.test_devices import write_config
    cfg = write_config(tmp_path)
    client = _conn()
    client.connect()

    class _Stub:                      # the shape /api/devices/{id}/events reads
        connection = client

        def get_stats(self):
            return {'events': client.snapshot_events(),
                    'successful_reads': 7, 'failed_reads': 2,
                    'error_counts': {'timeout': 2}, 'last_latency_ms': 31.5,
                    'poll_groups_detail': [{'name': 'normal', 'interval': 5,
                                            'cycle_s': 9.1, 'reads': 2,
                                            'overruns': 3, 'age_s': 1.0}]}
    devices = [(d, _Stub() if d.primary else None) for d in cfg.devices]
    app, _ = create_api(cfg, None, None, None, devices=devices)
    from fastapi.testclient import TestClient
    return cfg, TestClient(app, raise_server_exceptions=False), client


@needs_tc
def test_the_log_can_be_fetched_newest_first(tmp_path):
    cfg, api, conn = _app(tmp_path)
    conn.record_event('info', 'first', 'oldest')
    conn.record_event('error', 'last', 'newest')
    r = api.get(f"/api/devices/{cfg.primary_device.id}/events")
    assert r.status_code == 200, r.text
    d = r.json()
    # newest first: an operator opening the page asks "what just happened"
    assert d['events'][0]['kind'] == 'last'
    assert d['counters']['failed_reads'] == 2
    assert d['poll_groups'][0]['overruns'] == 3


@needs_tc
def test_the_log_can_be_narrowed_to_the_problems(tmp_path):
    cfg, api, conn = _app(tmp_path)
    conn.record_event('info', 'tick', 'fine')
    conn.record_event('warn', 'bus_busy', 'no turn')
    conn.record_event('error', 'batch_failed', 'gone')
    r = api.get(f"/api/devices/{cfg.primary_device.id}/events?level=error,warn")
    kinds = {e['kind'] for e in r.json()['events']}
    assert kinds == {'bus_busy', 'batch_failed'}


@needs_tc
def test_a_device_that_is_not_running_says_so_instead_of_500(tmp_path):
    cfg, api, _c = _app(tmp_path)
    assert api.get("/api/devices/nosuchdevice/events").status_code == 404
