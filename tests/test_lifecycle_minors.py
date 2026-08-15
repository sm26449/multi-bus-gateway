# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Lifecycle hardening (external audit, backlog §I 'mărunte'):

- start_polling is serialized + idempotent — a boot racing an Apply (or a
  double Apply) must never leave a DOUBLED poller set polling every group
  twice;
- read_bits shares read_registers' failure tail — wedge backstop (forced
  reopen) and the link-verdict gating of 'unreachable' (no per-batch flap);
- the connect-refused backoff follows config.retry_delay (was hardcoded
  100 ms — hammered a down device with connects at 10/s per group);
- vmeters routes stay sync `def` so manager joins run in the threadpool,
  not on the event loop.
"""
import time
from types import SimpleNamespace

from multibus.config import PollGroup, SelectedRegister
from multibus.modbus_client import ModbusClient, ModbusConfig, ModbusConnection


class _Result:
    def __init__(self, regs=None, bits=None):
        self.registers = regs or []
        self.bits = bits or []
        self._err = regs is None and bits is None

    def isError(self):
        return self._err


class _Client:
    ok = False

    def is_socket_open(self): return True
    def connect(self): return True
    def close(self): pass

    def read_holding_registers(self, address, count, device_id):
        return _Result(regs=[0] * count) if _Client.ok else _Result()

    read_input_registers = read_holding_registers

    def read_coils(self, address, count, device_id):
        return _Result(bits=[True] * count) if _Client.ok else _Result()

    read_discrete_inputs = read_coils


def _reg(addr, group="normal"):
    return SelectedRegister(address=addr, name=f"r{addr}", label=f"r{addr}",
                            unit="", data_type="uint16", poll_group=group)


def _mk_client(n_groups=2):
    regs = [_reg(i, group=f"g{i}") for i in range(n_groups)]
    groups = {f"g{i}": PollGroup(interval=3600, description="") for i in range(n_groups)}
    c = ModbusClient(config=ModbusConfig(), registers=regs, poll_groups=groups)
    return c


def _wire_fake(conn: ModbusConnection):
    conn.config.retry_attempts = 1
    _Client.ok = False
    conn.client = _Client()
    conn.connected = True
    conn._new_client = lambda: _Client()
    return conn


# ── double-start guard ───────────────────────────────────────────────────────

def test_double_start_polling_never_doubles_the_poller_set():
    c = _mk_client(n_groups=2)
    _wire_fake(c.connection)
    try:
        c.start_polling()
        first = list(c.pollers)
        assert len(first) == 2
        c.start_polling()                          # double Apply / boot race
        assert len(c.pollers) == 2                 # NOT 4
        for p in first:                            # old set was stopped, not leaked
            assert p._stop_event.is_set()
    finally:
        c.disconnect()


def test_disconnect_clears_the_poller_list():
    c = _mk_client(n_groups=1)
    _wire_fake(c.connection)
    c.start_polling()
    assert len(c.pollers) == 1
    c.disconnect()
    assert c.pollers == []                         # a later start begins clean


def test_http_double_start_polling_guard():
    from multibus.http_client import HttpClient
    hc = HttpClient.__new__(HttpClient)
    import threading
    hc._lifecycle_lock = threading.RLock()
    hc.pollers = []
    hc.registers = [SelectedRegister(address=1, name="p", label="p", unit="",
                                     data_type="float", poll_group="g",
                                     json_path="p")]
    hc.poll_groups = {"g": PollGroup(interval=3600, description="")}
    hc._fetch = lambda: None
    hc.publish_callback = None
    hc._note_success = lambda n: None
    hc._note_failure = lambda: None
    try:
        hc.start_polling()
        assert len(hc.pollers) == 1
        hc.start_polling()
        assert len(hc.pollers) == 1                # NOT 2
    finally:
        for p in hc.pollers:
            p.stop()


# ── read_bits failure tail = read_registers failure tail ─────────────────────

def test_read_bits_gets_the_wedge_backstop_and_link_verdict():
    conn = _wire_fake(ModbusConnection(ModbusConfig()))
    events = []
    conn.record_event = lambda lvl, kind, msg: events.append(kind)
    # below the threshold: failures accumulate, NO unreachable event (DP-8)
    for _ in range(conn._reopen_after_fails - 1):
        assert conn.read_bits(0, 1) is None
    assert "unreachable" not in events
    assert conn.forced_reopens == 0
    # threshold hit: forced reopen + ONE unreachable
    assert conn.read_bits(0, 1) is None
    assert conn.forced_reopens == 1
    assert events.count("forced_reopen") == 1
    assert events.count("unreachable") == 1
    assert conn._consecutive_fail == 0             # counter reset after reopen
    assert conn.batch_failures == conn._reopen_after_fails


def test_read_bits_success_still_works():
    conn = _wire_fake(ModbusConnection(ModbusConfig()))
    _Client.ok = True
    assert conn.read_bits(0, 3) == [True, True, True]


# ── connect-refused backoff honours config ───────────────────────────────────

def test_connect_refused_backoff_uses_retry_delay(monkeypatch):
    conn = ModbusConnection(ModbusConfig())
    conn.config.retry_attempts = 2
    conn.config.retry_delay = 7.5

    class _Refuses(_Client):
        def connect(self): return False
        def is_socket_open(self): return False
    conn.client = _Refuses()
    conn.connected = False
    conn._new_client = lambda: _Refuses()

    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    assert conn.read_registers(0, 1) is None
    assert 7.5 in slept and 0.1 not in slept
    slept.clear()
    assert conn.read_bits(0, 1) is None
    assert 7.5 in slept and 0.1 not in slept


# ── vmeters routes stay sync (threadpool), never async on the event loop ─────

def test_vmeters_routes_are_sync_defs():
    import asyncio
    import multibus.routes.vmeters as vm
    r = vm.build(SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()),
                                 config=None, audit_log=None, auth_state=None))
    checked = 0
    for route in r.routes:
        fn = getattr(route, "endpoint", None)
        if fn is not None:
            assert not asyncio.iscoroutinefunction(fn), route.path
            checked += 1
    assert checked >= 10                            # the module's route surface


# ── reconcile pass vs the re-verified external list (post-3.32.0) ────────────

def test_read_bits_success_resets_shared_fail_counter():
    # mixed device: register failures + bits SUCCESSES share one wire — a
    # bits success must reset the counter or the paths interfere and trip a
    # bogus forced reopen
    conn = _wire_fake(ModbusConnection(ModbusConfig()))
    conn.record_event = lambda *a: None
    for _ in range(ModbusConnection._reopen_after_fails if isinstance(
            getattr(ModbusConnection, '_reopen_after_fails', None), int) else 4):
        conn.read_registers(0, 1)
    assert conn._consecutive_fail > 0
    _Client.ok = True
    assert conn.read_bits(0, 1) == [True]
    assert conn._consecutive_fail == 0            # link proven healthy


def test_boot_discovery_hooks_are_write_aware(tmp_path):
    import pytest
    try:
        from fastapi.testclient import TestClient  # noqa: F401
    except Exception:
        pytest.skip("TestClient not installed")
    from multibus.api import create_api
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml="""
devices:
  - id: em24-hala
    connection: { protocol: tcp, host: 192.168.1.42, port: 1502, unit_id: 5 }
""")
    calls = []

    class _Pub:
        connected = False

        def __init__(self):
            self.discovery_hooks = []

        def publish_device_discovery(self, *a, **kw):
            calls.append(kw)

        def __getattr__(self, name):              # everything else → no-op
            return lambda *a, **k: None
    pub = _Pub()
    create_api(cfg, None, pub, None, devices=[(d, None) for d in cfg.devices])
    # external audit: main.py's write-blind hooks used to own boot; now
    # create_api registers the write-aware set itself
    assert len(pub.discovery_hooks) == 1
    pub.discovery_hooks[0]()
    assert calls and "write_rules" in calls[0]


def test_encode_string_roundtrips_under_all_orders():
    # 'ABCD' must read back as 'ABCD' in every byte order — strings are
    # byte-sequential; ordering is a numeric-only concern (parser contract)
    from multibus.encoder import RegisterEncoder
    from multibus.register_parser import RegisterParser
    for order in ("big", "little", "badc", "dcba"):
        enc = RegisterEncoder(order)
        par = RegisterParser(order)
        words = enc.encode_string("ABCD", 3)
        assert par._parse_string(words) == "ABCD", order
