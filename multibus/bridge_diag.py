# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bus diagnostics: what our serial bridge measures on each bus, next to what
the gateway itself sees there, and one sentence that says where the trouble
is (ADR-0017).

The bridge reports per served port (docs/serial-bridge-api.md): the kernel's
line counters (frame, parity, overrun, break), the TCP client and its
connects, bytes per direction and how long the bus has been silent. The
gateway counts per device: answers, timeouts, connection errors, exceptions.
Put side by side over the same window, they point at one place:

    timeouts + a clean line + TCP reconnects → the network between the
                                               gateway and the bridge
    timeouts/CRC + frame or parity errors    → RS-485 wiring, termination, baud
    timeouts + nothing back from the bus     → the device is off or disconnected
    UART overrun                             → the bridge's host is too busy

``diagnose()`` is that table, pure and tested on its own. ``BridgeDiag``
polls each bridge every ``POLL_S`` in one background thread (never from a
request), keeps a short history per bus, and from it: the rates the Devices
page shows, the alerts (off unless the ``bridge`` signal is on), and the
per-bus health published on MQTT and InfluxDB.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

POLL_S = 30.0                # one /adapters + one /events per bridge per poll
WINDOW_S = 300.0             # rates and the diagnosis are over the last 5 minutes
HISTORY_S = 900.0            # samples kept per bus
TIMEOUT_S = 4.0
EVENTS_KEPT = 100            # per bridge, newest last

LINE_ERRORS = ("frame", "parity", "overrun", "brk", "buf_overrun")
WIRING = ("frame", "parity", "brk")
HOST = ("overrun", "buf_overrun")

# conservative: a healthy RS-485 bus has no line errors at all, a gateway
# keeps one connection, and a slave answers within a poll interval
DEFAULT_THRESHOLDS = {"line_errors_per_min": 10.0, "silent_s": 120.0,
                      "reconnects_per_10min": 6}
THRESHOLD_KEYS = tuple(DEFAULT_THRESHOLDS)

# the diagnosis codes, most decisive first (the first that holds is the line)
CODES = ("no_data", "unsupported", "collecting", "host_overrun", "wiring", "device_silent", "network",
         "timeouts", "line_noise", "ok")
SEVERITY = {"no_data": "unknown", "unsupported": "unknown", "collecting": "unknown", "host_overrun": "warn",
            "wiring": "warn", "device_silent": "warn", "network": "warn",
            "timeouts": "warn", "line_noise": "info", "ok": "ok"}


# ── the correlation table ────────────────────────────────────────────────────
def _n(d: Optional[Dict], *keys) -> Optional[int]:
    """Sum of the keys present in d (None when d is None or none is known)."""
    if not d:
        return None
    vals = [d.get(k) for k in keys if d.get(k) is not None]
    return sum(vals) if vals else None


