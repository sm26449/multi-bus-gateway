"""CommandTransport — the one seam the Modbus/MQTT command branches collapse into.

ADR-0036 (§2 L0). The command path above the engine is transport-neutral; the
Modbus-vs-MQTT split is a ``drv``/``mq`` dichotomy repeated across
``_admit_command`` / ``_already_set`` / ``_exec_timeout`` / ``_execute`` /
``_record`` (api/command_service.py) and ``control/lanes.py``. This module
expresses that branch ONCE: a ``CommandTransport`` protocol with a Modbus and an
MQTT adapter that wrap today's code verbatim, and ``transport_of(client)``
replacing the scattered ``_modbus_driver_of`` / ``_mqtt_driver_of`` selection.

L0 step 1 adds the module with NO caller: the adapters reproduce today's outputs
so steps 2-5 can route each site through them, Modbus/MQTT byte-identical.
``lane_key`` / ``lock_for`` join the protocol in L0 step 2 (they live in
``control/lanes.py`` today and move onto the transport then, with the
lock-table-singleton invariant of ADR-0036 point 5).

Control-layer and framework-free (ADR-0038): methods return result dicts / None
and raise only domain errors, never ``HTTPException``. Every collaborator
(the publisher's forbidden topics, the device registry's store, the read-back
helpers) is INJECTED, never imported upward — the precedent control/lanes.py and
control/raw_writes.py set.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, runtime_checkable

from multibus.control.commands import evaluate as _cmd_eval, run_command, run_mqtt_command
from multibus.core.protocols import store_key
from multibus.core.config import NETWORK_PROTOCOLS, SERIAL_PROTOCOLS
from multibus.transports.driver import RegisterReadable

# every Modbus way but listening (mirrors control/raw_writes.py and
# api/endpoints_service.py).
_POLLED_MODBUS = NETWORK_PROTOCOLS + SERIAL_PROTOCOLS


# The ONE body of the Modbus/MQTT driver resolution (M1): api/command_service.py
# imports these (and exposes them as self._modbus_driver_of / _mqtt_driver_of for
# control/lanes.py, raw_writes.py, rules_seq.py, command_exec.py, routes/components.py)
# and `transport_of` below uses the same functions — no more byte-duplicated copies.
def modbus_driver_of(client: Any) -> Any:
    """The Modbus driver behind a client: the client itself, or the first Modbus
    source of a unit read several ways. None when nothing can write."""
    if client is None:
        return None
    if hasattr(client, 'connection') and hasattr(client.connection, 'read_registers'):
        return client
    for src, drv in getattr(client, 'parts', []) or []:
        if str(getattr(src, 'protocol', 'tcp')).lower() in _POLLED_MODBUS \
                and hasattr(drv, 'connection'):
            return drv
    return None


def mqtt_driver_of(client: Any) -> Any:
    """The MQTT input source behind a client, the one that publishes a command
    when the unit has no Modbus source. None when there is none."""
    if client is None:
        return None
    if hasattr(client, 'publish') and hasattr(client, 'wait_for'):
        return client
    for src, drv in getattr(client, 'parts', []) or []:
        if str(getattr(src, 'protocol', '')).lower() == 'mqtt' and hasattr(drv, 'wait_for'):
            return drv
    return None


@runtime_checkable
class CommandTransport(Protocol):
    """What a command needs of its transport, the one branch Modbus and MQTT
    differ on. Each member is a ``drv``/``mq`` branch lifted from
    api/command_service.py; the result-dict shapes are unchanged so ``_record``
    and every publisher stay the same."""

    kind: str
    supports_commands: bool
    supports_raw_write: bool

    def lane_key(self, dev_cfg: Any, client: Any) -> str:
        """The dispatcher lane this command serializes on (bus:/dev:/mqtt:).
        Delegates to the shared lane helper, so keys stay byte-equal."""
        ...

    def lock_for(self, ctx: Dict[str, Any]) -> Any:
        """The bus lock this command takes, from the one shared lock table
        (ADR-0036 point 5): the adapter never owns or recreates it."""
        ...

    def exec_timeout(self, ctx: Dict[str, Any]) -> float:
        """How long a running command may take (ADR-0018 D4)."""
        ...

    def already_set(self, ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """D6: the device already holds what this command says, or None."""
        ...

    def run(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        """Run the command on the device and return the result dict."""
        ...

    def record_event(self, level: str, kind: str, summary: str) -> None:
        """Record a device-side event for this transport's connection."""
        ...

    def readback(self, ctx: Dict[str, Any], res: Dict[str, Any]) -> None:
        """After a successful write, make the new value reach the store (a
        transport difference: Modbus re-reads; MQTT reads back through its own
        subscription, so it is a no-op)."""
        ...

    def target_id(self, cmd: Any, regs: Dict[str, Any]) -> str:
        """A stable string for logs and audit (CMD-6), e.g. ``modbus:holding@40001``
        or ``mqtt:<topic>`` — never the raw numeric key kept by write_lease."""
        ...


