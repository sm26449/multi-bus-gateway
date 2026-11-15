# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""ADR-0038 #3: the publishers live behind a holder that is mutated in place,
never rebound. The property that matters is that a reader holding the holder
(a closure, a service, a router) sees a publisher the apply path swapped —
which is exactly how create_api's sink lambdas read _pub.mqtt / _pub.influx.
"""
from multibus.outputs.holder import PublisherHolder


def test_holder_starts_with_the_two_publishers():
    h = PublisherHolder("mqtt0", "influx0")
    assert (h.mqtt, h.influx) == ("mqtt0", "influx0")


def test_holder_defaults_to_no_sinks():
    h = PublisherHolder()
    assert h.mqtt is None and h.influx is None


def test_a_retained_reference_sees_a_swap():
    """What the nonlocal rebind used to do, without rebinding anything: a reader
    that kept the holder sees the new object after the apply path swaps it."""
    h = PublisherHolder(mqtt="old")
    seen = h  # a service/router keeps the holder, not the publisher
    h.mqtt = "new"           # /api/config/apply swaps the live publisher
    assert seen.mqtt == "new"


def test_a_closure_reading_at_call_time_sees_a_swap():
    """The create_api pattern: lambda: (_pub.mqtt, _pub.influx)."""
    h = PublisherHolder(mqtt="old_m", influx="old_i")
    resolve = lambda: (h.mqtt, h.influx)  # noqa: E731 — mirrors the calc-engine sink lambda
    assert resolve() == ("old_m", "old_i")
    h.mqtt, h.influx = "new_m", "new_i"
    assert resolve() == ("new_m", "new_i")


def test_the_holder_cannot_grow_stray_attributes():
    """__slots__ keeps it to the two sinks — a typo'd slot name fails loudly
    instead of silently creating a dead attribute no reader consults."""
    h = PublisherHolder()
    try:
        h.mqqt = "typo"  # noqa: — intentional wrong name
    except AttributeError:
        return
    raise AssertionError("PublisherHolder should reject unknown attributes")


# --- REFAC-HOLDER-1 (ADR-0038 amendment 2026-11-07): the holder is NOT enough on its
# own. RulesRuntime and SequenceRunner capture the publisher OBJECT at construction (they
# do not read the holder each call, unlike the calc-engine lambda above). So when a sink
# that was OFF at boot is enabled through /api/config/apply — which BUILDS a new publisher
# object rather than mutating one in place — the apply path must re-point those capturers,
# or rules and sequences publish nothing until the next restart. ---
from tests.test_endpoints import needs_tc  # noqa: E402


@needs_tc
def test_apply_enabling_sinks_repoints_rules_and_sequences(tmp_path, monkeypatch):
    from tests.test_commands_api import _app
    import multibus.routes.config_apply as capply

    cfg, app, client = _app(tmp_path, {})                 # mqtt=None -> both sinks off at boot
    rt, seq = app.state.rules_runtime, app.state.sequences
    assert rt.mqtt is None and rt.influx is None and seq.mqtt is None  # captured the boot-time None

    class _FakeMqtt:
        def __init__(self, **kw):
            self.config = cfg.mqtt
        def connect(self):
            return False                                  # skip HA discovery / real I/O
        def disconnect(self):
            pass

    class _FakeInflux:
        def __init__(self, **kw):
            self.connected = False
        def close(self):
            pass

    monkeypatch.setattr(capply, "MQTTPublisher", _FakeMqtt)
    monkeypatch.setattr(capply, "InfluxDBPublisher", _FakeInflux)

    cfg.mqtt.enabled = True                               # operator turned both sinks on
    cfg.influxdb.enabled = True
    r = client.post("/api/config/apply")
    assert r.status_code == 200, r.text

    assert isinstance(rt.mqtt, _FakeMqtt), "rules_runtime.mqtt still points at the dead boot-time None"
    assert isinstance(seq.mqtt, _FakeMqtt), "sequences.mqtt still points at the dead boot-time None"
    assert isinstance(rt.influx, _FakeInflux), "rules_runtime.influx still points at the dead boot-time None"
