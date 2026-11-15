# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Command dispatch — one way in for every write a device is told (ADR-0018).

A **lane** is one wire: the bus of a Modbus connection, or an MQTT source.
Each lane with work has a thread of its own, made on demand and gone after
a while idle — one slow device delays only its own wire (CMD-Q-1). A
**slot** is (device, channel): a short FIFO of pending commands. A newer
value of the same setting replaces the one still waiting (CMD-Q-2); a
command that waited past its deadline writes nothing (CMD-Q-3). A revert
goes to the head of its lane and is never replaced; a refresh only fills an
empty slot. A running command is never interrupted.

Pure: no FastAPI, no device. What a command does is the ``run`` callable a
caller hands in; the clock is injectable, so deadlines are testable.
"""
from __future__ import annotations

import logging
import threading
import time
import weakref
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGE_S = 30.0       # a command older than this when its turn comes is not written
SLOT_CAP = 8                   # pending commands per (device, channel); the 9th is refused
IDLE_S = 60.0                  # a lane's thread ends after this long with nothing to do

KINDS = ('command', 'sequence', 'refresh', 'revert')

Result = Tuple[int, Dict[str, Any]]

_in_lane = threading.local()


@dataclass(eq=False)
class Job:
    """One command on its way to a device. ``run(job) -> (http_status,
    result)`` does the work in the lane; ``on_done(code, result)`` is told
    the outcome whatever it is (superseded, expired, cancelled included)."""
    lane: str
    device: str
    channel: str
    command: str
    params: Dict[str, Any]
    run: Callable[['Job'], Result]
    kind: str = 'command'
    coalesce: bool = True
    value_keys: Tuple[str, ...] = ('value',)
    max_age_s: Optional[float] = DEFAULT_MAX_AGE_S
    id: Optional[str] = None
    on_done: Optional[Callable[[int, Dict[str, Any]], None]] = None
    # what the run needs and the dispatcher does not look at
    ctx: Dict[str, Any] = field(default_factory=dict)
    # filled by the dispatcher
    admitted: float = 0.0
    deadline: Optional[float] = None
    started: Optional[float] = None
    result: Optional[Result] = None
    done: threading.Event = field(default_factory=threading.Event)

    def same_intent(self, other: 'Job') -> bool:
        """A newer value of the same setting: the same resolved command, the
        same parameters except the value (review 3 — on a motor channel
        `set_frequency` then `run` are two intents, never merged)."""
        if self.command != other.command or self.device != other.device or self.channel != other.channel:
            return False
        strip = (lambda p: {k: v for k, v in p.items() if k not in self.value_keys})  # noqa: E731
        return strip(self.params) == strip(other.params)

    def mergeable(self) -> bool:
        return self.kind == 'command' and self.coalesce

    def wait(self, timeout: Optional[float] = None) -> Optional[Result]:
        return self.result if self.done.wait(timeout) else None


class _Lane:
    def __init__(self, key: str):
        self.key = key
        self.urgent: Deque[Job] = deque()                 # reverts: the head of the lane
        self.pending: Dict[str, Deque[Job]] = {}          # device → its commands, FIFO
        self.order: Deque[str] = deque()                  # devices with work, round-robin
        self.running: Optional[Job] = None
        self.thread: Optional[threading.Thread] = None

    def jobs(self) -> List[Job]:
        return list(self.urgent) + [j for q in self.pending.values() for j in q]

    def slot(self, device: str, channel: str) -> List[Job]:
        return [j for j in self.pending.get(device, ()) if j.channel == channel]

    def remove(self, job: Job) -> bool:
        if job in self.urgent:
            self.urgent.remove(job)
            return True
        q = self.pending.get(job.device)
        if q is None or job not in q:
            return False
        q.remove(job)
        if not q:
            del self.pending[job.device]
            if job.device in self.order:
                self.order.remove(job.device)
        return True

    def next(self) -> Optional[Job]:
        if self.urgent:
            return self.urgent.popleft()
        while self.order:
            dev = self.order.popleft()
            q = self.pending.get(dev)
            if not q:
                self.pending.pop(dev, None)
                continue
            job = q.popleft()
            if q:
                self.order.append(dev)                    # the next device gets the next turn
            else:
                del self.pending[dev]
            return job
        return None


class CommandDispatcher:
    """Lanes, slots and the threads that serve them."""

    # every dispatcher made in this process, so a test harness can stop the
    # lanes of an app it no longer uses (tests/conftest.py)
    _instances: "weakref.WeakSet[CommandDispatcher]" = weakref.WeakSet()

    def __init__(self, clock: Callable[[], float] = time.monotonic, idle_s: float = IDLE_S,
                 slot_cap: int = SLOT_CAP, name: str = 'cmd-lane'):
        self.clock = clock
        self.idle_s = float(idle_s)
        self.slot_cap = int(slot_cap)
        self.name = name
        self._cond = threading.Condition()
        self._lanes: Dict[str, _Lane] = {}
        self._counters: Dict[Tuple[str, str], Dict[str, int]] = {}
        self._stopped = False
        CommandDispatcher._instances.add(self)
        self._closing = False          # shutting down: only the dead-man's reverts still run

    # ── submitting ──
    @staticmethod
    def in_lane() -> bool:
        return bool(getattr(_in_lane, 'key', None))

    def submit(self, job: Job) -> Job:
        """Queue ``job`` and return it at once; ``job.done`` may already be
        set (refused, or a refresh with nothing to refresh)."""
        if job.kind not in KINDS:
            raise ValueError(f"unknown job kind {job.kind!r}")
        told: List[Tuple[Job, Result]] = []
        with self._cond:
            now = self.clock()
            job.admitted = now
            job.deadline = (now + float(job.max_age_s)) if (job.kind != 'revert' and job.max_age_s) else None
            if self._stopped or (self._closing and job.kind != 'revert'):
                told.append((job, (503, {'status': 'cancelled', 'reason': 'the gateway is stopping'})))
            else:
                lane = self._lanes.get(job.lane)
                if lane is None:
                    lane = self._lanes[job.lane] = _Lane(job.lane)
                told.extend(self._admit(lane, job))
                if not job.done.is_set() and not any(j is job for j, _r in told):
                    self._ensure_thread(lane)
                    self._cond.notify_all()
        for j, res in told:
            self._complete(j, *res)
        return job

    def _admit(self, lane: _Lane, job: Job) -> List[Tuple[Job, Result]]:
        """Place the job in its lane (under the lock). What it replaces or
        what refuses it comes back to be told outside the lock."""
        if job.kind == 'revert':
            # the dead-man first, whatever waits: it never replaces nor is replaced
            lane.urgent.append(job)
            return []
        slot = lane.slot(job.device, job.channel)
        if job.kind == 'refresh':
            # a refresh only repeats a setting: never over newer intent (review 2)
            # — but what waits may still expire or be cancelled: the refresh is
            # put off (a 409 its timer tries again), never ended here; ending
            # it is the generation check's, once a newer command ran (CR2-2)
            if slot:
                return [(job, (409, {'status': 'rejected', 'retry': True,
                                     'reason': 'a command waits on this channel — refresh put off'}))]
        tail = slot[-1] if slot else None
        if tail is not None and job.mergeable() and tail.mergeable() and tail.same_intent(job):
            q = lane.pending[job.device]
            q[q.index(tail)] = job                         # its place in the queue, the newer value
            self._count(tail, 'superseded')
            return [(tail, (409, {'status': 'superseded', 'superseded': True,
                                  'reason': 'a newer value of the same setting replaced it before it '
                                            'ran — nothing written'}))]
        if len(slot) >= self.slot_cap:
            return [(job, (429, {'status': 'rejected',
                                 'reason': f"queue full — {self.slot_cap} commands already wait on "
                                           f"channel {job.channel} of {job.device}"}))]
        if job.device not in lane.pending:
            lane.pending[job.device] = deque()
            lane.order.append(job.device)
        lane.pending[job.device].append(job)
        return []

    def submit_wait(self, job: Job, timeout: Optional[float] = None) -> Result:
        """Submit and wait for the verdict. Never from inside a lane: a lane
        waiting for itself (or for one waiting for it) never ends (review 8)."""
        if self.in_lane():
            raise RuntimeError(f"submit_wait from inside lane {_in_lane.key} — a lane never waits on a lane")
        self.submit(job)
        return self.wait_for(job, timeout)

    def wait_for(self, job: Job, timeout: Optional[float] = None) -> Result:
        """The verdict of a submitted job; one that has not started when the
        wait ends is withdrawn (`cancelled`), one that runs is said to."""
        if self.in_lane():
            raise RuntimeError(f"wait from inside lane {_in_lane.key} — a lane never waits on a lane")
        got = job.wait(timeout)
        if got is not None:
            return got
        if self.withdraw(job, 'its caller waited too long for the bus — nothing written'):
            return job.result                                  # type: ignore[return-value]
        got = job.wait(0)
        if got is not None:
            return got
        return 504, {'status': 'error', 'reason': f"still running after {timeout:g} s — its outcome "
                                                  "will be in the log and the audit", 'id': job.id}

    def withdraw(self, job: Job, reason: str) -> bool:
        """Take back a command that has not started; False once it runs."""
        with self._cond:
            lane = self._lanes.get(job.lane)
            if lane is None or not lane.remove(job):
                return False
        self._complete(job, 409, {'status': 'cancelled', 'reason': reason})
        return True

    def cancel(self, device: Optional[str] = None, reason: str = 'cancelled') -> int:
        """Every pending command (of one device, or all): `cancelled`. On a
        role change to standby, a device deleted or stopped, the gateway's
        stop. A running one finishes."""
        out: List[Job] = []
        with self._cond:
            for lane in self._lanes.values():
                for j in lane.jobs():
                    if device is None or j.device == device:
                        lane.remove(j)
                        out.append(j)
        for j in out:
            self._complete(j, 409, {'status': 'cancelled', 'reason': reason})
        return len(out)

    def close(self) -> None:
        """The gateway is shutting down: nothing new is taken but the
        dead-man's reverts, which the shutdown runs through the lanes while the
        devices are still connected (F-28); what waits is cancelled. stop()
        follows once those reverts are done (CR2-8)."""
        out: List[Job] = []
        with self._cond:
            self._closing = True
            for lane in self._lanes.values():
                for j in lane.jobs():
                    if j.kind != 'revert':
                        lane.remove(j)
                        out.append(j)
        for j in out:
            self._complete(j, 409, {'status': 'cancelled', 'reason': 'the gateway is stopping — nothing written'})

    def forget(self, device: str) -> None:
        """A deleted device's counters go with it."""
        with self._cond:
            for k in [k for k in self._counters if k[0] == device]:
                del self._counters[k]

    def stop(self) -> None:
        self._stopped = True
        self.cancel(reason='the gateway is stopping')
        with self._cond:
            self._cond.notify_all()

    # ── what is going on ──
    def pending(self, lane: str, device: str, channel: str) -> int:
        """Commands waiting in one slot (the hold's fast answer is given only
        when nothing waits ahead of a command)."""
        with self._cond:
            ln = self._lanes.get(lane)
            return len(ln.slot(device, channel)) if ln is not None else 0

    def drain(self, timeout: float = 10.0) -> bool:
        """Wait until nothing waits or runs (shutdown, tests)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._cond:
                if not any(ln.jobs() or ln.running is not None for ln in self._lanes.values()):
                    return True
            time.sleep(0.005)
        return False

    def lanes(self) -> Dict[str, Dict[str, Any]]:
        """Each lane with a thread or work: pending count, the oldest's age
        and its max_age_s (the status page's attention item)."""
        now = self.clock()
        with self._cond:
            out = {}
            for k, lane in self._lanes.items():
                jobs = lane.jobs()
                oldest = min(jobs, key=lambda j: j.admitted) if jobs else None
                out[k] = {'pending': len(jobs), 'running': lane.running is not None,
                          'oldest_s': round(now - oldest.admitted, 3) if oldest else 0.0,
                          'max_age_s': oldest.max_age_s if oldest else None}
            return out

    def counters(self, device: str, command: str) -> Dict[str, int]:
        with self._cond:
            return dict(self._counters.get((device, command)) or {'superseded': 0, 'expired': 0, 'held': 0})

    def count(self, device: str, command: str, what: str) -> None:
        with self._cond:
            c = self._counters.setdefault((device, command), {'superseded': 0, 'expired': 0, 'held': 0})
            c[what] = c.get(what, 0) + 1

    def _count(self, job: Job, what: str) -> None:
        c = self._counters.setdefault((job.device, job.command), {'superseded': 0, 'expired': 0, 'held': 0})
        c[what] = c.get(what, 0) + 1

    # ── the lanes ──
    def _ensure_thread(self, lane: _Lane) -> None:
        if lane.thread is not None and lane.thread.is_alive():
            return
        lane.thread = threading.Thread(target=self._serve, args=(lane,), daemon=True,
                                       name=f"{self.name}:{lane.key}"[:60])
        lane.thread.start()

    def _serve(self, lane: _Lane) -> None:
        _in_lane.key = lane.key
        try:
            while True:
                with self._cond:
                    job = lane.next()
                    idle_since = time.monotonic()
                    while job is None:
                        left = self.idle_s - (time.monotonic() - idle_since)
                        if self._stopped or left <= 0:
                            # nothing came while idle: the lane ends (its thread too);
                            # the next command makes it again
                            if lane.thread is threading.current_thread():
                                lane.thread = None
                            if not lane.jobs() and lane.running is None:
                                self._lanes.pop(lane.key, None)
                            return
                        self._cond.wait(left)
                        job = lane.next()
                    lane.running = job
                    now = self.clock()
                    expired = job.deadline is not None and now > job.deadline
                    if expired:
                        self._count(job, 'expired')
                    else:
                        job.started = now
                try:
                    if expired:
                        res: Any = (409, {'status': 'expired', 'reason':
                                          f"waited {now - job.admitted:.1f} s for the bus, longer than its "
                                          f"{job.max_age_s:g} s — nothing written"})
                    else:
                        try:
                            res = job.run(job)
                        except Exception as e:  # noqa: BLE001 — a crashed command is a failed one, said
                            logger.exception("command %s on %s crashed", job.command, job.device)
                            res = (500, {'status': 'error', 'reason': f"{type(e).__name__}: {e}"})
                    # whatever came back, the lane goes on: a result that is not
                    # (status, dict) is an error said, never a dead lane (CR2-7)
                    try:
                        code, body = int(res[0]), dict(res[1])
                    except Exception:  # noqa: BLE001
                        logger.error("command %s on %s returned no result: %r", job.command, job.device, res)
                        code, body = 500, {'status': 'error', 'reason': 'the command returned no result'}
                    # told before the lane is free: whoever drains sees the outcome
                    try:
                        self._complete(job, code, body)
                    except Exception:  # noqa: BLE001
                        logger.exception("command %s on %s: its result could not be recorded", job.command, job.device)
                        if not job.done.is_set():
                            job.result = (500, {'status': 'error', 'reason': 'its result could not be recorded'})
                            job.done.set()
                finally:
                    with self._cond:
                        lane.running = None
        finally:
            _in_lane.key = None

    def _complete(self, job: Job, code: int, res: Dict[str, Any]) -> None:
        if job.done.is_set():
            return
        res = dict(res or {})
        res.setdefault('queued_ms', round(((job.started if job.started is not None else self.clock())
                                           - job.admitted) * 1000.0, 1))
        if job.id is not None:
            res.setdefault('id', job.id)
        job.result = (code, res)
        job.done.set()
        if job.on_done is not None:
            try:
                job.on_done(code, res)
            except Exception:  # noqa: BLE001 — a listener never stops the lane
                logger.exception("command %s on %s: its result could not be told", job.command, job.device)