def diagnose(w: Dict[str, Any]) -> Dict[str, Any]:
    """One bus over one window → {code, severity, confidence, findings,
    evidence}. Pure.

    ``w``: window_s; reachable (the bridge answered); mbg (the gateway's
    counters on this bus over the window: ok, timeouts, connection,
    exceptions, other, crc — None when no device of the gateway reads it);
    line (line-error deltas, None when the adapter does not count them);
    bytes (from_device / to_device deltas, None when unknown); connects (new
    TCP clients in the window); silent_s (since the last byte from the bus);
    client (a client is connected now); supported (False: the bridge has
    no diagnostics); stats_known (False: no sample of the port yet)."""
    if not w.get("reachable"):
        return _verdict("no_data", [], {}, "high")
    if w.get("supported") is False:
        # a bridge older than diagnostics: nothing is known about the line,
        # which is not the same as a healthy one (review E1b #1)
        return _verdict("unsupported", [], {}, "high")
    if not w.get("window_s") or w.get("stats_known") is False:
        return _verdict("collecting", [], {}, "high")
    mbg = w.get("mbg")
    line = w.get("line")
    b = w.get("bytes") or {}
    timeouts = _n(mbg, "timeouts") or 0
    conn_err = _n(mbg, "connection") or 0
    garbled = _n(mbg, "crc", "other") or 0
    answered = _n(mbg, "ok") or 0
    wiring = _n(line, *WIRING)
    host = _n(line, *HOST)
    connects = w.get("connects") or 0
    rx, tx = b.get("from_device"), b.get("to_device")
    silent = w.get("silent_s")
    window = float(w["window_s"])
    line_known = line is not None
    ev = {"timeouts": timeouts, "connection_errors": conn_err, "garbled": garbled,
          "answered": answered, "line_wiring": wiring, "line_overrun": host,
          "connects": connects, "rx_bytes": rx, "tx_bytes": tx, "silent_s": silent}
    findings: List[str] = []
    if host:
        findings.append("host_overrun")
    if wiring and (timeouts or garbled):
        findings.append("wiring")
    # asked and nothing came back at all: the bus is dead quiet (a device that
    # answers some and not others still sends bytes)
    nothing_back = (tx or 0) > 0 and rx == 0
    long_silent = silent is not None and silent >= min(window, 60.0) and (tx or 0) > 0
    if timeouts and (nothing_back or long_silent) and not wiring:
        findings.append("device_silent")
    line_clean = line_known and not wiring and not host
    if (timeouts or conn_err) and connects >= 2 and "device_silent" not in findings \
            and (line_clean or not line_known):
        findings.append("network")
    if timeouts and not findings:
        findings.append("timeouts")
    if wiring and "wiring" not in findings:
        findings.append("line_noise")
    if not findings:
        findings.append("ok")
    code = findings[0]
    # how sure: a network verdict without line counters cannot rule the line out
    confidence = "low" if (code == "network" and not line_known) or \
        (code == "ok" and not line_known and mbg is None) else "high"
    return _verdict(code, findings, ev, confidence)


def _verdict(code: str, findings: List[str], ev: Dict, confidence: str) -> Dict[str, Any]:
    return {"code": code, "severity": SEVERITY[code], "confidence": confidence,
            "findings": findings or [code], "evidence": ev}


# ── counters → window deltas ─────────────────────────────────────────────────
def _delta(cur: Optional[int], old: Optional[int]) -> Optional[int]:
    """A cumulative counter's growth. A counter that went down restarted
    (adapter replugged, bridge restarted): a new base with no growth — never
    a spike of its whole value (review E1b #7)."""
    if cur is None or old is None:
        return None
    return cur - old if cur >= old else 0


def mbg_counts(stats_list: List[Dict]) -> Dict[str, Any]:
    """The gateway's own cumulative counters for one bus: every device on it,
    summed (get_stats() of each), and the bus's longest poll interval. Pure."""
    out = {"ok": 0, "timeouts": 0, "connection": 0, "exceptions": 0, "other": 0, "crc": 0,
           "devices": 0, "max_interval_s": None}
    for st in stats_list:
        if not st:
            continue
        out["devices"] += 1
        ec = st.get("error_counts") or {}
        out["ok"] += int(st.get("successful_reads") or 0)
        out["timeouts"] += int(ec.get("timeout") or 0)
        out["connection"] += int(ec.get("connection") or 0)
        out["other"] += int(ec.get("other") or 0)
        out["crc"] += int(ec.get("crc_errors") or 0)
        out["exceptions"] += sum(int(v or 0) for k, v in ec.items()
                                 if str(k).startswith("exception_"))
        for g in st.get("poll_groups_detail") or []:
            iv = g.get("interval") if isinstance(g, dict) else None
            if isinstance(iv, (int, float)) and not isinstance(iv, bool) and iv > 0:
                out["max_interval_s"] = max(out["max_interval_s"] or 0.0, float(iv))
    return out


