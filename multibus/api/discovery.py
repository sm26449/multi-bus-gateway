# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Attribute-meta publishing and HA autodiscovery, extracted whole from create_api (P0.8b squeeze).

DiscoveryService holds the attribute-metadata closures (attr_devices, sun_elev_at,
meta_topics, publish_attribute_meta, clear_attribute_meta, attributes_changed) and the
HA-discovery closures (mark_disc_cleared, ha_write_rules, discovery_regs,
sync_device_discovery). They publish to MQTT through the publisher holder. Kept as
closures built in __init__ so the bodies are verbatim; collaborators are an explicit
keyword constructor. Layer-clean intermediate in the api layer.
"""
from __future__ import annotations

import json
import logging
from typing import Dict, List

from multibus.catalog.device_kind import attribute_kind as _attribute_kind
from multibus.catalog.device_template import mppt_count_of as _mppt_count_of
from multibus.catalog.device_types import meter_only_model as _meter_only_model
from multibus.core.config import store_key

logger = logging.getLogger("multibus.api")  # preserve caplog logger name


class DiscoveryService:
    """The attribute-meta + HA-discovery seam (ADR-0038 service, api-layer for now)."""

    def __init__(self, *, config, registry, template_registry, attributes, calc_engine, gw_role, snapshot_store, _pub, _disc_cleared, _disc_cleared_path, _late_hooks, _meta_conn, _meta_sent, _commands_for, _write_rule, _writes_by_mqtt, _raw_channel_conflict, _words_of):
        def _attr_devices():
            out = []
            for d in config.devices:
                tid = d.template or next((sx.template for sx in (d.sources or []) if sx.template), '')
                kind = _attribute_kind(d, template_registry)
                mppt = 0
                for t_id in {tid} | {sx.template for sx in (d.sources or []) if sx.template}:
                    t_ = template_registry.get(t_id) if t_id else None
                    mppt = max(mppt, _mppt_count_of(t_) if t_ else 0)
                out.append((d.id, getattr(d, 'endpoint_id', '') or '', kind or None, mppt or None))
            return out

        def _sun_elev_at(place):
            loc = attributes.place_location(place)
            if not loc:
                return None
            from datetime import datetime as _dt, timezone as _tz
            from multibus.core import clock as _clock, sun as _sun
            return _sun.position(loc[0], loc[1], _dt.fromtimestamp(_clock.now(), _tz.utc))[0]

        def _meta_topics() -> Dict[str, Dict]:
            """Where each place's attributes go, on this gateway's own roots so
            two gateways on one broker never share a retained topic:
            <gw>/site/meta, <installation totals root>/meta, <device prefix>/meta."""
            from multibus.devices.endpoint_aggregator import aggregate_topic as _agg_topic
            out = {f"{config.mqtt.topic_prefix}/site/meta": attributes.meta('site')}
            for p_ in config.endpoints:
                if p_.get('id'):
                    out[f"{_agg_topic(p_, str(p_['id']), '', True)}/meta"] = attributes.meta('installation', str(p_['id']))
            for d_ in config.devices:
                if getattr(d_, 'mqtt_topic_prefix', '') and getattr(d_, 'mqtt_enabled', True):
                    out[f"{d_.mqtt_topic_prefix}/meta"] = attributes.meta('device', d_.id)
            return out

        def _publish_attribute_meta(force: bool = False) -> None:
            """What the plant and its equipment are, for every system that reads
            the broker: retained, published when a place's content changes, again
            in full after a (re)connect, and only by an active gateway (two would
            race on the retained topics). A topic no longer published (a renamed
            prefix, a device whose MQTT was turned off) is cleared."""
            if not _pub.mqtt or not getattr(_pub.mqtt, 'connected', False) or not gw_role.active:
                return
            conns = getattr(_pub.mqtt, 'connects', 0)
            if conns != _meta_conn['seen']:
                force, _meta_conn['seen'] = True, conns
            out = _meta_topics()
            for topic in [t for t in _meta_sent if t not in out]:
                _clear_attribute_meta(topic)
            for topic, body in out.items():
                content = json.dumps({k: v for k, v in body.items() if k != 'rev'}, default=str, sort_keys=True)
                if force or _meta_sent.get(topic) != content:
                    if _pub.mqtt._publish(topic, json.dumps(body, default=str, sort_keys=True), retain=True):
                        _meta_sent[topic] = content

        def _clear_attribute_meta(topic: str) -> None:
            _meta_sent.pop(topic, None)
            if _pub.mqtt and getattr(_pub.mqtt, 'connected', False):
                try:
                    _pub.mqtt._publish(topic, "", retain=True)
                except Exception:  # noqa: BLE001
                    pass

        def _attributes_changed(trigger: str, who: str) -> None:
            snapshot_store.schedule(trigger, user=who or '')
            try:
                _publish_attribute_meta()
            except Exception:  # noqa: BLE001 — publishing never fails a save
                logger.exception("attributes: publishing meta failed")

        def _mark_disc_cleared(device_id: str, cleared: bool) -> None:
            if (device_id in _disc_cleared) == cleared:
                return
            (_disc_cleared.add if cleared else _disc_cleared.discard)(device_id)
            try:
                _disc_cleared_path.write_text(json.dumps(sorted(_disc_cleared)), encoding="utf-8")
            except OSError as e:
                logger.warning(f"HA discovery: cannot keep which devices were cleared: {e}")

        def _ha_write_rules(d, regs) -> Dict:
            """The registers a device announces as write entities (number/select),
            by store key: only when FULLY enabled; the template is the write
            allowlist, so writability + bounds come from _write_rule, never from
            the saved register."""
            wrules = {}
            if not (config.mqtt.allow_write_entities and config.security.allow_writes
                    and not d.primary and d.protocol != 'http'):
                return wrules
            for r in regs:
                if getattr(r, 'register_type', 'holding') != 'holding':
                    continue
                rule = _write_rule(d, r.address, 'holding')
                if rule is None or not rule.writable:
                    continue
                # advertise only what the write handler would accept:
                # a scale_from or command-fronted register writes
                # THROUGH its command, so without an enabled HA-faced
                # value command the entity would be dead — or, worse,
                # a raw write with the wrong scale (audit 2026-10)
                _name = getattr(r, 'name', '')
                _cmds = _commands_for(d)
                _ha_cmd = any(
                    c['enabled'] and c['faces'].get('ha', True)
                    and c['def'].writes and c['def'].value_param is not None
                    and c['def'].writes[0].get('register') == _name
                    for c in _cmds)
                # what the handler refuses is not advertised: any
                # register any command writes on this slave (D4)
                _fronted = _raw_channel_conflict(
                    d, 'holding', int(r.address), _words_of(rule.data_type or 'uint16')) is not None
                if (getattr(rule, 'scale_from', None) or _fronted) and not _ha_cmd:
                    continue
                if _writes_by_mqtt(d) and not _ha_cmd:
                    continue          # an MQTT unit writes through commands only
                wrules[store_key(r.address, 'holding')] = rule   # keyed by table: a coil at
                # the same address must never pick up this holding row's rule
            return wrules

        def _discovery_regs(d) -> List:
            regs = config.unit_registers(d)     # all of the unit's sources, one entry per name
            # derived measurements (status/text, status/alarm, …) are entities too
            return regs + [e['_reg'] for e in calc_engine.store.get(d.id, []) if e.get('_reg') is not None]

        def _sync_device_discovery():
            """Rebuild the MQTT discovery hooks from the current non-primary
            ENABLED devices and publish them now (so HA sees a device the moment it is
            added/edited, not only on the next reconnect). Idempotent (retained)."""
            if not _pub.mqtt or not gw_role.active:   # the active one announces the devices
                return
            hooks = []
            for dev_cfg, _c in registry:
                if dev_cfg.primary:
                    continue
                if not (dev_cfg.enabled and dev_cfg.ha_discovery_enabled and dev_cfg.mqtt_enabled):
                    if dev_cfg.id in _disc_cleared:
                        continue                  # taken back already, this run or before
                    # no discovery without its values on MQTT (ADR-0025, ADD-14),
                    # nor for a device switched off (HA-DISC-2): what it would
                    # have announced is taken back, once

                    def _unhook(d=dev_cfg):
                        regs = _discovery_regs(d)
                        if _pub.mqtt.clear_device_discovery(d.id, regs, write_keys=set(_ha_write_rules(d, regs)),
                                                                 ha_node=d.ha_node):
                            _mark_disc_cleared(d.id, True)
                    hooks.append(_unhook)
                    continue

                def _hook(d=dev_cfg):
                    regs = _discovery_regs(d)
                    _mark_disc_cleared(d.id, False)   # announced: a later switch-off clears it again
                    _pub.mqtt.publish_device_discovery(
                        d.id, d.name, d.mqtt_topic_prefix, regs,
                        model=(d.template if d.protocol != 'none'
                               else _meter_only_model(d)),
                        write_rules=_ha_write_rules(d, regs), ha_node=d.ha_node)
                hooks.append(_hook)
            hooks.append(_publish_attribute_meta)    # the attributes: all after a (re)connect, else what changed
            _pub.mqtt.discovery_hooks = hooks
            if getattr(_pub.mqtt, "connected", False):
                for h in hooks:
                    try:
                        h()
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"device discovery publish failed: {e}")
            # the controller's command topics follow the same lifecycle (the
            # function is defined further down; at boot it runs itself once)
            for _hook in ('command_topics', 'vbms_command_topics', 'rules_reload'):
                fn = _late_hooks.get(_hook)
                if fn is None:
                    continue
                try:
                    fn()
                except Exception as e:  # noqa: BLE001
                    logger.warning("power-limit command topics: %s", e)

        # --- public surface -------------------------------------------------
        self.attr_devices = _attr_devices
        self.sun_elev_at = _sun_elev_at
        self.publish_attribute_meta = _publish_attribute_meta
        self.attributes_changed = _attributes_changed
        self.sync_device_discovery = _sync_device_discovery
