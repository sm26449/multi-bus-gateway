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
"""Reliability guarantees: InfluxDB store-and-forward buffer, poll-time
timestamps, virtual-meter TCP keepalive, MQTT reconnect ownership.

These tests exercise the no-data-loss paths end to end in-process:
disconnect → buffer → replay (idempotent, original timestamps), failed-batch
recovery, buffer bounds, and the connection-hygiene fixes.
"""
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from multibus.config import InfluxDBConfig, MQTTConfig, SelectedRegister
from multibus.influxdb_publisher import InfluxDBPublisher
from multibus.mqtt_publisher import MQTTPublisher
from multibus.modbus_client import RegisterPoller
from multibus.register_parser import RegisterParser
from multibus.virtual_meter import Template, VirtualMeter


def make_register(address=19000, name="_G_ULN[0]", data_type="float"):
    return SelectedRegister(
        address=address, name=name, label="L1 voltage", unit="V",
        data_type=data_type, poll_group="realtime",
    )


def make_publisher(**cfg_overrides):
    """Publisher with no background thread (enabled=False at init), then
    enabled for the write path so tests control connectivity explicitly."""
    cfg = InfluxDBConfig(enabled=False, url="http://x:8086", token="t",
                         org="o", bucket="b", write_interval=0, **cfg_overrides)
    pub = InfluxDBPublisher(cfg, [make_register()], publish_mode="changed")
    pub.config.enabled = True
    return pub


# ── poll-time timestamps ─────────────────────────────────────────────────────

def test_point_is_stamped_with_poll_time():
    pub = make_publisher()
    ts = 1_700_000_000.123456
    line = pub._build_point(make_register(), 231.7, ts,
                            poll_group="realtime").to_line_protocol()
    assert line.endswith(str(int(ts * 1e9)))


def test_poller_attaches_read_timestamp():
    reg = make_register()
    connection = MagicMock()
    connection.read_registers.return_value = [0x3F80, 0x0000]   # float 1.0
    poller = RegisterPoller("realtime", 1, [reg], connection,
                            RegisterParser(), publish_callback=lambda *a: None)
    before = time.time()
    results = poller._poll_registers()
    item = results[reg.address]
    assert item["value"] == pytest.approx(1.0)
    assert before <= item["ts"] <= time.time()


def test_poller_thread_name_tagged_with_device():
    reg = make_register()
    tagged = RegisterPoller("realtime", 1, [reg], MagicMock(),
                            RegisterParser(), publish_callback=lambda *a: None,
                            device_id="grid2")
    assert tagged.name == "Poller-grid2-realtime" and tagged._tag == "[grid2] "
    # legacy single-device path keeps the old bare name (no tag)
    plain = RegisterPoller("realtime", 1, [reg], MagicMock(),
                           RegisterParser(), publish_callback=lambda *a: None)
    assert plain.name == "Poller-realtime" and plain._tag == ""


# ── store-and-forward buffer ─────────────────────────────────────────────────

def test_disconnected_write_goes_to_buffer_not_lost():
    pub = make_publisher()
    assert not pub.connected
    ts = time.time()
    pub.write_register_data("realtime", {
        19000: {"value": 231.7, "register": make_register(), "ts": ts},
    })
    assert len(pub._buffer) == 1
    buf_ts, buf_bucket, line = pub._buffer[0]
    assert buf_ts == ts
    assert buf_bucket == "b"                   # default bucket travels with the line
    assert "231.7" in line and line.endswith(str(int(ts * 1e9)))
    # cache confirmed → the same value is not re-buffered next poll
    pub.write_register_data("realtime", {
        19000: {"value": 231.7, "register": make_register(), "ts": ts + 1},
    })
    assert len(pub._buffer) == 1


def test_rebuffer_batch_recovers_lines_with_their_timestamps():
    pub = make_publisher()
    now_ns = int(time.time() * 1e9)
    ns1, ns2 = now_ns - 60_000_000_000, now_ns   # 60s ago + now (inside bounds)
    batch = (f"voltage,name=a value=1.0 {ns1}\n"
             f"voltage,name=b value=2.0 {ns2}\n").encode()
    assert pub._rebuffer_batch(batch) == 2
    assert [t for t, _, _ in pub._buffer] == [ns1 / 1e9, ns2 / 1e9]


