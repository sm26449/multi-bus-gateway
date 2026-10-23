# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
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
"""MQTT input driver (southbound source): subscribe to an external broker and
turn incoming messages into the SAME normalized ``{address: {value, register, ts}}``
batches a ModbusClient/HttpClient produces, so every downstream sink (MQTT
re-publish, InfluxDB, HTTP output, virtual meters, calculated registers) works
unchanged.

Push-driven, not polled: a register is updated when a message arrives on its
topic. Value extraction reuses the HTTP driver's ``json_path`` (into a JSON
payload); a register with no json_path takes the whole payload as its number.
Each register may carry its own ``topic`` (MQTT wildcards + / # supported);
without one it uses the device's base topic.
"""
import json
import logging
import re
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional

from .counter_filter import DailyCounterFilter, MonotonicFilter
from .http_client import resolve_json_path, _coerce_numeric
from .value_decode import apply_corrections

logger = logging.getLogger(__name__)


def topic_matches(pattern: str, topic: str) -> bool:
    """MQTT topic-filter match supporting ``+`` (one level) and ``#`` (rest)."""
    if pattern == topic:
        return True
    pp, tp = pattern.split('/'), topic.split('/')
    for i, seg in enumerate(pp):
        if seg == '#':
            # '#' is a valid multi-level wildcard ONLY as the final segment; a
            # mid-pattern '#' (e.g. a/#/b) is an invalid filter → match nothing.
            return i == len(pp) - 1
        if i >= len(tp):
            return False
        if seg == '+':
            continue
        if seg != tp[i]:
            return False
    return len(pp) == len(tp)


_VAR_RE = re.compile(r"\{([a-z][a-z0-9_]{0,31})\}")
# a topic variable's value: one or more topic levels, never a wildcard
VAR_VALUE_RE = re.compile(r"^[^#+\s\x00]{1,128}$")

# the largest MQTT payload a source is allowed to carry (F-63, 3.83.0)
MAX_PAYLOAD_BYTES = 64 * 1024

KEEPALIVE_MIN_S, KEEPALIVE_MAX_S = 5.0, 3600.0


def parse_keepalive(raw: Any) -> Optional[Dict[str, Any]]:
    """``{topic, payload, first_payload, every_s}`` → the same, checked; None
    when absent, turned off (``false``) or unusable (keepalive_errors says
    why to the editor). ``first_payload`` goes out on connect (a Venus GX:
    empty = republish everything), ``payload`` every ``every_s`` after."""
    if not isinstance(raw, dict) or keepalive_errors(raw):
        return None
    payload = str(raw.get('payload', '') or '')
    first = raw.get('first_payload')
    return {'topic': raw['topic'].strip(), 'payload': payload,
            'first_payload': payload if first is None else str(first),
            'every_s': float(raw.get('every_s', 30) or 30)}


def keepalive_errors(raw: Any, where: str = 'keepalive') -> List[str]:
    if raw is None or raw is False:
        return []
    if not isinstance(raw, dict) or not isinstance(raw.get('topic'), str) or not raw['topic'].strip():
        return [f"{where}: {{topic, payload, first_payload, every_s}} with the topic, or false"]
    errs = []
    if any(c in raw['topic'] for c in '+#'):
        errs.append(f"{where}.topic: one topic, no wildcards")
    from .text_check import topic_issue
    _ti = topic_issue(raw['topic'], f"{where}.topic")
    if _ti:
        errs.append(_ti)
    for k in ('payload', 'first_payload'):
        if raw.get(k) is not None and not isinstance(raw.get(k), str):
            errs.append(f"{where}.{k}: text (may be empty)")
    try:
        every = float(raw.get('every_s', 30))
        if not (KEEPALIVE_MIN_S <= every <= KEEPALIVE_MAX_S):
            raise ValueError
    except (TypeError, ValueError):
        errs.append(f"{where}.every_s: seconds, {KEEPALIVE_MIN_S:g}..{KEEPALIVE_MAX_S:g}")
    return errs


