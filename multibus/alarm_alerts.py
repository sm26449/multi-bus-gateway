# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""What a device says is wrong, as alerts.

A template declares its alarm fields (``display.alarms``: a fault word, an
error code, a calculated flag) — the dashboard counts them. This turns their
changes into alerts on the gateway's delivery path (MQTT, webhook):

- an alarm that turns active, and stays so for its ``for_s``, fires once
  (``danger`` → error, ``warning`` → warn);
- a new value while active fires again (error code 101 became 201);
- clearing fires an info — only for an alarm that was announced.

Fed only while the device is live: a dead device's alarms neither fire nor
clear (its own "down" alert says what happened). An alarm field whose value
went stale while the device lives on (another source still answers) is
unknown, not cleared — it keeps its state. A clear waits until the alarm has
stayed inactive for its `for_s`, at least CLEAR_HOLD_S: an error code
flapping 0 ↔ code is one alarm, not an alert every few seconds. Nothing here
knows a vendor.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

CLEAR_HOLD_S = 60.0


class AlarmTracker:
    def __init__(self, fire: Callable[..., None], clock: Callable[[], float] = time.monotonic):
        self._fire = fire            # AlertManager.fire(severity, key, source, message, transition=)
        self._clock = clock
        self.state: Dict[str, Dict[str, Any]] = {}

    def update(self, device_id: str, source: str, declared: Iterable[Dict[str, Any]],
               active: List[Dict[str, Any]], seen: Optional[Set[str]] = None,
               known: Optional[Set[str]] = None) -> None:
        """``declared`` = the template's display.alarms; ``active`` = what
        display.active_alarms reports active now ([{field, label, severity,
        value}]); ``known`` = the fields with a fresh value (None: all)."""
        now = self._clock()
        act = {a['field']: a for a in active or []}
        for a in declared or []:
            f = a.get('field') if isinstance(a, dict) else None
            if not f:
                continue
            k = f'alarm:{device_id}:{f}'
            if seen is not None:
                seen.add(k)
            cur, st = act.get(f), self.state.get(k)
            if cur is None:
                if known is not None and f not in known:
                    continue                     # unknown (stale), not cleared
                if st and st.get('fired'):
                    st.setdefault('clear_since', now)
                    if now - st['clear_since'] < max(float(a.get('for_s') or 0), CLEAR_HOLD_S):
                        continue                 # wait: it may come straight back
                    self._fire('info', k, source, f"{st['label']}: cleared", transition=True)
                self.state.pop(k, None)
                continue
            if st is not None:
                st.pop('clear_since', None)      # back before the clear went out
            if st is None or st.get('value') != cur.get('value'):
                st = self.state[k] = {'since': now if st is None else st['since'], 'fired': False,
                                      'label': cur.get('label') or f, 'value': cur.get('value')}
            if not st['fired'] and now - st['since'] >= float(a.get('for_s') or 0):
                st['fired'] = True
                self._fire('error' if cur.get('severity') == 'danger' else 'warn', k, source,
                           f"{st['label']}: {cur.get('value')}", transition=True)

    def retain(self, device_ids: Set[str], seen: Optional[Set[str]] = None,
               evaluated: Optional[Set[str]] = None) -> None:
        """Forget the alarms of devices that went away, and those a device's
        template no longer declares (``seen`` this round on an ``evaluated``
        device). A device that is only down keeps its state: back up with the
        same fault, nothing is re-sent; back up healthy, the clear goes out."""
        for k in list(self.state):
            dev = k.split(':', 2)[1]
            if dev not in device_ids or (seen is not None and evaluated and dev in evaluated and k not in seen):
                self.state.pop(k, None)
