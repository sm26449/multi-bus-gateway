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
from typing import Any, Dict, List, Tuple

from .config import MODBUS_PROTOCOLS

logger = logging.getLogger(__name__)

DRIVER_PROTOCOLS = MODBUS_PROTOCOLS + ('http', 'mqtt')


GATEWAY_BROKER = '@gateway'


def gateway_broker(config, mqtt_cfg):
    """``broker: "@gateway"``: the source reads from the broker the gateway
    itself publishes to (host, port, credentials, TLS), so a device on the
    stack's own broker carries no copy of its password. No hostname contains
    an ``@``, so the word cannot shadow a real host."""
    if str(mqtt_cfg.get('broker', '')).strip() != GATEWAY_BROKER:
        return mqtt_cfg
    g = getattr(config, 'mqtt', None)
    out = dict(mqtt_cfg)
    out.update(broker=getattr(g, 'broker', '') or '', port=getattr(g, 'port', 1883) or 1883,
               username=getattr(g, 'username', '') or '', password=getattr(g, 'password', '') or '',
               tls=bool(getattr(g, 'tls_enabled', False)))
    return out


# an RTU bus this gateway polls it can also LISTEN to (rtu_tap): what a
# standby does with it, its partner being the master there
TAPPABLE = ('rtu', 'rtu-tcp')

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
        # the template knows the source's topic layout, so it may declare
        # how the source says it is alive; the connection's own word wins
        tpl = template_registry.get(src.template or dev_cfg.template)
        proto = (getattr(tpl, 'protocol', None) or {}) if tpl is not None else {}
        av = proto.get('availability')
        mqtt_cfg = dict(src.mqtt_in or {})
        if isinstance(av, dict) and not mqtt_cfg.get('availability'):
            mqtt_cfg['availability'] = av
        # the keepalive too; only when the connection says nothing about it,
        # so its `keepalive: false` turns the template's off
        if isinstance(proto.get('keepalive'), dict) and mqtt_cfg.get('keepalive') is None:
            mqtt_cfg['keepalive'] = proto['keepalive']
        mqtt_cfg = gateway_broker(config, mqtt_cfg)
        # one connection per device: its components read through their
        # parent's (mqtt_link.py)
        mqtt_cfg['link'] = getattr(dev_cfg, 'component_of', '') or dev_cfg.id
        return MqttInputClient(mqtt_cfg=mqtt_cfg, registers=regs,
                               poll_groups=groups)
    if proto == 'rtu_tap':
        # listen-only observer of a bus mastered by someone else — byte order
        # resolves from the template exactly like an active Modbus source
        from .rtu_tap import RtuTapClient
        tid = src.template or dev_cfg.template
        if tid and template_registry.get(tid) is None:
            raise ValueError(f"template {tid!r} is not loaded — "
                             "not decoding a tapped bus with a default byte order")
        return RtuTapClient(conn_cfg=src.connection, registers=regs,
                            poll_groups=groups,
                            byte_order=template_registry.byte_order_for(tid),
                            device_id=dev_cfg.id)
    from .modbus_client import ModbusClient
    # Decode order resolves from the SOURCE's template (falling back to the
    # device's), through the one resolver both boot and runtime use — so a
    # restart is byte-identical. A template the device DECLARES but the
    # registry could not load (file gone, parse error) must not fall back to
    # 'big' — a little-endian meter would then publish word-swapped garbage
    # as if it were fine (3.83.0); the source stays idle and says why.
    tid = src.template or dev_cfg.template
    if tid and template_registry.get(tid) is None:
        raise ValueError(f"template {tid!r} is not loaded (missing or failed to parse) — "
                         "not polling with a default byte order")
    bo = template_registry.byte_order_for(tid)
    multi = len(dev_cfg.sources or []) > 1
    tpl = template_registry.get(tid) if tid else None
    setup = list(getattr(tpl, 'setup', None) or [])

    def setup_gate():
        # the template's setup writes obey the same switches as any write
        if not getattr(config.security, 'allow_writes', False):
            return "writes are off (Settings → Security → allow writes)"
        if getattr(dev_cfg, 'write_locked', False):
            return "the device is write-locked"
        return None
    return ModbusClient(config=src.connection, registers=regs, poll_groups=groups,
                        byte_order=bo,
                        device_id=f'{dev_cfg.id}:{src.id}' if multi else dev_cfg.id,
                        setup_steps=setup, setup_gate=setup_gate if setup else None,
                        min_write_gap_s=float(((getattr(tpl, 'protocol', None) or {}).get('min_write_gap_s')) or 0),
                        max_read=int(((getattr(tpl, 'protocol', None) or {}).get('max_registers_per_read')) or 0),
                        single_writes=bool((getattr(tpl, 'protocol', None) or {}).get('single_register_writes')))


