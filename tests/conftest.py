# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Shared test fixtures."""
import pytest


@pytest.fixture(autouse=True)
def _isolate_shared_endpoints():
    """Give every test a clean set of access points.

    The socket a master device fronts its units with is pooled per host:port
    and outlives the connection objects that borrow it — which is the whole
    point in production, and cross-contamination in a test file. Most tests
    build a connection from the default ModbusConfig, so without this they all
    land on one pooled socket and inherit whichever fake client the previous
    test happened to install.
    """
    from multibus import modbus_client as mc
    mc._TRANSPORTS.clear()
    mc._ARBITERS.clear()
    yield
    mc._TRANSPORTS.clear()
    mc._ARBITERS.clear()
