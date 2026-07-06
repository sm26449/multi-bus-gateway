"""API route modules — the 95 routes of ``create_api()`` split by domain.

Each module exposes ``build(ctx) -> APIRouter``; ``api.py`` constructs one
``ApiCtx`` with the shared singletons and mounts every router with
``app.include_router``. Paths are IDENTICAL to the old ``@app`` registrations
(the auth/allowlist middleware guards by path, so nothing changes there).

The publishers deserve a note: ``/api/config/apply`` can REBIND
``mqtt_publisher``/``influxdb_publisher`` (it creates a new one when a sink is
enabled after boot). Inside ``create_api`` that rebinding is a ``nonlocal``;
route modules can't see closure cells, so the same apply handler also writes
the new object into ``ctx``. Routers must therefore read publishers from
``ctx.<name>`` AT REQUEST TIME — never grab them once in ``build()``.
"""
from __future__ import annotations


class ApiCtx:
    """Shared state handed to route modules.

    Attributes (stable singletons unless noted):
      app                FastAPI — for app.state lookups (e.g. vmeter_manager)
      config             Config — YAML config manager
      template_registry  TemplateRegistry — device-template library
      current_values     dict — the primary device's live store (legacy alias)
      last_update        dict — {'timestamp': iso} stamped by the poller callback
      modbus_client      ModbusClient|None — the PRIMARY's client (stable ref;
                         mutated in place by config updates, never rebound)
      registry           DeviceRegistry — device pairs + per-device stores
      calc_engine        CalcEngine — calculated registers runtime
      event_log          EventLog — persisted event ring
      alert_mgr          AlertManager — alerting channels/state
      auth_state         auth.AuthState — sessions/lockout
      api_key            str — optional API key ("" when unset)
      mqtt_publisher     MQTTPublisher|None — MUTABLE: rebound by /api/config/apply
      influxdb_publisher InfluxDBPublisher|None — MUTABLE: rebound by /api/config/apply
      audit_log          AuditLog — append-only who-changed-what trail
    """

    def __init__(self, **kw):
        self.__dict__.update(kw)
