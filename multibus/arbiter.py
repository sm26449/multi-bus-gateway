# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The channel referee (rules phase 3).

A **channel** is what some commands of one device control together (the
registers they write, or a name the template gives them). Several rules may
drive one channel: the referee says which one writes —
``priority`` (the highest layer with an opinion), ``min`` or ``max`` (the
lowest / highest value asked: limits) — and keeps the equipment's own
protection (minimum run and stop time, starts per hour) whoever writes:
a rule, an API or MQTT command, a force.

Pure: the runtime feeds it the candidates and the clock.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

COMBINE = ('priority', 'min', 'max')


@dataclass
class Candidate:
    rule: str
    prio: int
    value: Optional[float]
    forced: bool = False


def winner(cands: List[Candidate], combine: str) -> Optional[Candidate]:
    """The candidate that writes the channel. Priority: the highest, a forced
    one before an equal one, then the rule id (deterministic). min / max:
    the lowest / highest value, the higher priority on a tie."""
    if not cands:
        return None
    if combine in ('min', 'max'):
        valued = [c for c in cands if c.value is not None]
        if valued:
            sign = 1 if combine == 'min' else -1
            return sorted(valued, key=lambda c: (sign * c.value, -c.prio, c.rule))[0]
    return sorted(cands, key=lambda c: (-c.prio, not c.forced, c.rule))[0]


def beats(other: Candidate, me: Candidate, combine: str) -> bool:
    """Does ``other`` take the channel from ``me``?"""
    return winner([me, other], combine) is other


class ChannelState:
    """On / off as the writes left it, and the starts of the last hour —
    the motor's view, whoever wrote. ``None``: not known yet (nothing that
    starts or stops was written since the gateway started)."""

    def __init__(self) -> None:
        self.on: Optional[bool] = None
        self.on_since: Optional[float] = None
        self.off_since: Optional[float] = None
        self.starts: deque = deque()
        self.last_write: Optional[float] = None

    def starts_last_h(self, now: float) -> int:
        while self.starts and now - self.starts[0] >= 3600:
            self.starts.popleft()
        return len(self.starts)

    def hold(self, cfg: Dict[str, Any], turns_on: Optional[bool], now: float) -> Optional[str]:
        """Why a write that leaves the channel ``turns_on`` must wait, or None."""
        cfg = cfg or {}
        mi = float(cfg.get('min_interval_s') or 0)
        if mi and self.last_write is not None and now - self.last_write < mi:
            return f'{int(mi - (now - self.last_write))} s to go before the next write (min_interval_s)'
        if turns_on is None or turns_on == self.on:
            return None
        if not turns_on:
            mo = float(cfg.get('min_on_s') or 0)
            if mo and self.on_since is not None and now - self.on_since < mo:
                return f'minimum run time — {int(mo - (now - self.on_since))} s to go before stopping'
            return None
        mf = float(cfg.get('min_off_s') or 0)
        if mf and self.off_since is not None and now - self.off_since < mf:
            return f'minimum stop time — {int(mf - (now - self.off_since))} s to go before starting'
        ms = int(cfg.get('max_starts_per_h') or 0)
        if ms and self.starts_last_h(now) >= ms:
            return f'{len(self.starts)} starts in the last hour (max {ms}) — {int(3600 - (now - self.starts[0]))} s to go'
        return None

    def note(self, turns_on: Optional[bool], now: float) -> None:
        self.last_write = now
        if turns_on is None:
            return
        if turns_on and self.on is not True:
            self.on_since, self.off_since = now, None
            self.starts.append(now)
        elif not turns_on and self.on is not False:
            self.off_since, self.on_since = now, None
        self.on = turns_on

    # ── to a partner gateway (high availability): the monotonic clock is this
    #    process's own, so instants travel as ages (seconds before `now`) ──
    def export_mirror(self, now: float) -> Dict[str, Any]:
        age = (lambda t: None if t is None else now - t)
        return {'on': self.on, 'on_since': age(self.on_since), 'off_since': age(self.off_since),
                'last_write': age(self.last_write), 'starts': [now - t for t in self.starts]}

    @classmethod
    def from_mirror(cls, d: Dict[str, Any], now: float) -> 'ChannelState':
        at = (lambda a: None if a is None else now - float(a))
        c = cls()
        c.on = d.get('on') if d.get('on') in (True, False, None) else None
        c.on_since, c.off_since, c.last_write = at(d.get('on_since')), at(d.get('off_since')), at(d.get('last_write'))
        c.starts = deque(now - float(a) for a in (d.get('starts') or []))
        return c

    def snapshot(self, now: float) -> Dict[str, Any]:
        return {'on': self.on, 'on_for_s': None if self.on_since is None else round(now - self.on_since),
                'off_for_s': None if self.off_since is None else round(now - self.off_since),
                'starts_last_h': self.starts_last_h(now)}
