"""Poller data-callback factory — the poll → store → calc → publish fan-out.

``make_data_callback`` builds the callback a Modbus poller fires for one device:
it updates the value store on the measurement clock, publishes to the device's
MQTT and InfluxDB sinks (each isolated), runs calculated registers, broadcasts
over the WebSocket, and nudges the devices that device feeds. ``run_linked``
routes what a device's meters give it and clears retained topics for values
gone absent.

Extracted verbatim from ``create_api`` (P0.8b Lot D). This is the hot
poll/publish path: no added allocation or branch per sample, and the logger
name stays ``multibus.api`` (injected) so caplog assertions and log lines are
unchanged. ``devices`` must not import the api layer, so every collaborator is
injected through the constructor.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Dict, List, Set


class DataCallbackFactory:
    """Builds the per-device poller callback (ADR-0038 #5)."""

    def __init__(self, *, current_values, registry, gw_role, config, pub,
                 logger, calc_engine, main_loop, ws_manager, calc_runners,
                 last_update) -> None:
        _pub = pub

        def _run_dependents(meter_id: str) -> None:
            """A meter reported: the devices it measures compute what it gives them."""
            for dep in sorted(calc_engine.dependents(meter_id)):
                runner = calc_runners.get(dep)
                if runner is None:
                    continue
                try:
                    runner()
                except Exception:  # noqa: BLE001 — another device's values never break this one's poll
                    logger.debug("linked values of %s after %s reported", dep, meter_id, exc_info=True)

        def make_data_callback(device_cfg=None):
            """Build the poller callback for one device. device_cfg None (or the
            primary device) keeps the exact legacy behavior: publishers fall back
            to their own config for routing (topics/bucket/tags byte-identical and
            live-tracking config edits); WS broadcast stays primary-only until the
            UI grows a device dimension (Phase B)."""
            primary = device_cfg is None or device_cfg.primary
            topic_prefix = None if primary else device_cfg.mqtt_topic_prefix
            bucket = None if primary else device_cfg.influxdb_bucket
            device_tag = None if primary else device_cfg.influxdb_device_tag
            influx_tags = None if primary else (device_cfg.influxdb_tags or None)
            device_id = "" if primary else device_cfg.id
            values_store = (current_values if primary
                            else registry.ensure_store(device_cfg.id))
            # Per-device output sinks (Phase 2): a device can opt out of MQTT and/or
            # InfluxDB while still polling, and publish on its own rhythm (its own
            # publish mode / heartbeat / write interval; unset = Settings). The
            # primary always routes to both, the gateway's way. Read at every
            # poll, so a change from its Outputs tab applies at the next reading
            # without restarting the poller.
            def _routing():
                if not gw_role.active:
                    # a standby keeps its values (what it hears on the bus) but
                    # publishes none: the active one does
                    return False, False, {}, {}
                if device_cfg is None:
                    return True, True, {}, {}
                cur = config.get_device(device_cfg.id) or device_cfg
                mp = {k: v for k, v in (('mode', cur.mqtt_publish_mode or None),
                                        ('heartbeat', cur.mqtt_heartbeat_s)) if v is not None}
                ip = {k: v for k, v in (('mode', cur.influxdb_publish_mode or None),
                                        ('interval', cur.influxdb_write_interval_s),
                                        ('heartbeat', cur.influxdb_heartbeat_s)) if v is not None}
                return cur.mqtt_enabled, cur.influxdb_enabled, mp, ip
            calc_key = config.primary_id if (device_cfg is None or device_cfg.primary) else device_cfg.id

            def data_callback(poll_group: str, data: Dict[int, Dict]):
                """Callback from a Modbus poller to update values and publish."""
                # Update current values. The timestamp is the MEASUREMENT time
                # (item['ts'], set by the driver when the value was actually read),
                # NOT callback time — the virtual-meter freshness watchdog reads
                # this to decide stale-vs-fresh, so under slow/retried reads callback
                # time would make stale data look newer than it is and defeat the
                # fail-safe. InfluxDB already records item['ts']; this keeps every
                # sink on the same clock.
                for address, item in data.items():
                    _ts = item.get('ts')
                    values_store[address] = {
                        'value': item.get('value'),
                        'name': item.get('register').name if item.get('register') else '',
                        'label': item.get('register').label if item.get('register') else '',
                        'unit': item.get('register').unit if item.get('register') else '',
                        'poll_group': poll_group,
                        # 'timestamp' is the ISO DISPLAY time (falls back to now for
                        # a rare untimestamped value); 'ts' is the numeric wall
                        # measurement time (public via /api/values). The vmeter
                        # freshness clock is 'mono' below — a MONOTONIC stamp, so it
                        # is immune to wall-clock steps; None when the driver gave no
                        # time, so a missing time fails CLOSED (stale).
                        'timestamp': (datetime.fromtimestamp(_ts).isoformat()
                                      if _ts else datetime.now().isoformat()),
                        'ts': _ts if _ts else None,
                        # monotonic stamp for STEP-IMMUNE freshness (the vmeter reads
                        # this, not the wall clock). None → the vmeter fails closed.
                        'mono': item.get('mono'),
                        # poll cadence of the group that produced this value; the
                        # vmeter derives a per-row bound from it (a 60s slow-group
                        # row must not be judged by a 15s instance bound). None for
                        # push sources → the instance bound applies.
                        'interval': item.get('interval'),
                        # endpoint fan-out rules the template declared for this
                        # field — the aggregator reads them straight off the store
                        'aggregates': getattr(item.get('register'), 'aggregates', None),
                        # the store key is address + its table's offset; the
                        # register's own address and table, for display
                        'address': getattr(item.get('register'), 'address', address),
                        'register_type': getattr(item.get('register'), 'register_type', 'holding'),
                    }

                last_update['timestamp'] = datetime.now().isoformat()
                last_update['ts'] = time.time()

                # Publish to each output sink independently — one sink failing (broker
                # down, bucket missing, network blip) must never skip the other sink or
                # the value store above. The poller thread itself is already isolated
                # per device, so a fault here stays local.
                mqtt_on, influx_on, mqtt_policy, influx_policy = _routing()
                if _pub.mqtt and mqtt_on:
                    try:
                        _pub.mqtt.publish_register_data(poll_group, data,
                                                             topic_prefix=topic_prefix, **mqtt_policy)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"MQTT publish failed for {device_id or 'primary'}: {e}")
                if _pub.influx and influx_on:
                    try:
                        _pub.influx.write_register_data(
                            poll_group, data, bucket=bucket,
                            device_tag=device_tag, device_id=device_id,
                            extra_tags=influx_tags, **influx_policy)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"InfluxDB write failed for {device_id or 'primary'}: {e}")

                # Calculated registers: derive formula values from the freshly-updated
                # store and route them to this device's sinks (same routing as above).
                calc_batch = calc_engine.run(calc_key, poll_group, values_store,
                                             topic_prefix=topic_prefix, bucket=bucket,
                                             device_tag=device_tag, device_id=device_id,
                                             mqtt_on=mqtt_on, influx_on=influx_on,
                                             influx_tags=influx_tags,
                                             mqtt_policy=mqtt_policy, influx_policy=influx_policy)

                # Broadcast via WebSocket (thread-safe async call). Phase B: every
                # device broadcasts, tagged with its id — the client filters on the
                # active dashboard device (address spaces overlap across devices, so
                # an untagged merge would collide). Primary messages are unchanged
                # apart from the additive `device` field.
                if main_loop["loop"]:
                    ws_values = {
                        str(addr): {
                            'value': item.get('value'),
                            'name': item.get('register').name if item.get('register') else '',
                            **({'address': item['register'].address, 'register_type': item['register'].register_type}
                               if getattr(item.get('register'), 'register_type', 'holding') not in ('holding', '', None)
                               else {}),
                        }
                        for addr, item in data.items()
                    }
                    # Include calc values so the live Monitor (which reads the WS-fed
                    # store) can chart them; carry unit+label since they have no entry
                    # in the register catalog the picker draws from.
                    for addr, item in (calc_batch or {}).items():
                        reg = item.get('register')
                        ws_values[str(addr)] = {
                            'value': item.get('value'),
                            'name': reg.name if reg else '',
                            'unit': reg.unit if reg else '',
                            'label': reg.label if reg else '',
                            'calculated': True,
                        }
                    asyncio.run_coroutine_threadsafe(
                        ws_manager.broadcast({
                            'type': 'data',
                            'device': calc_key,               # primary id for legacy/primary
                            'poll_group': poll_group,
                            'values': ws_values,
                            'timestamp': last_update['timestamp'],
                            'ts': last_update['ts'],
                        }),
                        main_loop["loop"]
                    )

                # the devices this one measures compute what it gives them now
                _run_dependents(calc_key)

            # absent values whose clear could not be sent yet (broker away)
            pending_clear: Set[str] = set()

            def run_linked(force: bool = False):
                """What this device's meters give it, routed as its own values;
                a value gone absent has its retained topic cleared (the gateway's
                "no value": an empty retained payload, as for an unticked row)."""
                mqtt_on, influx_on, mqtt_policy, influx_policy = _routing()
                gone: List = []
                batch = calc_engine.run_linked(calc_key, values_store, gone=gone, force=force,
                                               rerun=run_linked,
                                               topic_prefix=topic_prefix, bucket=bucket,
                                               device_tag=device_tag, device_id=device_id,
                                               mqtt_on=mqtt_on, influx_on=influx_on,
                                               influx_tags=influx_tags,
                                               mqtt_policy=mqtt_policy, influx_policy=influx_policy)
                if _pub.mqtt and (gone or pending_clear):
                    try:
                        want = pending_clear | {_pub.mqtt._build_topic(r, topic_prefix) for r in gone}
                        # a value now published again is not cleared after all
                        sent = {_pub.mqtt._build_topic(it['register'], topic_prefix)
                                for it in (batch or {}).values()}
                        want -= sent
                        pending_clear.clear()
                        if want and mqtt_on and getattr(_pub.mqtt, 'connected', False):
                            _pub.mqtt.clear_topics(want)
                        elif want:
                            # the broker is away (or this gateway is standby): the
                            # clear goes out at the next evaluation that can send it
                            pending_clear.update(want)
                    except Exception:  # noqa: BLE001
                        logger.debug("clearing absent values of %s", calc_key, exc_info=True)
                if batch and main_loop["loop"]:
                    asyncio.run_coroutine_threadsafe(
                        ws_manager.broadcast({
                            'type': 'data', 'device': calc_key,
                            'poll_group': next(iter(batch.values()))['register'].poll_group,
                            'values': {str(addr): {'value': it.get('value'),
                                                   'name': it['register'].name, 'unit': it['register'].unit,
                                                   'label': it['register'].label, 'calculated': True}
                                       for addr, it in batch.items()},
                            'timestamp': datetime.now().isoformat(), 'ts': time.time(),
                        }),
                        main_loop["loop"])
                return batch

            if not primary:
                calc_runners[device_cfg.id] = run_linked
            return data_callback


        self.make = make_data_callback
