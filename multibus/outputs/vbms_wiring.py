# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Wire the Virtual BMS executor to MBG's live data and templates (ADR-0037, lifecycle).

Thin adapters that keep the store/catalog details out of ``VbmsManager``:
* ``make_read_value(registry)`` reads a pack device's canonical field from the live value store,
  returning it **only while fresh** (the driver's monotonic stamp within the producing group's
  stale bound) — a stale pack then drops out of the aggregation (design §4.4);
* ``make_load_profile(template_registry)`` resolves a profile by template id → its CAN rows.

``build_vbms_manager`` ties them together. With no ``config.virtual_bms`` it yields a manager with
zero instances — nothing transmits — so adding this to boot is safe before anything is configured.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

from multibus.outputs.vbms_manager import VbmsManager

logger = logging.getLogger(__name__)

_DEFAULT_STALE_S = 30.0
_GROUP_STALE_MULT = 2.5          # same spirit as the vmeter bound
# topics read back from the retained-topic inventory, at most (R2D-TELE-1): a few instances of a
# few dozen fields each are the real size; a file beyond this is not trusted past the bound
MAX_RETAINED_INVENTORY = 4096
MAX_INVENTORY_BYTES = 1024 * 1024            # a larger file is not read at all (review L5)
_WRITE_FAIL_LOG_S = 600                       # an inventory write failure is said this often


def make_read_value(registry: Any, *, default_stale_s: float = _DEFAULT_STALE_S,
                    clock: Callable[[], float] = time.monotonic) -> Callable[[str, str], Any]:
    """``read_value(device_id, field)`` → the fresh value for a register NAME in that device's
    live store, else None (missing, never-measured, or stale)."""
    def read_value(device_id: str, field: str) -> Any:
        store = registry.store_for(device_id)
        if not store:
            return None
        for info in list(store.values()):          # snapshot vs concurrent poller writes
            if not isinstance(info, dict) or info.get("name") != field:
                continue
            value, mono = info.get("value"), info.get("mono")
            if value is None or mono is None:       # fail closed: no measurement time = not fresh
                return None
            iv = info.get("interval")
            bound = _GROUP_STALE_MULT * float(iv) if iv else default_stale_s
            return value if (clock() - mono) <= bound else None
        return None
    return read_value


def make_load_profile(template_registry: Any) -> Callable[[str], List[Any]]:
    """``load_profile(template_id)`` → the profile's CAN rows (``DeviceTemplate.registers``)."""
    def load_profile(name: str) -> List[Any]:
        t = template_registry.get(name) if template_registry is not None else None
        if t is None:
            raise ValueError(f"virtual_bms profile template {name!r} not found")
        return list(t.registers)
    return load_profile


def build_vbms_manager(config: Any, registry: Any, template_registry: Any,
                       **kw: Any) -> VbmsManager:
    # the flap/hold state of the packs lives beside the config (R2D-VBMS-3): a restart must
    # not readmit a pack an operator has not cleared
    cfg_path = getattr(config, "config_path", None)
    if cfg_path is not None and "state_path" not in kw:
        kw["state_path"] = Path(cfg_path).parent / "vbms_admission.json"
    return VbmsManager(config, make_read_value(registry), make_load_profile(template_registry), **kw)


class VbmsCommandBridge:
    """Subscribe the EMS command topics (``<base>/cmd/<field>``) for every Virtual BMS instance to
    MBG's own broker and route each payload to the manager's clamped control plane (ADR-0037 D4).

    It rides the publisher's existing command seam (``register_command``), so a Virtual BMS command
    gets the same treatment as a device command: it runs off paho's network thread, a RETAINED
    delivery is ignored (never actuate from a stale replay), and the subscription survives a broker
    reconnect. A command only mutates the control plane — nothing transmits unless the instance is
    separately armed — so these topics are safe to subscribe even while every instance is disarmed.

    ``publisher_of`` is read at ``sync`` time (not captured), so a publisher rebuilt by
    ``/api/config/apply`` is picked up. ``sync`` is idempotent: it drops the topics it previously
    registered and re-registers from the current instances, so it doubles as the config-reload hook.
    """

    def __init__(self, manager: VbmsManager, publisher_of: Callable[[], Any], *,
                 topic_pattern: str = "mbg/devices/{device}"):
        self._manager = manager
        self._publisher_of = publisher_of
        self._topic_pattern = topic_pattern
        self._registered: set = set()

    def sync(self) -> List[str]:
        pub = self._publisher_of()
        if pub is None or not hasattr(pub, "register_command"):
            return []
        for topic in list(self._registered):            # our own topics only — never a neighbour's
            try:
                pub.unregister_command(topic)
            except Exception:                           # noqa: BLE001 — unsubscribe is best-effort
                pass
        self._registered.clear()
        for topic, (instance_id, field) in self._manager.command_topics(self._topic_pattern).items():
            pub.register_command(
                topic,
                lambda payload, i=instance_id, f=field: self._manager.command(i, f, payload))
            self._registered.add(topic)
        return sorted(self._registered)


