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
