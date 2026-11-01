# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bridge guard: a device reached through our serial bridge never reads a bus
other than the one it was bound to (ADR-0016, FINDINGS BR-E1-11).

The serial bridge gives each adapter a stable TCP port, and the gateway
dials host:port. If the bridge ever hands that port to another adapter — its
port map lost with its volume, a restore, a hand edit, a narrowed port range
— the gateway would read and WRITE the wrong RS-485 bus without noticing:
the frames are valid Modbus either way.

So a bus remembers WHICH adapter it is (the bridge's ``stable_id``):
    - a bridge record's port entry carries ``adapter`` (all devices on that bus);
    - a device reaching the bridge by host:port carries ``adapter`` in its
      connection.
Both reach ``ModbusConfig.adapter``. Before every fresh connect to a host that
is one of our bridges, ``allow()`` asks the bridge's /adapters which adapter
serves that port now:
    - the same one → connect;
    - another one, or none → refuse: the device stays off the wire, in a clear
      state ("moved to :7004" / "missing"), until the operator follows the
      adapter (one click) or puts it back;
    - nothing recorded yet (configs from before this) → connect as before, and
      record what serves the port now (backfill), so the next change is caught.
Only positive evidence refuses: ``moved`` or ``missing``, from a bridge
that answered. A bus whose bridge cannot be asked (its control API down, a
token or firewall problem on the control port — which fails independently of
the data ports) keeps reading on its recorded binding, flagged ``unverified``
as a warning, and is checked again on the next connect once the API answers.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

CACHE_S = 3.0                # one /adapters answer serves a reconnect burst
TIMEOUT_S = 3.0

_lock = threading.RLock()
_bridges: Dict[str, Dict] = {}              # host → {url, token, bridge}
_learned: Dict[Tuple[str, int], str] = {}   # backfilled bindings (this run)
_state: Dict[Tuple[str, int], Dict] = {}    # last verdict per bus
_cache: Dict[str, Tuple[float, Optional[List[Dict]], str]] = {}
_recorder: Optional[Callable[[str, int, str], None]] = None
_fetch: Optional[Callable[[str, str], List[Dict]]] = None   # tests swap it


def configure(raw_bridges: List[Dict], types: Dict[str, Dict]) -> None:
    """Which hosts are our serial bridges: every bridge record whose kind is
    discovered through its API, and the same-host bridge (SERIAL_BRIDGE_URL)."""
    out: Dict[str, Dict] = {}
    legacy = os.environ.get("SERIAL_BRIDGE_URL", "http://mbg-serial-bridge:7000")
    host = urlparse(legacy).hostname
    if host:
        out[host] = {"url": legacy.rstrip("/"), "token": os.environ.get("SERIAL_BRIDGE_TOKEN", ""),
                     "bridge": ""}
    for b in raw_bridges or []:
        t = types.get(str(b.get("type") or ""), {})
        if t.get("discovery") != "api" or not b.get("host"):
            continue
        port = int(b.get("control_port") or t.get("control_port", 7000))
        out[str(b["host"])] = {"url": f"http://{b['host']}:{port}", "token": b.get("token", ""),
                               "bridge": b.get("id", "")}
    with _lock:
        _bridges.clear()
        _bridges.update(out)


def set_recorder(fn: Optional[Callable[[str, int, str], None]]) -> None:
    """Who stores a backfilled binding (the config)."""
    global _recorder
    _recorder = fn


def is_bridge(host: str) -> bool:
    return host in _bridges


def _get_adapters(url: str, token: str) -> List[Dict]:
    if _fetch is not None:
        return _fetch(url, token)
    req = urllib.request.Request(f"{url}/adapters")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:  # noqa: S310 — our bridge
        return json.loads(r.read().decode()).get("adapters") or []


def _adapters(host: str) -> Tuple[Optional[List[Dict]], str]:
    b = _bridges[host]
    now = time.monotonic()
    hit = _cache.get(host)
    if hit and now - hit[0] < CACHE_S:
        return hit[1], hit[2]
    try:
        got, err = _get_adapters(b["url"], b["token"]), ""
    except Exception as e:  # noqa: BLE001
        got, err = None, str(e)
    _cache[host] = (now, got, err)
    return got, err


def check(host: str, port: int, expected: str) -> Dict:
    """The verdict for one bus: {allowed, status, adapter, now_at, detail}.
    status: ok | moved | missing | unverified | unbound."""
    adapters, err = _adapters(host)
    base = {"host": host, "port": port, "adapter": expected, "now_at": None,
            "bridge": _bridges[host].get("bridge", "")}
    if adapters is None:
        if not expected:
            return {**base, "allowed": True, "status": "unbound",
                    "detail": f"the bridge's control API did not answer ({err}); nothing recorded "
                              f"for :{port}, so it is read as before"}
        # no evidence either way: keep reading on the recorded binding (a
        # control-port problem must not stop production reads), say so
        return {**base, "allowed": True, "status": "unverified",
                "detail": f"cannot ask the bridge which adapter serves :{port} ({err}) — "
                          f"not verified, still reading as {expected}"}
    served = {int(a["tcp_port"]): str(a.get("stable_id") or "") for a in adapters
              if a.get("tcp_port") and a.get("status", "ok") == "ok"}
    here = served.get(int(port), "")
    if not expected:
        if here:
            return {**base, "allowed": True, "status": "ok", "adapter": here, "backfill": True,
                    "detail": f":{port} is {here} (recorded now)"}
        return {**base, "allowed": True, "status": "unbound",
                "detail": f"no adapter serves :{port} now"}
    if here == expected:
        return {**base, "allowed": True, "status": "ok", "detail": f":{port} is {expected}"}
    now_at = next((p for p, sid in served.items() if sid == expected), None)
    listed = next((a for a in adapters if a.get("stable_id") == expected), None)
    if now_at is not None:
        detail = (f"the adapter {expected} is now on :{now_at}"
                  + (f", and :{port} is {here}" if here else "")
                  + " — not read; follow the adapter to read it there")
        return {**base, "allowed": False, "status": "moved", "now_at": now_at, "found": here,
                "detail": detail}
    if listed is not None:
        detail = (f"the adapter {expected} is on the bridge but not served "
                  f"({listed.get('status')}: {listed.get('reason') or '—'})")
    else:
        detail = f"the adapter {expected} is not on the bridge (unplugged?)"
    if here:
        detail += f"; :{port} is {here} now — not read"
    return {**base, "allowed": False, "status": "missing", "found": here, "detail": detail}


def allow(cfg) -> bool:
    """May this connection open its socket now? Called before every FRESH
    connect, never per read. A host that is not one of our bridges: always."""
    host = str(getattr(cfg, "host", "") or "")
    if host not in _bridges:
        return True
    try:
        port = int(getattr(cfg, "port", 0))
    except (TypeError, ValueError):
        return True
    with _lock:
        expected = str(getattr(cfg, "adapter", "") or "") or _learned.get((host, port), "")
        st = check(host, port, expected)
        prev = _state.get((host, port))
        _state[(host, port)] = {**st, "ts": time.time()}
        if st.get("backfill"):
            _learned[(host, port)] = st["adapter"]
    if not prev or prev.get("status") != st["status"]:
        log = logger.info if st["allowed"] else logger.error
        log("bridge guard %s:%s — %s", host, port, st["detail"])
    if st.get("backfill") and _recorder is not None:
        try:
            _recorder(host, port, st["adapter"])
        except Exception:  # noqa: BLE001 — a failed save is retried next connect
            logger.warning("bridge guard: could not record %s:%s = %s", host, port,
                           st["adapter"], exc_info=True)
    return bool(st["allowed"])


def state(host: str, port: int) -> Optional[Dict]:
    return _state.get((host, int(port)))


def problems() -> List[Dict]:
    """Every bus the guard keeps off the wire now, and every one it could
    not verify (still read: allowed is true — a warning)."""
    with _lock:
        return [dict(v) for v in _state.values()
                if not v.get("allowed") or v.get("status") == "unverified"]


def moves(host: str) -> Dict[int, int]:
    """{old port: new port} for every bus of this bridge whose adapter is
    now on another port — applied together, so two swapped buses swap."""
    with _lock:
        return {p: st["now_at"] for (h, p), st in _state.items()
                if h == host and st.get("status") == "moved" and st.get("now_at")}


def forget(host: str, ports=None) -> None:
    """Drop verdicts and the cache after a re-bind: the next connect checks anew."""
    with _lock:
        for k in [k for k in _state if k[0] == host and (ports is None or k[1] in ports)]:
            _state.pop(k, None)
        for k in [k for k in _learned if k[0] == host and (ports is None or k[1] in ports)]:
            _learned.pop(k, None)
        _cache.pop(host, None)


def reset() -> None:
    """Tests: forget everything."""
    with _lock:
        _bridges.clear()
        _learned.clear()
        _state.clear()
        _cache.clear()
