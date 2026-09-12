# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Poller startup jitter: the first read is staggered by a random delay in
[0, min(interval, startup_jitter_s)] so devices/groups don't fire in lock-step
on a shared transport at boot. 0 = off (fire immediately)."""
from unittest.mock import MagicMock

from multibus.config import SelectedRegister
from multibus.modbus_client import RegisterPoller
from multibus.register_parser import RegisterParser


def _reg():
    return SelectedRegister(address=0, name="v", label="v", unit="V",
                            data_type="uint16", poll_group="realtime")


def _poller(interval, jitter):
    return RegisterPoller("realtime", interval, [_reg()], MagicMock(),
                          RegisterParser(), publish_callback=lambda *a: None,
                          startup_jitter_s=jitter)


def test_jitter_is_capped_at_the_interval():
    # a slow (60s) group asked for 120s jitter is clamped to its own cadence
    assert _poller(60, 120).startup_jitter_s == 60
    assert _poller(5, 2).startup_jitter_s == 2
    assert _poller(1, 10).startup_jitter_s == 1


def test_zero_jitter_is_off():
    assert _poller(5, 0).startup_jitter_s == 0.0


def test_negative_or_bad_jitter_is_off():
    assert _poller(5, -3).startup_jitter_s == 0.0
    assert _poller(5, "nan").startup_jitter_s == 0.0


def test_no_jitter_fires_immediately(monkeypatch):
    # with jitter 0 the run loop must not wait before the first poll
    p = _poller(5, 0)
    waits = []

    def fake_wait(d=None):
        waits.append(d)
        p.stop()                             # real primitive: sets the stop event
        return True

    monkeypatch.setattr(p._stop_event, "wait", fake_wait)
    p.connection.read_registers.return_value = [0]
    p.run()
    # the only wait is the end-of-loop cadence wait, never a jitter pre-wait
    assert len(waits) == 1                    # not [<jitter>, <cadence>]
    # fixed RATE: the wait is the interval MINUS what the sweep just cost, so a
    # 5 s interval stays a 5 s cadence instead of becoming 5 s + read time
    assert 4.9 <= waits[0] <= 5.0


def test_jitter_waits_before_first_poll(monkeypatch):
    p = _poller(5, 3)
    monkeypatch.setattr("multibus.modbus_client.random.uniform",
                        lambda a, b: 2.5)     # deterministic jitter
    waits = []

    def fake_wait(d=None):
        waits.append(d)
        p.stop()                              # stop → exit before any poll
        return True

    monkeypatch.setattr(p._stop_event, "wait", fake_wait)
    p.run()
    assert waits[0] == 2.5                     # jittered first, before any poll


def test_config_threads_jitter_from_polling_section(tmp_path):
    import yaml
    from multibus.config import Config
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "modbus": {"host": "1.2.3.4"},
        "polling": {"default_interval": 5, "startup_jitter_s": 2.5},
    }))
    c = Config(str(cfg_path))
    assert c.modbus.startup_jitter_s == 2.5
    # round-trips through to_dict + save
    assert c.to_dict()["modbus"]["startup_jitter_s"] == 2.5


def test_the_wait_is_the_interval_minus_the_sweep(monkeypatch):
    """Fixed RATE, not fixed delay: the interval is the cadence the operator
    asked for, not a pause bolted onto however long the bus took. A 3 s sweep
    under a 5 s interval used to produce an 8 s cadence."""
    p = _poller(5, 0)
    waits = []
    clock = {"t": 0.0}
    monkeypatch.setattr("multibus.modbus_client.time.monotonic", lambda: clock["t"])

    def slow_read(*a, **kw):
        clock["t"] += 3.0                     # the sweep costs 3 s
        return [0]

    p.connection.read_registers.side_effect = slow_read

    def fake_wait(d=None):
        waits.append(d)
        p.stop()
        return True

    monkeypatch.setattr(p._stop_event, "wait", fake_wait)
    p.run()
    assert waits == [2.0]                     # 5 - 3, not 5
    assert p.last_cycle_s == 3.0


def test_a_sweep_that_outruns_its_interval_still_gets_a_breather(monkeypatch):
    """A group that cannot keep up must not poll back-to-back: that hammers a
    bus which is already the reason it is late."""
    p = _poller(5, 0)
    waits = []
    clock = {"t": 0.0}
    monkeypatch.setattr("multibus.modbus_client.time.monotonic", lambda: clock["t"])
    p.connection.read_registers.side_effect = lambda *a, **kw: (
        clock.__setitem__("t", clock["t"] + 9.0) or [0])

    def fake_wait(d=None):
        waits.append(d)
        p.stop()
        return True

    monkeypatch.setattr(p._stop_event, "wait", fake_wait)
    p.run()
    assert waits == [0.5]                     # 10% of the interval, never 0
    assert p.overruns == 1
