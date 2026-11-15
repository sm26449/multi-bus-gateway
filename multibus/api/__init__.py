# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
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
"""REST API and WebSocket server for Janitza Monitor."""

import asyncio
import json
import logging
import os
import re
import threading
import time
import weakref
from typing import Any, Callable, Dict, Optional, Set, Tuple
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from multibus import __version__
from multibus.outputs.holder import PublisherHolder
from multibus.core.config import (MODBUS_PROTOCOLS, NETWORK_PROTOCOLS,
                     SERIAL_PROTOCOLS,
                     VIRTUAL_MAP_PROTOCOLS)
from multibus.core.config import CAN_PROTOCOLS  # noqa: E402 — CAN (can_client.py), listen-only

# Module-level helpers extracted into api/ submodules (P0.8b); re-exported here
# so create_api's body and the routes/tests that import them from multibus.api
# keep working unchanged.
from multibus.devices.device_validation import (  # noqa: F401,E402
    _is_serial_path, _serial_host_error, _vmap_mode, _vmap_mode_missing)
from multibus.api.ui_assets import (  # noqa: F401,E402
    _canonical_redirect_script, _render_index_html, ui_build)
from multibus.api.resources import _read_self_resources  # noqa: F401,E402
from multibus.api.ws import WebSocketManager  # noqa: F401,E402
from multibus.api.driver_names import _driver_reg_names  # noqa: F401,E402
# DeviceValidator (P0.8b Lot B) holds the device-payload validation that used to
# be nested in create_api; the constants moved with it and are imported back for
# the sibling routes that still live here.
from multibus.api.validation import (  # noqa: E402
    DeviceValidator)
from multibus.api.probe import ProbeService  # noqa: E402 — probe/test seam (P0.8b squeeze)
from multibus.api.discovery import DiscoveryService  # noqa: E402 — attr-meta + HA-discovery seam (P0.8b squeeze)
from multibus.api.reload import ReloadService  # noqa: E402 — config reload/restore tail (P0.8b squeeze)
from multibus.outputs.alert_harvest import EventHarvester  # noqa: E402 — event/alert harvester (P0.8b squeeze)
from multibus.api.command_exec import CommandExec  # noqa: E402 — command write-path seam (P0.8b squeeze)
from multibus.api.rules_seq import RulesSeq  # noqa: E402 — rules-reload + sequence-run seam (P0.8b squeeze)
from multibus.api.device_service import DeviceService  # noqa: E402 — device CRUD/lifecycle seam (P0.8b squeeze)
from multibus.api.middleware import Middleware  # noqa: E402 — HTTP middleware/security guards (P0.8b squeeze)
from multibus.api.read_model import DeviceReadModel  # noqa: E402 — device read-model + write decorators (P0.8b squeeze)
from multibus.api.lifecycle import Lifecycle  # noqa: E402 — restart + role-change/take-over hooks (P0.8b squeeze)
from multibus.api.view import DeviceView  # noqa: E402 — device-row view seam (P0.8b squeeze)

_POLLED_MODBUS = NETWORK_PROTOCOLS + SERIAL_PROTOCOLS      # every Modbus way but listening
# what each kind of editor may offer — one place, served to the UI
# (/api/protocols) so a form never lists a protocol the API refuses, nor
# loses one the API accepts
PROTOCOLS_BY_CONTEXT = {
    'device': MODBUS_PROTOCOLS + VIRTUAL_MAP_PROTOCOLS + ('http', 'mqtt') + CAN_PROTOCOLS,
    'source': _POLLED_MODBUS + ('http', 'mqtt'),
    'endpoint': NETWORK_PROTOCOLS + ('rtu_tap',),
    'group': NETWORK_PROTOCOLS + ('rtu_tap', 'http'),
    # a device that IS its meters (a UPS with no communication of its own)
    'meter_only': ('none',),
}

# the stop of every app's event harvester: the lifespan's shutdown sets its
# own; an app built and dropped without one (a test) is stopped from here
_HARVESTER_STOPS: "weakref.WeakSet[threading.Event]" = weakref.WeakSet()


logger = logging.getLogger(__name__)

# group keys the endpoint's group form does not edit: an edit that omits them keeps them
GROUP_KEPT_KEYS = ('mqtt', 'influxdb', 'opcua', 'channels')

# Pydantic request models shared with the route modules — re-exported so
# janitza.api.<Model> references (tests, tooling) keep working.
from multibus.routes._models import (  # noqa: F401,E402 — re-exported for external refs
    InfluxDBConfigUpdate, ModbusConfigUpdate, MQTTConfigUpdate,
    RegisterBatchQuery, RegisterQuery, SelectedRegisterUpdate, ThresholdConfig)