class ModbusCommandTransport:
    """Modbus TCP/RTU/ASCII (and RTU-over-TCP bridges). The reference transport.

    Bodies are lifted verbatim from api/command_service.py's Modbus branches:
    ``_exec_timeout`` (connection timeout x retries), ``_already_set``
    (``skip_if_set`` against the polled store), ``_execute``'s ``run_command``
    call, ``_record``'s ``drv.connection.record_event`` and its ``mq is None``
    read-back block."""

    kind = "modbus"
    supports_commands = True
    supports_raw_write = True

    def __init__(self, drv: Any, deps: "CommandTransports") -> None:
        self._drv = drv
        self._deps = deps

    @property
    def driver(self) -> Any:
        return self._drv

    def lane_key(self, dev_cfg: Any, client: Any) -> str:
        return self._deps.lane_key(dev_cfg, client)

    def lock_for(self, ctx: Dict[str, Any]) -> Any:
        # the current call passes `drv` when Modbus; this is that driver
        return self._deps.lock_for(self._drv, ctx['dev'])

    def exec_timeout(self, ctx: Dict[str, Any]) -> float:
        settle = float(ctx['cmd'].settle_s or 0)
        cc = getattr(getattr(self._drv, 'connection', None), 'config', None)
        try:
            per = float(getattr(cc, 'timeout', 3) or 3) * (int(getattr(cc, 'retry_attempts', 3) or 0) + 1)
        except (TypeError, ValueError):
            per = 12.0
        cmd = ctx['cmd']
        n = 2 * len(cmd.guard or []) + len(cmd.writes or []) + 2 * len(cmd.verify or []) + 1
        return max(3, n) * per + settle

    def already_set(self, ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        cmd, norm = ctx['cmd'], ctx['norm']
        if not cmd.skip_if_set or cmd.refresh_s or 'revert_s' in cmd.params or not cmd.writes:
            return None
        store = self._deps.registry.store_for(ctx['dev'].id) or {}
        now = time.monotonic()
        held: Dict[str, Any] = {}
        for w in cmd.writes:
            r = ctx['regs'].get(w.get('register'))
            if r is None or (getattr(r, 'register_type', 'holding') or 'holding').lower() != 'holding':
                return None
            e = store.get(store_key(int(r.address), 'holding'))
            # polled, and fresher than two of its poll periods — else a write
            if not e or e.get('value') is None or e.get('mono') is None:
                return None
            every = float(e.get('interval') or 0)
            if every <= 0 or now - float(e['mono']) > 2 * every:
                return None
            try:
                want, got = float(_cmd_eval(w.get('value'), norm)), float(e['value'])
            except (TypeError, ValueError, ArithmeticError):
                return None
            tol = next((float(v.get('tolerance', 0) or 0) for v in cmd.verify if v.get('read') == r.name), 0.0)
            if abs(got - want) > max(tol, 1e-9):
                return None
            held[r.name] = got
        return {'command': cmd.name, 'params': dict(norm), 'status': 'success', 'already_set': True,
                'frames_sent': 0, 'before': dict(held), 'after': dict(held)}

    def run(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        return run_command(ctx['cmd'], ctx['norm'], self._drv.connection, ctx['regs'],
                           byte_order=getattr(self._drv, 'byte_order', 'big'), dry_run=ctx['dry_run'],
                           max_read=int(getattr(self._drv, 'max_read', 0) or 0))

    def record_event(self, level: str, kind: str, summary: str) -> None:
        self._drv.connection.record_event(level, kind, summary)

    def readback(self, ctx: Dict[str, Any], res: Dict[str, Any]) -> None:
        dev_cfg, cmd, norm, regs = ctx['dev'], ctx['cmd'], ctx['norm'], ctx['regs']
        drv = self._drv
        # the read-back reaches the live store at once, and the group that
        # carries it is swept now so MQTT/InfluxDB/HA follow in seconds
        for n, v in (res.get('after') or {}).items():
            r = regs.get(n)
            if r is not None and v is not None:
                self._deps.push_readback_to_store(dev_cfg.id, int(r.address), v,
                                                  getattr(r, 'register_type', 'holding') or 'holding')
        try:
            if cmd.readback_group and isinstance(drv, RegisterReadable):
                drv.poll_now(cmd.readback_group)
        except Exception:  # noqa: BLE001
            pass
        # a device-side revert timer (``revert_s``) changes the device again when
        # it fires with no write from here — sweep the group a few times after the
        # mark (REVERT_REREAD_AT_S) so MQTT/InfluxDB/HA and the rules see it.
        try:
            revert = float(norm.get('revert_s') or 0)
            if revert > 0 and cmd.readback_group and isinstance(drv, RegisterReadable):
                self._deps.arm_revert_reread(dev_cfg.id, cmd.name, cmd.readback_group,
                                             tuple(revert + d for d in self._deps.revert_reread_at_s))
        except Exception:  # noqa: BLE001
            pass

    def target_id(self, cmd: Any, regs: Dict[str, Any]) -> str:
        first = regs.get(cmd.writes[0].get('register')) if cmd.writes else None
        if first is None:
            return f"modbus:{cmd.name}"
        rtype = (getattr(first, 'register_type', 'holding') or 'holding')
        return f"modbus:{rtype}@{int(first.address)}"


class MqttCommandTransport:
    """An MQTT input source that also writes (``victron_gx_mqtt``). Commands only;
    no raw write (ADR-0022 D4). Read-back is through the source's subscription,
    so ``readback`` is a no-op. Bodies are lifted verbatim from the ``mq`` branches
    of ``_already_set`` / ``_exec_timeout`` / ``_execute`` / ``_record``."""

    kind = "mqtt"
    supports_commands = True
    supports_raw_write = False

    def __init__(self, mq: Any, deps: "CommandTransports") -> None:
        self._mq = mq
        self._deps = deps

    @property
    def driver(self) -> Any:
        return self._mq

    def lane_key(self, dev_cfg: Any, client: Any) -> str:
        return self._deps.lane_key(dev_cfg, client)

    def lock_for(self, ctx: Dict[str, Any]) -> Any:
        # the current call passes `ctx['client']` when MQTT (drv is None)
        return self._deps.lock_for(ctx['client'], ctx['dev'])

    def exec_timeout(self, ctx: Dict[str, Any]) -> float:
        return float(ctx['cmd'].settle_s or 0) + 5.0

    def already_set(self, ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        cmd, norm = ctx['cmd'], ctx['norm']
        try:
            pre = run_mqtt_command(cmd, norm, self._mq, ctx['regs'], dry_run=True,
                                   forbidden_topics=self._deps.forbidden_topics(ctx['dev']))
        except ValueError:
            return None
        return pre if pre.get('already_set') else None

    def run(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        return run_mqtt_command(ctx['cmd'], ctx['norm'], self._mq, ctx['regs'], dry_run=ctx['dry_run'],
                                forbidden_topics=self._deps.forbidden_topics(ctx['dev']))

    def record_event(self, level: str, kind: str, summary: str) -> None:
        self._mq.record_event(level, kind, summary)

    def readback(self, ctx: Dict[str, Any], res: Dict[str, Any]) -> None:
        # an MQTT read-back reaches the store through the source's subscription
        return None

    def target_id(self, cmd: Any, regs: Dict[str, Any]) -> str:
        first = regs.get(cmd.writes[0].get('register')) if cmd.writes else None
        topic = getattr(first, 'write_topic', None) if first is not None else None
        return f"mqtt:{topic}" if topic else f"mqtt:{cmd.name}"


class CommandTransports:
    """Resolves the transport behind a client and holds the collaborators the
    adapters need. ``transport_of(client)`` replaces the ``_modbus_driver_of`` /
    ``_mqtt_driver_of`` selection (Modbus first, then MQTT, else None — the 409's
    'no transport' case in L0 step 5)."""

    def __init__(self, *, registry: Any, forbidden_topics: Callable[[Any], List[str]],
                 push_readback_to_store: Callable[..., Any], arm_revert_reread: Callable[..., Any],
                 revert_reread_at_s: Sequence[float], lane_key: Callable[[Any, Any], str],
                 lock_for: Callable[..., Any]) -> None:
        self.registry = registry
        self.forbidden_topics = forbidden_topics
        self.push_readback_to_store = push_readback_to_store
        self.arm_revert_reread = arm_revert_reread
        self.revert_reread_at_s = tuple(revert_reread_at_s)
        # the shared lane/lock helpers (control/lanes.py): the adapters delegate
        # to them so the one `_cmd_locks` table stays shared (ADR-0036 point 5)
        self.lane_key = lane_key
        self.lock_for = lock_for

    def transport_of(self, client: Any) -> Optional[CommandTransport]:
        drv = modbus_driver_of(client)          # the one shared body (M1)
        if drv is not None:
            return ModbusCommandTransport(drv, self)
        mq = mqtt_driver_of(client)
        if mq is not None:
            return MqttCommandTransport(mq, self)
        return None