def window_of(samples: List[Dict], now: float, window_s: float = WINDOW_S) -> Dict[str, Any]:
    """The evidence for diagnose() from a bus's samples (oldest first): the
    newest against the oldest one inside the window. Pure."""
    if not samples:
        return {"window_s": 0}
    new = samples[-1]
    old = next((s for s in samples if new["ts"] - s["ts"] <= window_s), new)
    span = new["ts"] - old["ts"]
    st_new, st_old = new.get("stats") or {}, old.get("stats") or {}
    ln_new, ln_old = st_new.get("line"), st_old.get("line")
    line = None
    if ln_new is not None and ln_old is not None:
        line = {f: _delta(ln_new.get(f), ln_old.get(f)) for f in LINE_ERRORS}
        if all(v is None for v in line.values()):
            line = None
    by_new, by_old = st_new.get("bytes") or {}, st_old.get("bytes") or {}
    bytes_ = ({k: _delta(by_new.get(k), by_old.get(k)) for k in ("from_device", "to_device")}
              if by_new and by_old else None)
    mbg = None
    if new.get("mbg") and old.get("mbg") and new["mbg"].get("devices"):
        mbg = {k: _delta(new["mbg"].get(k), old["mbg"].get(k))
               for k in ("ok", "timeouts", "connection", "exceptions", "other", "crc")}
    connects = _delta(st_new.get("connects"), st_old.get("connects"))
    return {"window_s": round(span, 1), "reachable": True, "mbg": mbg, "line": line,
            "bytes": bytes_, "connects": connects,
            "silent_s": st_new.get("silent_s"),
            "client": bool(st_new.get("client")) if st_new else None,
            "stats_known": bool(new.get("stats")),
            # how far back this bus's history goes (the alerts need a full window)
            "history_s": round(new["ts"] - samples[0]["ts"], 1),
            "poll_interval_s": (new.get("mbg") or {}).get("max_interval_s")}


def rates(w: Dict[str, Any]) -> Dict[str, Any]:
    """Per-minute rates of a window (None where unknown). Pure."""
    span = float(w.get("window_s") or 0)
    if span <= 0:
        return {}
    per_min = 60.0 / span
    line = w.get("line")
    out: Dict[str, Any] = {"window_s": span}
    if line is not None:
        errs = [v for v in line.values() if v is not None]
        out["line_errors_per_min"] = round(sum(errs) * per_min, 2) if errs else None
        for f, v in line.items():
            out[f"{f}_per_min"] = None if v is None else round(v * per_min, 2)
    else:
        out["line_errors_per_min"] = None
    b = w.get("bytes") or {}
    for k in ("from_device", "to_device"):
        v = b.get(k)
        out[f"bytes_{k}_per_min"] = None if v is None else round(v * per_min, 1)
    mbg = w.get("mbg") or {}
    for k in ("timeouts", "connection", "ok"):
        v = mbg.get(k)
        out[f"mbg_{k}_per_min"] = None if v is None else round(v * per_min, 2)
    return out


# ── thresholds ───────────────────────────────────────────────────────────────
def thresholds_for(global_cfg: Optional[Dict], bridge: Dict, port: int) -> Dict[str, Any]:
    """The alert thresholds of one bus: the port entry's ``alerts`` over the
    bridge's ``alerts`` over ``alerts.bridge`` over the defaults. ``enabled:
    false`` at any level mutes that bus. Pure."""
    out: Dict[str, Any] = dict(DEFAULT_THRESHOLDS, enabled=True)
    entry = next((p for p in bridge.get("ports") or []
                  if str(p.get("port")) == str(port)), {}) or {}
    for layer in (global_cfg or {}, bridge.get("alerts") or {}, entry.get("alerts") or {}):
        for k in THRESHOLD_KEYS:
            if layer.get(k) is not None:
                try:
                    out[k] = float(layer[k])
                except (TypeError, ValueError):
                    pass
        if "enabled" in layer:
            out["enabled"] = bool(layer["enabled"])
    return out


def validate_thresholds(raw: Any, where: str) -> List[str]:
    """Errors in words for an ``alerts`` mapping on a bridge or a port."""
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return [f"{where}: a mapping of {', '.join(THRESHOLD_KEYS)}, enabled"]
    errs = []
    for k, v in raw.items():
        if k == "enabled":
            if not isinstance(v, bool):
                errs.append(f"{where}.enabled: true or false")
        elif k in THRESHOLD_KEYS:
            try:
                if float(v) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                errs.append(f"{where}.{k}: a number above 0")
        else:
            errs.append(f"{where}.{k}: unknown (one of {', '.join(THRESHOLD_KEYS)}, enabled)")
    return errs


FIRE_AFTER = 2              # consecutive polls over a limit before an alert
CLEAR_AFTER = 2             # consecutive polls under CLEAR_RATIO × limit before "back to normal"
CLEAR_RATIO = 0.8