def create_api(config, modbus_client, mqtt_publisher, influxdb_publisher,
               devices=None, template_registry=None) -> FastAPI:
    """
    Create FastAPI application.

    Args:
        config: Application configuration
        modbus_client: ModbusClient instance (device #1 — legacy back-compat)
        mqtt_publisher: MQTTPublisher instance
        influxdb_publisher: InfluxDBPublisher instance
        devices: optional list of (DeviceConfig, ModbusClient) pairs including
            device #1 first (Tier 2 multi-device). None => single legacy device.

    Returns:
        FastAPI application
    """
    # ADR-0038 #3: the live publishers behind one handle, created once and
    # never rebound; /api/config/apply mutates _pub.mqtt / _pub.influx and
    # every reader (services, routes, calc engine) sees the swap.
    _pub = PublisherHolder(mqtt_publisher, influxdb_publisher)

    # WebSocket manager
    ws_manager = WebSocketManager()

    # Store current values for dashboard
    current_values: Dict[int, Dict] = {}
    # `timestamp` (naive ISO, the container's zone) stays as it always was for
    # whoever parses it; `ts` (epoch seconds) is what a browser can place in
    # its own zone
    last_update = {"timestamp": None, "ts": None}

    # Store event loop reference for thread-safe async calls
    main_loop = {"loop": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup
        main_loop["loop"] = asyncio.get_running_loop()
        # after uvicorn configured its loggers: a refused /ws handshake is
        # one line per client per minute, not two per attempt (WS-LOG-1)
        from multibus import ws_log
        ws_log.install()
        logger.info("API started, event loop captured")
        rt = getattr(app.state, 'rules_runtime', None)
        if rt is not None and not getattr(app.state, 'rules_runtime_manual', False):
            rt.start()
        disc = getattr(app.state, 'mqtt_discovery', None)
        if disc is not None and config.mqtt.enabled:
            from multibus.devices.device_runtime import gateway_broker
            disc.start(gateway_broker(config, {"broker": "@gateway"}))
        opcua_ctl = getattr(app.state, 'opcua', None)
        if opcua_ctl is not None:
            opcua_ctl.apply()                      # starts the OPC UA server when it is enabled
        vsp = getattr(app.state, 'vbms_state_publisher', None)
        if vsp is not None:
            vsp.start()                            # Virtual BMS telemetry loop (ADR-0037)
        yield
        if vsp is not None:
            vsp.stop()
        if opcua_ctl is not None:
            opcua_ctl.shutdown()
        if disc is not None:
            disc.stop()
        # Shutdown (cleanup if needed)
        logger.info("API shutting down")
        # what waits for a bus is cancelled and nothing new is taken — but the
        # dead-man's reverts, which main.stop() runs through the lanes next
        # while the devices are still connected (F-28); it stops the
        # dispatcher after them (ADR-0018 D12, CR2-8)
        cd = getattr(app.state, 'command_dispatch', None)
        if cd is not None:
            cd.close()
        if rt is not None:
            rt.stop()
        hs = getattr(app.state, 'harvester_stop', None)
        if hs is not None:
            hs.set()                               # let the event harvester exit its loop
        al = getattr(app.state, 'audit_log', None)
        for co in (getattr(al, 'repeats', None), getattr(al, 'login_failures', None)):
            if co is not None:
                co.flush()                         # the open streaks' counts (AUD-RET-1)
        bd = getattr(app.state, 'bridge_diag', None)
        if bd is not None:
            bd.stop()                              # the bridge diagnostics poller (ADR-0017)
        vbm = getattr(app.state, 'vbms_manager', None)
        if vbm is not None:
            vbm.stop_all()                         # stop any armed Virtual BMS (ADR-0037)

    app = FastAPI(
        title="Multi-Bus Gateway",
        description="Multi-protocol acquisition gateway (Modbus/HTTP/MQTT in — MQTT/InfluxDB/virtual meters out)",
        version=__version__,
        lifespan=lifespan
    )

    # Expose the live value cache so the virtual-meter engine can read it.
    app.state.current_values = current_values

    # config.yaml failed to load: every save is refused in words (409), never
    # an Internal Server Error (QA-D-11)


    # ADR-0038 #4: domain exceptions (api/errors.py) -> HTTP in one place, so a
    # service can raise NotFound/Validation/... instead of an HTTPException and
    # the response stays byte-identical. Inert until a service raises one.
    from multibus.api.errors import register_error_handlers
    register_error_handlers(app)

    # CORS — the UI is same-origin so it needs no CORS; the wildcard only eases
    # read-only third-party access. Credentials are OFF (wildcard + credentials is
    # spec-invalid and a CSRF liability). NOTE: the API is UNAUTHENTICATED, incl.
    # control endpoints — run on a trusted LAN / behind an auth proxy. See README.
    # The UI is served by this same app (same-origin), so cross-origin access
    # is not needed. A wildcard here would let any web page the operator visits
    # fire state-changing POSTs at the gateway (drive-by CSRF on a box that can
    # feed an ESS). Cross-origin API consumers can be added explicitly via
    # CORS_ALLOW_ORIGINS (comma-separated) if ever needed.
    _cors = [o.strip() for o in os.environ.get("CORS_ALLOW_ORIGINS", "").split(",") if o.strip()]
    if _cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=_cors,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
            # a client of another origin reads the device tag (ADR-0030)
            expose_headers=["ETag", "MBG-If-Match"],
        )

    # Compress responses over the LAN: the SPA payload and the 4000+-register
    # catalog compress ~8-25x (988 KB catalog -> ~40 KB), saving bandwidth and
    # transfer time — the CPU cost is on the response path, not the poll loop.
    from fastapi.middleware.gzip import GZipMiddleware
    app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=5)

    # Optional login/auth (off by default). auth_state manages sessions,
    # password hashing and per-IP lockout; middleware enforces it when enabled.
    from multibus import auth as _auth
    # sessions persist next to the other secret-bearing state (0600) so a
    # container restart/upgrade no longer logs everyone out
    auth_state = _auth.AuthState(
        config.ui, store_path=str(config.config_path.parent / "sessions.json"))
    # named tokens for programs (a companion service, a script): a role like a
    # login, sent as Authorization: Bearer, named in the audit log
    from multibus.service_tokens import ServiceTokens
    service_tokens = ServiceTokens(str(config.config_path.parent / "service_tokens.json"))
    # the gateway's role: active unless a companion service (an `ha` token)
    # makes it standby — kept on disk, never changed by the gateway itself
    from multibus.core.role import GatewayRole, install as _install_role
    gw_role = GatewayRole(str(config.config_path.parent / "role.json"))
    _install_role(gw_role)
    app.state.role = gw_role
    if auth_state.enabled and not getattr(config.ui, "tls_enabled", False):
        logger.warning("SECURITY: login is enabled but UI TLS is OFF — the session "
                       "cookie travels in cleartext; a LAN sniffer can hijack it. "
                       "Enable ui.tls or terminate TLS in front of the gateway.")
    if auth_state.enabled and not _auth.is_hashed(getattr(config.ui, "auth_password", "")):
        logger.warning("SECURITY: login is enabled but the admin password is stored "
                       "UNHASHED (likely a hand-edited default like 'admin') — change "
                       "it in the UI / hash it; a plaintext default is trivially guessed.")

    # Write-lease dead-man switch: a leased write auto-reverts to a safe value if
    # the controller stops renewing it.
    from multibus.control.write_lease import WriteLeaseManager
    _lease_mgr = WriteLeaseManager(persist_path=config.config_path.parent / "write_leases.json")
    app.state.lease_mgr = _lease_mgr
    # every write a device is told goes through one dispatcher: a lane per
    # wire, a slot per channel (ADR-0018). Built before boot recovery arms a
    # revert, which travels through it. The sweeper itself starts at the end
    # of create_api: a recovered lease fires at once, and its revert needs
    # what is defined further down (_submit_raw, …) — RP-12
    from multibus.control.command_dispatch import CommandDispatcher
    _cmd_dispatch = CommandDispatcher()
    app.state.command_dispatch = _cmd_dispatch

    # IP allowlist (opt-in): when config.security.allowlist is non-empty, only
    # peers whose IP is in the list (or a listed CIDR) may reach the HTTP
    # API/UI. Loopback and the docker gateway are always allowed so the
    # container's own health probes and same-host access keep working. Empty



    # Security response headers. CSP keeps 'unsafe-inline' because the UI relies
    # on inline event handlers + inline styles (the two XSS-dangerous, data-
    # interpolating handlers were converted to delegation); it still blocks
    # external script/frame injection, clickjacking (frame-ancestors) and
    # base-uri hijack. HSTS is emitted only over HTTPS.
    # No external hosts: bootstrap-icons is vendored (ui/vendor/bootstrap-icons,
    # audit 2026-08-14) — the UI is fully self-contained again, works air-gapped
    # and stops beaconing every operator's browser to a CDN.
    # script-src is 'self' only (3.82.0): the page has no inline script or
    # event handler — every action is a data-action attribute dispatched from
    # app-core.js — so an injected string can never run as code, whatever an
    # escaping slip elsewhere lets through. Inline styles stay allowed.




    # Optional write protection (opt-in, defense-in-depth for a LAN appliance):
    # if API_KEY (or JANITZA_API_KEY) is set, every state-changing request
    # (POST/PUT/PATCH/DELETE) must carry a matching X-API-Key header. Read-only
    # telemetry (GET) and the on-demand query POSTs stay open so the UI works
    # without a key. Unset => fully open (default, backward-compatible).
    _api_key = os.getenv("API_KEY") or os.getenv("JANITZA_API_KEY") or ""

    # OPERATOR: live actions yes, configuration no. Allowed mutations are the
    # commissioning tools (trace/probe/discovery/query), device tests, device
    # WRITES (bounded by the template's write_min/max — that is exactly the
    # operator's job) and logout. Everything that lands in a config file
    # (devices, registers, templates, vmeters, settings, snapshots) is admin's.

    # an `ha` token (a high-availability companion): sets the role, raises
    # alerts — and reads, like a viewer



    # Login/auth gate (opt-in). Open paths always work so the login flow and
    # static assets load; everything else needs a valid session when enabled.
    # A viewer session is read-only (GET/HEAD only).
    # /metrics joins /health: scrapers (Prometheus) can't log in; both stay
    # behind the IP allowlist and expose operational stats only, never config.

    # On demand, these talk to a device (probe, scan, test, query, identify)



    # Tier 2: the DeviceRegistry owns the (DeviceConfig, client) pairs and one
    # live-value store per device; device #1's store IS the legacy
    # current_values dict (alias), so the UI/vmeters/api keep reading the same
    # object. Mutations are atomic inside the registry (device CRUD runs in
    # FastAPI's threadpool); reads are lock-free snapshots, as before.
    from multibus.devices.device_registry import (DeviceRegistry,
                                  client_is_live)
    registry = DeviceRegistry(config.primary_id, current_values)
    for dev_cfg, _client in (devices or []):
        registry.register(dev_cfg, _client)
    app.state.registry = registry
    app.state.device_values = registry.values
    # ADR-0042: the wire-identity each running client was BUILT with. The write
    # heads refuse a write when the live config's wire differs from this (a
    # retarget whose client has not been rebuilt). The registry keeps it — it is
    # updated wherever a client is (re)built or dropped (boot, add, edit, restart,
    # the ADR-0047 P2 reconcile), plus reload's in-place primary reconnect.
    app.state.applied_wire = registry.applied_wire

    # ---- Calculated registers (formula-derived measurements) -------------------
    # Engine in multibus/calc_engine.py (synthetic 8M+ addressing, expression
    # eval, sink routing). store_for preserves the exact legacy store semantics;
    # the lambda reads _pub.mqtt / _pub.influx at call time, so a publisher the
    # apply path swapped (ADR-0038 #3) is picked up without re-wiring.
    from multibus.devices.calc_engine import CalcEngine

    # which tokens of a formula are device ids (`samsung-dvm.x`, `3150-iem.p`)
    from multibus.core import expressions as _expressions
    # an installation id reads its totals in a formula: battery-bank.pack_average_soc
    _expressions.set_device_ids_provider(lambda: [d.id for d in config.devices]
                                         + [str(p.get('id')) for p in (config.endpoints or []) if p.get('id')])
    calc_engine = CalcEngine(config, registry.store_for,
                             lambda: (_pub.mqtt, _pub.influx))
    app.state.calc_engine = calc_engine

    for _dc, _c in registry:
        calc_engine.load(_dc.id)

    # ---- Generic REST push sink (northbound) -----------------------------------
    from multibus.outputs.rest_push import RestPushManager
    rest_push_manager = RestPushManager()
    app.state.rest_push_manager = rest_push_manager
    _REST_HDR_MASK = "••••••"






    # what a device's meters give it is computed when a METER reports: each
    # device's own way of running and routing those values, by id
    calc_runners: Dict[str, Callable] = {}
    app.state.calc_runners = calc_runners
    # a meter-only device asks its meters' clients whether they are alive
    from multibus.devices import meter_only as _meter_only
    _meter_only.find_client = lambda mid: registry.find(mid)[2]

    # ── poller data-callback factory — extracted to devices/ingest.py (Lot D) ─
    # The poll → store → calc → publish fan-out (make_data_callback) and the
    # linked-device nudge (_run_dependents) now live in
    # multibus/devices/ingest.py. create_api wires the factory with its
    # collaborators; make_data_callback stays the same callable, so every call
    # site is unchanged. devices must not import the api layer, so the logger
    # (kept named "multibus.api") and the publisher holder are injected.
    from multibus.devices.ingest import DataCallbackFactory
    _ingest = DataCallbackFactory(
        current_values=current_values, registry=registry, gw_role=gw_role,
        config=config, pub=_pub, logger=logger, calc_engine=calc_engine,
        main_loop=main_loop, ws_manager=ws_manager, calc_runners=calc_runners,
        last_update=last_update)
    make_data_callback = _ingest.make

    # Wire a callback per device; legacy single-client mode keeps working.
    if registry:
        for dev_cfg, client in registry:
            if client:
                client.publish_callback = make_data_callback(dev_cfg)
    elif modbus_client:
        modbus_client.publish_callback = make_data_callback(None)

    # --- Routes ---

    @app.get("/")
    async def root():
        """Serve main UI (with mtime-derived asset cache-bust tokens).

        Cache-Control: no-cache is essential: without it browsers cache the
        SHELL heuristically, so after a deploy an operator keeps loading the
        OLD index (old script tags) and reports the old UI — the asset ?v=
        tokens can only bust caches if the shell itself is revalidated."""
        return HTMLResponse(_render_index_html(canonical_url=config.ui.canonical_url),
                            headers={"Cache-Control": "no-cache"})

    # /api/status(+resources) → routes/status_routes.py

    # ── Persisted cross-subsystem event log + alerting hooks (Status page) ──
    from multibus.core.event_log import EventLog
    from multibus.outputs.alerts import AlertManager
    event_log = EventLog(str(config.config_path.parent / "events.jsonl"))
    app.state.event_log = event_log
    alert_mgr = AlertManager(getattr(config, 'alerts', {}), _pub.mqtt, event_log)
    # a device block a hand edit left unreadable was ignored at load (the
    # device kept): said where the operator looks, not only in the log
    for _where, _why in sorted((getattr(config, 'ignored_blocks', None) or {}).items()):
        event_log.add("warn", "config", f"{_where.split('.', 1)[0]}: {_why}")
    from multibus.outputs.threshold_engine import ThresholdEngine
    threshold_engine = ThresholdEngine(
        deadband_pct=alert_mgr.threshold_deadband_pct,
        alert_on_start=alert_mgr.threshold_alert_on_start)
    app.state.threshold_engine = threshold_engine

    # /api/events, /api/alerts(+test), /api/config/alerts → routes/system.py


    harvester_stop = threading.Event()
    app.state.harvester_stop = harvester_stop
    _HARVESTER_STOPS.add(harvester_stop)
    # event/alert harvester → outputs/alert_harvest.py (EventHarvester, P0.8b
    # squeeze). _display_of/_unit_alarms are bound later (endpoint facades), so
    # they are passed as late-bound thunks, exactly as the original closure.
    _harvester = EventHarvester(
        config=config, app=app, alert_mgr=alert_mgr, event_log=event_log,
        registry=registry, gw_role=gw_role, threshold_engine=threshold_engine,
        modbus_client=modbus_client, _pub=_pub, harvester_stop=harvester_stop,
        client_is_live=client_is_live,
        _display_of=lambda *a, **k: _display_of(*a, **k),
        _unit_alarms=lambda *a, **k: _unit_alarms(*a, **k))
    threading.Thread(target=_harvester.run, daemon=True, name="event-harvester").start()

    # ── Tier 2: devices + device templates ─────────────────────────────────
    # Reuse the registry the boot path already built (main.py) instead of
    # loading every bundled template a SECOND time (~4000-register maps parsed
    # twice = wasted RSS + boot time). Tests that don't pass one get a fresh
    # registry, so behaviour is unchanged.
    from multibus.catalog.device_template import TemplateRegistry
    if template_registry is None:
        template_registry = TemplateRegistry(
            user_dir=config.config_path.parent / "device_templates")
    app.state.template_registry = template_registry
    # Virtual BMS executor (ADR-0037): instances from config.virtual_bms (none by default →
    # nothing transmits). Built disarmed; a thread starts only on an explicit
    # /api/vbms/<id>/arm, so a reboot never auto-transmits to an inverter.
    from multibus.outputs.vbms_wiring import (VbmsCommandBridge, VbmsStatePublisher,
                                              build_vbms_manager)
    def _vbms_gate() -> Optional[str]:
        """Why a Virtual BMS may not transmit right now, or None (WK7-SAFE-1). The same two
        switches every other hardware write obeys, read live."""
        if not config.security.allow_writes:
            return "the master write switch is off (security.allow_writes)"
        if not gw_role.active:
            return gw_role.status_text()
        return None
    app.state.vbms_manager = build_vbms_manager(config, registry, template_registry,
                                                gate=_vbms_gate)
    # filled like every device's topics ({gateway} = this gateway's id, {device_id} = {device});
    # only {device} is left for the per-instance base (VBMS-TOPIC-1: str.format raised on {gateway})
    from multibus.core.identity import fill as _fill_topic
    _vbms_topic_pattern = _fill_topic(getattr(config.mqtt, "default_topic_pattern", "mbg/devices/{device}"),
                                      "{device}", getattr(config, "gateway_id", "") or "")
    # EMS command plane (ADR-0037 D4): subscribe mbg/devices/<vbms>/cmd/<field> on our own
    # broker and route each (clamped) command to the manager. Reads _pub.mqtt at sync time so a
    # publisher rebuilt by /api/config/apply is picked up. A command only stages a control value —
    # nothing transmits unless the instance is armed — so this is safe even fully disarmed.
    app.state.vbms_command_bridge = VbmsCommandBridge(
        app.state.vbms_manager, lambda: _pub.mqtt, topic_pattern=_vbms_topic_pattern)
    # Telemetry (ADR-0037): publish each instance's sent values + arm/running status so the EMS
    # and operator see the loop close. Pure telemetry — never transmits on CAN. Runs whether or
    # not an instance is armed; started/stopped in the lifespan below.
    # its retained-topic inventory lives beside the other state files, so an instance deleted
    # while the gateway was down is still taken back off the broker (R2D-TELE-1)
    from multibus.outputs.vbms_registry import config_device_ids, taken_by_vbms
    app.state.vbms_state_publisher = VbmsStatePublisher(
        app.state.vbms_manager, lambda: _pub.mqtt, topic_pattern=_vbms_topic_pattern,
        inventory_path=config.config_path.parent / "vbms_retained.json",
        device_ids=lambda: config_device_ids(config))
    # a collision already in the file (written before R2D-NAMESPACE-1 closed every path) is
    # said, not refused: refusing at load would take a running site dark over a name
    for _clash in taken_by_vbms(config_device_ids(config), getattr(config, "virtual_bms", None)):
        logger.warning("Virtual BMS '%s' has the id of a device (one MQTT identity for two) — "
                       "give one of them another id", _clash)
    from multibus.catalog.canonical_fields import load_user_fields as _load_uf
    _uf_path = config.config_path.parent / "canonical_fields_user.json"
    for _bad in _load_uf(_uf_path):
        logger.warning("canonical field skipped: %s", _bad)

    # ── Config snapshots (rollback + last-known-good) ───────────────────────
    from pathlib import Path as _PathSnap
    from multibus.lifecycle.snapshots import SnapshotStore
    snapshot_store = SnapshotStore(
        config.config_path.parent,
        _PathSnap(getattr(template_registry, "user_dir", "config/device_templates")),
        device_ids=lambda: [d.id for d in config.devices],
        registers_path_for=config.device_registers_path)
    app.state.snapshot_store = snapshot_store

    if not snapshot_store.list():
        # first boot with the feature: capture a baseline so there is always a
        # "before" to return to
        try:
            snapshot_store.create("baseline")
        except Exception:  # noqa: BLE001
            logger.exception("baseline snapshot failed")

    # Auto-snapshot: a successful mutation on any config-bearing route captures
    # the resulting state (bursts coalesce into one). The trigger list is an
    # allowlist of path prefixes; live actions (writes, probes, tests, trace)
    # deliberately do NOT snapshot — they change devices, not config files.


    # ── Audit trail (who changed what, when, from where) ────────────────────
    from multibus.core.audit import AuditLog
    audit_log = AuditLog(str(config.config_path.parent / "audit.jsonl"))
    app.state.audit_log = audit_log

    # ── Asset attributes (docs/attributes.md) ────────────────
    from multibus.catalog.attributes import AttributeService





    attributes = AttributeService(
        config.config_path.parent / "attributes.yaml",
        devices=lambda: _discovery.attr_devices(),  # late thunk: DiscoveryService built below
        installations=lambda: [str(p.get('id')) for p in config.endpoints if p.get('id')],
        audit=audit_log,
        on_change=lambda trigger, who: snapshot_store.schedule(trigger, user=who or ''),
        device_type=lambda did: _device_type_of(config.get_device(did)))
    try:
        attributes.store.ensure()         # a bundle always carries the file
    except Exception:  # noqa: BLE001 — a read-only config dir must not stop the gateway
        logger.exception("attributes.yaml could not be created")
    app.state.attributes = attributes
    # formulas and rules read the plant through it: the sun at a place, an attribute
    _expressions.set_place_provider(attributes.place_location, attributes.place_ids)
    _expressions.set_attr_provider(attributes.attr_value)


    # ── the attributes on MQTT: retained, versioned, from the active gateway ──
    _meta_sent: Dict[str, str] = {}          # topic → its content (without the rev)
    _meta_conn = {'seen': None}





    # every mutating /api/ call is recorded — including DENIED ones (401/403
    # are exactly what a security review wants to see). Live-data actions with
    # no config effect (queries, probes, discovery) are skipped; device writes
    # have their own richer entry on the write route.




    # ── raw-write engine (ADR-0022) — extracted to control/ (P0.8b Lot C) ────
    # Lane keys + bus locks (CommandLanes) and the raw-write executor
    # (RawWriteExecutor) now live in multibus/control/{lanes,raw_writes}.py.
    # create_api wires them with its collaborators and keeps thin facade
    # aliases so every existing call site is unchanged. A few helpers
    # (_modbus_driver_of, _mqtt_driver_of, _push_readback_to_store,
    # _write_rate_ok, _commands_for, _modbus_template_of) are defined further
    # down in create_api and passed late-bound (the forward-reference the
    # module already relies on, RP-12).
    from multibus.control.lanes import CommandLanes
    from multibus.control.raw_writes import RawWriteExecutor
    _cmds_gen = [0]
    _lanes = CommandLanes(
        modbus_driver_of=lambda *a, **k: _modbus_driver_of(*a, **k),
        mqtt_driver_of=lambda *a, **k: _mqtt_driver_of(*a, **k),
    )
    _rw = RawWriteExecutor(
        lanes=_lanes, registry=registry, gw_role=gw_role, lease_mgr=_lease_mgr,
        cmd_dispatch=_cmd_dispatch, audit_log=audit_log, pub=_pub, app=app,
        logger=logger, find_device=lambda *a, **k: _find_device(*a, **k),
        push_readback_to_store=lambda *a, **k: _push_readback_to_store(*a, **k),
        modbus_driver_of=lambda *a, **k: _modbus_driver_of(*a, **k),
        modbus_template_of=lambda *a, **k: _modbus_template_of(*a, **k),
        commands_for=lambda *a, **k: _commands_for(*a, **k),
        write_rate_ok=lambda *a, **k: _write_rate_ok(*a, **k),
        cmds_gen=_cmds_gen,
    )
    _lane_key = _lanes.lane_key
    _cmd_lock_for = _lanes.cmd_lock_for
    _bus_lock = _lanes.bus_lock
    _words_of = _rw.words_of
    _raw_channel_conflict = _rw.raw_channel_conflict
    _conflict_text = _rw.conflict_text
    _submit_raw = _rw.submit_raw
    _publish_raw_result = _rw.publish_raw_result
    _budget_deadline = _rw.budget_deadline
    _make_lease_revert = _rw.make_lease_revert

    def _make_command_lease_revert(dev, cmd_name):
        """The dead-man revert for a lease taken by a COMMAND: run the command's
        own `safe` recipe (every register it writes, encoded through its scale
        factors, verified, audited) — never a bare word into the first
        register. Resolved at fire time from the current template, so boot
        recovery and the live path behave identically (3.83.0: recovery used
        to write the raw safe value, which a SunSpec scale factor of -2
        turned into a 1 % limit)."""
        def _revert(is_current):
            if not is_current():
                return
            _i, _c, cl = _find_device(dev)
            if _c is None or cl is None:
                raise RuntimeError(f"device {dev} not running; cannot run {cmd_name} safe")
            c = _cs._find_command(_c, cmd_name)
            if c is None or not c['def'].safe:
                raise RuntimeError(f"device {dev} no longer offers command {cmd_name} with a safe recipe")
            # at the head of its lane, never superseded; is_current() is asked
            # again there, under the bus lock (ADR-0018 D3)
            st, r = _run_named_command(_c, cl, cmd_name, dict(c['def'].safe),
                                       who='lease', ip='-', via='lease-revert', is_current=is_current)
            if r.get('lease_renewed'):
                return
            if r.get('status') not in ('success', 'mismatch'):
                raise RuntimeError(f"lease-revert failed: {r.get('reason')}")
        return _revert

    # Boot recovery: any write-lease left on disk means a previous run may have
    # crashed while holding a device at a non-safe setpoint. Re-arm each as
    # already-expired so the next sweep reverts it to safe (retrying until the
    # device is reachable). This closes the "crash strands a dangerous setpoint"
    # gap that the in-RAM-only lease store had. A record taken by a command
    # reverts through the command's safe recipe, never through a raw write.
    _persisted_leases = _lease_mgr.load_persisted()
    if _persisted_leases and not gw_role.active:
        # a standby holds no device: the leases it took while active belong to
        # the past — reverting them now could undo what the active one set
        logger.warning("standby gateway: %d write lease(s) from when it was active dropped, not reverted",
                       len(_persisted_leases))
        _lease_mgr.drop_persisted()
        _persisted_leases = []
    for _m in _persisted_leases:
        try:
            if _m.get('command'):
                _rv = _make_command_lease_revert(_m['device'], _m['command'])
            else:
                _rv = _make_lease_revert(_m['device'], _m['register_type'], int(_m['address']),
                                         _m['data_type'], float(_m['scale']), _m['safe_value'],
                                         off=float(_m.get('offset', 0.0) or 0.0))
            _lease_mgr.arm(_m['device'], _m['register_type'], int(_m['address']),
                           int(_m.get('lease_ms') or 0), _rv, meta=_m, fire_now=True)
            logger.warning("WRITE-LEASE recovered after restart → reverting to safe: "
                           "device=%s addr=%s safe=%r", _m['device'], _m['address'], _m['safe_value'])
        except Exception as _e:  # noqa: BLE001
            logger.error("write-lease recovery: bad persisted record %r: %s", _m, _e)











    # the ETag's key: random per process. A bare hash of the stored entry
    # would let a viewer confirm a guessed password against it; keyed, it
    # says only "changed or not" (review). An ETag held across a restart is
    # stale: 412, read again.
    _etag_key = __import__('secrets').token_bytes(32)





    # One lock per stored entry, held by a checked write around its check,
    # its write and its tag (ADR-0030): two writes with one tag never both
    # pass. Keyed by what owns the stored entry — a component's parent, a
    # unit's installation. Always taken before _device_mutation_lock (the
    # guard sits above @_serialized_mutation), never after it: one order.
    _entry_locks: Dict[str, threading.Lock] = {}
    _entry_locks_guard = threading.Lock()









    # device-row view builders (_device_entry + _unit_read_via) → api/view.py
    # (DeviceView, P0.8b squeeze); built + facade-aliased just before ApiCtx so
    # every collaborator it captures (the _es.* facades, _commands_for) exists.

    # outermost of all: every response, including the guards' own 401/403s
    # HTTP middleware / security-guard stack → api/middleware.py (Middleware,
    # P0.8b squeeze). Registers the guards on `app` in create_api's original order
    # (CORS+GZip above stay first); exposes req_user/req_ip/ip_allowed for ApiCtx.
    # device read-model + write-decorator seam → api/read_model.py (DeviceReadModel,
    # P0.8b squeeze). Built here, before the services that inject these primitives;
    # _device_entry (the view, built later) is threaded in as a late-bound thunk.
    _rm = DeviceReadModel(
        app=app,
        config=config,
        registry=registry,
        template_registry=template_registry,
        calc_engine=calc_engine,
        calc_runners=calc_runners,
        current_values=current_values,
        rest_push_manager=rest_push_manager,
        _cmd_dispatch=_cmd_dispatch,
        _entry_locks=_entry_locks,
        _entry_locks_guard=_entry_locks_guard,
        _etag_key=_etag_key,
        _REST_HDR_MASK=_REST_HDR_MASK,
        _raw_channel_conflict=_raw_channel_conflict,
        _words_of=_words_of,
        make_data_callback=make_data_callback,
        _device_entry=lambda *a, **k: _device_entry(*a, **k))
    _apply_rest_push = _rm.apply_rest_push
    _rest_push_public = _rm.rest_push_public
    _device_type_of = _rm.device_type_of
    _find_device = _rm.find_device
    _writes_by_mqtt = _rm.writes_by_mqtt
    _write_rule = _rm.write_rule
    _declared_writes = _rm.declared_writes
    _start_device_client = _rm.start_device_client
    _opcua_sink = _rm.opcua_sink
    _opcua_block = _rm.opcua_block
    _meters_status = _rm.meters_status
    _stored_device_raw = _rm.stored_device_raw
    _primary_kept = _rm.primary_kept
    _check_if_match = _rm.check_if_match
    _entry_lock = _rm.entry_lock
    _device_write = _rm.device_write
    _device_runtime = _rm.device_runtime
    _device_specs = _rm.device_specs
    _drop_spec_keys = _rm.drop_spec_keys
    _set_new_node = _rm.set_new_node
    # apply each device's REST-push config at startup (moved below DeviceReadModel,
    # which now owns _apply_rest_push — same effect, still before the app serves)
    for _dc, _c in registry:
        _apply_rest_push(_dc)
    _mw = Middleware(
        app=app, config=config, auth_state=auth_state, gw_role=gw_role,
        snapshot_store=snapshot_store, audit_log=audit_log,
        service_tokens=service_tokens, _api_key=_api_key)
    _ip_allowed = _mw.ip_allowed

    # device CRUD (list/create/restorable/restore/forget/update/patch/delete/get) → routes/devices.py (P0.8b Lot G)
    # /api/serial-ports, /api/bridge/adapters → routes/commissioning.py

    _late_hooks: Dict[str, Any] = {}

    # devices whose HA discovery this gateway has taken back (MQTT or its
    # discovery off), kept across restarts: a device cleared once is not
    # cleared again at every start (ADR-0025, ADD-14)
    _disc_cleared_path = config.config_path.parent / "ha_discovery_cleared.json"
    try:
        _disc_cleared: Set[str] = set(json.loads(_disc_cleared_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        _disc_cleared = set()






    # Seconds AFTER a command's revert_s at which the read-back group is swept
    # again. Measured 2026-09-14 on a Fronius Symo: the inverter clears
    # WMaxLim_Ena about 120-127 s after a write with revert_s = 120, but a
    # single read 5 s past the mark still saw it enabled once — its own timer
    # ticks coarsely. A short series catches it without polling the hourly
    # controls block continuously.
    REVERT_REREAD_AT_S = (5.0, 20.0, 60.0, 180.0)
    # One series per (device, command). A new command restarts the device-side
    # revert timer, so the series armed for the previous command is cancelled
    # and replaced — otherwise a controller that re-sends every minute stacks
    # its series on the datalogger (1681 extra sweeps in 3.5 h on 2026-09-15,
    # ~50 Solar API ok→degraded flaps an hour). Each series carries a
    # generation; a callback from a superseded one sweeps nothing.
    _revert_rereads: Dict[tuple, tuple] = {}        # (device, command) -> (generation, Timer)
    _revert_rereads_lock = threading.Lock()







    # ── commands: the universal write path (docs/commands-design.md) ─────────
    #
    # A controller says WHAT it wants; the device's template says HOW that is
    # said to this device; the engine (multibus/commands.py) is vendor-blind.
    # Every face — API, MQTT, HA — lands here, with the same gates, the same
    # verification, the same audit. The gateway never decides WHEN.
    _cmd_last: Dict[str, Dict] = {}       # (device:command) → last result, for the UI and the state topic


    # ── device-command seam → api/command_service.py (Lot F) ──
    # The command catalog, readback-refresh scheduler, admission→lane→execute→
    # record path, named-command run/await/publish, binding CRUD, lease-pace
    # warnings and the MQTT command handler move verbatim into CommandService
    # (closure-holder); create_api wires it and keeps a thin alias per function
    # so the @app.* routes and the MQTT/OPC-UA callers reach the same callables.
    # Stays in the api layer (raises HTTPException). Forward-defined collaborators
    # _write_rate_ok is passed as a late-bound thunk (RP-12);
    # the device-mutation lock is created here so the service can hold the same one.
    _device_mutation_lock = threading.Lock()
    from multibus.api.command_service import CommandService
    _cs = CommandService(
        config=config, registry=registry, template_registry=template_registry,
        logger=logger, app=app, REVERT_REREAD_AT_S=REVERT_REREAD_AT_S,
        _api_key=_api_key, _arm_revert_reread=lambda *a, **k: _arm_revert_reread(*a, **k),
        _budget_deadline=_budget_deadline, _bus_lock=_bus_lock, _cmd_dispatch=_cmd_dispatch,
        _cmd_lock_for=_cmd_lock_for, _cmds_gen=_cmds_gen, _device_runtime=_device_runtime,
        _device_write=_device_write, _find_device=_find_device, _lane_key=_lane_key,
        _lease_mgr=_lease_mgr, _make_command_lease_revert=_make_command_lease_revert,
        _make_lease_revert=_make_lease_revert, _mqtt_write_command=lambda *a, **k: _mqtt_write_command(*a, **k),
        _pub=_pub, _push_readback_to_store=lambda *a, **k: _push_readback_to_store(*a, **k),
        _sync_device_discovery=lambda *a, **k: _sync_device_discovery(*a, **k), audit_log=audit_log,
        auth_state=auth_state, calc_engine=calc_engine, gw_role=gw_role,
        _cmd_last=_cmd_last, _device_mutation_lock=_device_mutation_lock,
        _write_rate_ok=lambda *a, **k: _write_rate_ok(*a, **k))
    # facade aliases — routes/closures/app.state call the same callables unchanged
    _modbus_driver_of = _cs._modbus_driver_of
    _mqtt_driver_of = _cs._mqtt_driver_of
    _modbus_template_of = _cs._modbus_template_of
    _commands_for = _cs._commands_for
    # attribute-meta + HA-discovery seam → api/discovery.py (DiscoveryService,
    # P0.8b squeeze). Built here, after _commands_for, so every collaborator
    # exists; facade aliases + the relocated registrations keep behaviour.
    _discovery = DiscoveryService(
        config=config, registry=registry, template_registry=template_registry,
        attributes=attributes, calc_engine=calc_engine, gw_role=gw_role,
        snapshot_store=snapshot_store, _pub=_pub, _disc_cleared=_disc_cleared,
        _disc_cleared_path=_disc_cleared_path, _late_hooks=_late_hooks,
        _meta_conn=_meta_conn, _meta_sent=_meta_sent, _commands_for=_commands_for,
        _write_rule=_write_rule, _writes_by_mqtt=_writes_by_mqtt,
        _raw_channel_conflict=_raw_channel_conflict, _words_of=_words_of)
    _publish_attribute_meta = _discovery.publish_attribute_meta
    _sync_device_discovery = _discovery.sync_device_discovery
    # registrations relocated here (their funcs now live in DiscoveryService)
    from multibus.devices import endpoint_aggregator as _agg_mod
    _agg_mod.set_sun_provider(_discovery.sun_elev_at)
    attributes._on_change = _discovery.attributes_changed
    app.state.publish_attribute_meta = _publish_attribute_meta
    _group_cmd_prefix = _cs._group_cmd_prefix
    _group_commands = _cs._group_commands
    _cancel_device_refreshes = _cs._cancel_device_refreshes
    _submit_named_command = _cs._submit_named_command
    _run_named_command = _cs._run_named_command
    _publish_command_result = _cs._publish_command_result
    _mqtt_writes_refused = _cs._mqtt_writes_refused
    _sync_command_topics = _cs._sync_command_topics
    # /api/devices/{id}/commands(+/{name}(+/enabled)), /channels/{key} → routes/commands.py (P0.8b Lot G)

    # /api/meter-roles, /api/devices/{id}/meters, /energy-meter → routes/meters.py (P0.8b Lot G)

    # run/dry-run/group commands, /commands/history, power_limit aliases → routes/commands.py (P0.8b Lot G)


    # command write-path seam → api/command_exec.py (CommandExec, P0.8b squeeze).
    # Built after CommandService's facades (uses several directly); the three funcs
    # CommandService's own ctor needs are threaded in above as late-bound thunks.
    # _make_command_lease_revert stays in create_api (called at boot lease recovery).
    _cmdexec = CommandExec(
        registry=registry, _find_device=_find_device, _write_rule=_write_rule,
        _writes_by_mqtt=_writes_by_mqtt, _revert_rereads=_revert_rereads,
        _revert_rereads_lock=_revert_rereads_lock, _commands_for=_commands_for,
        _submit_named_command=_submit_named_command,
        _publish_command_result=_publish_command_result,
        _modbus_driver_of=_modbus_driver_of, _mqtt_writes_refused=_mqtt_writes_refused,
        _submit_raw=_submit_raw, _publish_raw_result=_publish_raw_result,
        _raw_channel_conflict=_raw_channel_conflict, _words_of=_words_of,
        _conflict_text=_conflict_text)
    _push_readback_to_store = _cmdexec.push_readback_to_store
    _arm_revert_reread = _cmdexec.arm_revert_reread
    _mqtt_write_command = _cmdexec.mqtt_write_command
    if _pub.mqtt:
        _pub.mqtt.set_command_write_handler(_mqtt_write_command)
    app.state.commands_for = _commands_for
    app.state.run_command = _run_named_command
    app.state.make_lease_revert = _make_lease_revert
    app.state.cmd_lock_for = _cmd_lock_for
    app.state.lane_key = _lane_key
    app.state.exec_timeout = _cs._exec_timeout
    app.state.commands_changed = _cs._commands_changed
    app.state.lease_manager = _lease_mgr
    app.state.mqtt_command = _cs._mqtt_command
    app.state.mqtt_write_command = _mqtt_write_command
    app.state.sync_command_topics = _sync_command_topics
    _late_hooks['command_topics'] = _sync_command_topics
    try:
        _sync_command_topics()                       # boot: the topics exist from the first second
    except Exception as e:  # noqa: BLE001
        logger.warning("command topics at boot: %s", e)

    # Virtual BMS EMS command topics — same lifecycle as the device command topics (boot now,
    # re-synced on config reload so a newly-configured instance gets its topics).
    _late_hooks['vbms_command_topics'] = app.state.vbms_command_bridge.sync
    try:
        app.state.vbms_command_bridge.sync()
    except Exception as e:  # noqa: BLE001
        logger.warning("vbms command topics at boot: %s", e)

    # ── rules: the declarative controller (docs/rules-design.md) ────────────
    from multibus.core import clock as _clock
    from multibus.control.rules_runtime import RulesRuntime
    _clock.set_timezone(config.ui.timezone or "Europe/Bucharest")   # hour(), between(), "today"


    # what an id reads, for a rule's `inputs` (input_check): the registers its
    # running drivers poll (memory only — a GET never touches the disk), the
    # calculated fields, whatever the live store holds, a linked meter's
    # fields; an installation: its totals. Never under the rules lock; cached,
    # and cleared whenever a device or its register selection changes.
    _fields_cache: Dict[str, Tuple[float, Any]] = {}
    _fields_warned: set = set()
    # rules-reload + sequence-run seam → api/rules_seq.py (RulesSeq, P0.8b squeeze).
    # Built before rules_runtime so it gets the REAL rule_fields_of/rule_poll_now
    # (rule_fields_of carries .cache_clear). _rules_reload stays in create_api (it
    # needs rules_runtime, built below, and is trivial).
    _rulesseq = RulesSeq(
        calc_engine=calc_engine, config=config, _fields_cache=_fields_cache,
        _fields_warned=_fields_warned, _find_device=_find_device,
        _modbus_driver_of=_modbus_driver_of, _mqtt_driver_of=_mqtt_driver_of,
        registry=registry, _run_named_command=_run_named_command)
    _rule_fields_of = _rulesseq.rule_fields_of
    _rule_fields_of.cache_clear = _fields_cache.clear   # _serialized_mutation clears it




    def _rules_reload():
        _fields_cache.clear()
        rules_runtime.load()

    rules_runtime = RulesRuntime(
        config_dir=config.config_path.parent, resolver_factory=calc_engine.resolver,
        find_device=_find_device, endpoint_devices=config.endpoint_devices,
        commands_for=_commands_for, run_command=_run_named_command, poll_now=_rulesseq.rule_poll_now,
        event_log=event_log, alert_mgr=alert_mgr, mqtt=_pub.mqtt, audit_log=audit_log,
        influx=_pub.influx,
        # a gateway that just became active acts on fresh readings, not only on
        # its partner's copy: rules wait a moment before their first write
        gates=lambda: (bool(config.security.allow_writes) and gw_role.active
                       and time.monotonic() >= getattr(app.state, 'role_warm_until', 0.0)),
        mqtt_refused=_mqtt_writes_refused, fields_of=_rule_fields_of)
    try:
        rules_runtime.load()
    except Exception as e:  # noqa: BLE001
        logger.error("rules.yaml: %s", e)
    app.state.rules_runtime = rules_runtime
    _late_hooks['rules_reload'] = _rules_reload         # devices changed: rebind targets

    # ── sequences: commands in order, with waits and checks (rules phase 3 §4)
    from multibus.control.sequences import SequenceRunner, SequenceStore


    app.state.sequence_store = SequenceStore(config.config_path.parent / 'sequences.yaml')
    app.state.sequences = SequenceRunner(run_command=_rulesseq.seq_run_command, resolver_factory=calc_engine.resolver,
                                         audit=audit_log, mqtt=_pub.mqtt)
    app.state.mqtt_writes_refused = _mqtt_writes_refused

    # ── OPC UA server: the northbound face for SCADA / HMI (docs/opcua.md) ──
    class _OpcUaControl:
        """Starts, stops and restarts the server as the configuration says;
        imports asyncua only when it is enabled."""

        def __init__(self):
            self.server = None
            self.lock = threading.Lock()
            self._declared: Dict[str, Tuple[float, list]] = {}

        @staticmethod
        def _ua_type(r) -> str:
            """The OPC UA type a declared value will have: text for strings and
            enum/bit maps, a boolean for coils, a number otherwise. A value that
            turns out otherwise corrects its node in place (server._put)."""
            dt = str(getattr(r, 'data_type', '') or '').lower()
            if getattr(r, 'enum', None) or getattr(r, 'bits', None) or dt.startswith('string'):
                return 'String'
            if str(getattr(r, 'register_type', '') or '').lower() in ('coil', 'coils', 'discrete', 'discrete_input'):
                return 'Boolean'
            return 'Double'

        def _declared_for(self, dcfg) -> list:
            """[(name, label, unit, type)] the device's template and choices say
            it will read: its registers and calculated values. Known before the
            first reading, so its nodes exist from the start. Cached 30 s (the
            server asks every pass; the registers are files)."""
            now = time.monotonic()
            hit = self._declared.get(dcfg.id)
            if hit and now - hit[0] < 30.0:
                return hit[1]
            out = []
            try:
                regs = list(config.unit_registers(dcfg))
                regs += [e['_reg'] for e in calc_engine.store.get(dcfg.id, []) if e.get('_reg') is not None]
                seen = set()
                for r in regs:
                    if r.name and r.name not in seen:
                        seen.add(r.name)
                        out.append((r.name, r.label or '', r.unit or '', self._ua_type(r)))
            except Exception as e:  # noqa: BLE001 — a device without registers just has none declared
                logger.debug("opcua: declared fields of %s: %s", dcfg.id, e)
            self._declared[dcfg.id] = (now, out)
            return out

        def _devices(self):
            """What the server serves: the devices whose OPC UA output is on, from
            the configuration (a unit found seconds after the start is already
            here), each with its values as read, what it declares and which of
            its values are chosen."""
            for dcfg in list(config.devices):
                if not (dcfg.enabled and dcfg.opcua_enabled):
                    continue
                _i, _c, client = registry.find(dcfg.id)
                try:
                    health = (client.data_health() or {}).get('status', '') if client is not None else 'down'
                except Exception:  # noqa: BLE001
                    health = ''
                live = client is not None and client_is_live(client)
                cmds = {}
                try:
                    for c in _commands_for(dcfg):
                        if c['enabled'] and not c['def'].alias and c['faces'].get('opcua', True):
                            cmds[c['def'].name] = {'label': c['def'].label, 'params': {
                                n: {k: getattr(p, k) for k in ('label', 'unit', 'min', 'max', 'default', 'required')}
                                for n, p in c['def'].params.items()}}
                except Exception:  # noqa: BLE001
                    cmds = {}
                try:
                    attrs = attributes.flat('device', dcfg.id)
                except Exception:  # noqa: BLE001 — attributes never stop the values
                    attrs = {}
                yield (dcfg.id, dcfg.name or dcfg.id, live, health, registry.store_for(dcfg.id) or {}, cmds,
                       self._declared_for(dcfg), set(dcfg.opcua_fields) or None, attrs)

        def _run(self, device_id, name, params, user):
            _i, dcfg, cl = _find_device(device_id)
            if dcfg is None:
                return 404, {'status': 'rejected', 'reason': f"no device '{device_id}'"}
            return _run_named_command(dcfg, cl, name, dict(params), who=f"opcua:{user}", ip='-', via='opcua')

        def _authenticate(self, user, password):
            """A gateway account, or a service token: user = the token's name,
            password = the token (a SCADA or HMI is a program — Settings →
            Security → Service tokens, its role, revocable alone)."""
            if str(password or '').startswith('mbgst_'):
                svc = service_tokens.check(str(password))
                return svc[1] if svc and svc[0] == str(user or '').strip().lower() else None
            return auth_state.authenticate(user, password) if auth_state.enabled else None

        def apply(self):
            with self.lock:
                if self.server is not None:
                    self.server.stop()
                    self.server = None
                if not config.opcua.enabled or not gw_role.active:   # a standby serves nothing
                    return None
                from multibus.outputs.opcua import OpcUaServer
                self.server = OpcUaServer(config.opcua, devices=self._devices, authenticate=self._authenticate,
                                          config_dir=config.config_path.parent, version=__version__,
                                          run_command=self._run)
                if not self.server.start():
                    if self.server.error:
                        logger.error("OPC UA server did not start: %s", self.server.error)
                    else:   # a slow host: it keeps starting, status() says so
                        logger.warning("OPC UA server is still starting; it reports running when ready")
                return self.server.status()

        def shutdown(self):
            with self.lock:
                if self.server is not None:
                    self.server.stop()
                    self.server = None

        def status(self):
            s = self.server.status() if self.server is not None else {'running': False}
            return {**s, 'enabled': bool(config.opcua.enabled), 'port': config.opcua.port,
                    'host': config.opcua.host, 'encryption': config.opcua.encryption,
                    'allow_anonymous': bool(config.opcua.allow_anonymous), 'update_s': config.opcua.update_s,
                    'public_url': config.opcua.public_url,
                    'auth_enabled': bool(auth_state.enabled)}

    app.state.opcua = _OpcUaControl()


    # what a device's own routes manage (Outputs tab, write lock, Commands
    # tab, linked meter, components): an edit never changes them
    _DEVICE_OWN_KEYS = ('write_locked', 'http_output', 'rest_push', 'pq_recorder',
                        'energy_meter', 'meters', 'meter_options', 'components', 'commands', 'channels',
                        'opcua', 'kind', 'energy_counter_resets')






    # Serialize device mutations: create/update/delete/restore each do a
    # check-then-act across validate → persist → client-swap → registry. Two
    # concurrent requests (a double-clicked Apply, two admins) would otherwise
    # orphan a client's live poller threads or leave 2× pollers publishing
    # every value twice. functools.wraps keeps the handler's real signature so
    # FastAPI still resolves path/body params (it follows __wrapped__).
    # (_device_mutation_lock is created earlier, before the CommandService build.)

    # a path names a field: lowercase keys; a later segment may be a name
    # of the user's (an HTTP header: X-Auth)
    _FIELD_PATH_RE = re.compile(r'^[a-z_]+(\.[a-z_0-9]+)*(\.[A-Za-z0-9_-]+)?$')


    # The device-payload validation seam, lifted whole out of create_api into
    # DeviceValidator (P0.8b Lot B, ADR-0038). create_api injects what the
    # checks used to capture; the thin aliases below keep the ~20 call sites (and
    # the routes still inside this function) unchanged until they move to their
    # routers. ADR-0038's end state moves this to devices/ raising domain errors.
    _dv = DeviceValidator(
        config, template_registry, registry,
        stored_device_raw=_stored_device_raw, opcua_block=_opcua_block,
        errors_detail=lambda *a, **k: _errors_detail(*a, **k), protocols_by_context=PROTOCOLS_BY_CONTEXT)
    _validate_device_payload = _dv.validate_device_payload
    _device_claim_check = _dv.device_claim_check
    _timeout_error = _dv.timeout_error








    # ── devices that announce themselves over MQTT (mqtt_discovery.py) ──────
    from multibus.commissioning.mqtt_discovery import MqttDiscovery
    discovery_state = MqttDiscovery(str(config.config_path.parent / "mqtt_discovery.json"))
    app.state.mqtt_discovery = discovery_state

    # /api/mqtt-discovery/* → routes/mqtt_discovery.py (P0.8b Lot G)












    # what a device publishes and how: applied live (the poller reads the
    # switches at every reading), never a reason to restart acquisition
    _LIVE_FIELDS = ('ha_discovery_enabled', 'mqtt_enabled', 'influxdb_enabled', 'mqtt_publish_mode',
                    'mqtt_heartbeat_s', 'influxdb_publish_mode', 'influxdb_write_interval_s',
                    'influxdb_heartbeat_s', 'http_output_enabled', 'opcua_enabled', 'opcua_fields',
                    'rest_push')
    _COUNTER_RESETS_KEPT = 20






    # /api/devices/{id}/http-output, /outputs, /publishing → routes/device_outputs.py (P0.8b Lot G)


    # /api/devices/{id}/opcua-output, /rest-push(+/test) → routes/device_outputs.py (P0.8b Lot G)

    # ---- Modbus device auto-discovery -----------------------------------------
    # /api/discover/modbus/* → routes/discovery_routes.py

    # ---- Calculated registers API ---------------------------------------------
    # /api/calculated/* + /api/devices/{id}/calculated(+test) → routes/calculated.py

    # Per-client-IP token bucket for the write API. A write floods the shared
    # Modbus lock (blocking I/O), so a burst can starve the pollers; excess writes
    # get 429 instead of piling up. Generous default so a normal control loop is
    # unaffected; security.write_rate_limit_per_s=0 disables it.
    _write_rl_state: Dict[str, tuple] = {}
    _write_rl_lock = threading.Lock()


    # /api/devices/{id}/events, /write, /write-lock, /write-info, /poll-groups; /api/writes/leases → routes/writes.py (P0.8b Lot G)








    # ── what depends on a device, an installation or a template ─────────────
    # (multibus/dependents.py) Nothing is deleted while something uses it.



    # /api/devices/{id}/dependents, /api/endpoints/{id}/dependents, /api/dependents → routes/dependents.py (P0.8b Lot G)



    # ── components: what a device holds, added by instance ───────────────────
    # A component is an ordinary device
    # everywhere else; it is made, renamed, switched and removed here.







    # /api/devices/{id}/components (list, add) → routes/components.py (P0.8b Lot G)



    # /api/devices/{id}/organise, component update/delete/discover(+unsaved) → routes/components.py (P0.8b Lot G)

    # ── endpoints: one template × N unit ids behind one endpoint ──────────────
    # An endpoint materializes into N ordinary devices (own socket each — unit-
    # switching on a shared socket corrupts some gateway buffers, and units
    # must fail independently). The devices are managed THROUGH the endpoint:
    # device CRUD refuses them; edit/delete the endpoint instead.

    # ── installations/endpoints domain → api/endpoints_service.py (Lot E) ─
    # The endpoint view builders, payload validators and CRUD orchestration
    # move verbatim into EndpointService (closure-holder); create_api wires it
    # with its collaborators and keeps a thin alias per function, so the routes
    # below call the same callables. Stays in the api layer (these validators
    # raise HTTPException, like Lot B's DeviceValidator); PROTOCOLS_BY_CONTEXT and
    # the api logger are injected to avoid an import cycle / keep the logger name.
    from multibus.api.endpoints_service import EndpointService
    # device CRUD / lifecycle seam → api/device_service.py (DeviceService, P0.8b
    # squeeze). Built just before EndpointService so _es gets the device funcs it
    # injects directly; five collaborators bound later (the _es.* validators,
    # _cancel_device_refreshes, _restart_devices, _view.entry) are late thunks.
    _ds = DeviceService(
        app=app,
        config=config,
        registry=registry,
        template_registry=template_registry,
        attributes=attributes,
        calc_engine=calc_engine,
        calc_runners=calc_runners,
        modbus_client=modbus_client,
        _pub=_pub,
        _lease_mgr=_lease_mgr,
        rest_push_manager=rest_push_manager,
        _device_mutation_lock=_device_mutation_lock,
        _fields_cache=_fields_cache,
        _meta_sent=_meta_sent,
        _write_rl_lock=_write_rl_lock,
        _write_rl_state=_write_rl_state,
        _DEVICE_OWN_KEYS=_DEVICE_OWN_KEYS,
        _FIELD_PATH_RE=_FIELD_PATH_RE,
        _REST_HDR_MASK=_REST_HDR_MASK,
        _check_if_match=_check_if_match,
        _entry_lock=_entry_lock,
        _device_specs=_device_specs,
        _stored_device_raw=_stored_device_raw,
        _primary_kept=_primary_kept,
        _device_type_of=_device_type_of,
        _opcua_block=_opcua_block,
        _drop_spec_keys=_drop_spec_keys,
        _set_new_node=_set_new_node,
        _apply_rest_push=_apply_rest_push,
        _find_device=_find_device,
        _start_device_client=_start_device_client,
        _sync_device_discovery=_sync_device_discovery,
        _device_claim_check=_device_claim_check,
        _validate_device_payload=_validate_device_payload,
        _timeout_error=_timeout_error,
        _device_entry=lambda *a, **k: _device_entry(*a, **k),
        _cancel_device_refreshes=lambda *a, **k: _cancel_device_refreshes(*a, **k),
        _restart_devices=lambda *a, **k: _restart_devices(*a, **k))
    _built_sig = _ds.built_sig
    # runtime device reconciliation for a config reload (ADR-0047 P2): reload_from_disk reaches it
    # via app.state to start added / stop removed / rebuild retargeted devices (not just the primary)
    app.state.reconcile_devices = _ds.reconcile_runtime
    _autoselect_template_registers = _ds.autoselect_template_registers
    _ensure_device_bucket = _ds.ensure_device_bucket
    _errors_detail = _ds.errors_detail
    _serialized_mutation = _ds.serialized_mutation
    _publishing = _ds.publishing
    _write_rate_ok = _ds.write_rate_ok
    _teardown_device = _ds.teardown_device
    _device_value_topics = _ds.device_value_topics
    _clear_device_values = _ds.clear_device_values
    _dep_sources = _ds.dep_sources
    _refuse_if_used = _ds.refuse_if_used
    _component_type_name = _ds.component_type_name
    _es = EndpointService(
        config=config, registry=registry, template_registry=template_registry,
        logger=logger, app=app,
        _group_commands=_group_commands, _group_cmd_prefix=_group_cmd_prefix,
        _teardown_device=_teardown_device,
        _autoselect_template_registers=_autoselect_template_registers,
        _ensure_device_bucket=_ensure_device_bucket,
        _start_device_client=_start_device_client,
        # late-bound thunk: DeviceView is built after _es, below (P0.8b squeeze)
        _device_entry=lambda *a, **k: _device_entry(*a, **k),
        _opcua_block=_opcua_block,
        _dep_sources=_dep_sources,
        _built_sig=_built_sig, _sync_device_discovery=_sync_device_discovery,
        _cancel_device_refreshes=_cancel_device_refreshes,
        _device_value_topics=_device_value_topics, _clear_device_values=_clear_device_values,
        _publish_attribute_meta=_publish_attribute_meta, _refuse_if_used=_refuse_if_used,
        _serialized_mutation=_serialized_mutation,
        PROTOCOLS_BY_CONTEXT=PROTOCOLS_BY_CONTEXT)
    # facade aliases — call sites (routes, earlier closures) unchanged
    _display_of = _es._display_of
    _unit_alarms = _es._unit_alarms
    _glance_names = _es._glance_names
    _store_meta = _es._store_meta
    _field_meta_any = _es._field_meta_any
    _unit_live = _es._unit_live
    # ── presentation, from the template's `display` block ─────────────────────
    # What a unit row shows at a glance, by what the unit IS. An inverter is
    # judged by its power and its two voltages, a meter by power and the two
    # energy directions, the site by the balance the datalogger computes.
    # /api/protocols, /api/endpoints(+CRUD/test/template-changes/refresh), device template-changes/refresh → routes/endpoints.py (P0.8b Lot G)

    # probe / connection-test seam → api/probe.py (ProbeService, P0.8b squeeze).
    # config + the four _dv checks it needs; facade aliases keep the call
    # sites (device_tests routes via ctx, _take_over_partner_state) unchanged.
    _probe = ProbeService(
        config, refuse_if_listened=_dv.refuse_if_listened,
        serial_missing=_dv.serial_missing, test_on_shared_bus=_dv.test_on_shared_bus,
        listening_template=_dv.listening_template)

    # /api/device-templates/* → routes/device_templates.py

    # /health → routes/status_routes.py

    # /api/languages(+{code}) → routes/languages.py

    # /api/config (+modbus/mqtt/influxdb/ui-security/security/env-overrides) → routes/config.py

    # /api/registers/* → routes/registers_routes.py

    # /api/values*, /api/meters*, /api/history* → routes/values_routes.py

    # /api/energy/* → routes/energy.py

    # /api/fronius/discover → routes/discovery_routes.py

    # /api/virtual-meters/* → routes/vmeters.py

    # /api/query/*, /api/search, /api/poll-groups → routes/registers_routes.py

    # --- Config Management ---

    # /api/config, /api/config/{env-overrides,modbus,mqtt,ui-security,security,influxdb}
    #   → routes/config.py  (persist-only). The rest below rebind publishers /
    #   restart clients / touch the data path, so they stay in create_api.

    # /api/config/apply, /api/config/reload-registers → routes/config_apply.py (P0.8b Lot G)

    # /api/config/export, /api/config/import → routes/config_bundle.py (P0.8b Lot G)



    _reload_state: Dict[str, bool] = {'primary_changed': False}



    # config reload / restore tail → api/reload.py (ReloadService, P0.8b squeeze);
    # built after _reload_state (kept here, shared with ApiCtx); facade aliases
    # keep the snapshots routes (via ctx) and _take_over_partner_state unchanged.
    _reload = ReloadService(
        config=config, app=app, attributes=attributes, audit_log=audit_log,
        auth_state=auth_state, event_log=event_log, modbus_client=modbus_client,
        _pub=_pub, template_registry=template_registry,
        _publish_attribute_meta=_publish_attribute_meta, _reload_state=_reload_state)

    # /api/config/snapshots/* → routes/snapshots.py (P0.8b Lot G)

    # --- Auth (login / logout / status) ---

    # /api/auth/* → routes/auth_routes.py

    # --- WebSocket ---

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """WebSocket endpoint for real-time data."""
        # The HTTP middlewares (IP allowlist + auth) do not run for the websocket
        # ASGI scope, so enforce both here before streaming the live value cache.
        peer = websocket.client.host if websocket.client else ""
        if not _ip_allowed(peer):
            await websocket.close(code=1008)       # policy violation
            return
        if auth_state is not None and auth_state.enabled:
            token = websocket.cookies.get(_auth.COOKIE_NAME, "")
            if auth_state.role_for(token) is None:
                await websocket.close(code=1008)
                return
        # WebSockets are exempt from the browser same-origin policy, so a page the
        # operator visits could otherwise open ws://<lan-ip>/ws and read the live
        # telemetry stream. Reject a cross-origin Origin (host:port mismatch); a
        # non-browser client (no Origin header) is allowed, like the HTTP guard.
        _origin = websocket.headers.get("origin")
        if _origin:
            from urllib.parse import urlparse
            _o = urlparse(_origin).netloc.lower()
            if _o and _o != websocket.headers.get("host", "").lower():
                await websocket.close(code=1008)
                return
        if not await ws_manager.connect(websocket):
            return                       # refused at the connection cap
        try:
            # Send initial data
            await websocket.send_json({
                'type': 'init',
                'device': config.primary_id,   # the snapshot is the primary's
                # snapshot, NOT the live dict (external audit: json.dumps
                # walking the live store while pollers insert raises
                # 'dictionary changed size' and drops the socket — the same
                # bug values_routes.py already documents and fixes)
                'values': dict(current_values),
                'timestamp': last_update['timestamp'],
                'ts': last_update['ts'],
            })

            # Keep connection alive
            while True:
                try:
                    # Wait for messages (ping/pong handled automatically)
                    data = await asyncio.wait_for(websocket.receive_text(), timeout=30)

                    # Handle client messages
                    try:
                        msg = json.loads(data)
                        if msg.get('type') == 'ping':
                            await websocket.send_json({'type': 'pong'})
                        elif msg.get('type') == 'subscribe':
                            # Client can subscribe to specific addresses
                            pass
                    except json.JSONDecodeError:
                        pass

                except asyncio.TimeoutError:
                    # Send ping — unless the socket already closed under us
                    # (broadcast-path kick or the client vanished mid-timeout).
                    from starlette.websockets import WebSocketState
                    if websocket.application_state != WebSocketState.CONNECTED:
                        break
                    await websocket.send_json({'type': 'ping'})

        except WebSocketDisconnect:
            pass
        except RuntimeError as e:
            # send/receive raced a close ("Unexpected ASGI message ... after
            # 'websocket.close'") — normal client churn, not an error. Was
            # ~3/h of ERROR noise in the production log.
            logger.debug(f"WebSocket closed during send: {e}")
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
        finally:
            await ws_manager.disconnect(websocket)

    # --- Domain routers (multibus/routes/) ---
    # Shared context for the extracted route modules. The publisher slots are
    # MUTABLE: /api/config/apply swaps _pub.mqtt / _pub.influx (ADR-0038 #3) and
    # mirrors the new object onto ctx here, so routers reading ctx.<publisher>
    # at request time always see the live one. Everything else is a singleton.
    from multibus.routes import (ApiCtx, attributes as attributes_routes, auth_routes, calculated,
                         commissioning, config as config_routes, device_templates,
                         diagnostics, discovery_routes, energy, general_config,
                         languages, metrics, pq, registers_routes, rules as rules_routes,
                         sequences as sequences_routes, opcua as opcua_routes, role_routes,
                         status_routes, system, values_routes, vmeters, bridges as bridges_routes,
                         snapshots as snapshots_routes, mqtt_discovery as mqtt_discovery_routes,
                         meters as meters_routes, device_outputs as device_outputs_routes,
                         config_apply as config_apply_routes, config_bundle as config_bundle_routes,
                         commands as commands_routes, components as components_routes,
                         writes as writes_routes, endpoints as endpoints_routes,
                         dependents as dependents_routes, device_tests as device_tests_routes)

    # restart + role-change / take-over hooks → api/lifecycle.py (Lifecycle, P0.8b squeeze).
    _lifecycle = Lifecycle(
        app=app,
        config=config,
        registry=registry,
        alert_mgr=alert_mgr,
        event_log=event_log,
        gw_role=gw_role,
        _lease_mgr=_lease_mgr,
        _pub=_pub,
        rest_push_manager=rest_push_manager,
        _cmd_dispatch=_cmd_dispatch,
        _make_command_lease_revert=_make_command_lease_revert,
        _make_lease_revert=_make_lease_revert,
        _apply_rest_push=_apply_rest_push,
        _start_device_client=_start_device_client,
        _sync_device_discovery=_sync_device_discovery,
        _publish_attribute_meta=_publish_attribute_meta)
    _restart_devices = _lifecycle.restart_devices
    _take_over_partner_state = _lifecycle.take_over_partner_state




    gw_role.on_change(_lifecycle.on_role_change)
    app.state.role_hooks = []

    _view = DeviceView(
        config=config, template_registry=template_registry, attributes=attributes,
        calc_engine=calc_engine, rest_push_manager=rest_push_manager, _pub=_pub,
        _commands_for=_commands_for, _component_type_name=_component_type_name,
        _declared_writes=_declared_writes, _device_specs=_device_specs,
        _device_type_of=_device_type_of, _field_meta_any=_field_meta_any,
        _glance_names=_glance_names, _meters_status=_meters_status,
        _opcua_sink=_opcua_sink, _publishing=_publishing,
        _rest_push_public=_rest_push_public, _stored_device_raw=_stored_device_raw,
        _store_meta=_store_meta, _unit_alarms=_unit_alarms, _unit_live=_unit_live)
    _device_entry = _view.entry

    ctx = ApiCtx(
        restart_devices_using_template=_lifecycle.restart_devices_using_template,
        restart_devices=_restart_devices,
        app=app, config=config, registry=registry, calc_engine=calc_engine,
        event_log=event_log, alert_mgr=alert_mgr,
        auth_state=auth_state, api_key=_api_key, service_tokens=service_tokens,
        template_registry=template_registry,
        current_values=current_values, last_update=last_update,
        modbus_client=modbus_client, ws_manager=ws_manager,
        mqtt_publisher=_pub.mqtt, influxdb_publisher=_pub.influx,
        audit_log=audit_log,
        ip_allowed=_ip_allowed,   # WS routes enforce the allowlist themselves
        rule_fields_changed=_fields_cache.clear,
        dependents_of=_ds.dependents,
        mutation_lock=_device_mutation_lock, entry_lock=_entry_lock,
        device_write=_device_write, device_runtime=_device_runtime,
        device_write_query=_rm.device_write_query,
        validate_connection=_ds.dry_run_connection,
        write_primary_modbus=_ds.write_primary_modbus,
        snapshot_store=snapshot_store,
        reload_from_disk=_reload.reload_from_disk,
        reload_state=_reload_state,
        dangling_after_restore=_reload.dangling_after_restore,
        reissued_response=_reload.reissued_response,
        discovery_state=discovery_state,
        find_device=_find_device,
        fields_cache=_fields_cache,
        sync_device_discovery=_sync_device_discovery,
        refresh_shared=_ds.refresh_shared,
        publishing=_publishing,
        opcua_block=_opcua_block,
        apply_rest_push=_apply_rest_push,
        rest_push_public=_rest_push_public,
        rest_cfg=_rm.rest_cfg,
        rest_provider=_rm.rest_provider,
        rest_push_manager=rest_push_manager,
        rest_hdr_mask=_REST_HDR_MASK,
        pub=_pub,
        cs=_cs,
        serialized_mutation=_serialized_mutation,
        field_errors=_ds.field_errors,
        attributes=attributes,
        new_component=_ds.new_component,
        kind_of_payload=_ds.kind_of_payload,
        parent_or_404=_ds.parent_or_404,
        component_types=_ds.component_types,
        component_view=_ds.component_view,
        errors_detail=_errors_detail,
        start_new_component=_ds.start_new_component,
        set_new_node=_set_new_node,
        component_or_404=_ds.component_or_404,
        refuse_if_used=_refuse_if_used,
        drop_component_runtime=_ds.drop_component_runtime,
        reload_linked_to=_ds.reload_linked_to,
        device_type_of=_device_type_of,
        publish_attribute_meta=_publish_attribute_meta,
        start_device_client=_start_device_client,
        gw_role=gw_role,
        lease_mgr=_lease_mgr,
        cmd_dispatch=_cmd_dispatch,
        write_rate_ok=_write_rate_ok,
        write_rule=_write_rule,
        writes_by_mqtt=_writes_by_mqtt,
        refuse_busy_poll_groups=_ds.refuse_busy_poll_groups,
        reload_poll_groups=_ds.reload_poll_groups,
        rw=_rw,
        es=_es,
        dv=_dv,
        protocols_by_context=PROTOCOLS_BY_CONTEXT,
        modbus_probe=_probe.modbus_probe,
        driver_of=_probe.driver_of,
        tap_hearing=_probe.tap_hearing,
        mqtt_hearing=_probe.mqtt_hearing,
        one_test_at_a_time=_probe.one_test_at_a_time,
        test_done=_probe.test_done,
        device_entry=_device_entry,
        check_if_match=_check_if_match,
        device_tag_now=_rm.device_tag_now,
        source_probe=_probe.source_probe,
        dep_sources=_dep_sources,
        can_test=_probe.can_test,
        test_saved_device=_probe.test_saved_device,
        virtual_map_probe=_probe.virtual_map_probe,
        apply_routing_defaults=_ds.apply_routing_defaults,
        autoselect_template_registers=_autoselect_template_registers,
        built_sig=_built_sig,
        components_on_create=_ds.components_on_create,
        delete_device_locked=_ds.delete_device_locked,
        device_deletable=_ds.device_deletable,
        device_etag=_rm.device_etag,
        device_put_raw=_ds.device_put_raw,
        drop_echoed_defaults=_ds.drop_echoed_defaults,
        drop_spec_keys=_drop_spec_keys,
        ensure_device_bucket=_ensure_device_bucket,
        patch_locks=_ds.patch_locks,
        patch_primary=_ds.patch_primary,
        poll_groups_now=_ds.poll_groups_now,
        raw_index=_rm.raw_index,
        refuse_prefix=_rm.refuse_prefix,
        req_ip=_mw.req_ip,
        req_user=_mw.req_user,
        restore_plan=_ds.restore_plan,
        restore_resets=_ds.restore_resets,
        restore_resumes=_ds.restore_resumes,
        update_device_locked=_ds.update_device_locked,
        would_be_entry=_ds.would_be_entry,
        live_fields=_LIVE_FIELDS,
        counter_resets_kept=_COUNTER_RESETS_KEPT,
    )
    app.state.ctx = ctx
    for _mod in (calculated, commissioning, config_routes,
                 device_templates, diagnostics, discovery_routes, energy,
                 general_config, languages, metrics, pq, registers_routes, rules_routes, sequences_routes, opcua_routes,
                 role_routes,
                 status_routes, system, auth_routes, values_routes, vmeters, bridges_routes,
                 attributes_routes, snapshots_routes, mqtt_discovery_routes, meters_routes,
                 device_outputs_routes, config_apply_routes, config_bundle_routes,
                 commands_routes, components_routes, writes_routes, endpoints_routes,
                 dependents_routes, device_tests_routes):
        app.include_router(_mod.build(ctx))
    app.include_router(auth_routes.build_passkeys(ctx))
    from multibus.routes import device_types as device_types_routes      # ADR-0027
    app.include_router(device_types_routes.build(ctx))
    from multibus.routes import buses as buses_routes                    # ADR-0031 §5
    app.include_router(buses_routes.build(ctx))
    from multibus.routes import previews as previews_routes              # ADR-0031 §5, L3.8
    app.include_router(previews_routes.build(ctx))
    from multibus.routes import bus_scan as bus_scan_routes              # ADR-0031 §2, L3.7
    app.include_router(bus_scan_routes.build(ctx))
    from multibus.routes import vbms as vbms_routes                      # ADR-0037 virtual BMS
    app.include_router(vbms_routes.build(ctx))

    # Device CRUD, incl. the catch-all GET /api/devices/{device_id} (get_device).
    # Included LAST, and get_device is the LAST route defined in routes/devices.py,
    # so the specific /api/devices/<word> routes (restorable, …) are not shadowed.
    from multibus.routes import devices as devices_routes
    app.include_router(devices_routes.build(ctx))

    # --- Static files ---

    # Mount static files last
    app.mount("/static", StaticFiles(directory="ui"), name="static")

    # stored topic prefixes that share topics: said once, never refused (ADR-0026 §4)
    try:
        from multibus.devices.topic_prefix import report_stored
        report_stored(config)
    except Exception:  # noqa: BLE001 — a report must never stop the gateway
        logger.debug("topic prefix report skipped", exc_info=True)

    # Register the WRITE-AWARE discovery hooks at boot (external audit):
    # main.py used to append its own hooks that published device discovery
    # WITHOUT write_rules, and _sync_device_discovery — which builds them
    # with write rules — only ran from the device CRUD routes. So after a
    # plain restart the write-blind hooks won the first MQTT connect: every
    # writable register was republished as a plain sensor and its command
    # topic unsubscribed. HA control worked until the first restart, then
    # silently died. ONE owner now: this call (publish is skipped while the
    # broker is still disconnected; the hooks fire on connect).
    _sync_device_discovery()

    # the dead-man sweeper last: every revert it may fire (boot recovery's
    # first) finds the whole write path defined (RP-12)
    _lease_mgr.start()

    return app, ws_manager
