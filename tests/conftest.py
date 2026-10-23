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


@pytest.fixture(autouse=True)
def _stop_leaked_rules_runtimes():
    """Stop every rules thread the test started (or leaked).

    The 9162777 flake's root cause was a leaked rules thread from an earlier
    test still sweeping its own units under random ordering; that fix
    immunized one victim — this closes the class: any runtime whose thread is
    still alive after the test is stopped before the next one runs.
    """
    yield
    try:
        from multibus.rules_runtime import RulesRuntime
        for rt in list(RulesRuntime._instances):
            try:
                rt.stop()
            except Exception:  # noqa: BLE001 — teardown must never fail a test
                pass
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture(autouse=True)
def _forget_device_ids():
    """create_api tells the expression engine which device ids exist (a
    process-wide provider); one test's devices must not shape another's."""
    yield
    from multibus import expressions
    expressions.set_device_ids_provider(None)


@pytest.fixture(autouse=True)
def _no_role_left_behind():
    """create_api installs its gateway's role process-wide; one test that
    leaves a standby installed would silence every later test's wire,
    writes and announcements."""
    yield
    from multibus import role as gateway_role
    gateway_role.install(None)


@pytest.fixture(autouse=True)
def _reset_attribute_providers():
    """create_api installs process-wide providers (the plant's places and
    attributes for formulas, the sun for the aggregator) bound to that app's
    config: a later test must not read a dead app's files."""
    yield
    from multibus import endpoint_aggregator as _ea, expressions as _ex
    _ex.set_place_provider(None, None)
    _ex.set_attr_provider(None)
    _ex._SUN_CACHE.clear()
    _ea.set_sun_provider(None)
    _ea._last_fresh_elev.clear()
