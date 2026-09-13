# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""ONE liveness verdict for the whole product: a device is alive when its
acquisition pipeline is PRODUCING, not when a socket happens to be open.

The transport flag (`client.connected`) clears only on an explicit disconnect
or the wedge backstop's forced reopen, so a datalogger that went dark overnight
kept reporting "online" to Home Assistant, to MQTT and to the alert log while
its data froze. `data_health()` already owns the freshness verdict.
"""
from multibus.device_registry import client_health, client_is_live


class _Client:
    def __init__(self, status, connected=True):
        self._status = status
        self.connected = connected

    def data_health(self, stale_threshold_s=30):
        return {"status": self._status}


class _Broken:
    connected = True

    def data_health(self, stale_threshold_s=30):
        raise RuntimeError("boom")


def test_down_is_not_live_even_with_an_open_socket():
    assert client_is_live(_Client("down", connected=True)) is False
    assert client_health(_Client("down")) == "down"


def test_ok_and_degraded_are_live():
    assert client_is_live(_Client("ok")) is True
    assert client_is_live(_Client("degraded")) is True   # slow, not gone
    assert client_health(_Client("degraded")) == "degraded"


def test_no_client_is_not_live():
    assert client_is_live(None) is False
    assert client_health(None) == "idle"


def test_a_health_probe_never_breaks_its_caller():
    # a client that cannot answer falls back to the transport flag rather than
    # taking down the harvester thread that asked
    assert client_is_live(_Broken()) is True
    assert client_health(_Broken()) == "ok"


def test_every_client_type_implements_the_verdict():
    """client_is_live is called on whatever the registry holds — Modbus, HTTP
    and MQTT-input clients alike must answer data_health()."""
    from multibus.http_client import HttpClient
    from multibus.modbus_client import ModbusClient
    from multibus.mqtt_input import MqttInputClient
    for cls in (ModbusClient, HttpClient, MqttInputClient):
        assert callable(getattr(cls, "data_health", None)), cls.__name__


class _Cold:
    """A client whose pollers never started: data_health() answers `ok` because
    nothing has been polled, not because anything is healthy."""

    def __init__(self, connected):
        self.connected = connected

    def data_health(self, stale_threshold_s=30):
        return {"status": "ok", "last_success_ts": None, "connected": self.connected}


def test_a_client_that_never_read_anything_is_not_live_unless_connected():
    # the endpoint symptom: three unreachable units reported green under an
    # `offline` endpoint, because `ok` also means "nothing polled yet"
    assert client_is_live(_Cold(connected=False)) is False
    assert client_health(_Cold(connected=False)) == "degraded"
    # connected but still warming up → alive; a cold start must not scream down
    assert client_is_live(_Cold(connected=True)) is True
    assert client_health(_Cold(connected=True)) == "ok"


def test_a_client_that_has_read_before_trusts_the_freshness_verdict():
    class _Warm:
        connected = True          # the flag that survived a vanished endpoint

        def __init__(self, status):
            self._s = status

        def data_health(self, stale_threshold_s=30):
            return {"status": self._s, "last_success_ts": 1_700_000_000}

    assert client_is_live(_Warm("ok")) is True
    assert client_is_live(_Warm("degraded")) is True
    assert client_is_live(_Warm("down")) is False


class _Empty:
    """A client with nothing selected to read — its data_health() says so."""
    connected = True

    def data_health(self, stale_threshold_s=30):
        return {"status": "idle", "last_success_ts": None, "connected": True}


def test_a_client_with_nothing_to_read_is_idle_not_live():
    """UI audit 4.1: a unit polling nothing wore a green light for hours. It
    is neither healthy nor broken — it is idle, grey, and not counted online."""
    assert client_health(_Empty()) == "idle"
    assert client_is_live(_Empty()) is False


def test_an_idle_source_does_not_vote_on_the_unit():
    from multibus.multi_source import MultiSourceClient
    from multibus.config import SourceConfig

    class _Drv:
        def __init__(self, status, connected=True):
            self._s, self.connected = status, connected

        def data_health(self, *a, **kw):
            return {"status": self._s, "last_success_ts": 1.0, "connected": self.connected}

    def unit(*statuses):
        parts = [(SourceConfig(id=f"s{i}"), _Drv(s)) for i, s in enumerate(statuses)]
        return MultiSourceClient("u", parts).data_health()

    assert unit("idle", "ok")["status"] == "ok"           # the empty source is silent
    assert unit("idle", "down")["status"] == "down"
    assert unit("idle", "idle")["status"] == "idle"       # nothing reads → idle
    assert unit("idle", "idle")["sources"] == {"s0": "idle", "s1": "idle"}
