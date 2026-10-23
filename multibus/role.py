# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The gateway's role: active (the usual: it polls, writes, runs its rules
and publishes) or standby (it does none of that; devices it only listens to
keep being heard). A gateway is active unless an outside service, holding a
service token with the `ha` role, sets it otherwise.

The role carries an EPOCH, a number that only grows: a role change with an
epoch lower than the current one is refused, so a controller that fell
behind (a gateway coming back after its partner took over) can never make
two gateways active.

The gateway never changes its own role. The role and epoch are kept on disk,
so a standby that restarts is still a standby.
"""
import json
import logging
import os
import threading
import time
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

ROLES = ('active', 'standby')
# the sources a standby keeps: they only listen (a tap on the bus, an MQTT
# subscription), so the active one and the standby never both talk to a device
LISTENING_PROTOCOLS = ('rtu_tap', 'mqtt')

# the role of this process, for the code that cannot be handed it (a Modbus
# connection deep in a poller); None = no role installed = active
_installed: 'Optional[GatewayRole]' = None


def install(role: 'Optional[GatewayRole]') -> None:
    global _installed
    _installed = role


def wire_allowed() -> bool:
    """May this gateway put a frame on a device's wire (read, write, probe)?"""
    return _installed is None or _installed.active


def is_active() -> bool:
    """Does this gateway publish (outputs, announcements)? The same answer as
    wire_allowed, asked for another reason."""
    return _installed is None or _installed.active


def listens_only(protocol: str) -> bool:
    return str(protocol or '').lower() in LISTENING_PROTOCOLS


STANDBY_WRITE = "standby gateway: writes are done by the active one"


class StandbyRefused(Exception):
    """A write, a poll or a publish a standby gateway does not do."""


class GatewayRole:
    def __init__(self, path: Optional[str] = None, clock=time.time):
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._listeners: List[Callable[[str, str], None]] = []
        self.role, self.epoch, self.since, self.set_by, self.reason = 'active', 0, clock(), '', ''
        self._load()

    # ── reading ──
    @property
    def active(self) -> bool:
        return self.role == 'active'

    def status(self) -> Dict:
        return {'role': self.role, 'epoch': self.epoch, 'since': self.since,
                'set_by': self.set_by, 'reason': self.reason}

    def status_text(self) -> str:
        return f"standby gateway (epoch {self.epoch}): the active one does this"

    def check(self, what: str = 'this') -> None:
        """Raise StandbyRefused on a standby gateway: the one call every write
        path makes before it touches a device."""
        if self.role != 'active':
            raise StandbyRefused(f"standby gateway (epoch {self.epoch}): {what} is done by the active one")

    # ── changing (only from outside) ──
    def set(self, role: str, epoch: int, by: str, reason: str = '') -> Dict:
        """Set the role. Refused: an unknown role, an epoch lower than ours, or
        the same epoch with another role (a change needs a new epoch)."""
        if role not in ROLES:
            raise ValueError(f"role: one of {', '.join(ROLES)}")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            raise ValueError("epoch: a whole number, 0 or more")
        with self._lock:
            if epoch < self.epoch:
                raise ValueError(f"epoch {epoch} is older than this gateway's ({self.epoch})")
            if epoch == self.epoch and role != self.role:
                raise ValueError(f"a change of role needs a new epoch (now {self.epoch})")
            old = self.role
            changed = role != old
            self.epoch = epoch
            if changed:
                self.role, self.since = role, self._clock()
            self.set_by, self.reason = by, str(reason or '')[:200]
            self._save_locked()
        if changed:
            logger.warning("gateway role: %s → %s (epoch %d, by %s%s)", old, role, epoch, by,
                           f": {reason}" if reason else '')
            for fn in list(self._listeners):
                try:
                    fn(old, role)
                except Exception as e:  # noqa: BLE001 — one listener must not stop the others
                    logger.exception("role listener: %s", e)
        return self.status()

    def on_change(self, fn: Callable[[str, str], None]) -> None:
        """fn(old, new) after the role changed (the gateway stops or starts
        its pollers, rules and outputs)."""
        self._listeners.append(fn)

    # ── persistence ──
    def _load(self) -> None:
        if not self._path or not os.path.exists(self._path):
            return
        try:
            with open(self._path) as f:
                d = json.load(f)
            if d.get('role') in ROLES and isinstance(d.get('epoch'), int) and d['epoch'] >= 0:
                self.role, self.epoch = d['role'], d['epoch']
                self.since = float(d.get('since') or self._clock())
                self.set_by, self.reason = str(d.get('set_by') or ''), str(d.get('reason') or '')
                if self.role != 'active':
                    logger.warning("gateway starts as %s (epoch %d, set by %s)", self.role, self.epoch, self.set_by)
        except Exception as e:  # noqa: BLE001
            # unreadable: a STANDBY would rather stay quiet than poll beside
            # its partner — but a gateway that never had a role must work.
            # The file only exists once someone set a role, so: standby.
            logger.error("role file unreadable (%s): starting as standby until a role is set", e)
            self.role, self.reason = 'standby', 'role file unreadable'

    def _save_locked(self) -> None:
        if not self._path:
            return
        tmp = self._path + '.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(self.status(), f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)
