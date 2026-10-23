# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The client of a device that IS its meters (``connection: {protocol: none}``).

A UPS, a boiler, a charger with no communication of its own is measured by
the meters it names (meter_links.py) and nothing else: what they give it is
computed when they report (CalcEngine.run_linked). This client has no
transport, reads nothing and writes nothing; it only answers the questions
every client answers — is it alive, how healthy — from its meters, so the
availability topic, the HA connectivity entity, the alert harvester and the
status page work unchanged: online when its meters are, never a fake host
that is always offline.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional

# how the gateway finds a meter's running client (set by the API at start:
# registry.find(meter_id)[2]); None until then — the device reads as down
find_client: Optional[Callable[[str], Any]] = None


class MeterOnlyClient:
    """No transport: alive when its meters are."""

    meter_only = True

    def __init__(self, config: Any, device_id: str):
        self._config = config
        self.device_id = device_id
        self.parts: List = []
        self.events: deque = deque(maxlen=50)
        self._publish_callback = None

    # ── the client surface, with nothing behind it ──────────────────────────
    @property
    def publish_callback(self):
        return self._publish_callback

    @publish_callback.setter
    def publish_callback(self, cb):
        self._publish_callback = cb

    sources: List = []
    primary = None
    connection = None

    def connect(self) -> bool:
        return True

    def start_polling(self) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def standby(self) -> None:
        return None

    def resume(self) -> None:
        return None

    def update_registers(self, *a, **kw) -> None:
        return None

    def reload_registers(self, *a, **kw) -> None:
        return None

    def note_source_transitions(self) -> None:
        return None

    def write_value(self, *a, **kw):
        return False, "a meter-only device has nothing to write to", None

    # ── its meters ──────────────────────────────────────────────────────────
    def meters(self) -> List[str]:
        from .meter_links import links_of
        dev = self._config.get_device(self.device_id)
        return list(dict.fromkeys(ln['device'] for ln in links_of(dev))) if dev is not None else []

    def _meter_states(self) -> Dict[str, bool]:
        from .device_registry import client_is_live
        out = {}
        for mid in self.meters():
            client = find_client(mid) if find_client is not None else None
            out[mid] = client_is_live(client)
        return out

    def _meter_last_seen(self, ids) -> Optional[float]:
        stamps = []
        for mid in ids:
            client = find_client(mid) if find_client is not None else None
            try:
                ts = (client.get_stats() or {}).get('last_success_ts') if client is not None else None
            except Exception:  # noqa: BLE001 — a meter that cannot say counts as unknown
                ts = None
            if ts is None:
                return None
            stamps.append(float(ts))
        return min(stamps) if stamps else None

    @property
    def connected(self) -> bool:
        return any(self._meter_states().values())

    def data_health(self, *a, **kw) -> Dict:
        """``ok`` when every meter is live, ``degraded`` when some are,
        ``down`` when none (or it names none)."""
        states = self._meter_states()
        live = [m for m, ok in states.items() if ok]
        status = ('ok' if states and len(live) == len(states)
                  else 'degraded' if live else 'down')
        # its last read is its OLDEST live meter's: what it gives needs both
        # (never "now" — that would say fresh while a meter lags)
        seen = self._meter_last_seen(live)
        age = round(max(0.0, time.time() - seen), 1) if seen is not None else None
        return {'status': status, 'stale': status == 'down', 'staleness_age_s': age,
                'last_success_ts': seen, 'connected': bool(live),
                'sources': {m: ('ok' if ok else 'down') for m, ok in states.items()},
                'sources_down': [m for m, ok in states.items() if not ok],
                'measured_by': list(states)}

    def get_stats(self) -> Dict:
        h = self.data_health()
        return {'connected': h['connected'], 'successful_reads': 0, 'failed_reads': 0,
                'poll_rate': 0.0, 'last_success_ts': h['last_success_ts'], 'staleness_age_s': h['staleness_age_s'],
                'error_counts': {}, 'sources': [], 'poll_groups_detail': [],
                'meter_only': True, 'measured_by': h['measured_by']}
