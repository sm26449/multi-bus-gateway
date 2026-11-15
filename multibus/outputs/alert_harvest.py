# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Event/alert harvester, extracted whole from create_api (P0.8b squeeze).

EventHarvester holds the harvest loop (run): it folds new subsystem events into the
persisted log every ~5s and fires alerts off them (source read failures, per-device
up/down with a down-grace, MQTT/InfluxDB connect transitions, sustained latency,
InfluxDB backlog, virtual-meter last errors, template alarms). Kept as a closure built
in __init__ so the body is verbatim; collaborators are an explicit keyword constructor.
The _display_of / _unit_alarms endpoint facades arrive as late-bound thunks (they are
bound after the harvester is built, as in the original closure).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Dict, Optional, Set

from multibus import __version__

logger = logging.getLogger("multibus.api")  # preserve caplog logger name

_clock = time.monotonic        # the virtual-meter alert clock (a patch point in tests)


class EventHarvester:
    """The event/alert harvester (ADR-0038 service)."""

    def __init__(self, *, config, app, alert_mgr, event_log, registry, gw_role, threshold_engine, modbus_client, _pub, harvester_stop, client_is_live, _display_of, _unit_alarms):
        def _harvest_events():
            """Fold new subsystem events into the persisted log (~every 5s) and fire
            alerts off them: source read failures, per-device up/down, MQTT/InfluxDB
            connect transitions, sustained high latency, InfluxDB buffer backlog and
            virtual-meter last errors. Deduped so an event/alert is emitted once."""
            seen: Set = set()
            prev: Dict[str, Optional[bool]] = {}
            drop_prev = {"schema": None, "buffer": None}
            last_err: Dict[str, str] = {"msg": ""}

            def note(src, evs):
                for e in (evs or []):
                    k = (src, round(e.get('ts') or 0, 1), e.get('message') or e.get('kind') or '')
                    if k in seen:
                        continue
                    seen.add(k)
                    event_log.add(e.get('level', 'warn'), src,
                                  e.get('message') or e.get('kind') or 'event',
                                  e.get('kind', ''), e.get('ts'))

            # A device's "down" alert waits DOWN_GRACE_S: a datalogger that stalls
            # for a few seconds (the Fronius Datamanager, also polled by the
            # Cerbo, does so several times a day) flips a single-source unit
            # down and back within one or two harvests, and an error alert for
            # every such blink is noise. The transition is still logged at once;
            # the alert fires only if the device is still down after the grace,
            # and the recovery alert only if a down alert was sent.
            DOWN_GRACE_S = 45.0
            down_since: Dict[str, float] = {}
            alerted_down: Set[str] = set()
            # template alarms (display.alarms) → alerts; see alarm_alerts.py
            from multibus.outputs.alarm_alerts import AlarmTracker
            alarm_tracker = AlarmTracker(lambda *a, **k: alert_mgr.fire(*a, **k))
            alarm_seen: Set[str] = set()
            alarm_evaluated: Set[str] = set()
            # virtual meters: not serving / held / consumer lost (VM-ALERT-1)
            from multibus.outputs.vmeter_alerts import VMeterAlertTracker
            vm_tracker = VMeterAlertTracker(lambda *a, **k: alert_mgr.fire(*a, **k))

            def _alarm_transitions(dev_cfg, name, seen):
                try:
                    decl = [a for a in (_display_of(dev_cfg).get('alarms') or [])
                            if isinstance(a, dict) and a.get('field')]
                    if decl:
                        from multibus.catalog.display import is_fresh
                        entries = list((registry.store_for(dev_cfg.id) or {}).values())
                        known = {e.get('name') for e in entries if e.get('name') and is_fresh(e)}
                        alarm_tracker.update(dev_cfg.id, name, decl, _unit_alarms(dev_cfg)['active'], seen,
                                             known=known)
                        alarm_evaluated.add(dev_cfg.id)
                except Exception:  # noqa: BLE001 — an alarm check must not stop the harvester
                    logger.debug("alarm check for %s failed", dev_cfg.id, exc_info=True)

            def transition(pk, src, connected, signal):
                now = time.time()
                if prev.get(pk) is not None and connected != prev[pk]:
                    event_log.add('info' if connected else 'error', src,
                                  'connected' if connected else 'disconnected', 'transition')
                    if not connected:
                        down_since[pk] = now
                    else:
                        down_since.pop(pk, None)
                fire = (signal == 'device' and alert_mgr.sig_device) or \
                       (signal == 'sink' and alert_mgr.sig_sink)
                if fire:
                    # device_id for alertd per-device routing/rules (ADR-0043 C)
                    _ctx = {'device_id': pk[4:]} if signal == 'device' and pk.startswith('dev:') else None
                    if (not connected and pk in down_since and pk not in alerted_down
                            and now - down_since[pk] >= DOWN_GRACE_S):
                        alerted_down.add(pk)
                        alert_mgr.fire('error', f'{pk}:down', src, 'down — not responding', context=_ctx)
                    elif connected and pk in alerted_down:
                        alerted_down.discard(pk)
                        alert_mgr.fire('info', f'{pk}:up', src, 'recovered — connected', context=_ctx)
                prev[pk] = connected

            def _vmeter_alerts(mgr):
                """Fire/clear the virtual-meter conditions. A standby serves only
                its balanced meters and does not judge them; switched off, the
                state is dropped (no orphan "back to normal")."""
                if not mgr or not alert_mgr.sig_vmeter or not gw_role.active:
                    vm_tracker.reset()
                    return
                try:
                    samples = mgr.alert_samples()
                except Exception:  # noqa: BLE001
                    logger.debug("vmeter alert samples failed", exc_info=True)
                    return
                now = _clock()
                for s in samples:
                    vm_tracker.update(s, {**alert_mgr.vmeter_thresholds,
                                          **(s.get("alerts") or {})}, now)
                vm_tracker.retain(s.get("id") for s in samples)

            while not harvester_stop.is_set():
                try:
                    pairs = registry.pairs() or ([(None, modbus_client)] if modbus_client else [])
                    if not gw_role.active:
                        # a standby does not poll its devices on purpose: no "down"
                        # alert, no availability published over the active one's,
                        # no alarm or threshold from values it only heard. What it
                        # knew is forgotten, so becoming active starts clean.
                        pairs = []
                        for _k in [k for k in prev if not k.startswith(('mqtt', 'influx'))]:
                            prev.pop(_k, None)
                            down_since.pop(_k, None)
                            alerted_down.discard(_k)
                    # keep the threshold engine's tunables in sync with live config
                    threshold_engine.deadband_pct = alert_mgr.threshold_deadband_pct
                    threshold_engine.alert_on_start = alert_mgr.threshold_alert_on_start
                    thr_seen: Set = set()
                    for dev_cfg, client in pairs:
                        if not client or not hasattr(client, 'get_stats'):
                            continue
                        try:
                            st = client.get_stats()
                        except Exception:  # noqa: BLE001
                            continue
                        did = dev_cfg.id if dev_cfg else 'modbus'
                        name = (dev_cfg.name or dev_cfg.id) if dev_cfg else 'Modbus'
                        note(name, st.get('events'))
                        # ONE liveness verdict (see device_registry.client_is_live):
                        # data freshness, not socket state. The transport flag said
                        # "connected" for hours after an endpoint vanished, so a
                        # dark datalogger looked identical to a producing one.
                        live = client_is_live(client)
                        transition('dev:' + did, name, live, 'device')
                        # feed the per-device HA connectivity binary_sensor (publishes
                        # only on change). The PRIMARY publishes too (audit DP-1): it
                        # was the only device without an availability topic, so its
                        # retained data values had no liveness signal at all — a dead
                        # Janitza looked identical to a steady one on the broker.
                        if _pub.mqtt:
                            _avail_prefix = (_pub.mqtt.config.topic_prefix
                                             if (dev_cfg is None or dev_cfg.primary)
                                             else dev_cfg.mqtt_topic_prefix)
                            _pub.mqtt.publish_device_availability(
                                _avail_prefix, live)
                            _seen_ts = st.get('last_success_ts')
                            _pub.mqtt.publish_device_runtime(
                                _avail_prefix, live,
                                datetime.fromtimestamp(_seen_ts).isoformat()
                                if _seen_ts else None,
                                read_errors=st.get('failed_reads'))
                        if alert_mgr.sig_alarm and dev_cfg is not None and live:
                            _alarm_transitions(dev_cfg, name, alarm_seen)
                        lat = st.get('last_latency_ms')
                        if alert_mgr.sig_latency and lat and lat > alert_mgr.latency_ms:
                            alert_mgr.fire('warn', 'lat:' + did, name,
                                           f'read latency {lat} ms exceeds {int(alert_mgr.latency_ms)} ms')
                        # per-register threshold crossings → alert events (off by
                        # default). Read-only over the device's live value store, so
                        # this never touches the poll hot path.
                        if alert_mgr.sig_threshold:
                            store = registry.store_for(did) or {}
                            from multibus.outputs.threshold_engine import gate_open
                            _by_name = {e.get('name'): e.get('value') for e in list(store.values())}
                            for reg in getattr(client, 'registers', None) or []:
                                th = getattr(reg, 'thresholds', None)
                                entry = store.get(reg.key) if th else None
                                if not entry:
                                    continue
                                key = f'thr:{did}:{reg.key}'
                                thr_seen.add(key)
                                # suppress on stale data: don't alarm on a value the
                                # device stopped refreshing (down/frozen) — the band
                                # holds and resumes cleanly on reconnect
                                _mono = entry.get('mono')
                                _iv = entry.get('interval') or 5
                                if _mono is not None and (time.monotonic() - _mono) > max(2.5 * _iv, 15):
                                    continue
                                ev = threshold_engine.evaluate(
                                    key, entry.get('value'), th, source=name,
                                    label=(reg.label or reg.name),
                                    unit=getattr(reg, 'unit', ''),
                                    gated=not gate_open(th, _by_name.get))
                                if ev:
                                    # band CHANGES are edge-triggered: the clear
                                    # must not be swallowed by the per-key rate
                                    # limit the alarm just armed
                                    alert_mgr.fire(ev['severity'], ev['key'],
                                                   ev['source'], ev['message'],
                                                   transition=True)
                    if alert_mgr.sig_threshold:
                        # drop band state for registers/devices that went away, so a
                        # removed threshold can't leave a stuck alarm behind
                        threshold_engine.retain(thr_seen)
                    # deleted devices, and alarms a template no longer declares
                    alarm_tracker.retain({d.id for d, _c in pairs if d is not None},
                                         seen=alarm_seen, evaluated=alarm_evaluated)
                    alarm_seen.clear()
                    alarm_evaluated.clear()
                    # a config that failed to load (self-healed to snapshot/defaults)
                    # is a loud, operator-actionable condition — surface it (rate-
                    # limited per key so it doesn't spam)
                    if getattr(config, '_load_failed', False):
                        alert_mgr.fire('error', 'config', 'Config',
                                       ('config.yaml could not be reloaded — the running '
                                        'configuration was kept; saves disabled until repaired')
                                       if getattr(config, '_reload_refused', False) else
                                       'config.yaml failed to load — running on '
                                       'last-known-good/defaults; saves disabled until repaired')
                    if getattr(config, 'config_written_by_newer', False):
                        # downgrade detected: the file carries a newer gateway's
                        # stamp — a save from this version silently drops any
                        # settings that version introduced (audit MEDIUM-2)
                        alert_mgr.fire('warn', 'config-downgrade', 'Config',
                                       f'config.yaml written by '
                                       f'{config.config_written_by} but '
                                       f'{__version__} is running — saving from '
                                       f'this version drops newer settings')
                    if _pub.mqtt:
                        transition('mqtt', 'MQTT', bool(_pub.mqtt.get_stats().get('connected')), 'sink')
                    if _pub.influx:
                        ist = _pub.influx.get_stats()
                        transition('influx', 'InfluxDB', bool(ist.get('connected')), 'sink')
                        if ist.get('auth_failed'):
                            # rotated token / missing bucket: writes 401/403/404
                            # while /ping stays green (audit DP-3) — the operator
                            # must act; points are buffering meanwhile
                            alert_mgr.fire('error', 'influx-auth', 'InfluxDB',
                                           'InfluxDB rejects writes (auth/bucket) — '
                                           'token rotated or bucket missing; points '
                                           'are buffering until fixed')
                        if alert_mgr.sig_buffer:
                            bp = ist.get('buffer_points') or 0
                            if bp > alert_mgr.buffer_points:
                                alert_mgr.fire('warn', 'buffer', 'InfluxDB',
                                               f'store-and-forward buffer {bp} points exceeds {alert_mgr.buffer_points}')
                            # drops are split by cause (A-MED-3): a schema/field-type
                            # refusal is NOT a buffer-overflow and points the operator
                            # at a different fix (field types / the template, not the
                            # buffer size). Fall back to the total when a stats source
                            # has no breakdown.
                            _sch = ist.get('dropped_schema')
                            _buf = ist.get('dropped_buffer')
                            if _sch is None and _buf is None:
                                _buf = ist.get('dropped_total') or 0   # legacy: attribute to buffer
                                _sch = 0
                            _sch, _buf = _sch or 0, _buf or 0
                            if drop_prev['schema'] is not None and _sch > drop_prev['schema']:
                                alert_mgr.fire('error', 'dropped-schema', 'InfluxDB',
                                               f'{_sch - drop_prev["schema"]} point(s) rejected by InfluxDB '
                                               f'(field-type/schema conflict) — check the field types, not the buffer')
                            if drop_prev['buffer'] is not None and _buf > drop_prev['buffer']:
                                alert_mgr.fire('error', 'dropped-buffer', 'InfluxDB',
                                               f'{_buf - drop_prev["buffer"]} point(s) dropped '
                                               f'(store-and-forward buffer full or too old) — raise the buffer size/retention')
                            drop_prev['schema'], drop_prev['buffer'] = _sch, _buf
                    mgr = getattr(app.state, 'vmeter_manager', None)
                    if mgr:
                        try:
                            for i in mgr.overview():
                                le = i.get('last_error')
                                nm = i.get('name') or i.get('template') or ''
                                if le:
                                    k = ('vmeter:' + nm, round(le.get('ts') or 0, 1), le.get('message') or le.get('kind') or '')
                                    if k not in seen:
                                        seen.add(k)
                                        event_log.add(le.get('level', 'error'), 'vMeter ' + nm,
                                                      le.get('message') or le.get('kind') or 'error',
                                                      le.get('kind', ''), le.get('ts'))
                        except Exception:  # noqa: BLE001
                            pass
                    _vmeter_alerts(mgr)
                    if len(seen) > 3000:
                        seen.clear()
                    last_err["msg"] = ""               # a clean pass resets the dedupe
                except Exception as e:  # noqa: BLE001
                    # Inner operations already guard themselves, so a top-level error
                    # here is a real bug — surface it (deduped, so no 5s spam).
                    msg = f"{type(e).__name__}: {e}"
                    if msg != last_err["msg"]:
                        logger.warning("event harvest error: %s", msg)
                        last_err["msg"] = msg
                harvester_stop.wait(5)

        # --- public surface -------------------------------------------------
        self.run = _harvest_events
