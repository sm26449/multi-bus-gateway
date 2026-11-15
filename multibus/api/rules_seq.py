# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Rules-reload and sequence-run glue, extracted whole from create_api (P0.8b squeeze).

RulesSeq holds the rules-reload closures (rule_poll_now, rule_fields_of, rules_reload)
that rebind rule targets when devices change, and seq_run_command (the SequenceRunner's
per-step command runner). Kept as closures built in __init__ so the bodies are verbatim.
"""
from __future__ import annotations

import logging
import time

from multibus.api.driver_names import _driver_reg_names
from multibus.transports.driver import RegisterReadable

logger = logging.getLogger("multibus.api")  # preserve caplog logger name


class RulesSeq:
    """The rules-reload + sequence-run seam (ADR-0038 service, api-layer for now)."""

    def __init__(self, *, calc_engine, config, _fields_cache, _fields_warned, _find_device, _modbus_driver_of, _mqtt_driver_of, registry, _run_named_command):
        def _rule_poll_now(dev_cfg, client, group):
            drv = _modbus_driver_of(client)
            if isinstance(drv, RegisterReadable):
                drv.poll_now(group or None)
                return
            # an MQTT source cannot be swept; one with a keepalive is asked to
            # republish everything (a Venus GX on an empty keepalive)
            mq = _mqtt_driver_of(client)
            if mq is not None and getattr(mq, 'keepalive', None):
                mq.send_keepalive(first=True)

        def _rule_fields_of(dev_id: str):
            hit = _fields_cache.get(dev_id)
            if hit is not None and time.monotonic() - hit[0] < 30.0:
                return hit[1]
            dcfg = config.get_device(dev_id)
            if dcfg is None:
                if not any(str(p.get('id')) == dev_id for p in (config.endpoints or [])):
                    names = False
                else:
                    from multibus.devices.endpoint_aggregator import latest_totals
                    got = latest_totals(dev_id)
                    # empty totals (every unit asleep at night) cannot judge a
                    # name: "reads nothing" would light the rule amber nightly
                    names = set(got[0]) if got and got[0] else None
            else:
                try:
                    _i, _c, client = _find_device(dev_id)
                    names = _driver_reg_names(client) if client is not None else None
                except Exception:  # noqa: BLE001
                    names = None
                try:
                    held = {it.get('name') for it in list((registry.store_for(dev_id) or {}).values()) if it.get('name')}
                    if names is None and held:  # no driver lists its registers: what it has read
                        names = set()
                    if names is not None:      # nothing running and nothing read: cannot judge
                        names |= held
                        names |= {e['_reg'].name for e in calc_engine.store.get(dev_id, []) if e.get('_reg') is not None}
                        from multibus.devices.linked_meter import names_for
                        from multibus.devices.meter_links import links_of
                        names |= set(names_for(links_of(dcfg)))
                except Exception:  # noqa: BLE001 — cannot judge, so no problem
                    if dev_id not in _fields_warned:
                        _fields_warned.add(dev_id)
                        logger.warning("rule inputs: cannot list what %s reads; its references are not judged",
                                       dev_id, exc_info=True)
                    names = None
                # an MQTT input learns its names from what arrives: an empty list
                # is "not yet", not "nothing"
                if names is not None and not names and str(getattr(dcfg, 'protocol', '')).lower() == 'mqtt':
                    names = None
            _fields_cache[dev_id] = (time.monotonic(), names)
            return names

        def _seq_run_command(device_id, name, params, *, who, via, dry_run=False):
            _i, dcfg, cl = _find_device(device_id)
            if dcfg is None:
                return 404, {'status': 'rejected', 'reason': f"no device '{device_id}'"}
            return _run_named_command(dcfg, cl, name, dict(params), who=who, ip='-', via=via, dry_run=dry_run)

        # --- public surface -------------------------------------------------
        self.rule_poll_now = _rule_poll_now
        self.rule_fields_of = _rule_fields_of
        self.seq_run_command = _seq_run_command