def alert_levels(w: Dict[str, Any], th: Dict[str, Any]) -> Dict[str, Optional[Tuple[float, float, str]]]:
    """{line | silent | reconnects: (value, limit, message) — or None when it
    cannot be judged now}. Pure.

    - line errors and reconnects are rates: judged only once the bus has a
      full window of history (WINDOW_S), so one reconnect in the first 30 s
      after a restart is not "10 per 10 minutes";
    - a silent bus counts only while somebody asks it something, and never
      below twice the bus's longest poll interval (a device read every 4
      minutes is silent for 4 minutes between reads)."""
    out: Dict[str, Optional[Tuple[float, float, str]]] = {"line": None, "silent": None,
                                                          "reconnects": None}
    if not th.get("enabled", True) or not w.get("window_s") or not w.get("reachable", True):
        return out
    full = float(w.get("history_s", w.get("window_s")) or 0) >= WINDOW_S
    if full:
        lpm = rates(w).get("line_errors_per_min")
        if lpm is not None:
            lim = float(th["line_errors_per_min"])
            out["line"] = (lpm, lim, f"line errors {lpm:g}/min (frame, parity, overrun, break) "
                                     f"over {lim:g}/min")
        connects = w.get("connects")
        if connects is not None:
            per10 = round(connects * 600.0 / max(float(w["window_s"]), 60.0), 2)
            lim = float(th["reconnects_per_10min"])
            out["reconnects"] = (per10, lim, f"{connects} TCP reconnects in {w['window_s']:.0f} s "
                                             f"(limit {lim:g} per 10 min)")
    silent, tx = w.get("silent_s"), (w.get("bytes") or {}).get("to_device")
    if silent is not None and tx is not None:
        iv = w.get("poll_interval_s")
        lim = max(float(th["silent_s"]), 2.0 * float(iv) if iv else 0.0)
        val = float(silent) if tx > 0 else 0.0       # nobody asks: nothing is wrong
        out["silent"] = (val, lim, f"bus silent for {val:.0f} s while asked (limit {lim:g} s)")
    return out


