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
"""MQTT Publisher for Janitza UMG 512-PRO with change detection and custom topics."""

import math
import time
import json
import queue
import threading
from typing import Dict, Any, Optional, List

import paho.mqtt.client as mqtt

from . import __version__
from .config import MQTTConfig, SelectedRegister

import logging
logger = logging.getLogger(__name__)

# Retry configuration
RETRY_MAX_ATTEMPTS = 10
RETRY_INITIAL_DELAY = 2
RETRY_MAX_DELAY = 60
RETRY_BACKOFF_FACTOR = 2
RECONNECT_CHECK_INTERVAL = 30
# HA write commands waiting for the command worker. Far above any human rate
# through the HA UI — only a broker flood hits it, and flooding must drop
# commands, not grow an unbounded backlog of stale writes.
COMMAND_QUEUE_MAX = 32

# Home Assistant device classes
HA_DEVICE_CLASSES = {
    'V': 'voltage',
    'A': 'current',
    'W': 'power',
    'VA': 'apparent_power',
    'var': 'reactive_power',
    'Wh': 'energy',
    'kWh': 'energy',
    'Hz': 'frequency',
    '%': None,
    '°C': 'temperature',
}

HA_STATE_CLASSES = {
    'Wh': 'total_increasing',
    'kWh': 'total_increasing',
    'MWh': 'total_increasing',
    'varh': 'total_increasing',
    'kvarh': 'total_increasing',
    'VAh': 'total_increasing',
    'kVAh': 'total_increasing',
}


def apply_ha_typing(config: Dict, register) -> None:
    """Fill a discovery config's entity-typing keys for one register.

    Explicit per-register fields (device_class/state_class/entity_category/
    enabled_by_default/icon/suggested_display_precision) win; where a field is
    unset the unit heuristic fills device_class + state_class as before. The
    literal ``"none"`` explicitly SUPPRESSES an inferred device_class/state_class
    (e.g. a text/diagnostic sensor that must carry neither). Mutates ``config``.
    """
    # an enum/bitfield register decodes to TEXT — a unit, device_class or
    # state_class on it is invalid (HA rejects a measurement without a number),
    # so the numeric inference is suppressed unless the template is explicit.
    from .value_decode import is_textual
    textual = is_textual(register)

    if register.unit and not textual:
        config["unit_of_measurement"] = register.unit

    # device_class: explicit override → inference; "none" suppresses
    dc = (getattr(register, "device_class", "") or "").strip()
    if dc.lower() == "none":
        pass
    elif dc:
        config["device_class"] = dc
    elif not textual:
        inferred = HA_DEVICE_CLASSES.get(register.unit)
        if inferred:
            config["device_class"] = inferred

    # state_class: explicit override → inference; "none" suppresses. The
    # heuristic default of "measurement" is unchanged (a unit-less real
    # measurement such as power factor must keep it to stay in HA statistics);
    # a text/diagnostic register opts out with an explicit state_class "none".
    sc = (getattr(register, "state_class", "") or "").strip()
    if sc.lower() == "none":
        pass
    elif sc:
        config["state_class"] = sc
    elif not textual:
        config["state_class"] = HA_STATE_CLASSES.get(register.unit, "measurement")

    ec = (getattr(register, "entity_category", "") or "").strip()
    if ec:
        config["entity_category"] = ec
    ebd = getattr(register, "enabled_by_default", None)
    if ebd is not None:
        config["enabled_by_default"] = bool(ebd)
    icon = (getattr(register, "icon", "") or "").strip()
    if icon:
        config["icon"] = icon
    sdp = getattr(register, "suggested_display_precision", None)
    if sdp is not None:
        config["suggested_display_precision"] = int(sdp)


