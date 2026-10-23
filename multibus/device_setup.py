# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Setup writes: what a device must be told before some of its registers exist.

Some devices expose part of their map only after being configured over the
same bus. A Samsung MIM-B19N, for one, shows its compressor frequency, flow
or pressures only once the NASA message ids are written to 6000+/7000+; until
then those addresses answer "illegal data address", and nothing says whether
the module keeps them over a power cut. Other devices need a unit or a mode
written once before they report.

The TEMPLATE declares it — nothing about any vendor lives here:

    "setup": [{"name": "outdoor_messages", "label": "...", "address": 6000,
               "values": ["0x8001", "0x8238", ...], "provides": {"from": 4, "to": 35}}]

Each step goes out as one write-multiple frame (FC16). The gateway writes the
steps when it first talks to the device, again after the device comes back
from being unreachable (it may have rebooted), and again when a register the
step ``provides`` is refused with "illegal data address" — the device forgot.
Each run is spaced from the last, so a device that refuses for another reason
is not hammered. It writes only where writes are allowed (security.allow_writes,
the device's write lock); otherwise it says why and leaves the device alone.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

RETRY_S = 60.0          # after a failed run: the earliest next try
REARM_S = 120.0         # after a good run: how soon a refusal may trigger another
REARM_MAX_S = 3600.0    # …doubling while the provided registers keep being refused
                        # right after a good run (a model that simply lacks them)


def step_values(step: Dict[str, Any]) -> List[int]:
    """The step's words: ints, or strings like "0x8238" (readable in a map)."""
    out = []
    for v in step.get("values") or []:
        out.append(int(v, 0) if isinstance(v, str) else int(v))
    return out


def provides(step: Dict[str, Any]) -> Optional[tuple]:
    p = step.get("provides")
    if isinstance(p, dict) and p.get("from") is not None:
        lo = int(p["from"])
        return lo, int(p.get("to", lo))
    return None


class SetupRunner:
    """Runs a device's setup steps over its own connection, when needed."""

    def __init__(self, connection, steps: List[Dict[str, Any]], device_id: str = "",
                 gate: Optional[Callable[[], Optional[str]]] = None):
        self.connection = connection
        self.steps = [s for s in (steps or []) if step_values(s)]
        self.device_id = device_id
        self.gate = gate or (lambda: None)       # → None when writing is allowed, else why not
        self._lock = threading.Lock()
        self._done = False
        self._next_try = 0.0
        self._last_ok = 0.0
        self.runs = 0
        self.last_error = ""
        self.last_run_ts: Optional[float] = None
        self._rearm_s = REARM_S          # grows while refusals follow good runs
        self._gate_said = ""             # the gate's reason, logged once (not every minute)

    def __bool__(self) -> bool:
        return bool(self.steps)

    def ranges(self) -> List[tuple]:
        """The (from, to) address ranges the steps provide."""
        return [p for p in (provides(s) for s in self.steps) if p]

    def covers(self, address: int, count: int) -> bool:
        """Does a read of [address, address+count) touch what a step provides?"""
        for s in self.steps:
            p = provides(s)
            if p and address <= p[1] and address + max(1, count) - 1 >= p[0]:
                return True
        return False

    def invalidate(self, reason: str, force: bool = False) -> None:
        """The device may have forgotten: write the steps again (rate-limited).
        ``force`` (a new connection, an outage) skips the limit and the backoff."""
        with self._lock:
            if force:
                self._rearm_s = REARM_S
                if self._done:
                    self._done = False
                    logger.info("[%s] setup: %s — writing the setup steps again", self.device_id, reason)
                return
            if not self._done or time.monotonic() - self._last_ok < self._rearm_s:
                return
            self._done = False
            # refused again after a good run: either the device forgot, or it
            # never has these registers — back off so the second case does not
            # rewrite the setup every two minutes forever
            self._rearm_s = min(REARM_MAX_S, self._rearm_s * 2)
            logger.info("[%s] setup: %s — writing the setup steps again (next at the earliest in %d s)",
                        self.device_id, reason, int(self._rearm_s))

    def ensure(self) -> bool:
        """Write the steps if they are not in place. Cheap when they are."""
        if not self.steps or self._done:
            return self._done
        # another poll group is writing it right now: read what exists, don't queue
        if not self._lock.acquire(blocking=False):
            return self._done
        try:
            if self._done or time.monotonic() < self._next_try:
                return self._done
            why_not = self.gate()
            if why_not:
                self.last_error = why_not
                self._next_try = time.monotonic() + RETRY_S
                if why_not != self._gate_said:     # say it once, not every minute
                    logger.warning("[%s] setup not written: %s — the registers it provides stay unread",
                                   self.device_id, why_not)
                    self._gate_said = why_not
                return False
            self._gate_said = ""
            ok_all, errors = True, []
            for s in self.steps:
                ok, err = self.connection.write(int(s["address"]), register_type="holding",
                                                values=step_values(s))
                if not ok:
                    ok_all = False
                    errors.append(f"{s.get('name') or s['address']}: {err}")
                time.sleep(0.5)                  # a slow interface needs a breath between frames
            self.runs += 1
            self.last_run_ts = time.time()
            if ok_all:
                self._done, self._last_ok, self.last_error = True, time.monotonic(), ""
                names = ", ".join(str(s.get("name") or s["address"]) for s in self.steps)
                logger.info("[%s] setup written: %s", self.device_id, names)
                rec = getattr(self.connection, "record_event", None)
                if rec:
                    rec("info", "setup", f"setup written: {names}")
            else:
                self.last_error = "; ".join(errors)
                self._next_try = time.monotonic() + RETRY_S
                logger.warning("[%s] setup failed: %s", self.device_id, self.last_error)
            return self._done
        finally:
            self._lock.release()

    def stats(self) -> Dict[str, Any]:
        return {"steps": [s.get("name") or s["address"] for s in self.steps], "done": self._done,
                "runs": self.runs, "last_run_ts": self.last_run_ts, "error": self.last_error}
