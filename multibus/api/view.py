# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Device-row view builder, extracted whole from ``create_api`` (P0.8b squeeze).

``DeviceView`` holds the two closures that build a device's JSON row: the big
``device_entry`` (summary, kind/type, labels, components, bridge, meters,
commands, writable registers, connection, outputs, discovery) and its
``unit_read_via`` helper (the per-source read health). They are kept as closures
built in ``__init__`` so the bodies are **verbatim** — every collaborator they
captured from ``create_api`` arrives as a keyword constructor argument under the
same name.

Layer-clean intermediate in the ``api`` layer. The public surface is
``entry`` / ``unit_read_via``.
"""
from __future__ import annotations

import logging
from typing import Dict

from multibus.catalog.device_kind import device_kind as _device_kind
from multibus.core.config import (
    CAN_PROTOCOLS, SERIAL_PROTOCOLS, VIRTUAL_MAP_PROTOCOLS, VIRTUAL_MAP_SERIAL)

logger = logging.getLogger("multibus.api")  # preserve caplog logger name


class DeviceView:
    """The device-row view seam (ADR-0038 service, api-layer for now).

    Collaborators are injected explicitly (what used to be implicit capture of
    ``create_api``'s closure): the stores (config, template_registry,
    attributes, calc_engine) and the sibling read helpers.
    """

    def __init__(self, *, config, template_registry, attributes, calc_engine, rest_push_manager, _pub, _commands_for, _component_type_name, _declared_writes, _device_specs, _device_type_of, _field_meta_any, _glance_names, _meters_status, _opcua_sink, _publishing, _rest_push_public, _stored_device_raw, _store_meta, _unit_alarms, _unit_live):
        def _device_entry(dev_cfg, client, *, redact=False, raw_index=None, claims=None,
                          comp_counts=None) -> Dict:
            entry = dev_cfg.summary()
            entry['kind'], entry['kind_source'] = _device_kind(dev_cfg, template_registry)
            entry['type'], entry['type_label'] = _device_type_of(dev_cfg)          # ADR-0027
            try:
                _labels = attributes.labels_of('device', dev_cfg.id)
            except Exception:  # noqa: BLE001 — a label never fails a list
                _labels = []
            if _labels:
                entry['labels'] = _labels
            # whether acquisition runs for it: an enabled device that does not is
            # revived by saving it again (update_device) — the page offers that
            entry['running'] = client is not None
            # component count: O(1) from a prebuilt map when a whole list is
            # being rendered (PERF-12 — avoids an O(N) scan per row), else the
            # direct scan for a single-device call.
            _nc = (comp_counts.get(dev_cfg.id, 0) if comp_counts is not None
                   else sum(1 for d in config.devices if d.component_of == dev_cfg.id))
            if _nc:
                entry['components'] = _nc
            if dev_cfg.component_of:
                entry['component_type_name'] = _component_type_name(dev_cfg)
            # the bridge (and its bus port) this device reaches its bus through
            _bid = config._device_bridge(dev_cfg.id)
            if _bid:
                entry['bridge'] = _bid
                _raw = next((x for x in config._raw_devices if x.get('id') == dev_cfg.id), {}) or {}
                entry['bridge_port'] = (_raw.get('connection') or {}).get('bridge_port') or dev_cfg.connection.port
            regs = config.unit_registers(dev_cfg, shared=True)   # every source's selection, one entry per name (read only)
            entry['selected_registers'] = len(regs)
            # an energy counter to chart (the Energy tab means nothing without one)
            _E_UNITS = ('wh', 'kwh', 'mwh', 'varh', 'kvarh', 'vah', 'kvah', 'mvah')
            entry['energy_fields'] = sum(1 for r in regs if str(getattr(r, 'unit', '') or '').lower() in _E_UNITS
                                         or str(getattr(r, 'name', '')).startswith('energy_'))
            # another device that measures this one's electricity (linked_meter.py)
            from multibus.devices.meter_links import link_for, links_of
            # the consumption link in its earlier shape, wherever it is stored
            # (a sign other than auto says itself)
            _cons = link_for(dev_cfg, 'consumption')
            entry['energy_meter'] = ({k: v for k, v in _cons.items() if k != 'role' and (k != 'sign' or v != 'auto')}
                                     if _cons else {})
            entry['meters'] = links_of(dev_cfg)
            # an input / output pair: its honesty thresholds, and the verdict of
            # its LAST evaluation — what was published, what went absent and why
            # ({} without a pair; `waiting` until a meter has reported)
            from multibus.devices.meter_links import options_of
            entry['meter_options'] = options_of(dev_cfg)
            # the device's own thresholds, apart from the defaults: an editor
            # sends these back (a key left out is its default, null is refused)
            entry['meter_options_own'] = dict(getattr(dev_cfg, 'meter_options', None) or {})
            entry['meters_status'] = _meters_status(dev_cfg.id)
            # its meters' counters (built when its calculated fields loaded — the
            # list never reads the meters' files)
            entry['energy_fields'] += sum(1 for e in calc_engine.store.get(dev_cfg.id, [])
                                          if e.get('_linked') and e['_reg'].name in
                                          ('energy_electric', 'energy_in', 'energy_out'))
            entry['influxdb_device_tag'] = dev_cfg.influxdb_device_tag
            from multibus.vendors.pq_recorder import template_supports_pq
            _tpl = (template_registry.get(dev_cfg.template)
                    if template_registry and dev_cfg.template else None)
            entry['pq_supported'] = template_supports_pq(_tpl, dev_cfg.template)
            # its template declares components (a GX's batteries): Components…
            # is offered from the row, not after a template list loaded (L3.10 m4)
            entry['components_declared'] = bool(getattr(_tpl, 'components', None) or [])
            entry['template_notes'] = list(getattr(_tpl, 'notes', None) or [])
            entry['pq_recorder'] = dict(dev_cfg.pq_recorder or {})
            # Replace hardware's energy-counter reset markers (PATCH ?intent=replace,
            # L3-F17): [{at, ts, reason}], oldest first — only when there are any
            _resets = ([] if dev_cfg.primary
                       else _stored_device_raw(dev_cfg, raw_index).get('energy_counter_resets') or [])
            if _resets:
                entry['energy_counter_resets'] = list(_resets)
            try:
                _cmds = _commands_for(dev_cfg)
            except Exception:  # noqa: BLE001 — a bad recipe must not hide the device
                _cmds = []
            entry['commands'] = [c['def'].name for c in _cmds if c['enabled']]
            entry['commands_offered'] = [c['def'].name for c in _cmds]
            # the registers its template declares writable: an operator writes
            # only these (a raw write is the admin's) — the ⋯ menu says so before
            # the form is typed (ADR-0032 §3)
            entry['writable_registers'] = _declared_writes(dev_cfg)
            # full connection block for the device detail editor
            c = dev_cfg.connection
            _url = dev_cfg.http.get('url', '') if dev_cfg.protocol == 'http' else ''
            if redact and _url:
                # a viewer must not see credentials embedded in the URL (userinfo or
                # a token query param); the admin sees the real URL to edit it
                from multibus.core.redact import redact_url
                _url = redact_url(_url)
            entry['connection'] = {
                'protocol': dev_cfg.protocol,
                'host': c.host, 'port': c.port, 'unit_id': c.unit_id,
                'timeout': c.timeout, 'retry_attempts': c.retry_attempts,
                'retry_delay': c.retry_delay,
                'serial_port': c.serial_port, 'baudrate': c.baudrate,
                'parity': c.parity, 'stopbits': c.stopbits, 'bytesize': c.bytesize,
                'url': _url,
                'verify_tls': dev_cfg.http.get('verify_tls', True),
            }
            if dev_cfg.protocol == 'none':
                # a device that IS its meters has no address: none is shown, and
                # this is what a save sends back (review N3)
                from multibus.devices.meter_links import links_of as _lo
                entry['connection'] = {'protocol': 'none'}
                entry['measured_by'] = list(dict.fromkeys(ln['device'] for ln in _lo(dev_cfg)))
            if dev_cfg.protocol == 'mqtt':
                m = dev_cfg.mqtt_in
                entry['connection'].update({
                    'broker': m.get('broker', ''), 'port': m.get('port', 1883),
                    'topic': m.get('topic', ''), 'username': m.get('username', ''),
                    'tls': bool(m.get('tls', False)),
                    'tls_insecure': bool(m.get('tls_insecure', False)),
                    'tls_ca': m.get('tls_ca', ''),
                    'password': '******' if m.get('password') else '',   # never echo the secret
                    'vars': dict(m.get('vars') or {}),
                    'stale_after_s': m.get('stale_after_s'),
                    # the device's own keepalive (a dict, or false = off), and the
                    # template's — what applies when the device says nothing
                    'keepalive': m.get('keepalive'),
                })
                _tk = template_registry.get(dev_cfg.template) if dev_cfg.template else None
                entry['connection']['template_keepalive'] = ((getattr(_tk, 'protocol', None) or {}).get('keepalive')
                                                             if _tk is not None else None)
            if dev_cfg.protocol in CAN_PROTOCOLS:
                # what the connection form shows for a CAN link: the carrier, the
                # interface, the bitrate the bus runs at, how long quiet stays fresh
                _can = dict(getattr((dev_cfg.sources or [None])[0], 'can', None) or {})
                entry['connection'].update({
                    'carrier': _can.get('carrier', 'socketcan'), 'channel': _can.get('channel', ''),
                    'bitrate': _can.get('bitrate'),
                    'stale_after_s': _can.get('stale_after_s', c.stale_after_s),
                })
            if dev_cfg.protocol in VIRTUAL_MAP_PROTOCOLS:
                entry['connection']['mode'] = c.mode or 'poll'
            # what the stored connection means, on its axes (ADR-0026 §1): beside
            # `connection`, never inside it — a save echoes `connection` back
            try:
                _spec, _per = _device_specs(dev_cfg, raw_index)
                entry['connection_spec'] = _spec
                if _per:
                    entry['source_specs'] = _per
            except Exception:  # noqa: BLE001 — an unreadable shape must not hide the device
                logger.debug("device %s: connection spec unavailable", dev_cfg.id, exc_info=True)
                entry['connection_spec'] = None
            # the one-line connection summary and the edit locks (ADR-0031 §5),
            # beside `connection` like the spec: never sent back by a save
            try:
                from multibus.devices.device_summary import device_view
                entry.update(device_view(config, dev_cfg, template_registry,
                                         raw=_stored_device_raw(dev_cfg, raw_index),
                                         spec=entry.get('connection_spec'), claims=claims))
            except Exception:  # noqa: BLE001 — a summary must not hide the device
                logger.debug("device %s: summary unavailable", dev_cfg.id, exc_info=True)
                entry['summary'], entry['locks'] = None, []
            # what each PATCH section takes for it (ADR-0032 §6): the primary's
            # adapter takes fewer keys; a component or a unit is edited elsewhere
            from multibus.devices.device_patch import edit_sections
            entry['edit_sections'] = edit_sections(dev_cfg.primary,
                                                   whole_refused=bool(dev_cfg.component_of or dev_cfg.endpoint_id))
            if dev_cfg.protocol in SERIAL_PROTOCOLS + ('rtu_tap',) + VIRTUAL_MAP_SERIAL:
                entry['serial'] = dev_cfg.serial
            if dev_cfg.protocol == 'http':
                # Never echo header VALUES back — they can carry Authorization / API
                # tokens and /api/devices is readable by the viewer role. Show the
                # header names only (masked); the UI does not round-trip headers.
                _http = dict(dev_cfg.http)
                if _http.get('headers'):
                    _http['headers'] = {k: '******' for k in _http['headers']}
                entry['http'] = _http
            # Output-sink status (Phase 2): per-device enable + the shared broker/db
            # connection state, so the device detail can show each sink live.
            mqtt_conn = bool(getattr(_pub.mqtt, 'connected', False)) if _pub.mqtt else False
            influx_conn = bool(getattr(_pub.influx, 'connected', False)) if _pub.influx else False
            entry['sinks'] = {
                'mqtt': {
                    'enabled': dev_cfg.mqtt_enabled,
                    'available': _pub.mqtt is not None,
                    'connected': mqtt_conn,
                    'active': dev_cfg.mqtt_enabled and mqtt_conn,
                    'topic_prefix': dev_cfg.mqtt_topic_prefix,
                },
                'influxdb': {
                    'enabled': dev_cfg.influxdb_enabled,
                    'available': _pub.influx is not None,
                    'connected': influx_conn,
                    'active': dev_cfg.influxdb_enabled and influx_conn,
                    'bucket': dev_cfg.influxdb_bucket,
                },
                # HTTP/JSON output: serve this device's live values as JSON, Solar-API
                # style. Always "available" (it's just this app's own HTTP server);
                # active == enabled. Read-only, no external connection to fail.
                'http': {
                    'enabled': dev_cfg.http_output_enabled,
                    'available': True,
                    'connected': True,
                    'active': dev_cfg.http_output_enabled,
                    'path': f"/api/meters/{dev_cfg.id}",
                },
                # OPC UA: served on the gateway's OPC UA server (Settings → OPC UA),
                # every value or the chosen ones; active while the server runs
                'opcua': _opcua_sink(dev_cfg),
                # Generic REST push: POST values to an external URL on an interval.
                'rest': {
                    'enabled': bool(dev_cfg.rest_push.get('enabled')),
                    'available': True,
                    'connected': rest_push_manager.status(dev_cfg.id).get('ok') is not False,
                    'active': bool(dev_cfg.rest_push.get('enabled')),
                    'url': dev_cfg.rest_push.get('url', ''),
                },
            }
            entry['rest_push'] = _rest_push_public(dev_cfg)
            entry['publishing'] = _publishing(dev_cfg)
            if redact:
                # The same credential can ride in more than one URL copy — the
                # summary's http_url, the rest sink url, and the rest_push url all
                # re-emit it. Redact every copy for a viewer (connection.url above
                # was only one of them).
                from multibus.core.redact import redact_url
                if entry.get('http_url'):
                    entry['http_url'] = redact_url(entry['http_url'])
                if entry['sinks']['rest'].get('url'):
                    entry['sinks']['rest']['url'] = redact_url(entry['sinks']['rest']['url'])
                if isinstance(entry.get('rest_push'), dict) and entry['rest_push'].get('url'):
                    entry['rest_push']['url'] = redact_url(entry['rest_push']['url'])
            stats, verdicts = {}, {}
            if client:
                stats = client.get_stats()
                connected = stats.get('connected')
                hv = client.data_health()
                health = hv.get('status')
                verdicts = dict(hv.get('sources') or {})
                # The status dot must never contradict the connection text: a device
                # that isn't connected can't be 'ok'. data_health() reports 'ok' on
                # cold start / when nothing has been polled yet, so gate it on the
                # actual connection — not connected but enabled => degraded (amber),
                # 'down' only once reads have actually been failing.
                if not connected and health == 'ok':
                    health = 'degraded'
                entry.update({
                    'connected': connected,
                    'successful_reads': stats.get('successful_reads'),
                    'failed_reads': stats.get('failed_reads'),
                    # a push-fed device (MQTT, tap) is never polled: its age is the
                    # age of its last message, which data_health() measures
                    'staleness_age_s': (stats.get('staleness_age_s')
                                        if stats.get('staleness_age_s') is not None
                                        else hv.get('staleness_age_s')),
                    'last_read_ts': stats.get('last_success_ts'),
                    'poll_rate': stats.get('poll_rate'),
                    'data_health': health,
                })
                from multibus.transports.can.can_client import stats_summary as _can_summary
                _cs = _can_summary(stats)
                if _cs:
                    entry['can'] = _cs          # frames, unknown ids, decode errors, listen-only
            else:
                # disabled or transport not run — idle (grey), not green
                entry.update({'connected': False, 'data_health': 'idle'})
            _unit_read_via(dev_cfg, entry, stats, verdicts, redact=redact)
            return entry

        def _unit_read_via(dev_cfg, entry: Dict, stats: Dict, verdicts: Dict, *, redact=False) -> None:
            """How a unit of an installation is actually read, for its own page.

            The flat ``connection`` block above is the fallback a unit was born with;
            a unit read through sources is described by THEM — protocol, address,
            interval, template, and what each one delivers right now. The unit page
            showed a Modbus form with an empty host for a unit read over HTTP every
            two seconds; this is the truth it shows instead."""
            if not dev_cfg.endpoint_id:
                return
            from multibus.core.redact import redact_url
            ep = config.get_raw_endpoint(dev_cfg.endpoint_id) or {}
            role = getattr(dev_cfg, 'role', '') or ''
            entry['endpoint_name'] = ep.get('name') or dev_cfg.endpoint_id
            entry['group_id'] = getattr(dev_cfg, 'group_id', '') or 'units'
            entry['role'] = role
            entry['glance'] = list(_glance_names(dev_cfg))
            entry['alarms'] = _unit_alarms(dev_cfg)
            entry['live'] = _unit_live(dev_cfg, entry['glance'])
            _sm = _store_meta([dev_cfg])
            entry['fields'] = {n: m for n, m in ((n, _field_meta_any(n, _sm)) for n in entry['glance']) if m}
            by_id = {r.get('id'): r for r in (stats.get('sources') or [])}
            provides: Dict[str, int] = {}
            for _f, prov in (stats.get('provenance') or {}).items():
                provides[prov.get('source')] = provides.get(prov.get('source'), 0) + 1
            out = []
            for src in dev_cfg.sources or []:
                proto = str(src.protocol or 'tcp').lower()
                if proto == 'http':
                    url = str((src.http or {}).get('url', ''))
                    address = redact_url(url) if redact else url
                elif proto == 'mqtt':
                    address = str((src.mqtt_in or {}).get('topic', ''))
                elif proto in SERIAL_PROTOCOLS + ('rtu_tap',) + VIRTUAL_MAP_SERIAL:
                    address = f"{src.connection.bus} · unit {src.connection.unit_id}"
                else:
                    address = f"{src.connection.host}:{src.connection.port} · unit {src.connection.unit_id}"
                regs, groups = config.load_device_registers(dev_cfg, source=src)
                used = {r.poll_group for r in regs}
                ivals = [g.interval for n, g in groups.items() if n in used]
                live = by_id.get(src.id) or {}
                r5 = live.get('reads_5m') or 0
                timeout = ((src.http or {}).get('timeout') if proto == 'http'
                           else getattr(src.connection, 'timeout', None))
                out.append({
                    'id': src.id, 'protocol': proto, 'address': address,
                    'template': src.template or dev_cfg.template,
                    'enabled': bool(getattr(src, 'enabled', True)),
                    'interval_s': min(ivals) if ivals else None,
                    'timeout_s': timeout,
                    'stale_after_s': float(getattr(src, 'stale_after_s', 0) or 0),
                    'registers': len(regs),
                    'provides': provides.get(src.id, 0),
                    'status': verdicts.get(src.id, 'idle' if not stats else 'down'),
                    'latency_ms': live.get('last_latency_ms'),
                    'reads_5m': r5, 'failed_5m': live.get('failed_5m') or 0,
                    'fail_pct_5m': (round(100.0 * (live.get('failed_5m') or 0) / r5, 1)
                                    if r5 else None),
                })
            entry['read_via'] = out

        # --- public surface -------------------------------------------------
        self.entry = _device_entry
        self.unit_read_via = _unit_read_via