def alert_conditions(w: Dict[str, Any], th: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """{line | silent | reconnects: message when over its limit now, else
    None}. Pure; one poll's view — the alerts add hysteresis (step())."""
    return {k: (v[2] if v is not None and v[0] > v[1] else None)
            for k, v in alert_levels(w, th).items()}


def step(state: Dict[str, Any], level: Optional[Tuple[float, float, str]]) -> Optional[str]:
    """One poll of one alert's hysteresis: "fire", "clear" or None. Fires
    after FIRE_AFTER polls in a row over the limit; clears after CLEAR_AFTER
    polls in a row under CLEAR_RATIO of it; in between, or when it cannot
    be judged, nothing moves. Pure but for `state`."""
    if level is None:
        return None
    val, lim, _msg = level
    if val > lim:
        state["over"], state["clean"] = state.get("over", 0) + 1, 0
        if not state.get("active") and state["over"] >= FIRE_AFTER:
            state["active"] = True
            return "fire"
    elif val < CLEAR_RATIO * lim:
        state["clean"], state["over"] = state.get("clean", 0) + 1, 0
        if state.get("active") and state["clean"] >= CLEAR_AFTER:
            state["active"] = False
            return "clear"
    else:
        state["over"] = state["clean"] = 0
    return None


# ── the poller ──────────────────────────────────────────────────────────────
MAX_BODY = 1 << 20          # a bridge's answer is a few kB; never read more than this


def _http_get(url: str, token: str, timeout: float = TIMEOUT_S) -> Dict[str, Any]:
    """GET a bridge's JSON: the body capped at MAX_BODY, the whole exchange
    bounded by `timeout` (a bridge that trickles bytes cannot hold the
    poller)."""
    deadline = time.monotonic() + timeout
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — operator-configured bridge
        body = b""
        while len(body) <= MAX_BODY:
            if time.monotonic() > deadline:
                raise TimeoutError(f"no complete answer within {timeout:g} s")
            chunk = r.read(65536)
            if not chunk:
                break
            body += chunk
        if len(body) > MAX_BODY:
            raise ValueError(f"answer larger than {MAX_BODY} bytes")
    data = json.loads(body.decode() or "{}")
    if not isinstance(data, dict):
        raise ValueError("answer is not a JSON object")
    return data


class BridgeDiag:
    """Polls every bridge whose kind is discovered through its API, keeps a
    short history per bus, and serves the cached result. Never polls from a
    request; one thread for all bridges."""

    def __init__(self, config, registry=None, alert_mgr=None,
                 publishers: Optional[Callable[[], Tuple[Any, Any]]] = None,
                 types: Optional[Dict[str, Dict]] = None,
                 fetch: Optional[Callable[[str, str], Dict]] = None,
                 clock: Callable[[], float] = time.time,
                 is_active: Optional[Callable[[], bool]] = None):
        from .bridges import TYPES
        self.config = config
        self.registry = registry
        self.alert_mgr = alert_mgr
        self.publishers = publishers or (lambda: (None, None))
        self.types = TYPES if types is None else types
        self.fetch = fetch or _http_get
        self.clock = clock
        if is_active is None:
            from . import role
            is_active = role.is_active
        self.is_active = is_active
        self._lock = threading.Lock()
        self._bridges: Dict[str, Dict[str, Any]] = {}   # bridge id → state
        self._alerting: Dict[str, Dict[str, Any]] = {}  # alert key → hysteresis state
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle --
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name="bridge-diag")
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(5.0 if not self._bridges else POLL_S):
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 — diagnostics never take the gateway down
                logger.warning("bridge diagnostics poll failed", exc_info=True)

    # -- what to poll --
    def _api_bridges(self) -> List[Dict]:
        return [b for b in (self.config.bridges or [])
                if self.types.get(str(b.get("type") or ""), {}).get("discovery") == "api"
                and b.get("host")]

    def _url(self, b: Dict, path: str) -> str:
        t = self.types.get(str(b.get("type") or ""), {})
        port = int(b.get("control_port") or t.get("control_port", 7000))
        return f"http://{b['host']}:{port}{path}"

    def _bus_stats(self, b: Dict, port: int) -> List[Dict]:
        """get_stats() of every device of the gateway that reads this bus."""
        ids = {d.id for d in self.config.bridge_devices(b["id"])
               if int(getattr(d.connection, "port", 0) or 0) == int(port)}
        host = str(b.get("host") or "")
        ids |= {d.id for d in self.config.devices
                if str(getattr(d.connection, "host", "") or "") == host
                and int(getattr(d.connection, "port", 0) or 0) == int(port)}
        out = []
        for d, c in (self.registry.pairs() if self.registry is not None else []):
            if d is not None and d.id in ids and c is not None and hasattr(c, "get_stats") \
                    and getattr(d, "enabled", True):
                try:
                    out.append(c.get_stats() or {})
                except Exception:  # noqa: BLE001
                    continue
        return out

    # -- one poll --
    def poll_once(self) -> None:
        now = self.clock()
        alive = set()
        for b in self._api_bridges():
            alive.add(b["id"])
            try:
                self._poll_bridge(b, now)
            except Exception:  # noqa: BLE001 — one bad bridge never stops the others
                logger.warning("bridge %s diagnostics poll failed", b.get("id"), exc_info=True)
        with self._lock:
            gone = {bid: self._bridges.pop(bid) for bid in [k for k in self._bridges if k not in alive]}
        for bid, st in gone.items():
            self._forget_alerts(bid)
            self._clear_published(bid, st)

    def _forget_alerts(self, bridge_id: str) -> None:
        """Drop a bridge's alert states without a "back to normal": it is gone,
        or its signal was switched off."""
        for k in [k for k in self._alerting if k.startswith(f"bridge:{bridge_id}:")]:
            self._alerting.pop(k, None)

    def _poll_bridge(self, b: Dict, now: float) -> None:
        bid = b["id"]
        with self._lock:
            st = self._bridges.setdefault(bid, {"ports": {}, "events": deque(maxlen=EVENTS_KEPT),
                                                "since": 0, "boot": None, "error": "",
                                                "fetched_ts": None, "supported": None})
        try:
            data = self.fetch(self._url(b, "/adapters"), b.get("token", ""))
        except Exception as e:  # noqa: BLE001
            with self._lock:
                st["error"] = f"no answer from {b.get('host')}: {e}"
                st["fetched_ts"] = now
            for port in list(st["ports"]):
                self._evaluate(b, port, now, reachable=False)
            return
        if not isinstance(data, dict):
            raise ValueError("the bridge's /adapters is not a JSON object")
        diag = data.get("diagnostics") if isinstance(data.get("diagnostics"), dict) else {}
        supported = bool(diag)
        boot = diag.get("boot")
        with self._lock:
            if boot and st["boot"] and boot != st["boot"]:
                # the bridge restarted: its counters and event numbers began again
                st["since"] = 0
                for ps in st["ports"].values():
                    ps["samples"].clear()
            st.update(boot=boot, error="", fetched_ts=now, supported=supported,
                      version=data.get("version", ""))
        if supported:
            try:
                ev = self.fetch(self._url(b, f"/events?since={int(st['since'])}"), b.get("token", ""))
                with self._lock:
                    evs = ev.get("events") if isinstance(ev, dict) else None
                    for e in evs if isinstance(evs, list) else []:
                        if isinstance(e, dict):
                            st["events"].append(e)
                    st["since"] = int(ev.get("next") or st["since"])
                    st["events_lost"] = bool(ev.get("lost"))
            except Exception as e:  # noqa: BLE001 — the stats still count
                logger.debug("bridge %s events: %s", bid, e)
        adapters = data.get("adapters") if isinstance(data.get("adapters"), list) else []
        for a in adapters:
            if not isinstance(a, dict) or a.get("status", "ok") != "ok":
                continue
            port = a.get("tcp_port")
            if isinstance(port, bool) or not isinstance(port, (int, str)) \
                    or not str(port).isdigit() or not 0 < int(port) < 65536:
                continue
            port = int(port)
            if not isinstance(a.get("stats"), dict):
                a = {**a, "stats": None}
            sample = {"ts": now, "stats": a.get("stats"), "adapter": a.get("stable_id", ""),
                      "mbg": mbg_counts(self._bus_stats(b, port))}
            with self._lock:
                ps = st["ports"].setdefault(port, {"samples": deque()})
                ps["samples"].append(sample)
                while ps["samples"] and now - ps["samples"][0]["ts"] > HISTORY_S:
                    ps["samples"].popleft()
                ps["adapter"] = a.get("stable_id", "")
                ps["label"] = a.get("label", "")
                ps["driver"] = a.get("driver", "")
            self._evaluate(b, port, now, reachable=True)

    def _evaluate(self, b: Dict, port: int, now: float, reachable: bool) -> None:
        bid = b["id"]
        with self._lock:
            st = self._bridges.get(bid)
            ps = (st or {}).get("ports", {}).get(port)
            if ps is None:
                return
            samples = list(ps["samples"])
        w = window_of(samples, now) if reachable else {"reachable": False}
        if reachable and w.get("window_s") == 0 and samples:
            w["reachable"] = True
        if reachable:
            w["supported"] = (st or {}).get("supported")
        verdict = diagnose(w)
        r = rates(w) if reachable else {}
        with self._lock:
            ps["window"], ps["diagnosis"], ps["rates"] = w, verdict, r
        if not self.is_active():
            return                                   # a standby neither alerts nor publishes
        self._alerts(b, port, w)
        self._publish(b, port, ps, w, verdict, r, now)

    # -- alerts --
    def _alerts(self, b: Dict, port: int, w: Dict) -> None:
        am = self.alert_mgr
        if am is None or not getattr(am, "sig_bridge", False):
            self._forget_alerts(b["id"])              # switched off: no orphan "back to normal"
            return
        th = thresholds_for(getattr(am, "bridge_thresholds", None), b, port)
        levels = alert_levels(w, th)
        name = b.get("name") or b["id"]
        for kind in ("line", "silent", "reconnects"):
            key = f"bridge:{b['id']}:{port}:{kind}"
            state = self._alerting.setdefault(key, {})
            move = step(state, levels.get(kind))
            if move == "fire":
                am.fire("warn", key, f"{name} :{port}", levels[kind][2], transition=True)
            elif move == "clear":
                am.fire("info", key, f"{name} :{port}", f"{kind}: back to normal", transition=True)

    # -- MQTT + InfluxDB --
    def health_payload(self, b: Dict, port: int, ps: Dict, w: Dict, verdict: Dict,
                       r: Dict, now: float) -> Dict[str, Any]:
        stats = (ps["samples"][-1].get("stats") if ps.get("samples") else None) or {}
        return {"bridge": b["id"], "port": port, "adapter": ps.get("adapter", ""),
                "diagnosis": verdict["code"], "severity": verdict["severity"],
                "confidence": verdict["confidence"],
                "client": (stats.get("client") or {}).get("address") if stats.get("client") else None,
                "connects_10min": stats.get("connects_10min"),
                "silent_s": stats.get("silent_s"),
                "line": stats.get("line"), "line_note": stats.get("line_note", ""),
                "rates": r, "window_s": w.get("window_s"), "ts": round(now, 1)}

    def _publish(self, b, port, ps, w, verdict, r, now) -> None:
        mqtt, influx = self.publishers()
        payload = self.health_payload(b, port, ps, w, verdict, r, now)
        if verdict["code"] == "unsupported":
            influx = None                             # nothing measured: no point written
        if mqtt is not None and hasattr(mqtt, "publish_state"):
            try:
                mqtt.publish_state(f"bridges/{b['id']}/{port}/health", json.dumps(payload))
            except Exception:  # noqa: BLE001
                logger.debug("bridge health MQTT publish failed", exc_info=True)
        if influx is not None and hasattr(influx, "write_point") and w.get("reachable"):
            try:
                influx.write_point(self.influx_point(payload), ts=now)
            except Exception:  # noqa: BLE001
                logger.debug("bridge health InfluxDB write failed", exc_info=True)

    @staticmethod
    def influx_point(p: Dict[str, Any]):
        from influxdb_client import Point
        pt = (Point("bridge_port").tag("bridge", p["bridge"]).tag("port", str(p["port"]))
              .tag("adapter", p.get("adapter") or "").field("diagnosis", p["diagnosis"]))
        for k, v in (p.get("line") or {}).items():
            if isinstance(v, int):
                pt = pt.field(f"line_{k}", v)
        for k, v in (p.get("rates") or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                pt = pt.field(k, float(v))
        for k in ("connects_10min", "silent_s"):
            if isinstance(p.get(k), (int, float)):
                pt = pt.field(k, float(p[k]))
        return pt.field("client_connected", 1 if p.get("client") else 0)

    def _clear_published(self, bid: str, st: Dict) -> None:
        if not self.is_active():
            return                                    # a standby does not touch the broker
        mqtt, _ = self.publishers()
        if mqtt is None or not hasattr(mqtt, "publish_state"):
            return
        for port in st.get("ports", {}):
            try:
                mqtt.publish_state(f"bridges/{bid}/{port}/health", "")
            except Exception:  # noqa: BLE001
                pass

    # -- the views the routes serve --
    def port_view(self, bridge_id: str, port: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            st = self._bridges.get(bridge_id)
            ps = (st or {}).get("ports", {}).get(int(port))
            if ps is None:
                return None
            last = ps["samples"][-1] if ps["samples"] else {}
            sid = ps.get("adapter", "")
            events = [e for e in (st or {}).get("events", []) if e.get("key") == sid
                      or e.get("tcp_port") == int(port)][-10:]
            return {"adapter": sid, "label": ps.get("label", ""), "driver": ps.get("driver", ""),
                    "stats": last.get("stats"), "sampled_ts": last.get("ts"),
                    "mbg": (ps.get("window") or {}).get("mbg"),
                    "rates": ps.get("rates") or {},
                    "diagnosis": ps.get("diagnosis") or diagnose({"reachable": False}),
                    "events": events, "error": st.get("error", ""),
                    "supported": st.get("supported")}

    def bridge_view(self, bridge_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            st = self._bridges.get(bridge_id)
            if st is None:
                return None
            ports = list(st["ports"])
            out = {"fetched_ts": st.get("fetched_ts"), "error": st.get("error", ""),
                   "supported": st.get("supported"), "version": st.get("version", ""),
                   "events": list(st["events"])[-30:], "events_lost": st.get("events_lost", False)}
        out["ports"] = {str(p): self.port_view(bridge_id, p) for p in ports}
        return out
