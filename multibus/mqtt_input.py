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
import threading
import time
from typing import Any, Dict, List, Optional

from .http_client import resolve_json_path, _coerce_numeric

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


class MqttInputClient:
    """MQTT subscriber with a ModbusClient/HttpClient-compatible surface."""

    def __init__(self, mqtt_cfg: Dict[str, Any], registers: list, poll_groups: dict = None):
        self.broker = str(mqtt_cfg.get('broker', '') or '').strip()
        self.port = int(mqtt_cfg.get('port', 1883) or 1883)
        self.username = str(mqtt_cfg.get('username', '') or '')
        self.password = str(mqtt_cfg.get('password', '') or '')
        self.tls = bool(mqtt_cfg.get('tls', False))
        self.base_topic = str(mqtt_cfg.get('topic', '') or '').strip()
        self.registers = registers
        self.publish_callback = None
        self.connected = False
        self.messages = 0
        self.updates = 0
        self.last_msg_ts: Optional[float] = None
        self._client = None
        self._by_topic = self._index()

    # ── register/topic mapping ────────────────────────────────────────────
    def _reg_topic(self, reg) -> str:
        return (getattr(reg, 'topic', '') or '').strip() or self.base_topic

    def _index(self) -> Dict[str, list]:
        d: Dict[str, list] = {}
        for r in self.registers:
            t = self._reg_topic(r)
            if t:
                d.setdefault(t, []).append(r)
        return d

    def _subscriptions(self) -> List[str]:
        subs = list(self._by_topic.keys())
        return subs or ([self.base_topic] if self.base_topic else [])

    def _match(self, topic: str) -> list:
        out = []
        for pat, regs in self._by_topic.items():
            if topic_matches(pat, topic):
                out.extend(regs)
        return out

    # ── paho callbacks ────────────────────────────────────────────────────
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = True
        for t in self._subscriptions():
            try:
                client.subscribe(t)
            except Exception as e:  # noqa: BLE001
                logger.warning("MQTT-in subscribe %s failed: %s", t, e)
        logger.info("MQTT-in connected to %s:%s — subscribed: %s",
                    self.broker, self.port, self._subscriptions())

    def _on_disconnect(self, *args):
        self.connected = False

    def _on_message(self, client, userdata, msg):
        self.messages += 1
        self.last_msg_ts = time.time()
        raw = msg.payload.decode('utf-8', 'replace') if msg.payload else ''
        try:
            doc = json.loads(raw)
        except (ValueError, TypeError):
            doc = None
        regs = self._match(msg.topic)
        if not regs:
            return
        data: Dict[int, Dict] = {}
        for r in regs:
            jp = getattr(r, 'json_path', '') or ''
            if jp:
                val = resolve_json_path(doc, jp) if doc is not None else None
            else:
                val = doc if isinstance(doc, (int, float, bool)) else raw
            val = _coerce_numeric(val)
            if val is None:
                continue
            data[r.address] = {'value': val, 'register': r, 'ts': self.last_msg_ts}
        if data and self.publish_callback:
            self.updates += len(data)
            self.publish_callback('mqtt', data)

    # ── lifecycle (ModbusClient-compatible) ───────────────────────────────
    def connect(self) -> bool:
        import paho.mqtt.client as mqtt
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if self.username:
            c.username_pw_set(self.username, self.password)
        if self.tls:
            try:
                c.tls_set()
            except Exception as e:  # noqa: BLE001
                logger.warning("MQTT-in TLS setup failed: %s", e)
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        c.on_disconnect = self._on_disconnect
        self._client = c
        try:
            c.connect(self.broker, self.port, keepalive=60)
            c.loop_start()
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("MQTT-in connect failed (%s:%s): %s", self.broker, self.port, e)
            return False

    def start_polling(self):
        """No-op: MQTT input is push-driven (messages arrive on the paho loop)."""

    def update_registers(self, registers: list, poll_groups: dict = None):
        self.registers = registers
        self._by_topic = self._index()
        if self._client and self.connected:
            for t in self._subscriptions():
                try:
                    self._client.subscribe(t)
                except Exception:  # noqa: BLE001
                    pass

    def disconnect(self):
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.connected = False

    def get_stats(self) -> Dict:
        return {
            'connected': self.connected,
            'messages': self.messages,
            'updates': self.updates,
            'successful_reads': self.updates,
            'failed_reads': 0,
            'last_success': self.last_msg_ts,
            'endpoint': f"mqtt://{self.broker}:{self.port} · {self.base_topic or '/'.join(self._subscriptions())}",
        }

    def data_health(self, stale_threshold_s: float = 30) -> Dict:
        if not self.registers:
            return {"status": "ok", "stale": False, "staleness_age_s": None}
        if self.last_msg_ts is None:
            return {"status": "ok" if self.connected else "down",
                    "stale": False, "staleness_age_s": None}
        age = time.time() - self.last_msg_ts
        stale = age > stale_threshold_s
        status = "ok" if (self.connected and not stale) else ("down" if stale else "degraded")
        return {"status": status, "stale": stale, "staleness_age_s": round(age, 1)}