class VbmsStatePublisher:
    """Publish each Virtual BMS instance's telemetry to MBG's broker so the EMS and the operator
    see the loop close (ADR-0037): the values it is sending to the inverter (measured aggregation +
    the resolved, clamped control fields), plus its arm/running status.

    Per-field retained topics under the same config-owned base the command bridge uses
    (``<base>/<field>``), via ``publish_if_changed`` — so a stable control limit is not republished
    every tick, a changing current is, and a late subscriber reads the last value from the retained
    topic. Publishing is independent of the executor: it runs whether an instance is armed or not,
    so an operator can watch the computed bank before arming. It is pure telemetry — it never
    transmits on CAN. ``publisher_of`` is read each tick, so a rebuilt publisher is picked up.
    """

    def __init__(self, manager: VbmsManager, publisher_of: Callable[[], Any], *,
                 topic_pattern: str = "mbg/devices/{device}", interval_s: float = 2.0,
                 heartbeat: int = 30, inventory_path: Union[str, Path, None] = None,
                 device_ids: Optional[Callable[[], Iterable[str]]] = None):
        self._manager = manager
        self._publisher_of = publisher_of
        self._topic_pattern = topic_pattern
        self._interval_s = interval_s
        self._heartbeat = heartbeat
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # instance id -> the retained topics it holds on the broker (to take them back)
        self._retained: dict = {}
        self._owed: set = set()                 # retained topics still to be cleared on the broker
        # the ids of the configured devices, components and units, read live: a Virtual BMS and a
        # device share the topic namespace, so a topic under a LIVE device's id is never withdrawn
        # — not from a stale inventory, not from an owed clear (review M1)
        self._device_ids = device_ids
        self._spared: set = set()               # ids already said to be spared (logged once)
        # The inventory outlives the process (R2D-TELE-1): an instance deleted while the gateway
        # was down is no longer in the config, so only this file still knows what it left
        # retained. It holds per instance every topic it EVER retained while it lived (a union),
        # so a field that comes and goes changes nothing on disk (review M2: no write per flap on
        # an SD card); it is rewritten only when an instance appears, retains a never-seen topic,
        # or is gone with nothing more owed.
        self._inventory_path = Path(inventory_path) if inventory_path else None
        self._known: Dict[str, set] = {}
        self._saved: Optional[Dict[str, Any]] = None
        self._write_failed_at: Optional[float] = None
        self._load_inventory()

    # ── the retained-topic inventory on disk (R2D-TELE-1) ───────────────────────────────
    def _instance_of(self, topic: Any) -> Optional[str]:
        """The Virtual BMS id whose field ``topic`` is under the topic pattern, or None — a
        topic read back from the file is withdrawn only when it is one of ours."""
        from multibus.outputs.vbms_registry import _SLUG
        if not isinstance(topic, str) or "+" in topic or "#" in topic:
            return None
        head, _sep, tail = self._topic_pattern.partition("{device}")
        if not _sep or not topic.startswith(head):
            return None
        iid, sep, field = topic[len(head):].partition(tail + "/")
        return iid if sep and field and _SLUG.match(iid) else None

    def _inventory_doc(self) -> Dict[str, Any]:
        return {"retained": {i: sorted(t) for i, t in sorted(self._known.items())}}

    def _load_inventory(self) -> None:
        """Read back what an earlier process left retained. A damaged or oversized file is an
        empty inventory (logged), never a crash; only our own topics, each tied to the instance
        it is filed under, up to the bound, are taken."""
        path = self._inventory_path
        if path is None:
            return
        if not path.exists():
            self._saved = self._inventory_doc()         # nothing to write until something changes
            return
        try:
            size = path.stat().st_size
            if size > MAX_INVENTORY_BYTES:
                logger.warning("vbms: retained-topic inventory %s is %d bytes (more than %d) — "
                               "not read, starting empty", path, size, MAX_INVENTORY_BYTES)
                return
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("vbms: retained-topic inventory %s unreadable (%s) — starting empty", path, e)
            return
        if not isinstance(data, dict):
            return
        budget = MAX_RETAINED_INVENTORY
        retained = data.get("retained") if isinstance(data.get("retained"), dict) else {}
        for iid, topics in retained.items():
            for t in topics if isinstance(topics, list) else ():
                if budget and self._instance_of(t) == iid:
                    self._known.setdefault(iid, set()).add(t)
                    budget -= 1
        if not budget:
            logger.warning("vbms: retained-topic inventory %s holds more than %d topics — "
                           "the rest is ignored", path, MAX_RETAINED_INVENTORY)
        # what an earlier process retained is held until this one publishes it again or takes
        # it back (a deleted instance: all of it, at the first cycle)
        self._retained = {i: set(t) for i, t in self._known.items()}

    def _track_inventory(self) -> None:
        """Fold this cycle into the on-disk view: a live instance adds what it retains; one that
        is gone is dropped once nothing of it is owed any more."""
        for iid, topics in self._retained.items():
            if not topics <= self._known.get(iid, set()):
                self._known[iid] = self._known.get(iid, set()) | topics
        owed_ids = {self._instance_of(t) for t in self._owed}
        for iid in [i for i in self._known if i not in self._retained and i not in owed_ids]:
            del self._known[iid]

    def _save_inventory(self) -> None:
        """Write the inventory when it changed: atomically (tmp + fsync + replace, 0600 like
        the other state files). A failed write is tried again next cycle, but said once and then
        at most every ten minutes (an unwritable config dir must not log every 2 s)."""
        path = self._inventory_path
        doc = self._inventory_doc()
        if path is None or doc == self._saved:
            return
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError as e:
            now = time.monotonic()
            if self._write_failed_at is None or now - self._write_failed_at >= _WRITE_FAIL_LOG_S:
                logger.error("vbms: could not write the retained-topic inventory %s: %s "
                             "(retried every cycle, said again in %d s)", path, e, _WRITE_FAIL_LOG_S)
                self._write_failed_at = now
            return
        if self._write_failed_at is not None:
            logger.info("vbms: the retained-topic inventory %s is written again", path)
            self._write_failed_at = None
        self._saved = doc

    def _spare_live_devices(self) -> None:
        """Never clear a topic under the id of a configured device (review M1): a Virtual BMS
        deleted and its id since given to a device — the device owns those topics now."""
        if self._device_ids is None or not self._owed:
            return
        try:
            live = set(self._device_ids())
        except Exception:                               # noqa: BLE001 — unknown: spare everything
            logger.exception("vbms: device ids unavailable — no retained withdrawal this cycle")
            live = {self._instance_of(t) for t in self._owed}
        spared = {t for t in self._owed if self._instance_of(t) in live or self._instance_of(t) is None}
        if not spared:
            return
        self._owed -= spared
        ids = {self._instance_of(t) for t in spared} - {None} - self._spared
        if ids:
            self._spared |= ids
            logger.warning("vbms: retained topics of %s left alone — the id is a configured device "
                           "now", ", ".join(sorted(ids)))

    def _withdraw(self, pub: Any, topics: set) -> None:
        """Delete retained topics nobody publishes any more (WK7-TELE-1): a field whose source
        went away, an instance that was deleted. Left alone, a late subscriber reads the last
        SOC of a bank that is no longer measured as if it were live."""
        self._owed |= set(topics)
        self._spare_live_devices()
        if not self._owed or not hasattr(pub, "clear_topics"):
            return
        # a clear that did not go out (the broker is away: clear_topics answers 0) stays OWED
        # and is tried again every cycle — forgetting it left the last SOC of a bank that is
        # no longer measured retained for good (R2D-TELE-1)
        for topic in sorted(self._owed):                # one by one: one failure owes only itself
            try:
                done = pub.clear_topics({topic})
            except Exception:                           # noqa: BLE001 — telemetry is best-effort
                logger.exception("vbms retained withdrawal failed")
                return
            if done is None or done >= 1:
                self._owed.discard(topic)

    def publish_once(self) -> int:
        """Publish every instance's current state once. Returns the number of field topics sent
        (for tests). No-op without a publisher that can publish absolute topics."""
        pub = self._publisher_of()
        if pub is None or not hasattr(pub, "publish_if_changed"):
            return 0
        sent = 0
        self._withdraw(pub, set())              # whatever an earlier cycle could not clear
        instances = list(self._manager.instances.values())
        for gone in [i for i in self._retained if i not in {x.id for x in instances}]:
            self._withdraw(pub, self._retained.pop(gone))        # a deleted instance: all of it
        states = []
        for inst in instances:
            base = self._topic_pattern.format(device=inst.id)
            st = self._manager.state(inst)
            now = {f"{base}/{field}" for field in st["values"]}
            held = self._retained.get(inst.id, set())
            self._withdraw(pub, {t for t in held if t not in now
                                 and t not in (f"{base}/armed", f"{base}/status")})
            self._retained[inst.id] = now | {f"{base}/armed", f"{base}/status"}
            self._owed -= self._retained[inst.id]       # published again: nothing owed for it
            states.append((base, st))
        # write-ahead: what is about to be retained is on disk first, so a crash right after a
        # publish never leaves a topic the next process does not know about (R2D-TELE-1)
        self._track_inventory()
        self._save_inventory()
        for base, st in states:
            for field, value in st["values"].items():
                if pub.publish_if_changed(f"{base}/{field}", value, retain=True,
                                          heartbeat=self._heartbeat):
                    sent += 1
            pub.publish_if_changed(f"{base}/armed", 1 if st["armed"] else 0, retain=True,
                                   heartbeat=self._heartbeat)
            pub.publish_if_changed(f"{base}/status", {
                "armed": st["armed"], "running": st["running"], "channel": st["channel"],
                "packs_total": st["packs_total"], "packs_live": st["packs_live"],
                "min_packs": st["min_packs"], "data_state": st["data_state"],
            }, retain=True, heartbeat=self._heartbeat)
        return sent

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.publish_once()
            except Exception:                           # noqa: BLE001 — telemetry never kills its thread
                logger.exception("vbms state publish failed")
            self._stop.wait(self._interval_s)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="vbms-state-pub", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval_s + 2)
        self._thread = None