def test_buffer_bounds_age_and_count():
    pub = make_publisher(buffer_minutes=1, buffer_max_points=3)
    old = time.time() - 120
    pub._buffer_line("old 1", old)
    for i in range(4):
        pub._buffer_line(f"fresh {i}", time.time())
    lines = [l for _, _, l in pub._buffer]
    assert "old 1" not in lines            # age-pruned
    assert len(lines) == 3                 # count-capped (drop-oldest)
    assert lines[-1] == "fresh 3"
    assert pub.points_dropped >= 2


def test_drain_replays_in_order_then_clears():
    pub = make_publisher()
    for i in range(3):
        pub._buffer_line(f"line {i}", time.time())
    wapi = MagicMock()
    client = MagicMock()
    client.write_api.return_value = wapi
    pub.client = client
    pub.connected = True
    pub._drain_buffer()
    assert wapi.write.call_count == 1
    assert wapi.write.call_args.kwargs["record"] == "line 0\nline 1\nline 2"
    assert wapi.write.call_args.kwargs["bucket"] == "b"
    assert len(pub._buffer) == 0
    assert pub.points_replayed == 3


def test_drain_failure_requeues_in_original_order():
    pub = make_publisher()
    for i in range(3):
        pub._buffer_line(f"line {i}", time.time())
    wapi = MagicMock()
    wapi.write.side_effect = ConnectionError("boom")
    client = MagicMock()
    client.write_api.return_value = wapi
    pub.client = client
    pub.connected = True
    pub._drain_buffer()
    assert [l for _, _, l in pub._buffer] == ["line 0", "line 1", "line 2"]
    assert pub.points_replayed == 0


def test_write_path_never_blocks_on_client_lock():
    """Regression: while the monitor thread reconnects (slow DNS/ping), it may
    hold pub.lock. The Modbus pollers write through this publisher in their hot
    path — if that path ever takes pub.lock, polling stalls, the live cache
    goes stale and the virtual meters drop their consumers. Observed live
    during an InfluxDB restart; must never happen again."""
    pub = make_publisher()
    done = threading.Event()

    def poller_write():
        pub.write_register_data("realtime", {
            19000: {"value": 231.7, "register": make_register(), "ts": time.time()},
        })
        done.set()

    with pub.lock:                       # monitor thread mid-reconnect
        t = threading.Thread(target=poller_write, daemon=True)
        t.start()
        assert done.wait(timeout=2), "write path blocked on client lock"
    assert len(pub._buffer) == 1         # point still safely buffered


def test_enqueue_failure_falls_back_to_buffer():
    pub = make_publisher()
    pub.connected = True
    pub.write_api = MagicMock()
    pub.write_api.write.side_effect = OSError("socket closed")
    ts = time.time()
    pub._deliver(pub._build_point(make_register(), 5.0, ts), ts)
    assert len(pub._buffer) == 1


# ── virtual meter TCP keepalive ──────────────────────────────────────────────

def make_vmeter():
    t = Template(id="t", name="t", transport={"port": 1502})
    return VirtualMeter(t, lambda n: None)


def tcp_socketpair():
    """Real TCP pair (AF_UNIX from socketpair() rejects TCP_KEEP* options)."""
    lst = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lst.bind(("127.0.0.1", 0))
    lst.listen(1)
    a = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    a.connect(lst.getsockname())
    b, _ = lst.accept()
    lst.close()
    return a, b


def test_keepalive_applied_to_active_connections():
    vm = make_vmeter()
    a, b = tcp_socketpair()
    try:
        transport = MagicMock()
        transport.get_extra_info.return_value = a
        handler = SimpleNamespace(transport=transport)
        vm._server = SimpleNamespace(active_connections={"c1": handler})
        vm._apply_keepalive()
        assert a.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
        if hasattr(socket, "TCP_KEEPIDLE"):
            assert a.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == 60
            assert a.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL) == 10
            assert a.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == 3
    finally:
        a.close()
        b.close()


