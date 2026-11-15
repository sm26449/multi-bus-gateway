"""EndpointService — the installations/endpoints domain logic (ADR-0038).

The view builders (sources, bus, aggregate/headline, group entries, totals,
the full endpoint entry), the payload validators (commands, channels, sources,
group connection, the whole endpoint payload), and the endpoint CRUD
orchestration (runtime signature, echo-drop, put-raw, removed-unit refusal,
unit rename, update/delete under the mutation lock) move VERBATIM out of
``create_api`` (P0.8b Lot E) into this class via the closure-holder pattern:
every captured ``create_api`` collaborator becomes a constructor parameter, the
function bodies are byte-identical, and each is exposed as ``self.<name>`` so
the routes still in ``create_api`` call the same callables through thin aliases.

This stays in the ``api`` layer (like Lot B's ``DeviceValidator``): several of
these validators/CRUD helpers ``raise HTTPException`` directly, which a lower
layer may not. A move to ``devices/`` waits on the deferred exception-seam step.
``logger`` is injected (kept named ``multibus.api``) so log lines and caplog
assertions are unchanged; ``PROTOCOLS_BY_CONTEXT`` is injected rather than
imported from ``multibus.api`` to avoid an import cycle.
"""
from __future__ import annotations

import copy
import json
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from fastapi import HTTPException

from multibus.devices.device_registry import client_is_live, client_health
from multibus.devices.validation import (influx_block_errors, _INFLUX_NAME_RE,
                                          build_connection_claim_validators)
from multibus.transports.modbus.modbus_client import endpoint_bus_stats
from multibus.payload import flag
from multibus.devices.save_merge import keep_unsaid, merge_endpoint_routes
from multibus.api.validation import _DEVICE_ID_RE, validate_commands, validate_channels
from multibus.devices.device_validation import _is_serial_path, _serial_host_error
from multibus.core.config import SERIAL_PROTOCOLS, NETWORK_PROTOCOLS

# verbatim from create_api (api/__init__.py): stable contract constants the
# moved validators/CRUD read. _POLLED_MODBUS mirrors control/raw_writes.py.
_POLLED_MODBUS = NETWORK_PROTOCOLS + SERIAL_PROTOCOLS
GROUP_KEPT_KEYS = ('mqtt', 'influxdb', 'opcua', 'channels')


