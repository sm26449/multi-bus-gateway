# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Shared test fixtures."""
import threading

import pytest

_REAL_TIMER = threading.Timer          # before any test patches it


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
def _stop_leaked_leases_and_lanes():
    """Stop every write-lease sweeper and command dispatcher the test made.

    Each create_api starts a lease sweeper; a test that armed a lease (90 s,
    600 s) and left its app behind had that lease fire minutes later, into
    whichever test ran then — a revert retried every 3 s against a dead
    app's devices, its log in another test's capture (cmd-dispatch full
    suite, 2026-10-25). No revert runs here: the app is gone, its leases with
    it. The lanes of its dispatcher end too.
    """
    yield
    try:
        from multibus.command_dispatch import CommandDispatcher
        from multibus.write_lease import WriteLeaseManager
        for lm in list(WriteLeaseManager._instances):
            try:
                lm.stop(revert_now=False)
                with lm._lock:
                    lm._leases.clear()
            except Exception:  # noqa: BLE001 — teardown must never fail a test
                pass
        for cd in list(CommandDispatcher._instances):
            try:
                cd.stop()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture(autouse=True)
def _cancel_leaked_timers():
    """Cancel every threading.Timer a test left pending (RP-15).

    A command with revert_s arms a read-back series of real Timers (revert_s
    + 5 s, …); one left by an earlier test fired inside a later test that
    patches threading.Timer to count the series it arms — the next timer of
    the stale series landed in its list (`[15.0, 120.0, 125.0] == [125.0]`).
    """
    yield
    for t in threading.enumerate():
        if isinstance(t, _REAL_TIMER):
            try:
                t.cancel()
            except Exception:  # noqa: BLE001 — teardown must never fail a test
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


@pytest.fixture(autouse=True)
def _forget_preview_jobs():
    """The preview/scan job registry is process-wide (FLK-8): a job of an
    earlier test still counted as running made a later test's FIRST preview
    answer 429. Every job a test left is asked to stop and forgotten."""
    yield
    from multibus import preview
    with preview._LOCK:
        for job in preview._JOBS.values():
            job.cancel.set()
        preview._JOBS.clear()
        preview._BUSY.clear()


def stop_app_threads() -> None:
    """Stop the background threads an app built by create_api left running
    (FLK-9): its event harvester and its bridge poller — create_api starts
    both, only the lifespan's shutdown stops them, and most tests never run
    it — and an OPC UA server still up (FLK-7: one whose start outlived the
    test's wait served on for the rest of the run)."""
    from multibus import api
    from multibus.bridge_diag import BridgeDiag
    from multibus.opcua.server import OpcUaServer
    for ev in list(api._HARVESTER_STOPS):
        ev.set()
    for bd in list(BridgeDiag._instances):
        bd.stop()
    for srv in list(OpcUaServer._instances):
        if srv._thread is not None:
            srv.stop()


@pytest.fixture(autouse=True)
def _stop_leaked_app_threads():
    """Each harvester + poller pair kept its app alive and woke every 5 s for
    the rest of the run: ~90 pairs by 6 % of a random-order suite, over a
    thousand threads by a third of it — the GIL so contended that a plain
    test took over 120 s, an OPC UA server missed its start window, and the
    process still burned 175 % CPU after the last test, its summary unwritten."""
    yield
    try:
        stop_app_threads()
    except Exception:  # noqa: BLE001 — teardown must never fail a test
        pass


def pytest_sessionfinish(session, exitstatus):
    """Name any non-daemon thread still alive at the end: one would keep
    the process from exiting after its summary (FLK-7/FLK-9)."""
    left = [t for t in threading.enumerate()
            if t is not threading.main_thread() and not t.daemon and t.is_alive()]
    if left:
        tr = session.config.pluginmanager.get_plugin("terminalreporter")
        msg = "non-daemon threads still alive at the end: " + ", ".join(t.name for t in left)
        if tr is not None:
            tr.write_line(msg, yellow=True)