def apply_template_hygiene(regs, template):
    """Counter hygiene comes from the template, not from the day the device was
    created.

    ``monotonic`` and ``daily`` were added to the bundled templates after many
    device register sets had already been written to disk; a selection made
    before the flag existed keeps ``False`` forever, so the filter the
    template promises never runs (Site.E_Day kept falling at sunset in
    production for four days after 3.77.0 shipped it). A flag the template
    sets is applied to the matching selected register (same name, or same
    address when the names differ); a flag the selection already sets is
    kept. Returns the list it was given, mutated, plus the names it changed."""
    tregs = list(getattr(template, 'registers', None) or []) if template else []
    if not tregs:
        return regs, []
    by_name = {t.name: t for t in tregs if getattr(t, 'name', None)}
    from .config import store_key
    by_addr = {store_key(t.address, getattr(t, 'register_type', '') or 'holding'): t
               for t in tregs if getattr(t, 'address', None) is not None}
    changed = []
    for reg in regs:
        t = by_name.get(reg.name) or by_addr.get(store_key(reg.address, getattr(reg, 'register_type', '') or 'holding'))
        if t is None:
            continue
        for flag in ('monotonic', 'daily'):
            if getattr(t, flag, False) and not getattr(reg, flag, False):
                setattr(reg, flag, True)
                changed.append(f"{reg.name}:{flag}")
    return regs, changed


def build_device_client(config, template_registry, dev_cfg, allow_nonlan=False):
    """The client for one device, or None when it has nothing to run.

    Returns a :class:`MultiSourceClient` even for the ordinary one-source
    device, so identity, provenance and health follow the same rules everywhere.
    """
    if not dev_cfg.enabled:
        logger.info("Device '%s' disabled — skipping", dev_cfg.id)
        return None
    if (dev_cfg.protocol or '').lower() == 'none':
        # a device that IS its meters: nothing to read, alive when they are
        from .meter_only import MeterOnlyClient
        return MeterOnlyClient(config, dev_cfg.id)

    srcs = [s for s in (dev_cfg.sources or []) if s.enabled]
    unknown = [s for s in srcs if (s.protocol or 'tcp').lower() not in DRIVER_PROTOCOLS]
    for s in unknown:
        logger.warning("Device '%s' source '%s': unknown protocol %r — idle",
                       dev_cfg.id, s.id, s.protocol)
    srcs = [s for s in srcs if s not in unknown]
    if not srcs:
        return None

    parts: List[Tuple[Any, Any]] = []
    taps: Dict[str, Any] = {}
    for src in srcs:
        try:
            if dev_cfg.primary and not getattr(src, 'poll_groups', None):
                # The primary's selection is already loaded and IS the legacy
                # one; going to disk here would change a byte of it.
                regs, groups = config.selected_registers, config.poll_groups
            else:
                regs, groups = config.load_device_registers(dev_cfg, source=src)
            tpl = template_registry.get(src.template or dev_cfg.template) \
                if (src.template or dev_cfg.template) else None
            regs, hygiene = apply_template_hygiene(regs, tpl)
            if hygiene:
                logger.info("Device '%s' source '%s': counter hygiene from the template — %s",
                            dev_cfg.id, src.id, ', '.join(hygiene))
            parts.append((src, driver_for(config, template_registry, dev_cfg,
                                          src, regs, groups, allow_nonlan)))
            if (src.protocol or '').lower() in TAPPABLE:
                # what this source does while the gateway is standby: listen
                # to the bus its partner masters (the same port, the same map)
                def _tap(src=src, regs=regs, groups=groups):
                    from .rtu_tap import RtuTapClient
                    return RtuTapClient(conn_cfg=src.connection, registers=regs, poll_groups=groups,
                                        byte_order=template_registry.byte_order_for(src.template or dev_cfg.template),
                                        device_id=dev_cfg.id)
                taps[src.id] = _tap
            where = ((src.http or {}).get('url')
                     or src.connection.bus)
            logger.info("Device '%s' source '%s': %s, %d registers, %s",
                        dev_cfg.id, src.id, src.protocol, len(regs), where)
        except Exception as e:  # noqa: BLE001 — one bad source must not cost the
            # unit its other sources, nor the boot its other devices
            logger.warning("Device '%s' source '%s': not started — %s",
                           dev_cfg.id, src.id, e)
    if not parts:
        return None
    from .multi_source import MultiSourceClient
    return MultiSourceClient(dev_cfg.id, parts, tap_factories=taps)
