# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Template → register-selection seeding, shared by the API and the boot path.

Extracted from ``create_api()``'s ``_autoselect_template_registers`` closure so
plant-materialized devices can seed at BOOT too (main.py builds their clients
before the API exists — a plant unit with no selection file would otherwise
poll nothing until someone opened the UI). Behavior is identical to the
closure this replaces; the API keeps a thin wrapper.
"""
from __future__ import annotations

import logging
from typing import Any

from .canonical_fields import measurement_for, mqtt_topic_for

logger = logging.getLogger(__name__)


def autoselect_template_registers(config: Any, template_registry: Any,
                                  dev_cfg: Any) -> None:
    """Seed a device with its template's registers (poll groups + intervals
    from the template) so it polls immediately — no manual picking. Skips if
    it already has a fitting selection or has no template."""
    if not dev_cfg.template or dev_cfg.primary:
        return
    tpl = template_registry.get(dev_cfg.template)
    if tpl is None or not tpl.registers:
        return
    existing, _g = config.load_device_registers(dev_cfg)
    if existing:
        # A kept file from a PREVIOUS device at this id may belong to a
        # different template — reusing it would decode against the wrong
        # map. Keep it only if it still fits (any selected name is in the
        # assigned template); otherwise fall through and re-seed.
        tpl_names = {r.name for r in tpl.registers}
        if any(getattr(r, 'name', None) in tpl_names for r in existing):
            return
        logger.warning(f"device {dev_cfg.id}: kept registers don't match "
                       f"template {tpl.id} — re-seeding from the template")
    # Curated templates mark a recommended subset via per-register `defaults`
    # (the Janitza map has 58 of 4126) — seed only those. A template without
    # defaults is seeded whole, but capped so a huge map can't flood
    # MQTT/InfluxDB with thousands of series on a single click.
    chosen = [r for r in tpl.registers if r.defaults]
    if not chosen:
        chosen = tpl.registers
        if len(chosen) > 300:
            logger.warning(f"device {dev_cfg.id}: template {tpl.id} has "
                           f"{len(chosen)} registers and no curated defaults — "
                           "auto-selecting none (pick registers in the UI)")
            return

    def _seed_output(r):
        """MQTT topic + InfluxDB measurement + UI for a seeded register.
        Precedence: the template's explicit per-register ``defaults`` win;
        else derive from the canonical dictionary (name → hierarchical MQTT
        topic + InfluxDB measurement). A non-canonical name (e.g. a raw
        vendor map) leaves BOTH empty so the publisher owns the fallback
        uniformly — MQTT to the flat register name, InfluxDB to the
        name/unit heuristic in ``_get_measurement`` — instead of pinning the
        measurement to the raw ``category`` (often 'other'), which would
        shadow that heuristic and collapse every series into one measurement."""
        d = r.defaults or {}
        dm, di, du = d.get('mqtt') or {}, d.get('influxdb') or {}, d.get('ui') or {}
        return {
            'mqtt': {'enabled': dm.get('enabled', True),
                     'topic': dm.get('topic') or mqtt_topic_for(r.name) or ''},
            'influxdb': {'enabled': di.get('enabled', True),
                         'measurement': (di.get('measurement')
                                         or measurement_for(r.name) or ''),
                         'tags': di.get('tags') or {}},
            'ui': {'show_on_dashboard': du.get('show_on_dashboard', True),
                   'widget': du.get('widget', 'value')},
        }

    reg_list = [{
        'address': r.address, 'name': r.name, 'label': r.label or r.name,
        'unit': r.unit, 'data_type': r.data_type,
        'poll_group': r.poll_group or 'normal', 'json_path': r.json_path,
        'topic': getattr(r, 'topic', ''), 'scale': r.scale,
        **({'offset': r.offset} if getattr(r, 'offset', 0) else {}),
        **({'scale_from': r.scale_from} if getattr(r, 'scale_from', '') else {}),
        'register_type': getattr(r, 'register_type', 'holding'),
        **({'nan': r.nan} if getattr(r, 'nan', None) is not None else {}),
        **({'monotonic': True} if getattr(r, 'monotonic', False) else {}),
        **{k: getattr(r, k) for k in ('enum', 'bits', 'mask', 'shift')
           if getattr(r, k, None) is not None},
        **{k: getattr(r, k) for k in
           ('device_class', 'state_class', 'entity_category', 'icon')
           if getattr(r, k, '')},
        **({'enabled_by_default': r.enabled_by_default}
           if getattr(r, 'enabled_by_default', None) is not None else {}),
        **({'suggested_display_precision': r.suggested_display_precision}
           if getattr(r, 'suggested_display_precision', None) is not None else {}),
        **_seed_output(r),
    } for r in chosen]
    tpg = {n: {'interval': g.get('interval', 5), 'description': g.get('description', '')}
           for n, g in (tpl.poll_groups or {}).items()} or None
    config.save_device_registers(dev_cfg.id, reg_list, poll_groups=tpg)
    logger.info(f"device {dev_cfg.id}: auto-selected {len(reg_list)} template registers")