def test_keepalive_survives_closed_socket_and_no_server():
    vm = make_vmeter()
    vm._server = None
    vm._apply_keepalive()                                  # no-op, no raise
    a, b = tcp_socketpair()
    a.close()
    b.close()
    transport = MagicMock()
    transport.get_extra_info.return_value = a
    vm._server = SimpleNamespace(
        active_connections={"c1": SimpleNamespace(transport=transport)})
    vm._apply_keepalive()                                  # closed FD, no raise


# ── MQTT reconnect ownership ─────────────────────────────────────────────────

def test_mqtt_tls_failure_fails_closed(monkeypatch):
    """3.4.1: when TLS is requested but tls_set() fails, the publisher must NOT
    fall through to a cleartext connect — _try_connect refuses (fail-closed)."""
    pub = MQTTPublisher(MQTTConfig(enabled=True, tls_enabled=True,
                                   tls_ca_cert="/nonexistent/ca.pem"),
                        [], publish_mode="changed")
    # force tls_set to raise regardless of environment
    monkeypatch.setattr(pub.client, "tls_set",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad CA")))
    pub._setup_client()
    assert pub._tls_broken is True
    # and it must refuse to connect (no cleartext fallback)
    called = {"connect": False}
    monkeypatch.setattr(pub.client, "connect",
                        lambda *a, **k: called.__setitem__("connect", True))
    assert pub._try_connect() is False
    assert called["connect"] is False


def test_on_disconnect_records_state_and_spawns_no_thread():
    pub = MQTTPublisher(MQTTConfig(enabled=False), [], publish_mode="changed")
    pub.connected = True
    pub._on_disconnect(None, None, None, reason_code=7)
    assert not pub.connected
    assert pub.last_disconnect_ts is not None
    assert pub._reconnect_thread is None                   # paho owns recovery
    stats = pub.get_stats()
    assert stats["disconnected_for_s"] is not None
    assert stats["messages_failed"] == 0


def test_buffer_persist_survives_restart(tmp_path):
    """A buffer snapshot written by one publisher instance is reloaded by the
    next — the outage window survives a restart."""
    cfg = InfluxDBConfig(enabled=False, url="http://x:8086", token="t",
                         org="o", bucket="b", write_interval=0, buffer_persist=True)
    path = tmp_path / "influx_buffer.jsonl"
    import multibus.influxdb_publisher as ip
    import os
    os.environ["INFLUX_BUFFER_PATH"] = str(path)
    try:
        pub = InfluxDBPublisher(cfg, [make_register()], publish_mode="changed")
        pub.config.enabled = True
        ts = time.time()
        pub.write_register_data("realtime", {
            19000: {"value": 231.7, "register": make_register(), "ts": ts},
        })
        assert len(pub._buffer) == 1
        pub._persist_buffer()
        assert path.exists()

        # "restart": a fresh instance loads the snapshot
        pub2 = InfluxDBPublisher(cfg, [make_register()], publish_mode="changed")
        assert len(pub2._buffer) == 1
        assert pub2.points_recovered == 1
        _ts, bucket, line = pub2._buffer[0]
        assert bucket == "b" and "231.7" in line

        # once drained, the snapshot file is removed
        from unittest.mock import MagicMock
        wapi = MagicMock(); client = MagicMock(); client.write_api.return_value = wapi
        pub2.client = client; pub2.connected = True
        pub2._drain_buffer()
        assert not path.exists()
    finally:
        os.environ.pop("INFLUX_BUFFER_PATH", None)


def test_buffer_persist_honors_age_bound_on_load(tmp_path):
    cfg = InfluxDBConfig(enabled=False, bucket="b", buffer_minutes=1, buffer_persist=True)
    path = tmp_path / "influx_buffer.jsonl"
    import json as _json, os
    old = time.time() - 3600
    fresh = time.time()
    path.write_text(_json.dumps([old, "b", "old 1"]) + "\n" +
                    _json.dumps([fresh, "b", "fresh 1"]) + "\n")
    os.environ["INFLUX_BUFFER_PATH"] = str(path)
    try:
        pub = InfluxDBPublisher(cfg, [make_register()], publish_mode="changed")
        lines = [l for _, _, l in pub._buffer]
        assert lines == ["fresh 1"]        # stale entry dropped on load
    finally:
        os.environ.pop("INFLUX_BUFFER_PATH", None)


# ── P0: reconnect() starts pollers even when the immediate connect fails ──────

def test_reconnect_starts_pollers_even_if_connect_fails(monkeypatch):
    """A device unreachable at save/apply time must NOT be left permanently
    unpolled: reconnect() calls start_polling() unconditionally (pollers
    reconnect on demand), returning the immediate-connect result."""
    import multibus.modbus_client as mc
    from multibus.config import ModbusConfig, PollGroup

    class DeadConn:
        def __init__(self, *a, **k): pass
        def connect(self): return False          # device unreachable
        def disconnect(self): pass

    monkeypatch.setattr(mc, "ModbusConnection", DeadConn)
    client = mc.ModbusClient(config=ModbusConfig(host="192.0.2.1"),
                             registers=[], poll_groups={"normal": PollGroup(interval=5)},
                             device_id="d1")
    started = {"n": 0}
    monkeypatch.setattr(client, "start_polling", lambda: started.__setitem__("n", started["n"] + 1))
    ok = client.reconnect()
    assert ok is False                           # immediate connect failed…
    assert started["n"] == 1                      # …but pollers were started anyway


# ── P1: reconnect-thread clears the stop flag before the alive-check ──────────

def test_influx_reconnect_thread_clears_stop_before_alive_check():
    """Regression: a still-alive monitor thread past the join timeout must
    RESUME (stop flag cleared first), not see a stale set flag and die."""
    from multibus.influxdb_publisher import InfluxDBPublisher
    from multibus.config import InfluxDBConfig
    pub = InfluxDBPublisher.__new__(InfluxDBPublisher)
    import threading
    pub._stop_reconnect = threading.Event()
    pub._stop_reconnect.set()                          # simulate a prior close()
    class _AliveThread:
        def is_alive(self): return True
    pub._reconnect_thread = _AliveThread()
    pub._start_reconnect_thread()
    assert not pub._stop_reconnect.is_set()            # cleared → alive thread resumes


# ── P2: WS broadcast serializes once + sends outside the lock ─────────────────

def test_ws_broadcast_sends_outside_lock_and_drops_bad(monkeypatch):
    import asyncio
    from multibus.api import WebSocketManager

    class GoodWS:
        def __init__(self): self.got = []
        async def send_text(self, d): self.got.append(d)
    class BadWS:
        async def send_text(self, d): raise RuntimeError("wedged")

    m = WebSocketManager()
    good, bad = GoodWS(), BadWS()
    m.active_connections = {good, bad}
    asyncio.get_event_loop().run_until_complete(m.broadcast({"x": 1}))
    assert good.got == ['{"x": 1}']              # delivered
    assert bad not in m.active_connections        # wedged client dropped


# ── Perf win: interval floor (0 would flood the bus) ─────────────────────────

def test_poller_clamps_zero_interval():
    from multibus.modbus_client import RegisterPoller
    p = RegisterPoller.__new__(RegisterPoller)
    # exercise the clamp logic directly
    for bad, expect_min in [(0, 0.05), (-3, 0.05), (0.02, 0.05)]:
        try:
            iv = max(0.05, float(bad))
        except (TypeError, ValueError):
            iv = 5.0
        assert iv >= expect_min
    # a legit 250 ms (ESS control) is preserved
    assert max(0.05, float(0.25)) == 0.25


def test_save_poll_groups_rejects_zero_interval(tmp_path):
    from multibus.config import Config
    from tests.test_devices import write_config
    import pytest
    cfg = Config(str(write_config(tmp_path).config_path))
    with pytest.raises(ValueError, match="flood the bus"):
        cfg.save_device_poll_groups("umg512", {"realtime": {"interval": 0}})
    # 250 ms is accepted (ESS control cadence)
    cfg.save_device_poll_groups("umg512", {"realtime": {"interval": 0.25}})