class EndpointService:
    """Installations/endpoints view, validation and CRUD (ADR-0038 #1)."""

    def __init__(self, *, config, registry, template_registry, logger, app, _group_commands, _group_cmd_prefix, _teardown_device, _autoselect_template_registers, _ensure_device_bucket, _start_device_client, _device_entry, _opcua_block, _dep_sources, _built_sig, _sync_device_discovery, _cancel_device_refreshes, _device_value_topics, _clear_device_values, _publish_attribute_meta, _refuse_if_used, _serialized_mutation, PROTOCOLS_BY_CONTEXT) -> None:
        # Compose §1's shared connection/claim validators directly (ADR-0040 E3):
        # the endpoint bus-claim check is the shared `endpoint_claim_errors`, no
        # longer injected from DeviceValidator. The namespace closes over the live
        # `config`/`template_registry` — the same objects the device side uses.
        _cv = build_connection_claim_validators(config, template_registry)

        def _endpoint_claim_check(raw: Dict, pid: str, prev: Dict = None) -> None:
            errs = _cv.endpoint_claim_errors(raw, pid, prev)
            if errs:
                raise HTTPException(status_code=422, detail={"errors": errs})

        def _endpoint_sources(p: Dict, pid: str, holder: Dict = None,
                              group_id: str = '') -> List[Dict]:
            """Every declared way of reaching this endpoint's units, in precedence
            order, with what each one is actually contributing right now.

            Configuration and live state in one object on purpose: "which source is
            this value from" and "is that source still alive" are the same question
            for an operator, and answering them from two endpoints would invite them
            to drift. Flat JSON, so a Node-RED http-request node reads it directly.
            """
            h = holder if holder is not None else p
            raw = h.get('sources') or []
            if not raw:
                conn = dict(h.get('connection', {}) or {})
                if not conn:
                    return []
                raw = [{'id': 'default',
                        'template': h.get('template', '') or p.get('template', ''),
                        **conn}]
            # roll the live picture up across the endpoint's units
            live: Dict[str, Dict] = {}
            wire: Dict[str, Any] = {}     # a listen-only tap's shared-reader health
            for dev in config.endpoint_devices(pid):
                if group_id and (getattr(dev, 'group_id', '') or 'units') != group_id:
                    continue
                _i, _c, client = registry.find(dev.id)
                if client is None:
                    continue
                try:
                    st = client.get_stats() or {}
                except Exception:  # noqa: BLE001
                    continue
                for row in (st.get('sources') or []):
                    agg = live.setdefault(row['id'], {
                        'units_ok': 0, 'units_total': 0, 'reads': 0, 'failed': 0,
                        'reads_5m': 0, 'failed_5m': 0, 'latency_ms': [], 'owns': 0})
                    agg['units_total'] += 1
                    agg['units_ok'] += 1 if row.get('connected') else 0
                    agg['reads'] += int(row.get('successful_reads') or 0)
                    agg['failed'] += int(row.get('failed_reads') or 0)
                    agg['reads_5m'] += int(row.get('reads_5m') or 0)
                    agg['failed_5m'] += int(row.get('failed_5m') or 0)
                    if row.get('last_latency_ms') is not None:
                        agg['latency_ms'].append(row['last_latency_ms'])
                for _f, prov in (st.get('provenance') or {}).items():
                    a = live.get(prov.get('source'))
                    if a is not None:
                        a['owns'] += 1
                if not wire and isinstance(st.get('bus'), dict):
                    # one reader per port: any unit's view of it is THE view
                    ec = st.get('error_counts') or {}
                    wire = {'frames': st['bus'].get('frames'),
                            'writes_seen': st['bus'].get('writes_seen'),
                            'open_error': st['bus'].get('open_error') or '',
                            'crc_errors': ec.get('crc_errors'),
                            'orphan_frames': ec.get('orphan_frames'),
                            'exception_responses': ec.get('exception_responses'),
                            'resync_dropped_bytes': ec.get('resync_dropped_bytes'),
                            # the shared reader's own (TAP-DIAG-2): pairing quality
                            # and what the resync threw away, newest last
                            'unpaired_responses': st['bus'].get('unpaired_responses'),
                            'paired_across_junk': st['bus'].get('paired_across_junk'),
                            'junk_samples': list(st['bus'].get('junk_samples') or [])}

            out = []
            for rank, r in enumerate(raw):
                sid = str(r.get('id') or f'source{rank + 1}')
                agg = live.get(sid) or {}
                lat = agg.get('latency_ms') or []
                pg = {k: (v.get('interval') if isinstance(v, dict) else v)
                      for k, v in (r.get('poll_groups') or {}).items()}
                ivals = [float(v) for v in pg.values() if isinstance(v, (int, float))]
                r5 = agg.get('reads_5m') or 0
                out.append({
                    'id': sid, 'rank': rank,
                    'enabled': bool(r.get('enabled', True)),
                    'protocol': str(r.get('protocol', 'tcp')).lower(),
                    'template': (r.get('template', '') or h.get('template', '')
                                 or p.get('template', '')),
                    'address': (r.get('url')
                                or (r.get('serial_port') if r.get('protocol') in SERIAL_PROTOCOLS + ('rtu_tap',)
                                    and r.get('serial_port')
                                    else f"{r.get('host', '')}:{r.get('port', 502)}")),
                    # a serial line's settings, so an editor that writes the source
                    # back does not drop them (a 19200-baud bus read at 9600)
                    **({k: r[k] for k in ('baudrate', 'parity', 'stopbits', 'bytesize') if k in r}
                       if r.get('serial_port') else {}),
                    'poll_groups': pg,
                    # what is stored, verbatim (secrets left out — a save that does
                    # not carry them keeps them): an editor starts from this and
                    # sends back what it did not change, tuning included
                    'declared': {k: copy.deepcopy(v) for k, v in r.items()
                                 if k not in ('password', 'token', 'headers')},
                    # the fastest rhythm this source reads at — what "every N s"
                    # means to an operator; None when it inherits the template's
                    'interval_s': min(ivals) if ivals else None,
                    'timeout': r.get('timeout'),
                    'stale_after_s': float(r.get('stale_after_s', 0) or 0),
                    # live
                    'units_ok': agg.get('units_ok', 0),
                    'units_total': agg.get('units_total', 0),
                    'successful_reads': agg.get('reads', 0),
                    'failed_reads': agg.get('failed', 0),
                    # the last five minutes, not since boot: a counter that grew
                    # for a week says nothing about whether the source is fine NOW
                    'reads_5m': r5,
                    'failed_5m': agg.get('failed_5m') or 0,
                    'fail_pct_5m': (round(100.0 * (agg.get('failed_5m') or 0) / r5, 1)
                                    if r5 else None),
                    'latency_ms': round(sum(lat) / len(lat), 1) if lat else None,
                    # how many fields this source is currently authoritative for —
                    # the number that tells an operator whether it earns its place
                    'fields_owned': agg.get('owns', 0),
                    # a tap's wire: frames heard, CRC errors, orphans — the health
                    # of a bus nobody here masters
                    'wire': (wire or None) if str(r.get('protocol', '')).lower() == 'rtu_tap' else None,
                })
            return out


        def _endpoint_bus(p: Dict, units: List[Dict]) -> Dict:
            """What this access point costs, measured on the live wire.

            Every interval an operator can ask for is bounded by one number: how
            long a transaction actually takes here, times how many the sweep needs,
            divided by the lanes serving it. That number is a property of the master
            device and nobody can look it up — so it is measured, and reported next
            to the interval it constrains. ``floor_s`` is the fastest honest cadence
            at the current shape; an interval below it is a promise the wire cannot
            keep.
            """
            conn = p.get('connection', {}) or {}
            host, port = conn.get('host', ''), int(conn.get('port', 502) or 502)
            if not host:
                return {}
            try:
                st = endpoint_bus_stats(host, port)
            except Exception:  # noqa: BLE001 — telemetry must never break the list
                return {}
            st['max_connections'] = max(1, int(conn.get('max_connections', 1) or 1))
            # Transactions per sweep: every unit's batches, as the pollers report
            # them. Missing means nothing has swept yet, and no floor is claimed.
            reads = [u.get('reads_per_cycle') for u in units if u.get('reads_per_cycle')]
            st['reads_per_sweep'] = sum(reads) if reads else None
            if st.get('tx_p95_s') and st['reads_per_sweep']:
                st['floor_s'] = round(
                    st['tx_p95_s'] * st['reads_per_sweep'] / st['lanes'], 1)
            else:
                st['floor_s'] = None
            return st


        def _display_of(dev) -> Dict:
            from multibus.catalog.display import display_of
            return display_of(template_registry, dev)


        def _unit_alarms(dev) -> Dict:
            """What the unit itself says is wrong — the template-declared alarm
            fields that are active now (fresh values only)."""
            from multibus.catalog.display import active_alarms
            return active_alarms(_display_of(dev),
                                 list((registry.store_for(dev.id) or {}).values()))


        def _glance_names(dev) -> tuple:
            names = _display_of(dev).get('glance')
            if names:
                return tuple(names)
            return _ROLE_LIVE.get(getattr(dev, 'role', '') or '', ('power_active_total',))


        def _store_meta(devs) -> Dict[str, Dict[str, str]]:
            """label/unit per field name, as the units' registers carry them."""
            meta: Dict[str, Dict[str, str]] = {}
            for dev in devs:
                for e in list((registry.store_for(dev.id) or {}).values()):
                    n = e.get('name')
                    if n and n not in meta:
                        meta[n] = {'label': e.get('label') or n, 'unit': e.get('unit') or ''}
            return meta


        def _field_meta_any(name: str, smeta: Dict) -> Optional[Dict]:
            # what the unit itself says (its row's label and unit) first; the
            # canonical dictionary fills in only what the row does not say
            from multibus.catalog.canonical_fields import field_meta
            own, canon = smeta.get(name) or {}, field_meta(name) or {}
            if not own and not canon:
                return None
            mine = {k: v for k, v in own.items() if v not in (None, '')}
            if mine.get('label') == name and canon.get('label'):
                mine.pop('label')          # a bare name is no label; the dictionary's is
            return {**canon, **mine}


        _OP_WORD = {'sum': 'total', 'avg': 'average', 'min': 'min', 'max': 'max',
                    'spread': 'spread', 'mode': ''}


        def _aggregate_meta(pid: str, devs, agg: Dict, display: Dict) -> tuple:
            """label/unit for every total of a group — canonical names keep their
            vocabulary; a template- or operator-declared total inherits its source
            field's label and unit, with the operation spelled out; the template's
            headline labels win. Also the declaration ORDER (template first)."""
            from multibus.catalog.canonical_fields import field_meta
            from multibus.devices.endpoint_aggregator import endpoint_totals
            smeta = _store_meta(devs)
            meta: Dict[str, Dict[str, str]] = {}
            order: List[str] = []

            def put(out, srcs, op):
                if out in meta:
                    return
                sm = smeta.get(srcs[0] if srcs else '') or {}
                word = _OP_WORD.get(op, op)
                if len(srcs) == 1:
                    base = sm.get('label') or srcs[0]
                    label = f"{base} ({word})" if word else base
                else:
                    # pooled over several fields: no one field names it — its own
                    # name does ("pack_min_temp" → "Pack min temp")
                    label = out.replace('_', ' ').strip().capitalize()
                meta[out] = {'label': label,
                             'unit': '' if op == 'mode' else (sm.get('unit') or '')}
                order.append(out)
            own = endpoint_totals(config, pid)
            # the headline's totals first, then the template's declaration order,
            # then the operator's own totals
            heads = [h.get('field') for h in (display.get('headline') or []) if h.get('field')]
            declared: Dict[str, tuple] = {}
            for dev in devs:
                for e in list((registry.store_for(dev.id) or {}).values()):
                    for out, op in (e.get('aggregates') or {}).items():
                        declared.setdefault(str(out), ([], op))[0].append(e.get('name') or '')
            for out in heads:
                if out in own:
                    put(out, own[out].get('from') or [], own[out].get('op'))
                elif out in declared:
                    put(out, sorted(set(declared[out][0])), declared[out][1])
            for out, (srcs, op) in declared.items():
                if out in own:
                    put(out, own[out].get('from') or [], own[out].get('op'))
                else:
                    put(out, sorted(set(srcs)), op)
            for out, t in own.items():
                put(out, t.get('from') or [], t.get('op'))
            for n in agg:
                if n not in meta:
                    m = field_meta(n)
                    if m:
                        meta[n] = m
                    order.append(n)
            for h in (display.get('headline') or []):
                f = h.get('field')
                if f in meta and h.get('label'):
                    meta[f] = dict(meta[f], label=h['label'])
            return meta, [n for n in order if n in agg]


        def _headline_items(pid: str, devs, agg: Dict, display: Dict) -> List[Dict]:
            meta, _o = _aggregate_meta(pid, devs, agg, display)
            out = []
            for h in (display.get('headline') or []):
                f = h.get('field')
                if f in agg:
                    m = meta.get(f) or {}
                    out.append({'field': f, 'label': h.get('label') or m.get('label') or f,
                                'hint': h.get('hint', ''), 'unit': m.get('unit', ''),
                                'value': agg[f]})
            return out


        def _endpoint_group_entries(p: Dict, pid: str, units: List[Dict]) -> List[Dict]:
            """One entry per unit group, with its units, its sources and its own
            total. The FIRST group owns the endpoint's headline aggregate, which is
            what keeps every topic and series that predates groups unchanged."""
            from multibus.devices.endpoint_aggregator import (aggregate_topic,
                                              compute_endpoint_aggregates)
            out = []
            for i, g in enumerate(config._endpoint_groups(p)):
                gid = g['id']
                try:
                    agg = compute_endpoint_aggregates(config, registry, pid,
                                                      group_id=gid)
                except Exception:  # noqa: BLE001 — a total must not break the page
                    agg = {}
                mine = [u for u in units if u.get('group_id') == gid]
                # a total of one unit is that unit again under another name — the
                # aggregator does not publish it, so the page must not promise it
                one = len(mine) < 2
                gm = dict(p.get('mqtt') or {}, **(g.get('mqtt') or {}))
                gx = dict(p.get('influxdb') or {}, **(g.get('influxdb') or {}))
                gdevs = [d for d in config.endpoint_devices(pid)
                         if (getattr(d, 'group_id', '') or 'units') == gid]
                disp = _display_of(gdevs[0]) if gdevs else {}
                ameta, aorder = _aggregate_meta(pid, gdevs, agg, disp) if not one else ({}, [])
                out.append({
                    'id': gid,
                    'role': g.get('role', '') or '',
                    # how the template presents its units (absent → role defaults)
                    'unit_label': disp.get('unit_label', ''),
                    'unit_label_plural': disp.get('unit_label_plural', ''),
                    'icon': disp.get('icon', ''),
                    'glance': list(_glance_names(gdevs[0])) if gdevs else [],
                    'headline_items': [] if one else _headline_items(pid, gdevs, agg, disp),
                    'enabled': bool(g.get('enabled', True)),
                    'template': g.get('template', '') or p.get('template', ''),
                    # the group's own, '' when it takes the installation's: an
                    # editor writes back this one, never the effective template
                    # (writing that down froze an inherited one into the group)
                    'template_declared': g.get('template', '') or '',
                    'units': mine,
                    # a group that declares no connection and no sources is read the
                    # way the installation is; an editor must not write that back as
                    # the group's own (it did, and lost a serial port's settings)
                    'inherits': not (g.get('sources') or g.get('connection')),
                    # how it is declared, so an editor writes it back in the same
                    # shape (a tap bus is a group connection, never a source)
                    'route': 'sources' if g.get('sources') else 'connection' if g.get('connection') else 'inherit',
                    'sources': _endpoint_sources(p, pid, g if (g.get('sources') or g.get('connection')) else p, gid),
                    # what the group's units can be told — the template's presets,
                    # each enabled by a binding on the group (raw binding kept so
                    # the editor round-trips faces / lease untouched)
                    'commands': _group_commands(pid, gid),
                    # the group's own channel overrides, as stored: the editor
                    # writes them back (ADR-0019)
                    'channels': {k: dict(v) for k, v in (g.get('channels') or {}).items() if isinstance(v, dict)},
                    'cmd_topic_prefix': _group_cmd_prefix(pid, gid, [d for d in config.endpoint_devices(pid)
                                                                     if (getattr(d, 'group_id', '') or 'units') == gid]),
                    'aggregates': {} if one else agg,
                    'aggregate_fields': ameta,
                    'aggregate_order': aorder,
                    'online_units': sum(1 for u in mine if u['connected']),
                    'total_units': len(mine),
                    # the bare endpoint path belongs to the first group; the others
                    # publish under their own name
                    'topic': None if one else aggregate_topic(p, pid, gid, i == 0),
                    # where THIS group's units publish — the page states the real
                    # paths, not the endpoint default no group may be using
                    'outputs': {'topic_prefix': gm.get('topic_prefix', ''),
                                'bucket': gx.get('bucket', ''),
                                'device_tag': gx.get('device_tag', '')},
                })
            return out


        _ROLE_LIVE = {
            'inverter': ('power_active_total', 'voltage_ln_avg', 'voltage_dc', 'power_limit_pct'),
            'meter': ('power_active_total', 'energy_active_import', 'energy_active_export'),
            'site': ('power_pv', 'power_load', 'power_grid', 'autonomy',
                     'self_consumption', 'energy_today'),
            'battery': ('power_active_total', 'voltage_dc'),
        }

        _LIVE_FALLBACK = {'voltage_ln_avg': ('voltage_l1',)}


        def _unit_live(dev, names, now: float = None) -> Dict[str, Any]:
            """``{field: value}`` for the named canonical fields of one unit, FRESH
            values only — the same freshness rule the aggregator applies, so a row
            never shows a number the total has already dropped."""
            now = now if now is not None else time.time()
            store = registry.store_for(dev.id) or {}
            by_name = {}
            for entry in list(store.values()):
                n, v, ts = entry.get('name'), entry.get('value'), entry.get('ts')
                # numbers, and text a template asks to glance at (an enum's word:
                # a pack's "Charge")
                if not n or isinstance(v, bool) or not (
                        isinstance(v, (int, float)) or (isinstance(v, str) and v.strip())):
                    continue
                interval = float(entry.get('interval') or 30)
                if ts is not None and (now - ts) <= max(4 * interval, 60.0):
                    by_name[n] = v
            out = {}
            for n in names:
                for cand in (n,) + _LIVE_FALLBACK.get(n, ()):
                    if cand in by_name:
                        out[n] = by_name[cand]
                        break
            return out


        def _endpoint_totals_view(pid: str) -> Dict:
            """The template-declared totals as the units actually carry them
            ({name: {op, from}}) and the union of the units' field names."""
            declared: Dict[str, Dict] = {}
            fields: Set[str] = set()
            for dev in config.endpoint_devices(pid):
                for entry in list((registry.store_for(dev.id) or {}).values()):
                    name = entry.get('name')
                    if not name:
                        continue
                    fields.add(name)
                    for out, op in (entry.get('aggregates') or {}).items():
                        d = declared.setdefault(str(out), {'op': op, 'from': []})
                        if name not in d['from']:
                            d['from'].append(name)
            return {'totals_declared': declared, 'unit_fields': sorted(fields)}


        def _validate_totals(t, errors: List[str]) -> None:
            if t in (None, {}):
                return
            if not isinstance(t, dict) or len(t) > 200:
                errors.append("totals: a mapping of name: {op, from} (at most 200)")
                return
            for name, d in t.items():
                w = f"totals.{name}"
                if not _INFLUX_NAME_RE.match(str(name)):
                    errors.append(f"{w}: names start with a letter (letters, digits, _ . -)")
                if not isinstance(d, dict):
                    errors.append(f"{w}: must be {{op, from}}")
                    continue
                if d.get('op') not in ('sum', 'avg', 'min', 'max', 'spread', 'mode'):
                    errors.append(f"{w}.op: sum, avg, min, max, spread or mode")
                src = d.get('from')
                if not isinstance(src, list) or not [x for x in src if str(x).strip()]:
                    errors.append(f"{w}.from: at least one unit field")


        def _template_update_summary(pid: str) -> Dict:
            from multibus.catalog.template_refresh import plan_devices
            try:
                plan = plan_devices(config, template_registry, config.endpoint_devices(pid))
            except Exception:  # noqa: BLE001 — a status view must never 500 on this
                return {'pending': False, 'changes': 0, 'new': 0, 'missing_templates': []}
            return {'pending': plan['pending'], 'changes': len(plan['changes']),
                    'new': len(plan['new']), 'units_affected': plan['units_affected'],
                    'missing_templates': plan['missing_templates']}


        def _endpoint_entry(p: Dict) -> Dict:
            pid = p.get('id')
            units = []
            now = time.time()
            for dev in config.endpoint_devices(pid):
                _i, _cfg, client = registry.find(dev.id)
                st, src_verdicts = {}, {}
                if client is not None:
                    try:
                        st = client.get_stats() or {}
                    except Exception:  # noqa: BLE001 — a stats blip must not 500 the list
                        st = {}
                    try:
                        src_verdicts = dict(client.data_health().get('sources') or {})
                    except Exception:  # noqa: BLE001
                        src_verdicts = {}
                _seen = st.get('last_success_ts')
                _groups = st.get('poll_groups_detail') or []
                units.append({
                    'unit_id': dev.connection.unit_id,
                    'device_id': dev.id,
                    'name': dev.name,
                    'enabled': dev.enabled,
                    'group_id': getattr(dev, 'group_id', '') or 'units',
                    'role': getattr(dev, 'role', '') or '',
                    'running': client is not None,
                    # the same liveness verdict the MQTT/alert paths use, so the
                    # endpoint census can never disagree with the unit's own topics
                    'connected': client_is_live(client),
                    'health': client_health(client),
                    'last_seen': (datetime.fromtimestamp(_seen).isoformat()
                                  if _seen else None),
                    'staleness_age_s': st.get('staleness_age_s'),
                    'poll_rate': st.get('poll_rate'),
                    'failed_reads': st.get('failed_reads'),
                    # what this unit asks of the wire each sweep, and what its
                    # slowest group actually took — the two halves of the budget
                    'reads_per_cycle': sum(int(g.get('reads') or 0) for g in _groups),
                    'cycle_s': (max((g.get('cycle_s') or 0) for g in _groups)
                                if _groups else None),
                    'overruns': sum(int(g.get('overruns') or 0) for g in _groups),
                    # what the unit shows at a glance, and each source's verdict
                    # for it — "read fine over HTTP, Modbus side dead" in one row
                    'live': _unit_live(dev, _glance_names(dev), now),
                    'alarms': _unit_alarms(dev),
                    'sources': src_verdicts,
                })
            from multibus.catalog.canonical_fields import field_meta
            from multibus.devices.endpoint_aggregator import compute_endpoint_aggregates
            grouped = bool(p.get('groups'))
            agg = {}
            if not grouped:
                # a flat endpoint IS one group, so its total is meaningful; a
                # grouped installation has no whole-installation sum — inverter
                # generation added to a meter's import describes nothing
                try:
                    agg = compute_endpoint_aggregates(config, registry, pid)
                except Exception:  # noqa: BLE001 — aggregates must never break the list
                    agg = {}
            groups = _endpoint_group_entries(p, pid, units)
            bus = _endpoint_bus(p, units)
            # the four numbers at the top: the site's balance when the datalogger
            # gives one, else the inverters' sum and nothing invented for the rest
            headline = {'power_now': None, 'energy_today': None,
                        'autonomy': None, 'self_consumption': None}
            site = next((u for u in units if u.get('role') == 'site'
                         and (u.get('live') or {}).get('power_pv') is not None), None)
            if site is not None:
                lv = site['live']
                headline.update({'power_now': lv.get('power_pv'),
                                 'energy_today': lv.get('energy_today'),
                                 'autonomy': lv.get('autonomy'),
                                 'self_consumption': lv.get('self_consumption')})
            else:
                # the inverters' fresh powers, summed — the same values their rows
                # show, so the top number and the rows can never disagree
                pw = [(u.get('live') or {}).get('power_active_total') for u in units
                      if u.get('role') == 'inverter']
                pw = [x for x in pw if isinstance(x, (int, float))]
                if pw:
                    headline['power_now'] = round(sum(pw), 3)
                elif not grouped and isinstance(agg.get('power_active_total'), (int, float)):
                    headline['power_now'] = agg['power_active_total']
            # every distinct way the installation is read, rolled up across groups
            read_via: Dict[tuple, Dict] = {}
            for g in groups:
                for sx in g.get('sources') or []:
                    key = (sx['id'], sx['protocol'], sx.get('address') or '')
                    r = read_via.setdefault(key, {
                        'id': sx['id'], 'protocol': sx['protocol'],
                        'address': sx.get('address') or '', 'interval_s': None,
                        'units_ok': 0, 'units_total': 0, 'reads_5m': 0, 'failed_5m': 0,
                        '_lat': [], 'groups': []})
                    r['groups'].append(g['id'])
                    r['units_ok'] += sx.get('units_ok') or 0
                    r['units_total'] += sx.get('units_total') or 0
                    r['reads_5m'] += sx.get('reads_5m') or 0
                    r['failed_5m'] += sx.get('failed_5m') or 0
                    if sx.get('latency_ms') is not None:
                        r['_lat'].append(sx['latency_ms'])
                    iv = sx.get('interval_s')
                    if iv is not None and (r['interval_s'] is None or iv < r['interval_s']):
                        r['interval_s'] = iv
            for r in read_via.values():
                lat = r.pop('_lat')
                r['latency_ms'] = round(sum(lat) / len(lat), 1) if lat else None
                r['fail_pct_5m'] = (round(100.0 * r['failed_5m'] / r['reads_5m'], 1)
                                    if r['reads_5m'] else None)
                r['status'] = ('down' if not r['units_ok'] else
                               'ok' if r['units_ok'] == r['units_total'] else 'degraded') \
                    if r['units_total'] else 'idle'
            live_names = {n for u in units for n in (u.get('live') or {})}
            _ep_smeta = _store_meta(config.endpoint_devices(pid))
            online = sum(1 for u in units if u['connected'])
            # inverters asleep at night are not missing (endpoint_aggregator)
            asleep = int(agg.get('units_asleep') or 0) + sum(int((g.get('aggregates') or {}).get('units_asleep') or 0)
                                                             for g in groups)
            return {'id': pid, 'name': p.get('name') or pid,
                    'template': p.get('template', ''),
                    'enabled': bool(p.get('enabled', True)),
                    'connection': dict(p.get('connection', {}) or {}),
                    'mqtt': dict(p.get('mqtt', {}) or {}),
                    'influxdb': dict(p.get('influxdb', {}) or {}),
                    'units': units,
                    'aggregates': agg,
                    # canonical label/unit/topic per aggregate name, so a view can
                    # render "Total active power · W" without re-deriving the
                    # vocabulary client-side
                    # (a one-unit installation's group has no meta: fall back, so the
                    # field keeps the shape it had before groups carried their own)
                    'aggregate_fields': ((groups[0].get('aggregate_fields') if (groups and not grouped) else None)
                                         or {k: m for k, m in ((k, field_meta(k)) for k in agg) if m}),
                    'aggregate_order': (groups[0].get('aggregate_order') or []) if (groups and not grouped) else [],
                    # how the totals are made, in the open: the operator's own
                    # (`totals`), what the units' templates declare, and every field
                    # the units carry — what a total can be built from
                    'totals': dict(p.get('totals') or {}),
                    **_endpoint_totals_view(pid),
                    # the endpoint's own settings, so the page can render + edit them
                    'aggregates_enabled': bool(p.get('aggregates', True)),
                    'write_locked': bool(p.get('write_locked', False)),
                    'http_output_enabled': bool((p.get('http_output') or {}).get('enabled')),
                    'rest_push': dict(p.get('rest_push', {}) or {}),
                    'bus': bus,
                    'sources': _endpoint_sources(p, pid),
                    # An installation is not one kind of thing: a plant holds
                    # inverters and the meter at its grid connection. Each group
                    # carries its own units, sources and total, because adding a
                    # meter's power to the inverters' would describe nothing.
                    'groups': groups,
                    'headline': headline,
                    'read_via': list(read_via.values()),
                    # label/unit per live field name, so a row can say "DC voltage
                    # 636 V" without re-deriving the vocabulary client-side
                    'fields': {n: m for n, m in ((n, _field_meta_any(n, _ep_smeta))
                                                 for n in live_names) if m},
                    # the template's headline for the installation, from its totals
                    # (the first group that declares one) — beside the legacy
                    # power/energy/autonomy headline the page falls back to
                    'headline_items': next((g['headline_items'] for g in groups
                                            if g.get('headline_items')), []),
                    # units whose own declared alarm fields are active right now
                    'units_alarming': sum(1 for u in units
                                          if (u.get('alarms') or {}).get('danger')
                                          or (u.get('alarms') or {}).get('warning')),
                    # the template has something to bring in (the page offers the
                    # update only then); details at /template-changes
                    'template_update': _template_update_summary(pid),
                    'status': ('' if not units else 'online' if online == len(units)
                               else 'asleep' if online == 0 and asleep == len(units)
                               else 'online' if online + asleep == len(units)
                               else 'offline' if online == 0 else 'partial'),
                    'online_units': online,
                    'asleep_units': asleep,
                    'total_units': len(units)}


        def _merge_unit_overrides(pid: str, units, prev: Dict = None):
            """Keep a unit's hand-written id/name when the caller sends a bare id.

            ``units: [1, 2, 3]`` is the common shape, and a client that re-sends it
            after an edit used to ERASE `{unit_id: 3, id: inv3, name: East roof}`
            from the config. An explicit dict from the caller always wins."""
            if not prev:
                return units
            overrides = {u['unit_id']: u for u in config._endpoint_units(prev)}
            out = []
            for u in (units or []):
                if isinstance(u, dict):
                    out.append(u)                      # explicit wins
                    continue
                po = overrides.get(u if isinstance(u, int) else None)
                if po and (po['id'] != f"{pid}-u{po['unit_id']}" or po['name']):
                    out.append({'unit_id': po['unit_id'], 'id': po['id'],
                                **({'name': po['name']} if po['name'] else {})})
                else:
                    out.append(u)                      # no override → stays bare
            return out


        _HTTP_URL_RE = re.compile(r'^https?://\S+$')


        def _http_url_error(url, stored, w: str):
            """An http URL that is not one: the Add group form sent a Modbus host
            as the URL. Judged only when the URL is new or changed — a stored one
            never blocks a save that does not touch it (an installation written
            before this check must still be renamed, or saved unchanged)."""
            url = str(url or '').strip()
            if not url or url == str(stored or '').strip() or _HTTP_URL_RE.match(url):
                return None
            return (f"{w}: '{url}' is not an http(s) URL — a Modbus host goes in host, "
                    f"with protocol tcp")


        def _validate_sources(srcs, errors, inherit_template: str, prefix: str = "", stored=None):
            """Validate one ordered source list. Shared by an endpoint's flat form
            and by every group, so a rule written once applies everywhere.
            `stored`: the same list as saved, for the checks that judge only what
            changed."""
            if srcs is None:
                return
            stored_by = {str(x.get('id', '')).strip().lower(): x for x in (stored or []) if isinstance(x, dict)}
            if not isinstance(srcs, list) or not srcs:
                errors.append(f"{prefix}sources: must be a non-empty list")
                return
            seen = set()
            _SID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,31}$')
            for i, r in enumerate(srcs):
                w = f"{prefix}sources[{i}]"
                if not isinstance(r, dict):
                    errors.append(f"{w}: must be an object")
                    continue
                sid = str(r.get('id', '') or '').strip().lower()
                if not _SID_RE.match(sid):
                    errors.append(f"{w}.id: use a-z 0-9 - _ (1-32 chars)")
                elif sid in seen:
                    # two sources with one name make provenance ambiguous, which
                    # defeats the point of recording it
                    errors.append(f"{w}.id: '{sid}' is declared twice")
                seen.add(sid)
                proto = str(r.get('protocol', 'tcp')).lower()
                if proto not in PROTOCOLS_BY_CONTEXT['source']:
                    errors.append(f"{w}.protocol: must be one of {', '.join(PROTOCOLS_BY_CONTEXT['source'])}")
                if proto == 'http':
                    if not str(r.get('url', '')).strip():
                        errors.append(f"{w}.url: required for an http source")
                    else:
                        e = _http_url_error(r.get('url'), (stored_by.get(sid) or {}).get('url'), f"{w}.url")
                        if e:
                            errors.append(e)
                elif proto == 'mqtt':
                    if not str(r.get('topic', '')).strip():
                        errors.append(f"{w}.topic: required for an mqtt source")
                elif proto not in SERIAL_PROTOCOLS and not str(r.get('host', '')).strip():
                    errors.append(f"{w}.host: required")
                tid = str(r.get('template', '') or inherit_template).strip()
                if not tid:
                    errors.append(f"{w}.template: required (nothing to inherit)")
                elif template_registry.get(tid) is None:
                    errors.append(f"{w}.template: '{tid}' not found")
                try:
                    if float(r.get('stale_after_s', 0) or 0) < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append(f"{w}.stale_after_s: must be >= 0 "
                                  f"(0 = never yields, right for a counter)")
                for gname, g in (r.get('poll_groups') or {}).items():
                    iv = g.get('interval') if isinstance(g, dict) else g
                    try:
                        if float(iv) < 0.05:
                            raise ValueError
                    except (TypeError, ValueError):
                        errors.append(f"{w}.poll_groups.{gname}: interval must be "
                                      f"a number >= 0.05")


        def _validate_group_connection(conn: Dict, errors: List[str], w: str, stored=None) -> None:
            """A group's own connection, checked like the installation's. A serial
            device path is never a network host: that mistake made a tap open a
            TCP connection to "/dev/ttySEPLOS:502" and the batteries went dark."""
            protocol = str(conn.get('protocol', 'tcp')).lower()
            allowed = PROTOCOLS_BY_CONTEXT['group']
            if protocol not in allowed:
                errors.append(f"{w}.connection.protocol: must be one of " + ", ".join(allowed))
            host = str(conn.get('host', '') or '').strip()
            if _is_serial_path(host):
                errors.append(_serial_host_error(f"{w}.connection.host", host))
            if protocol == 'http':
                if not str(conn.get('url', '')).strip():
                    errors.append(f"{w}.connection.url: required for http")
                else:
                    e = _http_url_error(conn.get('url'), (stored or {}).get('url'), f"{w}.connection.url")
                    if e:
                        errors.append(e)
            elif protocol == 'rtu_tap':
                if not str(conn.get('serial_port', '')).strip() and not host:
                    errors.append(f"{w}.connection: rtu_tap needs a serial_port (or a bridge host)")
            elif not host:
                errors.append(f"{w}.connection.host: required")


        def _validate_endpoint_payload(payload: Dict, *, existing_id: str = None) -> Dict:
            """Normalize + validate a raw endpoint dict. Raises HTTPException(422)
            with a per-field error list."""
            errors = []
            pid = str(payload.get('id', existing_id or '')).strip().lower()
            if not _DEVICE_ID_RE.match(pid):
                errors.append("id: use a-z 0-9 - _ (2-64 chars, starts alphanumeric)")
            if existing_id and pid != existing_id:
                errors.append("id: cannot be changed after creation")
            if not existing_id and (config.get_raw_endpoint(pid) is not None
                                    or registry.has(pid)):
                errors.append(f"id: '{pid}' already exists")
            prev = config.get_raw_endpoint(existing_id) if existing_id else None
            if prev is not None:
                # An edit REPLACES the stored entry. A form that does not speak of
                # groups or sources — the settings dialog of an installation — keeps
                # the stored ones: taken literally, that same form flattened a plant
                # to one Modbus group, dropped every source and renamed its site
                # unit, on a save meant to change the name.
                if 'groups' not in payload and prev.get('groups'):
                    groups = [dict(g) for g in prev['groups']]
                    # a rename sent the flat way — `units: [{unit_id, id, name}]` —
                    # lands on the unit inside its group; the rest of the flat list
                    # is discarded, because membership is the groups' to say
                    named = {u['unit_id']: u for u in (payload.get('units') or [])
                             if isinstance(u, dict) and 'unit_id' in u and 'name' in u}
                    if named:
                        for g in groups:
                            gu = []
                            for u in config._endpoint_units({'id': pid}, g):
                                hit = named.get(u['unit_id'])
                                item = {'unit_id': u['unit_id'], 'id': u['id']}
                                name = hit['name'] if hit else u.get('name')
                                if name:
                                    item['name'] = name
                                gu.append(item)
                            g['units'] = gu
                    payload = {**payload, 'groups': groups}
                    payload.pop('units', None)          # units live in the groups
                if 'sources' not in payload and prev.get('sources'):
                    payload = {**payload, 'sources': prev['sources']}
                # same rule for FLAT units: a settings dialog that does not list
                # them must not dissolve the endpoint's membership
                if 'units' not in payload and prev.get('units'):
                    payload = {**payload, 'units': prev['units']}
            conn = payload.get('connection', {}) or {}
            # A form that does not SPEAK of the connection keeps the stored one —
            # the settings dialog saved with an empty conn and wiped the serial
            # port out from under 8 running units (seplos cutover, 2026-10-07).
            if prev is not None and not conn and prev.get('connection'):
                conn = dict(prev['connection'])
            # An endpoint that declares GROUPS carries its address per group — each
            # holds a different kind of thing and may be reached a different way. The
            # top-level connection is then only the shorthand for a single implicit
            # group, so it is required exactly when no group says otherwise.
            _grouped = bool(payload.get('groups'))
            if not _grouped:
                protocol = str(conn.get('protocol', 'tcp')).lower()
                if protocol not in PROTOCOLS_BY_CONTEXT['endpoint']:
                    # a serial line here is read by one device per slave (Devices);
                    # an installation reaches its units through a network master
                    # or listens (rtu_tap: it masters nothing, and a multi-drop
                    # bus is exactly what it is for)
                    errors.append("connection.protocol: must be one of " + ", ".join(PROTOCOLS_BY_CONTEXT['endpoint']))
                if protocol == 'rtu_tap':
                    if not str(conn.get('serial_port', '')).strip():
                        errors.append("connection.serial_port: required for rtu_tap")
                elif not str(conn.get('host', '')).strip():
                    errors.append("connection.host: required")
                if str(conn.get('host', '') or '').strip().startswith('/'):
                    errors.append(f"connection.host: '{conn.get('host')}' is a serial port, not a "
                                  f"network address — it goes in serial_port")
            if conn:
                try:
                    port = int(conn.get('port', 502))
                    if not (1 <= port <= 65535):
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append("connection.port: must be 1..65535")
            if conn.get('max_connections') is not None:
                # Sockets to a master device are a scarce, shared resource — the
                # datalogger this replaces serves ten and starts refusing near five.
                # A typo asking for thirty would take the access point down for
                # everything else on it, including our own other units.
                try:
                    lanes = int(conn['max_connections'])
                    if not (1 <= lanes <= 8):
                        raise ValueError
                    conn['max_connections'] = lanes
                except (TypeError, ValueError):
                    errors.append("connection.max_connections: must be 1..8 "
                                  "(measure it: scripts/calibrate_endpoint.py)")
            template_id = str(payload.get('template', '')).strip()
            if template_id and template_registry.get(template_id) is None:
                errors.append(f"template: '{template_id}' not found")
            # a flat installation's role: a type from the catalog, judged when it
            # changes (ADR-0027, ADR-0006); a hand-written one stored is kept
            from multibus.catalog.device_types import role_error as _flat_role_error
            _frerr = _flat_role_error(payload.get('role'), (prev or {}).get('role'))
            if _frerr:
                errors.append(f"role: {_frerr}")
            # ── groups: the kinds of thing this installation holds ──
            groups = payload.get('groups')
            if groups is not None:
                if not isinstance(groups, list) or not groups:
                    errors.append("groups: must be a non-empty list")
                    groups = []
                seen_gid, seen_uid_all = set(), set()
                _GID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,31}$')
                stored_groups = {str(x.get('id', '')).strip().lower(): x
                                 for x in ((prev or {}).get('groups') or []) if isinstance(x, dict)}
                stored_template = str((prev or {}).get('template', '') or '').strip()
                for gi, g in enumerate(groups):
                    w = f"groups[{gi}]"
                    if not isinstance(g, dict):
                        errors.append(f"{w}: must be an object")
                        continue
                    gid = str(g.get('id', '') or '').strip().lower()
                    if not _GID_RE.match(gid):
                        errors.append(f"{w}.id: use a-z 0-9 - _ (1-32 chars)")
                    elif gid in seen_gid:
                        errors.append(f"{w}.id: '{gid}' is declared twice")
                    seen_gid.add(gid)
                    gunits = config._endpoint_units({'id': pid}, g)
                    if not gunits:
                        errors.append(f"{w}.units: at least one valid unit id "
                                      f"(0..255) is required")
                    for u in gunits:
                        # a unit id is the address on the wire: the same one in two
                        # groups of one endpoint would be two devices racing it
                        if u['unit_id'] in seen_uid_all:
                            errors.append(f"{w}.units: unit {u['unit_id']} is "
                                          f"already used by another group")
                        seen_uid_all.add(u['unit_id'])
                    gt = str(g.get('template', '') or template_id).strip()
                    if gt and template_registry.get(gt) is None:
                        errors.append(f"{w}.template: '{gt}' not found")
                    sg = stored_groups.get(gid)
                    # its role: a type from the catalog, judged when it changes (ADR-0027)
                    from multibus.catalog.device_types import role_error as _role_error
                    _rerr = _role_error(g.get('role'), (sg or {}).get('role'))
                    if _rerr:
                        errors.append(f"{w}.role: {_rerr}")
                    # read through a connection (its own or the installation's)
                    # with no map anywhere: a device with nothing to read. Judged
                    # on a new group, or when its way in or its map changes
                    if not gt and (g.get('connection') or (not g.get('sources') and not payload.get('sources'))) \
                            and (sg is None or g.get('connection') != sg.get('connection')
                                 or str(g.get('template', '') or '') != str(sg.get('template', '') or '')
                                 or template_id != stored_template):
                        errors.append(f"{w}.template: required — the group's or the installation's")
                    if g.get('connection'):
                        _validate_group_connection(dict(g['connection']), errors, w,
                                                   stored=(sg or {}).get('connection'))
                    elif not g.get('sources') and not conn and not payload.get('sources'):
                        errors.append(f"{w}: no way to reach its units — give the group a "
                                      f"connection or sources, or the installation one")
                    _validate_sources(g.get('sources'), errors, gt, prefix=w + '.',
                                      stored=(sg or {}).get('sources'))
                    # the commands' template: the one a command writes through —
                    # a Modbus source's, else an MQTT one's, else the group's (as
                    # _modbus_template_of); a group read through sources has no
                    # template of its own and its `safe` went unchecked (CR2-5)
                    _ctid = next((str(s.get('template')) for proto in (_POLLED_MODBUS, ('mqtt',))
                                  for s in (g.get('sources') or payload.get('sources') or [])
                                  if isinstance(s, dict) and s.get('template')
                                  and str(s.get('protocol') or 'tcp').lower() in proto), '') or gt
                    _ctpl = template_registry.get(_ctid) if _ctid else None
                    validate_commands(g.get('commands'), errors, prefix=w + '.', tpl=_ctpl,
                                      stored=(sg or {}).get('commands'))
                    validate_channels(g.get('channels'), errors, prefix=w + '.', tpl=_ctpl,
                                      stored=(sg or {}).get('channels'),
                                      units=[u['id'] for u in (gunits or []) if isinstance(u, dict) and u.get('id')],
                                      rules_runtime=getattr(app.state, 'rules_runtime', None))
            # ── sources: the ordered ways of reaching this endpoint's units ──
            srcs = payload.get('sources')
            _validate_sources(srcs, errors, template_id, stored=(prev or {}).get('sources'))
            validate_commands(payload.get('commands'), errors,
                              tpl=template_registry.get(template_id) if template_id else None,
                              stored=(prev or {}).get('commands'))
            # an installation without groups: its units' channel overrides live on
            # it, as their bindings do (CR2-4)
            validate_channels(payload.get('channels'), errors,
                              tpl=template_registry.get(template_id) if template_id else None,
                              stored=(prev or {}).get('channels'),
                              units=[u['id'] for u in config._endpoint_units({'id': pid, 'units': payload.get('units')})
                                     if isinstance(u, dict) and u.get('id')],
                              rules_runtime=getattr(app.state, 'rules_runtime', None))
            probe = {'id': pid, 'units': payload.get('units')}
            units = config._endpoint_units(probe)
            if not units and not _grouped:
                errors.append("units: at least one valid unit id (0..255) is required")
            seen_uids, seen_ids = set(), set()
            for u in units:
                if u['unit_id'] in seen_uids:
                    errors.append(f"units: duplicate unit_id {u['unit_id']}")
                seen_uids.add(u['unit_id'])
                if not _DEVICE_ID_RE.match(u['id']):
                    errors.append(f"units: id '{u['id']}' is invalid")
                if u['id'] in seen_ids:
                    errors.append(f"units: duplicate device id '{u['id']}'")
                seen_ids.add(u['id'])
                ex = config.get_device(u['id'])
                if ex is not None and ex.endpoint_id != pid:
                    errors.append(f"units: id '{u['id']}' collides with an "
                                  f"existing device")
            # every unit is a device on the topics, flat or grouped: a new one never under a
            # Virtual BMS id (R2D-NAMESPACE-1); the units it already runs are not judged again
            from multibus.outputs.vbms_registry import device_namespace_ids, vbms_id_error
            _new_units = (device_namespace_ids({'endpoints': [{**payload, 'id': pid}]})
                          - {d.id for d in config.endpoint_devices(pid)})
            errors += vbms_id_error(_new_units, getattr(config, 'virtual_bms', None), 'units')
            if errors:
                raise HTTPException(status_code=422, detail={"errors": errors})
            raw = {
                'id': pid,
                'name': str(payload.get('name', '') or pid),
                'template': template_id,
                'enabled': flag(payload, 'enabled', True),
                'connection': conn,
                'units': _merge_unit_overrides(pid, payload.get('units'), prev),
                **({'sources': srcs} if srcs else {}),
                **({'groups': groups} if groups else {}),
                **({'commands': payload['commands']} if payload.get('commands') else {}),
                **({'channels': payload['channels']} if payload.get('channels') else {}),
                # a flat installation's role: a catalog type when it changes (ADR-0027)
                **({'role': payload['role']} if isinstance(payload.get('role'), str) and payload['role'] else {}),
            }
            if payload.get('aggregates') is not None:
                raw['aggregates'] = flag(payload, 'aggregates')
            for opt in ('mqtt', 'influxdb'):
                if payload.get(opt):
                    raw[opt] = dict(payload[opt])
            if 'opcua' in payload:
                raw['opcua'] = _opcua_block(payload['opcua'])
            if 'totals' in payload:
                _terr: List[str] = []
                _validate_totals(payload['totals'], _terr)
                if _terr:
                    raise HTTPException(status_code=422, detail={"errors": _terr})
                if payload['totals']:
                    raw['totals'] = {str(k): {'op': v['op'],
                                              'from': [str(x) for x in v['from'] if str(x).strip()]}
                                     for k, v in payload['totals'].items()}
            if prev is not None:
                # An edit REPLACES the stored entry, so anything the endpoint owns but
                # the form does not send would vanish: a locked endpoint silently
                # unlocked itself and `aggregates: false` came back on, one save
                # after the operator set them.
                for key in ('write_locked', 'aggregates', 'http_output', 'rest_push', 'opcua', 'role'):
                    if key not in raw and key in prev:
                        raw[key] = prev[key]
                # What a group owns but the group form does not edit — its own
                # routing (a meter on `pv/meter/grid`, not `pv/units/240`) and its
                # OPC UA choice (a unit's Outputs tab) — is kept when the form
                # rebuilds the group without it. Dropping the routing moved a
                # group's topics and series on an unrelated save.
                _prev_groups = {str(g.get('id')): g for g in (prev.get('groups') or []) if isinstance(g, dict)}
                for g in raw.get('groups') or []:
                    pg = _prev_groups.get(str(g.get('id'))) if isinstance(g, dict) else None
                    for key in GROUP_KEPT_KEYS if pg else ():
                        if key in pg and key not in g:
                            g[key] = dict(pg[key])
                # totals: an empty mapping sent on purpose clears them; a form that
                # does not send the key keeps them
                if 'totals' not in payload and prev.get('totals'):
                    raw['totals'] = prev['totals']
                # a form that omits a whole sink block keeps the stored one — the
                # pin below would otherwise rebuild it from the routing keys ALONE
                # and drop ha_discovery/enabled with it
                for sect in ('mqtt', 'influxdb'):
                    if sect not in raw and prev.get(sect):
                        raw[sect] = dict(prev[sect])
                # Routing identity is FIXED after creation, exactly as for a device
                # (see update_device): changing it re-routes every unit's future
                # data and orphans their history + Home Assistant entities.
                for sect, keys in (('mqtt', ('topic_prefix', 'aggregate_prefix')),
                                   ('influxdb', ('bucket', 'device_tag'))):
                    for k in keys:
                        old = (prev.get(sect) or {}).get(k)
                        if old is not None:
                            raw.setdefault(sect, {})[k] = old
                # the units' Home Assistant node: as stored, or none (ADR-0026 §3)
                _node = (prev.get('mqtt') or {}).get('ha_node')
                if _node:
                    raw.setdefault('mqtt', {})['ha_node'] = _node
                elif isinstance(raw.get('mqtt'), dict):
                    raw['mqtt'].pop('ha_node', None)
                # Static tags and extra outputs are the operator's to edit, from
                # the endpoint page — but a form that does not speak of them (the
                # settings dialog) must not wipe them.
                for k in ('tags', 'outputs'):
                    old = (prev.get('influxdb') or {}).get(k)
                    if old is not None and k not in (raw.get('influxdb') or {}):
                        raw.setdefault('influxdb', {})[k] = old
            errors = influx_block_errors(raw.get('influxdb') or {}, 'influxdb', outputs=True)
            if errors:
                raise HTTPException(status_code=422, detail={"errors": errors})
            _agg = str((raw.get('mqtt') or {}).get('aggregate_prefix', '') or '')
            if _agg and _agg != str(((prev or {}).get('mqtt') or {}).get('aggregate_prefix', '') or ''):
                _agg = _agg.strip().strip('/')            # as endpoint_aggregator reads it
                # an aggregate root being set (ADR-0026 §4); its own units may sit under it
                from multibus.devices.topic_prefix import aggregate_roots, conflicts, claims, shape_error
                _shape = shape_error(_agg.replace('${endpoint_id}', pid).replace('${group_id}', 'g')
                                     .replace('${id}', pid))
                _errs = [_shape] if _shape else [
                    c for root in aggregate_roots(raw) for c in conflicts(root, claims(config), {f"installation:{pid}"})]
                if _errs:
                    raise HTTPException(status_code=422, detail={
                        "errors": [f"mqtt.aggregate_prefix: {e}" for e in _errs],
                        "fields": {"mqtt.aggregate_prefix": _errs[0]}})
            _endpoint_claim_check(raw, pid, prev)
            return raw


        def _stop_endpoint_devices(pid: str) -> None:
            """Disconnect + deregister every materialized device of an endpoint."""
            for dev in config.endpoint_devices(pid):
                _i, _cfg, client = registry.find(dev.id)
                _teardown_device(dev.id, dev, client)
                registry.remove(dev.id)


        def _start_endpoint_devices(made) -> List[Dict]:
            """Seed + bucket + start + register each materialized device."""
            out = []
            for dev_cfg in made:
                _autoselect_template_registers(dev_cfg)
                _ensure_device_bucket(dev_cfg)
                client = _start_device_client(dev_cfg)
                registry.add(dev_cfg, client)
                out.append(_device_entry(dev_cfg, client))
            return out


        def _endpoint_runtime_sig(raw: Dict) -> str:
            """Everything a materialized unit is BUILT from. Two definitions with
            the same signature produce identical clients, so an edit that leaves it
            untouched — a rename, the endpoint-totals toggle — must not tear the
            pollers down: a cosmetic save should never punch a hole in acquisition.
            Unit NAMES are deliberately absent (cosmetic; refreshed in place)."""
            return json.dumps({
                'connection': raw.get('connection') or {},
                'template': raw.get('template', ''),
                'enabled': bool(raw.get('enabled', True)),
                'units': [(u['unit_id'], u['id']) for u in config._endpoint_units(raw)],
                'mqtt': raw.get('mqtt') or {},
                # extra outputs are the aggregator's, read live each cycle — an
                # edit to them must not restart a single unit
                'influxdb': {k: v for k, v in (raw.get('influxdb') or {}).items()
                             if k != 'outputs'},
                'write_locked': bool(raw.get('write_locked', False)),
                'http_output': raw.get('http_output') or {},
                'rest_push': raw.get('rest_push') or {},
            }, sort_keys=True, default=str)


        _ENDPOINT_OWN_KEYS = ('write_locked', 'http_output', 'rest_push', 'opcua')


        def _drop_endpoint_echoes(pid: str, payload: Dict, prev: Dict) -> Dict:
            """What a page sends back only because it was shown — `enabled: true`
            on a group or source that never said it, a unit's derived name, the
            flat `units` beside groups — is not written. Written, a
            derived name stopped following the installation's name (P11), and an
            untouched installation read as changed. Matched by id with what is
            stored; anything else in the payload is the caller's."""
            if not isinstance(payload.get('groups'), list) or not payload['groups'] or not prev.get('groups'):
                return payload
            payload = copy.deepcopy(payload)
            groups = payload['groups']
            if 'units' in payload:                    # with groups the flat list builds nothing
                if 'units' in prev:
                    payload['units'] = copy.deepcopy(prev['units'])
                else:
                    payload.pop('units')
            derived = {f"{n} unit {{}}" for n in (payload.get('name'), prev.get('name'), pid) if n}
            by_id = lambda xs: {str(x.get('id')): x for x in xs or [] if isinstance(x, dict)}  # noqa: E731
            stored = by_id(prev.get('groups'))
            for g in groups:
                pg = stored.get(str(g.get('id'))) if isinstance(g, dict) else None
                if pg is None:
                    continue
                if g.get('enabled') is True and 'enabled' not in pg:
                    g.pop('enabled')
                old_src = by_id(pg.get('sources'))
                for src in g.get('sources') or []:
                    ps = old_src.get(str(src.get('id'))) if isinstance(src, dict) else None
                    if ps is not None and src.get('enabled') is True and 'enabled' not in ps:
                        src.pop('enabled')
                old_units = {}
                for u in pg.get('units') or []:
                    uid = u.get('unit_id') if isinstance(u, dict) else u
                    old_units[uid] = u
                units = []
                for u in g.get('units') or []:
                    pu = old_units.get(u.get('unit_id')) if isinstance(u, dict) else None
                    if (pu is not None and not (isinstance(pu, dict) and pu.get('name'))
                            and any(u.get('name') == d.format(u['unit_id']) for d in derived)
                            and u.get('id') in (None, f"{pid}-u{u['unit_id']}",
                                                pu.get('id') if isinstance(pu, dict) else None)):
                        u = copy.deepcopy(pu)
                    units.append(u)
                if 'units' in g:
                    g['units'] = units
            return payload


        def _endpoint_put_raw(endpoint_id: str, payload: Dict, prev: Dict) -> Dict:
            """What saving ``payload`` over the stored installation writes: every
            route merged with the stored one (save_merge.py), validated, with what
            the payload did not speak of kept."""
            payload = _drop_endpoint_echoes(endpoint_id, dict(payload), prev)
            for k in ('name', 'template', 'enabled'):
                if k not in payload and k in prev:
                    payload[k] = prev[k]
            payload = merge_endpoint_routes(payload, prev)
            raw = _validate_endpoint_payload(payload, existing_id=endpoint_id)
            return keep_unsaid(raw, prev, payload, own=_ENDPOINT_OWN_KEYS)


        def _refuse_removed_units(endpoint_id: str, raw: Dict) -> List[Dict]:
            """An edit that takes units or groups away (fewer units, another
            address, a renamed group) is a delete of those units: refused (409)
            while anything names them, or a rule commands a group that goes.
            A rule that commands the group a unit leaves (dropped, or moved to
            another group) keeps working with the units that remain: said in
            the answer's ``warnings``, not refused. Disabling a group or the
            installation is neither — its units stay."""
            from multibus.lifecycle.dependents import Unreadable, summary, units_dependents
            before = {d.id: (d.group_id or 'units') for d in config.endpoint_devices(endpoint_id)}
            after = {d.id: (d.group_id or 'units') for d in config.preview_devices(endpoint=raw)}
            gone = set(before) - set(after)
            gone_groups = set(before.values()) - set(after.values())
            moved = {u: (g, after[u]) for u, g in before.items() if u in after and after[u] != g}
            if not gone and not gone_groups and not moved:
                return []
            try:
                res = units_dependents(_dep_sources(), endpoint_id, gone, gone_groups, moved)
            except Unreadable as e:
                raise HTTPException(status_code=503, detail={"errors": [
                    f"what uses the units this edit removes cannot be told: {e} — fix that first"]})
            if res['blocking']:
                deps = {"blocking": res['blocking'], "follows": [], "warnings": res['warnings']}
                groups = [str(g.get('id') or '') for g in (raw.get('groups') or []) if isinstance(g, dict)]
                msg = summary(deps, f"what this edit removes ({', '.join(sorted(gone | gone_groups))})")
                fields: Dict[str, str] = {}
                for u, g in before.items():
                    if u in gone or g in gone_groups:
                        fields[f"groups[{groups.index(g)}].units" if g in groups else "groups"] = msg
                raise HTTPException(status_code=409, detail={"errors": [msg], "fields": fields, "dependents": deps})
            return res['warnings']


        def _unit_device_id(endpoint_id: str, unit: str) -> Optional[str]:
            """The device id of an installation's unit, named by its id or by its
            unit address; None when it has no such unit."""
            u = str(unit or '').strip().lower()
            for d in config.endpoint_devices(endpoint_id):
                if d.id == u:
                    return d.id
            for d in config.endpoint_devices(endpoint_id):
                if u.isdigit() and int(d.connection.unit_id) == int(u):
                    return d.id
            return None


        def _update_endpoint_locked(endpoint_id: str, payload: Dict):
            prev_raw = config.get_raw_endpoint(endpoint_id)
            if prev_raw is None:
                raise HTTPException(status_code=404, detail="endpoint not found")
            prev_raw = copy.deepcopy(prev_raw)
            raw = _endpoint_put_raw(endpoint_id, payload, prev_raw)
            # an unchanged save changes nothing (the stored installation through
            # the same pipeline is what this save would write)
            try:
                _same = raw == _endpoint_put_raw(endpoint_id, copy.deepcopy(prev_raw), prev_raw)
            except Exception as e:  # noqa: BLE001 — the comparison never fails a save
                logger.debug("endpoint %s: unchanged-check skipped (%s)", endpoint_id, e)
                _same = False
            if _same:
                return {"status": "unchanged", "endpoint": _endpoint_entry(raw),
                        "devices": [_device_entry(d, registry.find(d.id)[2])
                                    for d in config.endpoint_devices(endpoint_id)]}
            warnings = _refuse_removed_units(endpoint_id, raw)
            # restart only the units whose RUNNING shape changed: compared unit by
            # unit on what each builds (groups and sources included — a source moved
            # to another port must reach its pollers), not on the raw text. A new
            # interval on the site meter's source restarts the meter, never the
            # four inverters beside it (their sockets are shared and ref-counted).
            restart = _units_to_restart(prev_raw, raw)
            old_ids = {d.id for d in config.endpoint_devices(endpoint_id)}
            if restart is None:                       # cannot be told unit by unit
                restart = set() if _endpoint_runtime_sig(prev_raw) == _endpoint_runtime_sig(raw) \
                    else old_ids | {d.id for d in config.preview_devices(endpoint=raw)}
            for dev in config.endpoint_devices(endpoint_id):
                if dev.id in restart:
                    _i, _cfg, client = registry.find(dev.id)
                    _teardown_device(dev.id, dev, client)
                    registry.remove(dev.id)
            try:
                made = config.upsert_raw_endpoint(raw)
            except ValueError as e:
                # config restored the previous definition — restart the units this
                # save stopped, so a failed edit doesn't leave them off
                _start_endpoint_devices([d for d in config.endpoint_devices(endpoint_id) if d.id in restart])
                raise HTTPException(status_code=422, detail={"errors": [str(e)]})
            devices_out = []
            for dev_cfg in made:
                if dev_cfg.id in restart:
                    devices_out += _start_endpoint_devices([dev_cfg])
                else:
                    # rebuilt by upsert; swapped in behind the RUNNING client so a
                    # rename shows up without a restart
                    registry.replace(dev_cfg.id, dev_cfg)
                    devices_out.append(_device_entry(dev_cfg, registry.find(dev_cfg.id)[2]))
            _sync_device_discovery()
            restarted = sorted(restart & {d.id for d in made})
            logger.info("endpoint %s: updated (%d unit(s))%s", endpoint_id, len(made),
                        f" — restarted {', '.join(restarted)}; the others kept running" if restarted
                        else " — settings only, pollers kept running")
            out = {"status": "updated", "endpoint": _endpoint_entry(raw), "devices": devices_out,
                   "restarted": restarted}
            if warnings:
                out["warnings"] = warnings
            return out


        def _units_to_restart(prev_raw: Dict, raw: Dict) -> Optional[set]:
            """The ids of the units whose running shape differs between two
            definitions of an installation (added and removed ones included), or
            None when a unit's shape cannot be told."""
            def _sigs(r):
                return {d.id: _built_sig([d]) for d in config.preview_devices(endpoint=r)}
            a, b = _sigs(prev_raw), _sigs(raw)
            if None in a.values() or None in b.values():
                return None
            return {uid for uid in a.keys() | b.keys() if a.get(uid) != b.get(uid)}


        @_serialized_mutation
        def _delete_endpoint_locked(endpoint_id: str):
            if config.get_raw_endpoint(endpoint_id) is None:
                raise HTTPException(status_code=404, detail="endpoint not found")
            _refuse_if_used('installation', endpoint_id, f"installation '{endpoint_id}'", False)
            _topics = set()
            for d in config.endpoint_devices(endpoint_id):
                _cancel_device_refreshes(d.id)
                _topics |= _device_value_topics(d)
            _stop_endpoint_devices(endpoint_id)
            _clear_device_values(_topics)
            try:
                removed = config.delete_endpoint(endpoint_id)
            except ValueError as e:
                raise HTTPException(status_code=404, detail=str(e))
            _sync_device_discovery()
            _publish_attribute_meta()      # the installation's meta is no longer published: cleared
            logger.info(f"endpoint {endpoint_id}: deleted ({len(removed)} unit(s))")
            return {"status": "deleted", "removed_devices": removed}



        self._endpoint_sources = _endpoint_sources
        self._endpoint_bus = _endpoint_bus
        self._display_of = _display_of
        self._unit_alarms = _unit_alarms
        self._glance_names = _glance_names
        self._store_meta = _store_meta
        self._field_meta_any = _field_meta_any
        self._aggregate_meta = _aggregate_meta
        self._headline_items = _headline_items
        self._endpoint_group_entries = _endpoint_group_entries
        self._unit_live = _unit_live
        self._endpoint_totals_view = _endpoint_totals_view
        self._validate_totals = _validate_totals
        self._template_update_summary = _template_update_summary
        self._endpoint_entry = _endpoint_entry
        self._merge_unit_overrides = _merge_unit_overrides
        self._http_url_error = _http_url_error
        self._validate_sources = _validate_sources
        self._validate_group_connection = _validate_group_connection
        self._validate_endpoint_payload = _validate_endpoint_payload
        self._stop_endpoint_devices = _stop_endpoint_devices
        self._start_endpoint_devices = _start_endpoint_devices
        self._endpoint_runtime_sig = _endpoint_runtime_sig
        self._drop_endpoint_echoes = _drop_endpoint_echoes
        self._endpoint_put_raw = _endpoint_put_raw
        self._refuse_removed_units = _refuse_removed_units
        self._unit_device_id = _unit_device_id
        self._update_endpoint_locked = _update_endpoint_locked
        self._units_to_restart = _units_to_restart
        self._delete_endpoint_locked = _delete_endpoint_locked