class MqttInputClient:
    """MQTT subscriber with a ModbusClient/HttpClient-compatible surface."""

    # what a source without availability answers (and a stub built without
    # __init__, as some health tests do)
    availability_topic = ''
    vars: Dict[str, str] = {}
    available: Optional[bool] = None
    stale_after_s = 30.0
    keepalive: Optional[Dict[str, Any]] = None
    keepalives_sent = 0
    link_name = ''
    _link = None

    def __init__(self, mqtt_cfg: Dict[str, Any], registers: list, poll_groups: dict = None):
        self.broker = str(mqtt_cfg.get('broker', '') or '').strip()
        self.port = int(mqtt_cfg.get('port', 1883) or 1883)
        self.username = str(mqtt_cfg.get('username', '') or '')
        self.password = str(mqtt_cfg.get('password', '') or '')
        self.tls = bool(mqtt_cfg.get('tls', False))
        self.base_topic = str(mqtt_cfg.get('topic', '') or '').strip()
        # Retained messages are the broker's LAST value, redelivered on every
        # (re)subscribe — stamping them with arrival time would launder an
        # hours-old value as fresh into a composite vmeter, defeating the
        # fail/sentinel/hold convention. Dropped by default; opt in only when
        # the source truly publishes current state as retained.
        self.accept_retained = bool(mqtt_cfg.get('accept_retained', False))
        self.retained_dropped = 0
        # Availability (birth / last will): a source that says whether it is
        # alive is judged by THAT, not by the age of its last message — an
        # ESPHome node publishes only on change, so a relay that sits still
        # for an hour is healthy, not stale. While the source says online its
        # retained values are its current state and are taken; before it has
        # said anything they wait (the last one per topic), and an offline
        # source's retained values are dropped. Without availability the
        # message age decides, against the source's own stale_after_s.
        av = mqtt_cfg.get('availability') or {}
        self.availability_topic = str(av.get('topic', '') or '').strip() if isinstance(av, dict) else ''
        self.payload_online = str((av or {}).get('online', 'online')) if self.availability_topic else 'online'
        self.payload_offline = str((av or {}).get('offline', 'offline')) if self.availability_topic else 'offline'
        self.available: Optional[bool] = None
        self.available_since: Optional[float] = None
        self._held_retained: Dict[str, Any] = {}
        try:
            self.stale_after_s = max(1.0, float(mqtt_cfg.get('stale_after_s') or 30))
        except (TypeError, ValueError):
            self.stale_after_s = 30.0
        # topic variables: a template's topics may say {name}, filled per device
        # (a Tasmota's stat/<topic>/POWER and tele/<topic>/STATE differ before
        # the device's own name, so one base topic cannot cover both)
        tv = mqtt_cfg.get('vars') or {}
        self.vars = {str(k): str(v) for k, v in tv.items()} if isinstance(tv, dict) else {}
        # keepalive ({topic, payload, every_s}): a source that publishes only
        # while somebody asks (a Victron GX answers R/<portal>/keepalive) is
        # asked on connect and every every_s after
        self.keepalive = parse_keepalive(mqtt_cfg.get('keepalive'))
        self.keepalives_sent = 0
        # the connection this input reads through (mqtt_link.py): inputs with
        # one link name and one connection identity share it — a device's
        # components use their parent's id, any other device its own
        self.link_name = str(mqtt_cfg.get('link', '') or '')
        self._link = None
        # TLS to a device with its own certificate (a Victron GX on 8883)
        self.tls_ca = str(mqtt_cfg.get('tls_ca', '') or '').strip()
        self.tls_insecure = bool(mqtt_cfg.get('tls_insecure', False))
        # each register's last value, by address: (value, arrival mono, seq).
        # The guards and read-backs of a command sent through this source;
        # recorded AFTER the store has the value, and announced on _cond so a
        # waiting command wakes up
        self._last: Dict[int, tuple] = {}
        self._seq = 0
        self._cond = threading.Condition()
        # what happened on this source (commands sent through it), for the
        # device's event log — the Modbus connection keeps the same ring
        self.events: deque = deque(maxlen=200)
        self.registers = registers
        self.publish_callback = None
        self.connected = False
        self.messages = 0
        self.updates = 0
        self.last_msg_ts: Optional[float] = None
        self.last_msg_mono: Optional[float] = None
        self._client = None
        self._by_topic = self._index()
        # monotonic-counter guards, per register (same as the pollers);
        # re-seeded on reload since a fresh input is built each time
        self._counter_filters: Dict[int, MonotonicFilter] = {}
        self._daily_filters: Dict[int, DailyCounterFilter] = {}
        # SunSpec dynamic scale factors, same contract as the pollers: the
        # names any register's scale_from references, plus their last-good
        # exponents. apply_corrections used to run here without `siblings`,
        # so a scale_from register on an MQTT source was unconditionally
        # dropped (sf_missing) — contradicting the shared-pipeline claim
        # (audit 2026-10-01).
        self._sf_refs = {getattr(r, 'scale_from', '') for r in registers
                         if getattr(r, 'scale_from', '')}
        self._sf_last_good: Dict[str, float] = {}

    # ── register/topic mapping ────────────────────────────────────────────
    def _reg_topic(self, reg) -> str:
        return self._resolve((getattr(reg, 'topic', '') or '').strip())

    def _resolve(self, t: str) -> str:
        if '{' in t and self.vars:
            t = _VAR_RE.sub(lambda m: self.vars.get(m.group(1), m.group(0)), t)
        if t.startswith('~/'):
            # RELATIVE to the device's input base: base 'seplos/battery_3/#'
            # + '~/soc' → 'seplos/battery_3/soc'. This is what lets one
            # TEMPLATE serve N units — absolute topics would pin every unit
            # to the same source (audit follow-up, 2026-10-03).
            base = (self.base_topic or '').rstrip('#+').rstrip('/')
            return f"{base}/{t[2:]}" if base else t[2:]
        return t or self.base_topic

    def _index(self) -> Dict[str, list]:
        d: Dict[str, list] = {}
        for r in self.registers:
            t = self._reg_topic(r)
            if t:
                d.setdefault(t, []).append(r)
        return d

    def _availability(self) -> str:
        return self._resolve(self.availability_topic) if self.availability_topic else ''

    def _subscriptions(self) -> List[str]:
        subs = list(self._by_topic.keys()) or ([self.base_topic] if self.base_topic else [])
        av = self._availability()
        if av and subs and not any(topic_matches(s, av) for s in subs):
            subs.append(av)
        return subs

    def _match(self, topic: str) -> list:
        out = []
        for pat, regs in self._by_topic.items():
            if topic_matches(pat, topic):
                out.extend(regs)
        return out

    # ── paho callbacks ────────────────────────────────────────────────────
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        # A non-zero CONNACK (bad auth, not authorized, server unavailable) is
        # NOT a successful connection — treating it as one would mark the source
        # live while it receives nothing. paho keeps retrying on the loop thread.
        if getattr(reason_code, 'is_failure', False) or (
                isinstance(reason_code, int) and reason_code != 0):
            self.connected = False
            logger.warning("MQTT-in connect refused by %s:%s — %s",
                           self.broker, self.port, reason_code)
            return
        self.connected = True
        for t in self._subscriptions():
            try:
                client.subscribe(t)
            except Exception as e:  # noqa: BLE001
                logger.warning("MQTT-in subscribe %s failed: %s", t, e)
        logger.info("MQTT-in connected to %s:%s — subscribed: %s",
                    self.broker, self.port, self._subscriptions())
        if self.keepalive:
            self.send_keepalive(first=True)

    def _link_up(self) -> None:
        """The link this input reads through is connected; it has
        subscribed this input's topics and asks for the keepalive."""
        self.connected = True
        logger.info("MQTT-in %s:%s — subscribed: %s", self.broker, self.port, self._subscriptions())

    def _on_disconnect(self, *args):
        self.connected = False

    def _on_message(self, client, userdata, msg):
        try:
            self._on_message_inner(client, userdata, msg)
        except Exception as e:  # noqa: BLE001
            logger.error("MQTT-input message handling failed on %s: %s",
                         getattr(msg, 'topic', '?'), e)

    def _on_availability(self, msg) -> None:
        text = (msg.payload or b'').decode('utf-8', 'replace').strip()
        if text == self.payload_online:
            now_online = True
        elif text == self.payload_offline:
            now_online = False
        else:
            now_online = None            # cleared or unknown: back to message age
        if now_online != self.available:
            logger.info("MQTT-in %s: source %s", msg.topic,
                        {True: 'online', False: 'offline', None: 'availability unknown'}[now_online])
            self.available_since = time.monotonic()
        self.available = now_online
        held, self._held_retained = self._held_retained, {}
        if now_online:
            for m in held.values():
                self._on_message_inner(None, None, m)

    def _on_message_inner(self, client, userdata, msg):
        av = self._availability()
        if av and topic_matches(av, msg.topic):
            self._on_availability(msg)
            if not self._match(msg.topic):
                return
        # A source with availability: its retained values are its state while
        # it is online, wait while nobody knows, and are dropped when offline.
        if getattr(msg, 'retain', False) and av:
            if self.available is None:
                if len(self._held_retained) < 1024:
                    self._held_retained[msg.topic] = msg
                return
            if self.available is False:
                self.retained_dropped += 1
                return
        # A retained delivery carries no trustworthy measurement time; treat it
        # as absence (the staleness policy then does the right thing) unless the
        # operator explicitly opts in for this source.
        elif getattr(msg, 'retain', False) and not self.accept_retained:
            self.retained_dropped += 1
            return
        self.messages += 1
        self.last_msg_ts = time.time()
        self.last_msg_mono = time.monotonic()
        if msg.payload and len(msg.payload) > MAX_PAYLOAD_BYTES:
            # a measurement is a few hundred bytes; a megabyte is not one
            self.oversized_dropped = getattr(self, 'oversized_dropped', 0) + 1
            if self.oversized_dropped in (1, 10, 100, 1000):
                logger.warning("MQTT-in %s: payload of %d bytes dropped (cap %d, %d so far)",
                               msg.topic, len(msg.payload), MAX_PAYLOAD_BYTES, self.oversized_dropped)
            return
        raw = msg.payload.decode('utf-8', 'replace') if msg.payload else ''
        try:
            doc = json.loads(raw)
        except (ValueError, TypeError, RecursionError):
            # RecursionError: a deliberately deep document (F-63, 3.83.0)
            doc = None
        regs = self._match(msg.topic)
        if not regs:
            return
        # a JSON null ({"value": null} from a Victron GX) says the value is
        # not available: the store keeps its last good value as before, but a
        # command must not take it for the device's current state
        cleared = [r for r in regs if doc is not None and getattr(r, 'json_path', '')
                   and resolve_json_path(doc, r.json_path) is None]
        if cleared:
            with self._cond:
                for r in cleared:
                    self._last.pop(r.address, None)
        # extract each register's raw value once; SF registers are recorded
        # FIRST so same-message dependents scale on this message's exponent
        # (the pollers do the same prescan per batch)
        extracted: list = []
        for r in regs:
            jp = getattr(r, 'json_path', '') or ''
            try:
                if jp:
                    val = resolve_json_path(doc, jp) if doc is not None else None
                else:
                    val = doc if isinstance(doc, (int, float, bool)) else raw
                # TEXT payloads (BMS collectors publish 'ON'/'Standby'/...):
                # a `string` register carries the text as-is (bounded); an
                # enum register accepts its LABELS via reverse lookup —
                # symmetric with the HA write face, which maps label → code.
                # Unknown labels drop (validation), like any undecodable value.
                if isinstance(val, str):
                    _s = val.strip()
                    _dt = (getattr(r, 'data_type', '') or '').lower()
                    if _dt.startswith('string'):
                        if _s:
                            extracted.append((r, _s[:256]))
                        continue
                    _em = getattr(r, 'enum', None)
                    if _em:
                        _rev = {str(lbl): code for code, lbl in _em.items()}
                        if _s in _rev:
                            try:
                                val = int(_rev[_s])
                            except (TypeError, ValueError):
                                val = _rev[_s]
                val = _coerce_numeric(val)
            except (ValueError, TypeError, OverflowError, RecursionError) as e:
                # one hostile field must not cost the message its other
                # registers (F-63, 3.83.0)
                logger.debug("MQTT-in %s: register %s skipped: %s", msg.topic, getattr(r, 'name', '?'), e)
                continue
            if val is None:
                continue
            if (self._sf_refs and getattr(r, 'name', '') in self._sf_refs
                    and isinstance(val, (int, float)) and not isinstance(val, bool)
                    and abs(val) <= 10):
                self._sf_last_good[r.name] = val
            extracted.append((r, val))
        data: Dict[int, Dict] = {}
        for r, val in extracted:
            if isinstance(val, str):
                # a string register's text goes to the store as-is — the
                # numeric corrections below have nothing to say about it
                data[r.address] = {'value': val, 'register': r,
                                   'ts': self.last_msg_ts, 'mono': self.last_msg_mono}
                continue
            # SHARED correction pipeline: nan/enum/bits/scale/scale_from/
            # offset/monotonic/daily — the full poller sequence. This path
            # used to skip `siblings` (so a scale_from register on an MQTT
            # source was dropped as sf_missing) and `daily` (so daily: true
            # was silently ignored on this transport) (audit 2026-10-01).
            _cf = None
            if getattr(r, 'monotonic', False):
                _cf = self._counter_filters.get(r.address)
                if _cf is None:
                    _cf = self._counter_filters[r.address] = MonotonicFilter()
            _df = None
            if getattr(r, 'daily', False):
                _df = self._daily_filters.get(r.address)
                if _df is None:
                    _df = self._daily_filters[r.address] = DailyCounterFilter()
            val = apply_corrections(val, r, counter_filter=_cf,
                                    siblings=self._sf_last_good, daily_filter=_df)
            if val is None:
                continue                  # sentinel/decode/filter → hold last-good
            data[r.address] = {'value': val, 'register': r, 'ts': self.last_msg_ts, 'mono': self.last_msg_mono}
        if data and self.publish_callback:
            self.updates += len(data)
            try:
                self.publish_callback('mqtt', data)
            except Exception as e:  # noqa: BLE001
                # external audit: an exception here propagates into paho's
                # network thread, which has no guard — the thread dies,
                # `connected` stays True, and the source is silently dead
                # until a process restart. Log and keep the client alive.
                if isinstance(e, RuntimeError) and 'Event loop is closed' in str(e):
                    # the gateway is stopping: the web loop closed before this
                    # source's last message — nothing is lost, nothing to say
                    logger.debug("MQTT-input fan-out during shutdown for %s", msg.topic)
                else:
                    logger.error("MQTT-input fan-out failed for %s: %s", msg.topic, e)
        if data:
            # after the store: a command woken by its read-back never runs
            # ahead of the rules and sequences that read the store
            with self._cond:
                for addr, e in data.items():
                    self._seq += 1
                    self._last[addr] = (e['value'], e['mono'], self._seq)
                self._cond.notify_all()

    # ── lifecycle (ModbusClient-compatible) ───────────────────────────────
    def connect(self) -> bool:
        """Read through a link (mqtt_link.py): made and connected by the first
        input that needs it, shared by the inputs with its name and
        connection. True = wired up; the link state is in ``connected``."""
        if self._link is not None:
            return True
        from . import mqtt_link
        link = mqtt_link.attach(self)
        if link is None:
            return False
        self._link = link
        return True

    def start_polling(self):
        """MQTT input is push-driven (messages arrive on the link's loop) and
        its link keeps the keepalive: nothing to start."""

    def send_keepalive(self, first: bool = False) -> bool:
        """Ask the source to keep publishing; ``first`` asks for everything
        (a Venus GX republishes its whole tree on an empty keepalive). It is a
        read request, so a standby gateway sends it too."""
        if not self.keepalive:
            return False
        if self._link is not None:
            return self._link._send(self._resolve(self.keepalive['topic']), first=first)
        payload = self.keepalive['first_payload' if first else 'payload']
        ok, err = self.publish(self._resolve(self.keepalive['topic']), payload)
        if ok:
            self.keepalives_sent += 1
        else:
            logger.debug("MQTT-in keepalive not sent: %s", err)
        return ok

    # ── the write side: a command sent through this source ────────────────
    def record_event(self, level: str, kind: str, message: str) -> None:
        self.events.append({'ts': round(time.time(), 3), 'level': level,
                            'kind': kind, 'message': str(message)[:300]})

    def resolve_topic(self, topic: str) -> str:
        """A template topic made concrete for this device ({vars}, ~/)."""
        return self._resolve((topic or '').strip())

    def subscriptions(self) -> List[str]:
        return self._subscriptions()

    def publish(self, topic: str, payload: str) -> tuple:
        """Publish once: QoS 0, never retained. A command is an event, not a
        state for the broker to replay; and a QoS 1 message queued while the
        link is down would go out after a reconnect, minutes late — the
        command's read-back already tells whether it arrived. (ok, error)."""
        if not topic or any(c in topic for c in '+#'):
            return False, f"not a topic to publish on: {topic!r}"
        if self._link is not None:
            return self._link.publish(topic, payload)
        if self._client is None or not self.connected:
            return False, f"not connected to {self.broker}:{self.port}"
        try:
            info = self._client.publish(topic, payload, qos=0, retain=False)
        except Exception as e:  # noqa: BLE001
            return False, str(e)
        rc = getattr(info, 'rc', 0)
        return (True, '') if rc == 0 else (False, f"publish refused (rc {rc})")

    def alive(self) -> bool:
        """The source is alive: its availability says online, or — without
        availability — something arrived within stale_after_s. Values are
        judged by this, not one by one: a device that publishes on change
        keeps a still value valid for as long as it is alive."""
        if self.availability_topic and self.available is not None:
            return bool(self.available) and self.connected
        if not self.connected or self.last_msg_mono is None:
            return False
        return time.monotonic() - self.last_msg_mono <= self.stale_after_s

    def last(self, address: int) -> Optional[tuple]:
        """(value, arrival mono, seq) of a register's last value, or None."""
        return self._last.get(int(address))

    def wait_for(self, predicate, timeout: float) -> bool:
        """Wait until ``predicate()`` is true (checked on each new value) or
        the timeout passes; the predicate's last answer."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while True:
                if predicate():
                    return True
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)

    def update_registers(self, registers: list, poll_groups: dict = None):
        old_subs = set(self._subscriptions())
        self.registers = registers
        self._by_topic = self._index()
        with self._cond:
            self._last.clear()            # an address may now mean another field
        # a live register swap can re-map an address to a different field —
        # a stale monotonic baseline would then reject the new counter
        self._counter_filters.clear()
        self._daily_filters.clear()
        self._sf_refs = {getattr(r, 'scale_from', '') for r in registers
                         if getattr(r, 'scale_from', '')}
        self._sf_last_good.clear()
        if self._link is not None:
            self._link.resubscribe(self, sorted(old_subs), self._subscriptions())
        elif self._client and self.connected:
            new_subs = set(self._subscriptions())
            # removed topics used to stay subscribed forever — their
            # deliveries matched nothing, pure broker/CPU waste
            for t in old_subs - new_subs:
                try:
                    self._client.unsubscribe(t)
                except Exception:  # noqa: BLE001
                    pass
            for t in new_subs:
                try:
                    self._client.subscribe(t)
                except Exception:  # noqa: BLE001
                    pass

    def disconnect(self):
        """Leave the link; the last input on it closes the connection."""
        link, self._link = self._link, None
        if link is not None:
            link.detach(self)
            self._client = None
        self.connected = False

    def get_stats(self) -> Dict:
        return {
            'connected': self.connected,
            'messages': self.messages,
            'updates': self.updates,
            'successful_reads': self.updates,
            'failed_reads': 0,
            'last_success': self.last_msg_ts,
            'last_success_ts': self.last_msg_ts,
            # the age of its last message (it is pushed, never polled)
            'staleness_age_s': (round(time.monotonic() - self.last_msg_mono, 1)
                                if getattr(self, 'last_msg_mono', None) is not None else None),
            **({'available': self.available} if self.availability_topic else {}),
            **({'keepalives_sent': self.keepalives_sent} if self.keepalive else {}),
            'events': list(getattr(self, 'events', ())),
            'endpoint': f"mqtt://{self.broker}:{self.port} · {self.base_topic or '/'.join(self._subscriptions())}",
        }

    def data_health(self, stale_threshold_s: Optional[float] = None) -> Dict:
        if not self.registers:
            return {"status": "idle", "stale": False, "staleness_age_s": None}
        age = (round(time.monotonic() - self.last_msg_mono, 1)
               if self.last_msg_mono is not None else None)
        if self.availability_topic and self.available is not None:
            # the source's own word beats the age of its last message
            if not self.available:
                return {"status": "down", "stale": True, "staleness_age_s": age,
                        "available": False}
            return {"status": "ok" if self.connected else "degraded", "stale": False,
                    "staleness_age_s": age, "available": True}
        if stale_threshold_s is None:
            stale_threshold_s = self.stale_after_s
        if self.last_msg_mono is None:
            return {"status": "ok" if self.connected else "down",
                    "stale": False, "staleness_age_s": None}
        age = time.monotonic() - self.last_msg_mono   # NTP-step-proof
        stale = age > stale_threshold_s
        status = "ok" if (self.connected and not stale) else ("down" if stale else "degraded")
        return {"status": status, "stale": stale, "staleness_age_s": round(age, 1)}
