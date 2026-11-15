# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Device-restart + role-change / take-over hooks, extracted whole from create_api (P0.8b squeeze).

Lifecycle holds restart_devices / restart_devices_using_template (rebuild device clients) and
the DR hooks on_role_change / take_over_partner_state (re-arm the partner's write leases and
report its interrupted sequences when this gateway becomes active). Verbatim closures with an
explicit keyword constructor; on_role_change / restart_devices* are exposed for the gw_role
registration and ApiCtx.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import List

logger = logging.getLogger("multibus.api")  # preserve caplog logger name
TAKEOVER_WARMUP_S = 10.0  # a gateway that took over waits this long before its rules write


class Lifecycle:
    """The restart + role-change/take-over hooks (ADR-0038)."""

    def __init__(self, *, app, config, registry, alert_mgr, event_log, gw_role, _lease_mgr, _pub, rest_push_manager, _cmd_dispatch, _make_command_lease_revert, _make_lease_revert, _apply_rest_push, _start_device_client, _sync_device_discovery, _publish_attribute_meta):
        def _restart_devices(device_ids: List[str]) -> List[str]:
            """Rebuild the running client of these devices from the CURRENT config
            (a bridge's new host, a template's new word order)."""
            out = []
            current = {d.id: d for d in config.devices}
            # a device's components read through its connection: they follow it
            device_ids = list(device_ids) + [d.id for d in config.devices
                                             if d.component_of and d.component_of in device_ids
                                             and d.id not in device_ids]
            for dev_cfg, client in list(registry):
                if dev_cfg.id not in device_ids or dev_cfg.primary:
                    continue
                fresh = current.get(dev_cfg.id, dev_cfg)
                try:
                    if client:
                        client.disconnect()
                    new_client = _start_device_client(fresh)
                    registry.replace(fresh.id, fresh, client=new_client, add_if_missing=True)
                    out.append(fresh.id)
                except Exception as e:  # noqa: BLE001 — one device must not stop the rest
                    logger.error("restart of %s failed: %s", fresh.id, e)
            return out

        def _restart_devices_using_template(template_id: str) -> List[str]:
            """A map's protocol (word order, read size) is read when a client is
            made: restart every running device that reads with it, so a saved
            change takes effect now instead of at the next container restart."""
            def uses(tid: str) -> bool:
                # a component's template is `<parent>:<type>`: it changes with it
                return tid == template_id or tid.startswith(template_id + ':')
            ids = [d.id for d, _c in list(registry) if not d.primary and any(uses(t) for t in (
                {getattr(d, 'template', '') or ''}
                | {getattr(sx, 'template', '') or '' for sx in (getattr(d, 'sources', None) or [])}))]
            out = _restart_devices(ids)
            if out:
                logger.info("template %s: protocol changed — restarted %s", template_id, ", ".join(out))
            return out

        def _on_role_change(old: str, new: str) -> None:
            """The gateway became standby or active (PUT /api/role): stop or start
            what it does. Writes are already refused or allowed by then — this only
            winds the pollers and outputs down or up, off the request thread
            (reconnecting devices takes time)."""
            standby = new != 'active'
            if standby:
                # a standby transmits nothing: an armed Virtual BMS is disarmed NOW, on the
                # request thread, and stays so until re-armed (WK7-SAFE-1)
                _vbm = getattr(app.state, 'vbms_manager', None)
                if _vbm is not None:
                    try:
                        _vbm.revoke(f"the gateway became {new}")
                    except Exception:  # noqa: BLE001 — the role change itself must go through
                        logger.exception("virtual BMS revoke on role change")
                # nothing waiting for a bus is written by a standby; it says
                # nothing on cmd/result either (ADR-0018 D10, D12)
                _cmd_dispatch.cancel(reason=f"the gateway became {new} — nothing written")

            def run():
                for _dc, client in registry.pairs():
                    fn = getattr(client, 'standby' if standby else 'resume', None)
                    if fn is None:
                        continue
                    try:
                        fn()
                    except Exception as e:  # noqa: BLE001 — one device must not stop the others
                        logger.warning("role %s: device %s — %s", new, getattr(_dc, 'id', '?'), e)
                if standby:
                    seqs = getattr(app.state, 'sequences', None)
                    for sid in list(getattr(seqs, 'running', {}) or {}):
                        seqs.cancel(sid, who='standby')        # a half-done sequence is never resumed blindly
                    for _dc, _c in registry.pairs():
                        _lease_mgr.clear_device(_dc.id)        # the active one owns the devices now
                    rest_push_manager.stop_all()
                else:
                    if old == 'standby':
                        app.state.role_warm_until = time.monotonic() + TAKEOVER_WARMUP_S
                        _take_over_partner_state()
                    try:
                        _publish_attribute_meta()               # the retained attributes are now this one's
                    except Exception as e:  # noqa: BLE001
                        logger.warning("role active: attributes meta — %s", e)
                    for _dc, _c in registry.pairs():
                        _apply_rest_push(_dc)
                    _sync_device_discovery()
                    if _pub.mqtt is not None:
                        try:
                            _pub.mqtt.publish_ha_discovery()
                        except Exception as e:  # noqa: BLE001
                            logger.warning("role active: discovery — %s", e)
                ctl = getattr(app.state, 'opcua', None)
                if ctl is not None:
                    ctl.apply()
                for fn in list(getattr(app.state, 'role_hooks', [])):   # main.py: virtual meters, PQ recorder
                    try:
                        fn(old, new)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("role %s: %s — %s", new, getattr(fn, '__name__', fn), e)
                event_log.add('warning', 'gateway', f"role {old} → {new} (epoch {gw_role.epoch})", 'role')
                logger.warning("gateway is %s: pollers and outputs %s", new, 'stopped' if standby else 'started')

            threading.Thread(target=run, name=f"role-{new}", daemon=True).start()

        def _take_over_partner_state() -> None:
            """What the partner held when this gateway took over: its write leases
            go on here with the time they had left (the dead-man keeps watching);
            a sequence it was running is said, not resumed."""
            pm = getattr(app.state, 'peer_mirror', None) or {}
            age = max(0.0, time.time() - float(pm.get('at') or time.time()))
            for m in pm.get('leases') or []:
                try:
                    left_ms = int(max(0.0, float(m.get('remaining_s') or 0) - age) * 1000)
                    meta = {k: v for k, v in m.items() if k != 'remaining_s'}
                    if m.get('command'):
                        rv = _make_command_lease_revert(m['device'], m['command'])
                    else:
                        rv = _make_lease_revert(m['device'], m['register_type'], int(m['address']), m['data_type'],
                                                float(m['scale']), m['safe_value'], off=float(m.get('offset', 0.0) or 0.0))
                    _lease_mgr.arm(m['device'], m['register_type'], int(m['address']), max(1, left_ms), rv,
                                   meta=meta, fire_now=left_ms <= 0)
                    logger.warning("take-over: write lease of %s @%s goes on here (%.1f s left)",
                                   m['device'], m.get('address'), left_ms / 1000)
                except Exception as e:  # noqa: BLE001
                    logger.error("take-over: the partner's lease %r could not be re-armed: %s", m, e)
                    alert_mgr.fire('error', 'role:lease', 'gateway', f"a write lease of the partner could not be re-armed: {e}")
            for run in pm.get('sequences') or []:
                msg = (f"sequence {run.get('sequence')} was running on the partner (step {run.get('step')}, "
                       f"by {run.get('by')}) when this gateway took over: it is NOT resumed — check its devices")
                event_log.add('warning', 'gateway', msg, 'role')
                alert_mgr.fire('warning', f"role:sequence:{run.get('sequence')}", 'gateway', msg, transition=True)
            app.state.peer_mirror = {}

        # --- public surface -------------------------------------------------
        self.restart_devices = _restart_devices
        self.restart_devices_using_template = _restart_devices_using_template
        self.on_role_change = _on_role_change
        self.take_over_partner_state = _take_over_partner_state