class MQTTPublisher:
    """
    MQTT Publisher for Janitza data.

    Features:
    - Custom topic per register from configuration
    - Two-phase cache: check before publish, confirm after success
    - NaN guard to prevent phantom republishes
    - Cache clear on reconnect to force re-publish current state
    - Home Assistant MQTT autodiscovery
    - Automatic reconnection
    """

    def __init__(self, config: MQTTConfig, registers: List[SelectedRegister],
                 publish_mode: str = 'changed', heartbeat_interval: int = 0):
        self.config = config
        self.registers = registers
        self.publish_mode = publish_mode
        # force a republish of an unchanged value after this many seconds (0=off)
        self.heartbeat_interval = int(heartbeat_interval or 0)

        self.client: Optional[mqtt.Client] = None
        self.connected = False
        self.last_values: Dict[str, Any] = {}
        self.last_publish_at: Dict[str, float] = {}   # topic → last publish time (heartbeat)
        self.lock = threading.Lock()
        # HA discovery configs we've published, so a later republish can clear the
        # ones for registers that were removed/disabled (else HA keeps a ghost
        # sensor forever on the retained config topic). Primary = a flat set;
        # non-primary = per-device sets.
        self._ha_discovery_topics: set = set()
        self._device_discovery_topics: Dict[str, set] = {}

        # Build register lookup by address
        self._register_map: Dict[int, SelectedRegister] = {
            r.address: r for r in registers if r.mqtt_enabled
        }

        # Stats
        self.messages_published = 0
        self.last_publish_ts = None
        self.messages_skipped = 0
        self.messages_failed = 0
        self.connection_count = 0
        self.last_disconnect_ts: Optional[float] = None

        # Extra HA-discovery hooks re-run on every (re)connect — used to publish
        # autodiscovery for non-primary devices and virtual meters, which the
        # publisher itself doesn't know about. Each hook is a no-arg callable.
        self.discovery_hooks: List = []

        # HA write-entities (number/select): command_topic → (device_id, register)
        # built while publishing discovery, plus the gated executor the on_message
        # callback hands a command to. Empty + no-op unless allow_write_entities.
        self._command_map: Dict[str, tuple] = {}
        self._write_handler = None
        # Commands execute on a dedicated worker, NOT on paho's network thread:
        # the handler does blocking Modbus I/O under the connection lock shared
        # with the pollers, and one slow RTU device would stall every publish
        # in the meantime (audit 2026-08-14 M4). Bounded queue: a flood drops.
        self._command_queue: queue.Queue = queue.Queue(maxsize=COMMAND_QUEUE_MAX)
        self._stop_commands = threading.Event()
        self._command_worker = None
        # Compatibility aliases (topic migrations): normalized once here so the
        # hot path does prefix math only. Every publish under `from` is ALSO
        # sent under `to` (leaf renames applied) — old consumers keep receiving
        # byte-identical topics while they migrate. See MQTTConfig.compat_aliases.
        self._compat_aliases: List[tuple] = []
        for al in (getattr(config, 'compat_aliases', None) or []):
            src = str(al.get('from', '')).rstrip('/')
            dst = str(al.get('to', '')).rstrip('/')
            if src and dst and src != dst:
                self._compat_aliases.append(
                    (src + '/', dst, dict(al.get('leaves') or {})))
        self.messages_aliased = 0
        # per-device availability (drives the HA connectivity binary_sensor);
        # publish only on change so a steady device doesn't churn the topic
        self._availability_last: Dict[str, str] = {}

        # Reconnection thread
        self._stop_reconnect = threading.Event()
        self._reconnect_thread = None

        if config.enabled:
            self._setup_client()

    def _setup_client(self):
        """Setup MQTT client with callbacks."""
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

        if self.config.username:
            self.client.username_pw_set(self.config.username, self.config.password)

        # TLS: encrypt the broker link. ca_cert verifies the broker;
        # client_cert+key add mutual TLS. Applied before connect().
        if getattr(self.config, 'tls_enabled', False):
            try:
                import ssl
                ca = self.config.tls_ca_cert or None
                cert = self.config.tls_client_cert or None
                key = self.config.tls_client_key or None
                self.client.tls_set(
                    ca_certs=ca,
                    certfile=cert if (cert and key) else None,
                    keyfile=key if (cert and key) else None,
                    cert_reqs=ssl.CERT_NONE if self.config.tls_insecure else ssl.CERT_REQUIRED,
                )
                if self.config.tls_insecure:
                    self.client.tls_insecure_set(True)
                logger.info("MQTT TLS enabled (mutual=%s, insecure=%s)",
                            bool(cert and key), self.config.tls_insecure)
                self._tls_broken = False
            except Exception as e:  # noqa: BLE001
                # FAIL CLOSED: TLS was requested but could not be configured —
                # connecting anyway would ship credentials + telemetry in the
                # clear while the operator believes the link is encrypted.
                logger.error("MQTT TLS setup FAILED (%s) — refusing to connect "
                             "in cleartext; fix the CA/cert config", e)
                self._tls_broken = True

        # Last Will Testament
        status_topic = f"{self.config.topic_prefix}/status"
        self.client.will_set(status_topic, payload="offline", qos=1, retain=True)

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        """Handle connection established."""
        if reason_code == 0:
            self.connected = True
            self.connection_count += 1
            self.last_disconnect_ts = None
            logger.info(f"MQTT connected to {self.config.broker}:{self.config.port}")

            # Clear cache to force re-publish of all current values
            # (broker restart loses retained messages)
            with self.lock:
                self.last_values.clear()
            # availability cache too (audit DP-15): a broker that lost retained
            # state would otherwise show every device topic EMPTY until the
            # device next flips state — could be days
            self._availability_last.clear()

            # Re-publish online status
            status_topic = f"{self.config.topic_prefix}/status"
            try:
                self.client.publish(status_topic, "online", qos=1, retain=True)
            except Exception:
                pass

            # Re-publish HA discovery on reconnect (primary + any registered
            # hooks: non-primary devices, virtual meters)
            if self.config.ha_discovery_enabled:
                self.publish_ha_discovery()
                for hook in list(self.discovery_hooks):
                    try:
                        hook()
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"discovery hook failed: {e}")
        else:
            self.connected = False
            logger.error(f"MQTT connection failed: {reason_code}")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        """Handle disconnection. Recovery is paho's job: with the network loop
        running and reconnect_delay_set(), it retries with backoff on its own.
        Spawning our thread here too would give TWO writers racing on the same
        socket — the custom thread exists only for the never-connected case
        (see connect()). Here we just record state for observability."""
        self.connected = False
        if reason_code != 0:
            self.last_disconnect_ts = time.time()
            logger.warning(f"MQTT disconnected unexpectedly: {reason_code} — paho auto-reconnect active")

    def _try_connect(self) -> bool:
        """Attempt a single connection."""
        if getattr(self, "_tls_broken", False):
            logger.error("MQTT: TLS requested but setup failed — not connecting "
                         "(fail-closed); no cleartext fallback")
            return False
        try:
            self.client.connect(self.config.broker, self.config.port, keepalive=60)
            self.client.loop_start()

            for _ in range(10):
                if self.connected:
                    break
                time.sleep(0.1)

            return self.connected
        except Exception as e:
            logger.warning(f"MQTT connection failed: {e}")
            return False

    def connect(self) -> bool:
        """Connect to MQTT broker with retry logic."""
        if not self.config.enabled:
            logger.info("MQTT publishing disabled")
            return False

        delay = RETRY_INITIAL_DELAY

        for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
            if self._try_connect():
                return True

            if attempt < RETRY_MAX_ATTEMPTS:
                logger.info(f"MQTT connection attempt {attempt}/{RETRY_MAX_ATTEMPTS} failed, retrying in {delay}s...")
                time.sleep(delay)
                delay = min(delay * RETRY_BACKOFF_FACTOR, RETRY_MAX_DELAY)

        logger.warning(f"MQTT: all {RETRY_MAX_ATTEMPTS} connection attempts failed. Will retry in background.")
        self._start_reconnect_thread()
        return False

    def _start_reconnect_thread(self):
        """Start background reconnection thread."""
        # Clear the stop flag before the alive-check: a previous thread still
        # winding down (past the join timeout) must resume duty rather than see
        # a stale set flag and exit, which would leave no retry running.
        self._stop_reconnect.clear()
        if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
            return

        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop,
            name="MQTT-Reconnect",
            daemon=True
        )
        self._reconnect_thread.start()
        logger.info("MQTT reconnection thread started")

    def _reconnect_loop(self):
        """Background loop for the never-connected case only: paho cannot
        auto-reconnect before a first successful connect() (there is no session
        to resume). Once the first connection succeeds, this thread exits and
        paho owns all subsequent recovery."""
        while not self._stop_reconnect.is_set():
            if not self.connected:
                logger.debug("Attempting MQTT reconnection...")
                if self._try_connect():
                    logger.info("MQTT reconnected successfully")
                    break
            else:
                break
            self._stop_reconnect.wait(RECONNECT_CHECK_INTERVAL)

    def disconnect(self):
        """Disconnect from MQTT broker."""
        self._stop_reconnect.set()
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            self._reconnect_thread.join(timeout=2)
        self._stop_commands.set()
        if self._command_worker and self._command_worker.is_alive():
            self._command_worker.join(timeout=2)

        if self.client:
            # Flush the retained "offline" status with qos=1 and WAIT for it to
            # leave before stopping the loop — otherwise loop_stop() can kill the
            # network thread before the message is sent, leaving a stale retained
            # "online" ghost that consumers (HA, alertd) never see clear.
            try:
                info = self.client.publish(f"{self.config.topic_prefix}/status",
                                           "offline", qos=1, retain=True)
                info.wait_for_publish(timeout=2)
            except Exception:  # noqa: BLE001
                pass
            self.client.loop_stop()
            self.client.disconnect()
        self.connected = False
        logger.info("MQTT disconnected")

    def publish_state(self, subtopic: str, payload: str, retain: bool = True) -> None:
        """Publish a raw payload to ``{prefix}/{subtopic}`` (e.g. virtual-meter
        state for alertd to monitor). No-op if disconnected; never raises."""
        try:
            if not self.connected or not self.client:
                return
            # via _publish so compat aliases apply (old-prefix consumers keep
            # seeing the retained state topics during a migration)
            self._publish(f"{self.config.topic_prefix}/{subtopic}", payload,
                          retain=retain)
        except Exception as e:  # noqa: BLE001
            logger.debug("publish_state(%s) failed: %s", subtopic, e)

    def _build_topic(self, register: SelectedRegister,
                     topic_prefix: Optional[str] = None) -> str:
        """Build MQTT topic for a register. ``topic_prefix`` is the per-device
        routing prefix (Tier 2); None = the legacy global prefix, which is also
        what device #1 passes — topics stay byte-identical."""
        prefix = topic_prefix or self.config.topic_prefix
        if register.mqtt_topic:
            return f"{prefix}/{register.mqtt_topic}"
        else:
            safe_name = register.name.lower().replace('[', '_').replace(']', '').replace('_g_', '')
            return f"{prefix}/{safe_name}"

    def _should_publish(self, topic: str, value: Any) -> bool:
        """
        Check if value should be published based on mode.
        Does NOT update cache — cache is updated after successful publish
        via _confirm_publish() to prevent data loss.
        """
        # NaN guard FIRST — a NaN must never be published (it serializes to the
        # non-numeric text "nan" that consumers can't parse), and NaN != NaN
        # would also bypass change detection below.
        if isinstance(value, float) and not math.isfinite(value):
            return False
        if self.publish_mode == 'all':
            return True

        with self.lock:
            if topic not in self.last_values:
                return True

            if self.last_values[topic] != value:
                return True

            # heartbeat: unchanged, but republish if it's been quiet too long so
            # the value keeps a fresh timestamp for HA/consumers
            if self.heartbeat_interval > 0:
                last = self.last_publish_at.get(topic, 0)
                if time.time() - last >= self.heartbeat_interval:
                    return True

            return False

    def _confirm_publish(self, topic: str, value: Any):
        """
        Update cache after successful publish.
        Store rounded float to match what was actually sent as payload,
        preventing phantom re-publishes from floating-point drift.
        """
        with self.lock:
            if isinstance(value, float):
                self.last_values[topic] = round(value, 3)
            else:
                self.last_values[topic] = value
            self.last_publish_at[topic] = time.time()   # heartbeat reference

    def _publish(self, topic: str, payload: str, retain: bool = None) -> bool:
        """Internal publish method."""
        if not self.connected:
            return False

        if retain is None:
            retain = self.config.retain

        try:
            result = self.client.publish(topic, payload, qos=self.config.qos, retain=retain)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                self.messages_published += 1
                self.last_publish_ts = time.time()
                # compat aliases: republish under the old prefix (leaf renames
                # applied) so not-yet-migrated consumers see identical topics.
                # Direct client.publish — never recursive, never affects the
                # primary result or the change-detection cache.
                for src, dst, leaves in self._compat_aliases:
                    if topic.startswith(src):
                        leaf = topic[len(src):]
                        alias_topic = f"{dst}/{leaves.get(leaf, leaf)}"
                        try:
                            self.client.publish(alias_topic, payload,
                                                qos=self.config.qos, retain=retain)
                            self.messages_aliased += 1
                        except Exception:  # noqa: BLE001
                            pass
                return True
            # NO_CONN/CONN_LOST: the socket died between the keepalive and this
            # publish. Connection state is owned by the paho callbacks
            # (on_disconnect fires and paho reconnects) — just count the miss.
            self.messages_failed += 1
            if result.rc in (mqtt.MQTT_ERR_NO_CONN, mqtt.MQTT_ERR_CONN_LOST):
                logger.warning("MQTT connection lost during publish")
            return False
        except Exception as e:
            self.messages_failed += 1
            logger.error(f"MQTT publish error: {e}")
            return False

    def publish(self, topic: str, value: Any, retain: bool = None) -> bool:
        """Publish a value to topic."""
        if isinstance(value, (dict, list)):
            payload = json.dumps(value)
        elif isinstance(value, float):
            payload = str(round(value, 3))
        else:
            payload = str(value)

        return self._publish(topic, payload, retain)

    def publish_if_changed(self, topic: str, value: Any, retain: bool = None) -> bool:
        """Publish only if value changed. Two-phase: check → publish → confirm."""
        if self._should_publish(topic, value):
            if self.publish(topic, value, retain):
                self._confirm_publish(topic, value)
                return True
            return False

        self.messages_skipped += 1
        return False

    def publish_register_data(self, poll_group: str, data: Dict[int, Dict],
                              topic_prefix: Optional[str] = None):
        """Publish register data from a poll group. ``topic_prefix`` routes the
        values of one device (Tier 2); omitted = legacy global prefix."""
        if not self.connected:
            return

        for address, item in data.items():
            register = item.get('register')
            value = item.get('value')

            if register is None or not register.mqtt_enabled:
                continue

            topic = self._build_topic(register, topic_prefix)
            self.publish_if_changed(topic, value)

    def publish_status(self, status: str):
        """Publish application status."""
        topic = f"{self.config.topic_prefix}/status"
        self.publish(topic, status, retain=True)

    def publish_ha_discovery(self) -> int:
        """Publish Home Assistant MQTT autodiscovery configs."""
        if not self.connected or not self.config.ha_discovery_enabled:
            return 0

        count = 0
        device_info = self._build_ha_device_info()

        published: set = set()
        for register in self.registers:
            if not register.mqtt_enabled:
                continue

            config = self._build_ha_sensor_config(register, device_info)
            if config:
                safe_id = f"{register.address}_{register.name.lower().replace('[', '_').replace(']', '')}"
                discovery_topic = f"{self.config.ha_discovery_prefix}/sensor/multibus/{safe_id}/config"
                published.add(discovery_topic)
                if self._publish(discovery_topic, json.dumps(config), retain=True):
                    count += 1

        # clear configs we published before but no longer do → no ghost sensor
        cleared = self._clear_stale_discovery(self._ha_discovery_topics - published)
        self._ha_discovery_topics = published
        logger.info(f"Published {count} HA discovery configs" +
                    (f" (cleared {cleared} stale)" if cleared else ""))
        return count

    def _clear_stale_discovery(self, topics) -> int:
        """Delete retained HA discovery configs by publishing an empty payload
        (HA treats an empty retained config as 'remove this entity')."""
        n = 0
        for topic in topics:
            if self._publish(topic, "", retain=True):
                n += 1
        return n

    def publish_device_discovery(self, device_id: str, device_name: str,
                                 topic_prefix: str, registers: List[SelectedRegister],
                                 model: str = "", write_rules: Dict = None) -> int:
        """HA autodiscovery for a NON-primary device: each becomes its own HA
        device (linked to the app via via_device=janitza_umg512), with sensors
        namespaced by device id so nothing collides with device #1 or other
        devices. Device #1 keeps using publish_ha_discovery() unchanged.

        ``write_rules`` (address → write rule) is supplied by the caller ONLY
        when writes are fully enabled (allow_write_entities + allow_writes); a
        register present there is published as a controllable number/select and
        its command topic is subscribed. The caller owns the gate; the actual
        write is re-validated by the command handler."""
        if not self.connected or not self.config.ha_discovery_enabled:
            return 0
        write_rules = write_rules or {}
        device_info = {
            "identifiers": [f"mbg_dev_{device_id}"],
            "name": device_name or device_id,
            "manufacturer": "multi-bus-gateway",
            "model": model or "Modbus device",
            "via_device": "janitza_umg512",
        }
        count = 0
        published: set = set()
        # rebuild this device's command subscriptions from scratch
        self._drop_device_commands(device_id)
        for register in registers:
            if not register.mqtt_enabled:
                continue
            safe_name = register.name.lower().replace('[', '_').replace(']', '').replace('_g_', '')
            topic = f"{topic_prefix}/{register.mqtt_topic or safe_name}"
            config = {
                "name": register.label or register.name,
                "state_topic": topic,
                "availability_topic": f"{self.config.topic_prefix}/status",
                "unique_id": f"mbg_dev_{device_id}_{register.address}_{safe_name}",
                "device": device_info,
            }
            component = "sensor"
            rule = write_rules.get(register.address)
            if rule is not None:
                component = self._write_entity_config(config, register, rule,
                                                      f"{topic}/set", device_id)
            else:
                apply_ha_typing(config, register)
            disc = f"{self.config.ha_discovery_prefix}/{component}/mbg_dev_{device_id}/{register.address}_{safe_name}/config"
            published.add(disc)
            if self._publish(disc, json.dumps(config), retain=True):
                count += 1
        # per-device connectivity binary_sensor (online/offline), fed by
        # publish_device_availability() from the health harvester
        bs = {
            "name": "Connectivity", "state_topic": f"{topic_prefix}/availability",
            "payload_on": "online", "payload_off": "offline",
            "device_class": "connectivity", "entity_category": "diagnostic",
            "availability_topic": f"{self.config.topic_prefix}/status",
            "unique_id": f"mbg_dev_{device_id}_connectivity", "device": device_info,
        }
        bs_disc = f"{self.config.ha_discovery_prefix}/binary_sensor/mbg_dev_{device_id}/connectivity/config"
        published.add(bs_disc)
        if self._publish(bs_disc, json.dumps(bs), retain=True):
            count += 1
        # clear this device's configs for registers it no longer exposes
        cleared = self._clear_stale_discovery(
            self._device_discovery_topics.get(device_id, set()) - published)
        self._device_discovery_topics[device_id] = published
        logger.info(f"Published {count} HA discovery configs for device {device_id}" +
                    (f" (cleared {cleared} stale)" if cleared else ""))
        return count

    def _write_entity_config(self, config: Dict, register, rule,
                             command_topic: str, device_id: str) -> str:
        """Turn a sensor config into a controllable number/select, register its
        command topic, and subscribe. Returns the HA component name. A register
        with an enum map → select (options = its labels); else → number (bounds
        from the write envelope). Enum/select carry no numeric typing."""
        config["command_topic"] = command_topic
        if getattr(register, "enum", None):
            component = "select"
            # options are the decoded labels — the state topic already carries
            # the decoded text, so HA's current option matches a label
            config["options"] = list(dict.fromkeys(str(v) for v in register.enum.values()))
        else:
            component = "number"
            apply_ha_typing(config, register)
            config.pop("state_class", None)      # invalid on a number entity
            if getattr(rule, "write_min", None) is not None:
                config["min"] = rule.write_min
            if getattr(rule, "write_max", None) is not None:
                config["max"] = rule.write_max
            config["mode"] = "box"               # free numeric entry (step-agnostic)
        self._command_map[command_topic] = (device_id, register)
        if self.client is not None:
            try:
                self.client.subscribe(command_topic)
            except Exception:  # noqa: BLE001
                pass
        return component

    def _drop_device_commands(self, device_id: str) -> None:
        """Forget (and unsubscribe) a device's command topics before a rebuild."""
        for topic in [t for t, (d, _r) in self._command_map.items() if d == device_id]:
            self._command_map.pop(topic, None)
            if self.client is not None:
                try:
                    self.client.unsubscribe(topic)
                except Exception:  # noqa: BLE001
                    pass

    def publish_device_availability(self, topic_prefix: str, online: bool) -> None:
        """Publish a device's online/offline state (retained) for its HA
        connectivity binary_sensor. No-op when disconnected or unchanged."""
        if not self.connected or not topic_prefix:
            return
        topic = f"{topic_prefix}/availability"
        payload = "online" if online else "offline"
        if self._availability_last.get(topic) == payload:
            return
        # confirm AFTER a successful publish (audit DP-15): marking first meant
        # a dropped 'offline' was never retried — a dead device stayed 'online'
        # in HA indefinitely. On failure the cache keeps the old state, so the
        # next tick retries.
        if self._publish(topic, payload, retain=True):
            self._availability_last[topic] = payload

    def set_command_write_handler(self, fn) -> None:
        """Install the gated executor for HA write commands. ``fn(device_id,
        register, payload_str)`` performs the fully-validated write (re-checks
        allow_writes + the template write envelope + rate limit + audit). Without
        it, incoming commands are ignored."""
        self._write_handler = fn
        self._start_command_worker()

    def _start_command_worker(self) -> None:
        if self._command_worker is not None and self._command_worker.is_alive():
            return
        self._stop_commands.clear()
        self._command_worker = threading.Thread(
            target=self._command_loop, name="mqtt-command-worker", daemon=True)
        self._command_worker.start()

    def _command_loop(self) -> None:
        """Drain HA write commands one at a time, in arrival order. The
        task_done/join pair lets tests wait deterministically."""
        while not self._stop_commands.is_set():
            try:
                device_id, register, payload = self._command_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._write_handler(device_id, register, payload)
            except Exception as e:  # noqa: BLE001
                logger.warning("MQTT command for %s addr=%s failed: %s", device_id,
                               getattr(register, "address", "?"), e)
            finally:
                self._command_queue.task_done()

    def _on_message(self, client, userdata, message):
        """Hand a command topic to the write worker. Every gate lives in the
        handler; here we only look the topic up, decode the payload and
        enqueue — the handler does blocking Modbus I/O, which must not run on
        paho's network thread. Never raises into the paho loop."""
        try:
            entry = self._command_map.get(message.topic)
            if entry is None or self._write_handler is None:
                return
            device_id, register = entry
            payload = message.payload.decode("utf-8", "replace").strip()
            self._command_queue.put_nowait((device_id, register, payload))
        except queue.Full:
            logger.warning("MQTT command on %s dropped: write queue full",
                           getattr(message, "topic", "?"))
        except Exception as e:  # noqa: BLE001
            logger.warning("MQTT command on %s failed: %s",
                           getattr(message, "topic", "?"), e)

    def publish_vmeter_discovery(self, meters: List[Dict]) -> int:
        """Publish HA autodiscovery for the virtual meters. Each meter becomes an
        HA device (linked to the Janitza via `via_device`) exposing serving
        state, throughput, connections, freshness, uptime and last error — read
        from the retained `…/vmeter/<id>/state` JSON. No electrical data here."""
        if not self.connected or not self.config.ha_discovery_enabled:
            return 0
        # (key, friendly, component, value_template, extra)
        specs = [
            ("serving", "serving", "binary_sensor",
             "{{ 'ON' if value_json.running else 'OFF' }}",
             {"payload_on": "ON", "payload_off": "OFF", "device_class": "connectivity"}),
            ("state", "state", "sensor", "{{ value_json.state }}", {"icon": "mdi:state-machine"}),
            ("req_rate", "req/s", "sensor", "{{ value_json.req_rate }}",
             {"unit_of_measurement": "req/s", "state_class": "measurement", "icon": "mdi:speedometer"}),
            ("requests", "requests", "sensor", "{{ value_json.requests }}",
             {"state_class": "total_increasing", "icon": "mdi:counter"}),
            ("errors", "errors", "sensor", "{{ value_json.errors }}",
             {"state_class": "total_increasing", "icon": "mdi:alert-circle-outline"}),
            ("connections", "connections", "sensor", "{{ value_json.conn_count }}",
             {"state_class": "measurement", "icon": "mdi:lan-connect"}),
            ("freshness", "data age", "sensor", "{{ value_json.freshness_age_s }}",
             {"unit_of_measurement": "s", "device_class": "duration", "icon": "mdi:clock-outline"}),
            ("uptime", "uptime", "sensor", "{{ value_json.uptime_s }}",
             {"unit_of_measurement": "s", "device_class": "duration", "icon": "mdi:timer-outline"}),
            ("last_error", "last error", "sensor",
             "{{ value_json.last_error.kind if value_json.last_error else 'none' }}",
             {"icon": "mdi:alert"}),
        ]
        count = 0
        for m in meters:
            mid = m.get("id")
            if not mid:
                continue
            state_topic = f"{self.config.topic_prefix}/vmeter/{mid}/state"
            device = {
                "identifiers": [f"janitza_vmeter_{mid}"],
                "name": f"Virtual Meter: {m.get('name', mid)}",
                "manufacturer": "multi-bus-gateway",
                "model": "Virtual Modbus meter",
                "via_device": "janitza_umg512",
            }
            for key, friendly, component, tmpl, extra in specs:
                config = {
                    "name": friendly,
                    "state_topic": state_topic,
                    "value_template": tmpl,
                    "availability_topic": f"{self.config.topic_prefix}/status",
                    "unique_id": f"janitza_vmeter_{mid}_{key}",
                    "device": device,
                }
                config.update(extra)
                topic = f"{self.config.ha_discovery_prefix}/{component}/janitza_vmeter/{mid}_{key}/config"
                if self._publish(topic, json.dumps(config), retain=True):
                    count += 1
        logger.info(f"Published {count} virtual-meter HA discovery configs ({len(meters)} meters)")
        return count

    def _build_ha_device_info(self) -> Dict:
        """Build Home Assistant device info block."""
        return {
            "identifiers": ["janitza_umg512"],
            "name": self.config.ha_device_name,
            "manufacturer": "Janitza electronics GmbH",
            "model": "UMG 512-PRO",
            "sw_version": __version__,
        }

    def _build_ha_sensor_config(self, register: SelectedRegister, device_info: Dict) -> Dict:
        """Build Home Assistant sensor discovery config."""
        topic = self._build_topic(register)

        safe_name = register.name.lower().replace('[', '_').replace(']', '').replace('_g_', '')
        unique_id = f"janitza_umg512_{register.address}_{safe_name}"

        config = {
            "name": register.label,
            "state_topic": topic,
            "availability_topic": f"{self.config.topic_prefix}/status",
            "unique_id": unique_id,
            "device": device_info,
        }

        apply_ha_typing(config, register)
        return config

    def update_config(self, new_config: MQTTConfig):
        """Update MQTT configuration."""
        self.config = new_config
        self.publish_mode = new_config.publish_mode
        self.heartbeat_interval = int(getattr(new_config, 'heartbeat_interval', 0) or 0)
        logger.info(f"MQTT config updated: {new_config.broker}:{new_config.port}")

    def update_registers(self, registers: List[SelectedRegister]):
        """Update register list."""
        self.registers = registers
        self._register_map = {r.address: r for r in registers if r.mqtt_enabled}
        logger.info(f"MQTT registers updated: {len(self._register_map)} enabled")

    def reconnect(self) -> bool:
        """Reconnect to MQTT broker with current config."""
        logger.info("MQTT reconnecting...")
        self.disconnect()

        if not self.config.enabled:
            logger.info("MQTT disabled, not reconnecting")
            return False

        self._setup_client()
        # disconnect() joined the HA command worker; without a restart every
        # post-reconnect command silently filled the queue to its cap and the
        # "queue full" warning blamed the wrong cause (audit DP-16)
        if self._write_handler is not None:
            self._start_command_worker()

        if self._try_connect():
            logger.info("MQTT reconnected successfully")
            return True
        else:
            logger.warning("MQTT reconnection failed, starting background retry")
            self._start_reconnect_thread()
            return False

    def get_stats(self) -> Dict:
        """Return publisher statistics."""
        return {
            'enabled': self.config.enabled,
            'connected': self.connected,
            'broker': self.config.broker,
            'port': self.config.port,
            'prefix': self.config.topic_prefix,
            'messages_published': self.messages_published,
            'last_publish_ts': self.last_publish_ts,
            'last_contact_age_s': round(time.time() - self.last_publish_ts, 1) if self.last_publish_ts else None,
            'messages_skipped': self.messages_skipped,
            'messages_failed': self.messages_failed,
            'messages_aliased': self.messages_aliased,
            'publish_mode': self.publish_mode,
            'connection_count': self.connection_count,
            'registered_topics': len(self._register_map),
            'disconnected_for_s': (round(time.time() - self.last_disconnect_ts, 1)
                                   if (not self.connected and self.last_disconnect_ts) else None),
        }
