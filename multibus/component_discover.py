# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Finding the components a device holds by listening to its MQTT topics:
the one way, for a saved device (through its own link) and for one not saved
yet (through a short-lived link of its own, ADR-0031 §5).

Read only: it subscribes, listens for a bounded time and unsubscribes. It
publishes nothing itself — a saved device's own keepalive may be asked for
by the caller, as its input already sends it."""
from __future__ import annotations

import json
import re
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

MIN_S, MAX_S, DEFAULT_S = 1.0, 10.0, 4.0
CONNECT_S = 5.0
# searches before saving at once: each holds a connection for seconds
MAX_AT_ONCE = 2
SLOTS = threading.BoundedSemaphore(MAX_AT_ONCE)


def discoverable(types: Dict[str, Dict]) -> Dict[str, Dict]:
    """The component types a template says how to find."""
    return {k: c for k, c in (types or {}).items()
            if isinstance(c.get('discover'), dict) and c['discover'].get('topic')}


def _text(payload) -> str:
    raw = (payload or b'').decode('utf-8', 'replace').strip()
    try:
        v = json.loads(raw)
        v = v.get('value') if isinstance(v, dict) else v
    except ValueError:
        v = raw
    return '' if v is None else str(v).strip()[:64]


def listen(link, types: Dict[str, Dict], tvars: Dict[str, str], instance_re,
           seconds: float = DEFAULT_S, before: Optional[Callable[[], Any]] = None,
           wait: Optional[Callable[[float], Any]] = None) -> List[Dict[str, str]]:
    """Tap the types' discover topics on ``link`` for ``seconds`` and say
    what answered: [{type, instance, product, name}] in a stable order.
    ``before`` runs once the taps are in place (a saved device's keepalive);
    ``wait(seconds)`` is how the window passes (a cancellable wait for the
    read preview; ``time.sleep`` otherwise)."""
    def fill(t: str) -> str:
        return re.sub(r'\{([A-Za-z0-9_]+)\}', lambda m: tvars.get(m.group(1), m.group(0)), t)

    found: Dict[Tuple[str, str], Dict[str, str]] = {}
    taps = []
    lock = threading.Lock()
    for ctype, c in types.items():
        for key, tmpl in (('product', c['discover']['topic']),
                          ('name', c['discover'].get('name_topic') or '')):
            if not tmpl:
                continue
            pattern = fill(tmpl).replace('{instance}', '+')
            idx = pattern.split('/').index('+') if '+' in pattern.split('/') else -1
            if idx < 0:
                continue

            def cb(msg, ctype=ctype, key=key, idx=idx):
                parts = msg.topic.split('/')
                inst = parts[idx] if idx < len(parts) else ''
                if not instance_re.match(inst):
                    return
                with lock:
                    found.setdefault((ctype, inst), {})[key] = _text(msg.payload)
            link.tap(pattern, cb)
            taps.append((pattern, cb))
    try:
        if before is not None:
            before()
        (wait or time.sleep)(seconds)
    finally:
        for pattern, cb in taps:
            link.untap(pattern, cb)

    def order(k):
        return (k[0], int(k[1]) if k[1].isdigit() else 10 ** 9, k[1])
    out = []
    for (ctype, inst) in sorted(found, key=order):
        v = found[(ctype, inst)]
        if not v.get('product') and not v.get('name'):
            continue                    # a service that has gone
        out.append({'type': ctype, 'instance': inst, 'product': v.get('product', ''), 'name': v.get('name', '')})
    return out


def seconds_of(raw) -> float:
    """The listening time asked for, bounded."""
    try:
        s = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_S
    return DEFAULT_S if s != s else min(MAX_S, max(MIN_S, s))


def open_link(conn: Dict[str, Any]):
    """A short-lived link to an unsaved device's broker, connected, or None
    (it could not be reached within CONNECT_S, or TLS could not be set up).
    A None comes only after the whole CONNECT_S, whatever happened — a port
    refused at once and one that never answers look the same (no probe of
    the LAN through this route). The caller closes it (``close``)."""
    from . import mqtt_link
    t0 = time.monotonic()

    def none():
        left = CONNECT_S - (time.monotonic() - t0)
        if left > 0:
            time.sleep(left)
        return None
    m = SimpleNamespace(broker=str(conn.get('broker', '')).strip(), port=int(conn.get('port', 1883) or 1883),
                        username=str(conn.get('username', '') or ''), password=str(conn.get('password', '') or ''),
                        tls=bool(conn.get('tls', False)), tls_ca=str(conn.get('tls_ca', '') or ''),
                        tls_insecure=bool(conn.get('tls_insecure', False)))
    link = mqtt_link.MqttLink(("@discover",), m)
    try:
        started = link.start()
    except Exception:  # noqa: BLE001 — a bad address: the same answer
        started = False
    if not started:
        return none()
    end = t0 + CONNECT_S
    while not link.connected and time.monotonic() < end:
        time.sleep(0.1)
    if not link.connected:
        close(link)
        return none()
    return link


def close(link) -> None:
    try:
        link._close()
    except Exception:  # noqa: BLE001
        pass
