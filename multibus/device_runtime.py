# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Building a device's client — the ONE place that does it.

There used to be two: one in ``main.py`` for boot and one in ``api.py`` for a
device added or edited at runtime. They drifted, and the drift was invisible
until a restart, when a device would come back decoding with a different byte
order than it had a minute earlier. A test was written to pin the two copies
together; this module removes the need for it by removing the second copy.

Everything a device needs to start lives here: which driver each of its sources
wants, what registers and intervals that source polls, and the
:class:`~multibus.multi_source.MultiSourceClient` that fronts them all under one
identity. A single-source device — every device written before sources existed —
goes through exactly the same path, so production never runs a rarely-exercised
branch.
"""
from __future__ import annotations

import logging
from typing import Any, List, Tuple

logger = logging.getLogger(__name__)

DRIVER_PROTOCOLS = ('tcp', 'rtu', 'rtu-tcp', 'http', 'mqtt')


def driver_for(config, template_registry, dev_cfg, src, regs, groups, allow_nonlan):
    """One real driver for one SOURCE of a unit.

    The transport comes from the SOURCE, never from the device: a unit can be
    reached over Modbus and over HTTP at once, and each way has its own address,
    template and rhythm.
    """
    proto = (src.protocol or 'tcp').lower()
    if proto == 'http':
        from .http_client import HttpClient
        return HttpClient(http_cfg=src.http, registers=regs, poll_groups=groups,
                          allow_nonlan=allow_nonlan)
    if proto == 'mqtt':
        from .mqtt_input import MqttInputClient
        return MqttInputClient(mqtt_cfg=src.mqtt_in, registers=regs,
                               poll_groups=groups)
    from .modbus_client import ModbusClient
    # Decode order resolves from the SOURCE's template (falling back to the
    # device's), through the one resolver both boot and runtime use — so a
    # restart is byte-identical.
    bo = template_registry.byte_order_for(src.template or dev_cfg.template)
    multi = len(dev_cfg.sources or []) > 1
    return ModbusClient(config=src.connection, registers=regs, poll_groups=groups,
                        byte_order=bo,
                        device_id=f'{dev_cfg.id}:{src.id}' if multi else dev_cfg.id)


def build_device_client(config, template_registry, dev_cfg, allow_nonlan=False):
    """The client for one device, or None when it has nothing to run.

    Returns a :class:`MultiSourceClient` even for the ordinary one-source
    device, so identity, provenance and health follow the same rules everywhere.
    """
    if not dev_cfg.enabled:
        logger.info("Device '%s' disabled — skipping", dev_cfg.id)
        return None

    srcs = [s for s in (dev_cfg.sources or []) if s.enabled]
    unknown = [s for s in srcs if (s.protocol or 'tcp').lower() not in DRIVER_PROTOCOLS]
    for s in unknown:
        logger.warning("Device '%s' source '%s': unknown protocol %r — idle",
                       dev_cfg.id, s.id, s.protocol)
    srcs = [s for s in srcs if s not in unknown]
    if not srcs:
        return None

    parts: List[Tuple[Any, Any]] = []
    for src in srcs:
        try:
            if dev_cfg.primary and not getattr(src, 'poll_groups', None):
                # The primary's selection is already loaded and IS the legacy
                # one; going to disk here would change a byte of it.
                regs, groups = config.selected_registers, config.poll_groups
            else:
                regs, groups = config.load_device_registers(dev_cfg, source=src)
            parts.append((src, driver_for(config, template_registry, dev_cfg,
                                          src, regs, groups, allow_nonlan)))
            where = ((src.http or {}).get('url')
                     or (src.connection.serial_port if src.protocol == 'rtu'
                         else f'{src.connection.host}:{src.connection.port}'))
            logger.info("Device '%s' source '%s': %s, %d registers, %s",
                        dev_cfg.id, src.id, src.protocol, len(regs), where)
        except Exception as e:  # noqa: BLE001 — one bad source must not cost the
            # unit its other sources, nor the boot its other devices
            logger.warning("Device '%s' source '%s': not started — %s",
                           dev_cfg.id, src.id, e)
    if not parts:
        return None
    from .multi_source import MultiSourceClient
    return MultiSourceClient(dev_cfg.id, parts)
