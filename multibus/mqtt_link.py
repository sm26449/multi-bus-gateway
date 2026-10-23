# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""One MQTT connection shared by the inputs that read through it.

A device's components read through their parent's connection: a Victron GX
with two batteries and three sensors is one TLS session to the GX, not six,
and one republish of its tree when they connect, not six.

Every MQTT input goes through a link. Inputs share one when they share its
NAME (a component uses its parent's id, any other device its own) and its
connection identity (broker, port, credentials, TLS) — so a standalone device
keeps a private connection, as before, through the same code.

The link:

- **counts subscriptions per topic.** A topic is unsubscribed only when no
  member uses it any more;
- **hands a message only to the members whose subscriptions match it.** A
  silent component must never look alive because its sibling talks;
- **owns the keepalive:**
  - one thread per link asks every distinct keepalive topic every `every_s`;
  - on (re)connect it asks once for everything;
  - a member that joins a link already up asks for everything itself,
    because a GX's values are not retained.

Locking: the link's own lock guards its bookkeeping only. No paho call is
ever made while it is held, because paho runs its callbacks under its own
locks.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_pool: Dict[Tuple, "MqttLink"] = {}
_pool_lock = threading.Lock()


def _topic_matches(pattern: str, topic: str) -> bool:
    from .mqtt_input import topic_matches
    return topic_matches(pattern, topic)


def link_key(member) -> Tuple:
    return (member.link_name or f"@{id(member)}", member.broker, member.port, member.username,
            member.password, member.tls, member.tls_ca, member.tls_insecure)


def attach(member) -> Optional["MqttLink"]:
    """The link this input reads through, made and connected when it is the
    first; None when the connection could not be set up (TLS)."""
    key = link_key(member)
    for _ in range(3):
        with _pool_lock:
            link = _pool.get(key)
            if link is None:
                # made AND started under the pool's lock, so nobody attaches
                # to a link without its client; start() does not block
                # (connect_async + loop_start)
                link = MqttLink(key, member)
                if not link.start():
                    return None
                _pool[key] = link
        if link.attach(member):
            return link
        # it closed between the lookup and the attach (its last member left)
        with _pool_lock:
            if _pool.get(key) is link:
                del _pool[key]
    return None


def links() -> List["MqttLink"]:
    with _pool_lock:
        return list(_pool.values())


class MqttLink:
    def __init__(self, key: Tuple, first):
        self.key = key
        self.name = key[0]
        self.broker, self.port = first.broker, first.port
        self._cfg = first
        self.client = None
        self.connected = False
        self._lock = threading.Lock()
        self._members: List[Any] = []
        self._subs: Counter = Counter()
        # keepalive topic → {payload, first_payload, every_s, last, owners}
        self._ka: Dict[str, Dict[str, Any]] = {}
        # temporary listeners (finding components): [(pattern, callback)]
        self._taps: List[Tuple[str, Callable]] = []
        self._stop = threading.Event()
        self._closed = False

    # ── the connection ────────────────────────────────────────────────────
    def start(self) -> bool:
        import paho.mqtt.client as mqtt
        m = self._cfg
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if m.username:
            c.username_pw_set(m.username, m.password)
        if m.tls:
            try:
                if m.tls_ca:
                    c.tls_set(ca_certs=m.tls_ca)
                else:
                    c.tls_set()
                if m.tls_insecure:
                    # a device with a self-signed certificate (a Victron GX):
                    # encrypted, but the certificate is not checked
                    c.tls_insecure_set(True)
            except Exception as e:  # noqa: BLE001
                # FAIL CLOSED: never fall through to a cleartext connect
                logger.error("MQTT-in TLS setup FAILED (%s) — refusing cleartext "
                             "connect to %s:%s", e, m.broker, m.port)
                return False
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        c.on_disconnect = self._on_disconnect
        self.client = c
        try:
            # paho owns the first connect AND every retry on its own thread,
            # so a broker that is down at startup is retried with backoff
            c.connect_async(m.broker, m.port, keepalive=60)
            c.loop_start()
        except Exception as e:  # noqa: BLE001 — bad host/args only; unreachable is retried
            logger.warning("MQTT-in setup failed (%s:%s): %s", m.broker, m.port, e)
            return False
        threading.Thread(target=self._keepalive_loop, daemon=True,
                         name=f"mqtt-link-{str(self.name)[:30]}").start()
        return True

    def _close(self) -> None:
        self._stop.set()
        c, self.client = self.client, None
        self.connected = False
        if c is not None:
            try:
                c.loop_stop()
                c.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # ── members ───────────────────────────────────────────────────────────
    def attach(self, member) -> bool:
        """Take a member in (a second call is nothing). On a link already
        up it subscribes what the member needs and asks for everything.
        False when the link has closed."""
        with self._lock:
            if self._closed:
                return False
            if member in self._members:
                return True
            self._members.append(member)
            member._link = self
            topics = member._subscriptions()
            new = [t for t in topics if self._subs[t] == 0]
            self._subs.update(topics)
            ka = self._add_keepalive(member)
            up = self.connected
        member._client = self.client
        if up:
            self._subscribe(new)
            member._link_up()
            if ka:
                self._send(ka, first=True)
        return True

    def detach(self, member) -> None:
        """Let a member go; the last one closes the link."""
        with self._lock:
            if member not in self._members:
                return
            self._members.remove(member)
            gone = self._uncount(member._subscriptions())
            self._drop_keepalive(member)
            last = not self._members
            if last:
                self._closed = True
        if last:
            with _pool_lock:
                if _pool.get(self.key) is self:
                    del _pool[self.key]
            self._close()
        elif self.connected:
            self._unsubscribe(gone)

    def resubscribe(self, member, old: List[str], new: List[str]) -> None:
        """A member's topics changed (its registers were edited live)."""
        with self._lock:
            if member not in self._members:
                return
            added = [t for t in set(new) - set(old) if self._subs[t] == 0]
            self._subs.update(new)
            gone = self._uncount(old)
            up = self.connected
        if up:
            self._unsubscribe(gone)
            self._subscribe(added)

    def _uncount(self, topics: List[str]) -> List[str]:
        """Under the lock: forget one use of each topic; the ones nobody uses."""
        out = []
        for t in topics:
            self._subs[t] -= 1
            if self._subs[t] <= 0:
                del self._subs[t]
                out.append(t)
        return out

    def members(self) -> List[Any]:
        with self._lock:
            return list(self._members)

    # ── the keepalive ─────────────────────────────────────────────────────
    def _add_keepalive(self, member) -> str:
        ka = member.keepalive
        if not ka:
            return ''
        topic = member._resolve(ka['topic'])
        e = self._ka.setdefault(topic, {'payload': ka['payload'], 'first_payload': ka['first_payload'],
                                        'every_s': ka['every_s'], 'last': 0.0, 'owners': []})
        e['every_s'] = min(e['every_s'], ka['every_s'])
        e['owners'].append(member)
        return topic

    def _drop_keepalive(self, member) -> None:
        for topic, e in list(self._ka.items()):
            if member in e['owners']:
                e['owners'].remove(member)
                if not e['owners']:
                    del self._ka[topic]

    def _send(self, topic: str, first: bool = False) -> bool:
        with self._lock:
            e = self._ka.get(topic)
            if e is None:
                return False
            payload = e['first_payload' if first else 'payload']
            owners = list(e['owners'])
            e['last'] = time.monotonic()
        ok, err = self.publish(topic, payload)
        if ok:
            for m in owners:
                m.keepalives_sent += 1
        else:
            logger.debug("MQTT-in keepalive not sent: %s", err)
        return ok

    def _keepalive_loop(self):
        while not self._stop.wait(1.0):
            if not self.connected:
                continue
            now = time.monotonic()
            with self._lock:
                due = [t for t, e in self._ka.items() if now - e['last'] >= e['every_s']]
            for t in due:
                self._send(t)

    # ── listening for a while ─────────────────────────────────────────────
    def tap(self, pattern: str, callback: Callable) -> None:
        with self._lock:
            self._taps.append((pattern, callback))
            new = self._subs[pattern] == 0
            self._subs[pattern] += 1
            up = self.connected
        if up and new:
            self._subscribe([pattern])

    def untap(self, pattern: str, callback: Callable) -> None:
        with self._lock:
            try:
                self._taps.remove((pattern, callback))
            except ValueError:
                return
            gone = self._uncount([pattern])
            up = self.connected
        if up:
            self._unsubscribe(gone)

    # ── paho ──────────────────────────────────────────────────────────────
    def publish(self, topic: str, payload: str) -> tuple:
        c = self.client
        if c is None or not self.connected:
            return False, f"not connected to {self.broker}:{self.port}"
        try:
            info = c.publish(topic, payload, qos=0, retain=False)
        except Exception as e:  # noqa: BLE001
            return False, str(e)
        rc = getattr(info, 'rc', 0)
        return (True, '') if rc == 0 else (False, f"publish refused (rc {rc})")

    def _subscribe(self, topics: List[str]) -> None:
        c = self.client
        for t in topics:
            try:
                c.subscribe(t)
            except Exception as e:  # noqa: BLE001
                logger.warning("MQTT-in subscribe %s failed: %s", t, e)

    def _unsubscribe(self, topics: List[str]) -> None:
        c = self.client
        for t in topics:
            try:
                c.unsubscribe(t)
            except Exception:  # noqa: BLE001
                pass

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        # a non-zero CONNACK (bad auth, not authorized) is not a connection
        if getattr(reason_code, 'is_failure', False) or (
                isinstance(reason_code, int) and reason_code != 0):
            self.connected = False
            logger.warning("MQTT-in connect refused by %s:%s — %s", self.broker, self.port, reason_code)
            return
        with self._lock:
            self.connected = True
            topics = list(self._subs)
            members = list(self._members)
            kas = list(self._ka)
        self._subscribe(topics)
        for m in members:
            m._link_up()
        logger.info("MQTT-in connected to %s:%s — %d member(s), %d topic(s)",
                    self.broker, self.port, len(members), len(topics))
        for t in kas:
            self._send(t, first=True)

    def _on_disconnect(self, *args):
        self.connected = False
        for m in self.members():
            m.connected = False

    def _on_message(self, client, userdata, msg):
        topic = getattr(msg, 'topic', '')
        with self._lock:
            members = list(self._members)
            taps = list(self._taps)
        for m in members:
            if any(_topic_matches(s, topic) for s in m._subscriptions()):
                m._on_message(client, userdata, msg)
        for pattern, cb in taps:
            if _topic_matches(pattern, topic):
                try:
                    cb(msg)
                except Exception as e:  # noqa: BLE001
                    logger.warning("MQTT-in listener on %s failed: %s", pattern, e)
