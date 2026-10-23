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
"""Devices that announce themselves over MQTT, found and offered for adoption
(docs/mqtt-discovery.md).

The gateway listens, on its own broker, to the topics devices announce
themselves on (retained, so a restart finds them all again). Each rule turns
an announcement into a device it could read: its name, its template, its
topics. Nothing is made by itself: a found device waits in Devices → Found on
MQTT until someone adopts it (a device made OFF) or ignores it.

Built-in rules:
- ESP nodes of the gateway's ESP nodes service (mbg/nodes/<mac>/announce):
  configured nodes, read with the template that service made for each;
- Tasmota (tasmota/discovery/<mac>/config): read with tasmota_generic, its
  topics from the announcement as {stat} and {tele}.

More rules, declarative, in config/mqtt_discovery.json `rules` (below).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .mqtt_input import VAR_VALUE_RE, topic_matches

logger = logging.getLogger(__name__)

GATEWAY_BROKER = "@gateway"
MAX_FOUND = 500
_SEG_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _get(doc: Any, path: str) -> Any:
    for part in (path or "").split("."):
        if not isinstance(doc, dict):
            return None
        doc = doc.get(part)
    return doc


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", str(s or "").lower()).strip("-")[:48] or "device"


# ── built-in rules ─────────────────────────────────────────────────────────
def _mbg_node(topic: str, doc: Dict) -> Optional[Dict]:
    mac = topic.split("/")[2].lower()
    if doc.get("firmware") != "configured":
        return None                       # the basic image: nothing to read yet
    name = str(doc.get("name") or mac)
    return {"uid": mac, "name": name, "device_id": _slug(name), "template": f"esphome_node_{mac}",
            "connection": {"protocol": "mqtt", "broker": GATEWAY_BROKER, "port": 1883, "topic": f"mbg/nodes/{mac}"},
            "info": {k: doc.get(k) for k in ("chip", "image", "ip") if doc.get(k)},
            "missing_template": "its template is made by the ESP nodes service: send the node to MBG from its page there"}


def _tasmota(topic: str, doc: Dict) -> Optional[Dict]:
    mac = topic.split("/")[2].lower()
    t = str(doc.get("t") or "")
    ft = str(doc.get("ft") or "%prefix%/%topic%/")
    tp = doc.get("tp") or ["cmnd", "stat", "tele"]
    if not t or not isinstance(tp, list) or len(tp) < 3:
        return None

    def base(prefix: str) -> str:
        b = (ft.replace("%prefix%", str(prefix)).replace("%topic%", t)
               .replace("%hostname%", str(doc.get("hn") or "")).replace("%id%", mac[-6:].upper()))
        return b.rstrip("/")
    stat, tele = base(tp[1]), base(tp[2])
    if not (VAR_VALUE_RE.match(stat) and VAR_VALUE_RE.match(tele)):
        return None
    name = str(doc.get("dn") or doc.get("hn") or t)
    relays = sum(1 for r in (doc.get("rl") or []) if r)
    return {"uid": mac, "name": name, "device_id": _slug(name), "template": "tasmota_generic",
            "connection": {"protocol": "mqtt", "broker": GATEWAY_BROKER, "port": 1883, "topic": tele,
                           "vars": {"stat": stat, "tele": tele},
                           "availability": {"topic": "{tele}/LWT", "online": str(doc.get("onln") or "Online"),
                                            "offline": str(doc.get("ofln") or "Offline")}},
            "info": {"model": doc.get("md"), "ip": doc.get("ip"), "relays": relays, "version": doc.get("sw")}}


BUILTIN = [
    {"id": "mbg_node", "label": "ESP node", "topic": "mbg/nodes/+/announce", "fn": _mbg_node},
    {"id": "tasmota", "label": "Tasmota", "topic": "tasmota/discovery/+/config", "fn": _tasmota},
]


def rule_errors(r: Dict) -> List[str]:
    """A declarative rule of the user's:
    {id, label, topic (with + where the device's id is), id_segment (which level
    of the topic is its id, from 0), name (a JSON path of the announcement),
    match {path: value} (optional), template ("…{id}…" allowed), base_topic
    ("…{id}…"), vars {name: JSON path} (optional)}."""
    errs = []
    if not re.match(r"^[a-z][a-z0-9_]{1,31}$", str(r.get("id") or "")):
        errs.append("id: a–z 0–9 _")
    if any(b["id"] == r.get("id") for b in BUILTIN):
        errs.append(f"id {r.get('id')!r} is a built-in rule's")
    topic = str(r.get("topic") or "")
    if not topic or "#" in topic:
        errs.append("topic: the announcement topic, + for the device's level, no #")
    seg = r.get("id_segment")
    if not isinstance(seg, int) or not 0 <= seg < len(topic.split("/")):
        errs.append("id_segment: which level of the topic is the device's id (from 0)")
    for k in ("template", "base_topic"):
        if not str(r.get(k) or ""):
            errs.append(f"{k}: required")
    return errs


def _declared(rule: Dict) -> Callable[[str, Dict], Optional[Dict]]:
    def fn(topic: str, doc: Dict) -> Optional[Dict]:
        uid = topic.split("/")[rule["id_segment"]]
        if not _SEG_RE.match(uid):
            return None
        for path, want in (rule.get("match") or {}).items():
            if _get(doc, path) != want:
                return None
        name = str(_get(doc, rule.get("name") or "") or uid)
        base = str(rule["base_topic"]).replace("{id}", uid)
        conn = {"protocol": "mqtt", "broker": GATEWAY_BROKER, "port": 1883, "topic": base}
        vs = {k: str(_get(doc, p)) for k, p in (rule.get("vars") or {}).items() if _get(doc, p) is not None}
        if any(not VAR_VALUE_RE.match(v) for v in vs.values()):
            return None
        if vs:
            conn["vars"] = vs
        return {"uid": uid, "name": name, "device_id": _slug(name), "template": str(rule["template"]).replace("{id}", uid),
                "connection": conn, "info": {}}
    return fn


class MqttDiscovery:
    """The found devices, and what was decided about them (ignored)."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self.found: Dict[str, Dict] = {}
        self.enabled = True
        self.ignored: List[str] = []
        self.user_rules: List[Dict] = []
        self.rule_problems: Dict[str, List[str]] = {}
        self.connected = False
        self.error = ""
        self._client = None
        self._load()

    # ── state file ──────────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            with open(self.path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        self.enabled = bool(d.get("enabled", True))
        self.ignored = [str(x) for x in (d.get("ignored") or [])][:2000]
        self.user_rules, self.rule_problems = [], {}
        for r in d.get("rules") or []:
            errs = rule_errors(r) if isinstance(r, dict) else ["a rule is a mapping"]
            if errs:
                self.rule_problems[str((r or {}).get("id") if isinstance(r, dict) else r)] = errs
            else:
                self.user_rules.append(r)

    def _save(self) -> None:
        data = {"enabled": self.enabled, "ignored": self.ignored, "rules": self.user_rules}
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.path)

    def rules(self) -> List[Dict]:
        return BUILTIN + [{**r, "fn": _declared(r)} for r in self.user_rules]

    # ── what arrives ────────────────────────────────────────────────────────
    def handle(self, topic: str, payload: bytes, now: Optional[float] = None) -> None:
        for rule in self.rules():
            if not topic_matches(rule["topic"], topic):
                continue
            key = None
            try:
                doc = json.loads(payload.decode("utf-8")) if payload else None
                item = rule["fn"](topic, doc) if isinstance(doc, dict) else None
            except (ValueError, UnicodeDecodeError, IndexError, KeyError, TypeError) as e:
                logger.debug("discovery %s: %s ignored: %s", rule["id"], topic, e)
                item = None
            with self._lock:
                if item is None:
                    # an empty (cleared) announcement forgets it
                    if not payload:
                        for k in [k for k, v in self.found.items() if v.get("topic") == topic]:
                            self.found.pop(k, None)
                    continue
                key = f"{rule['id']}:{item['uid']}"
                if key not in self.found and len(self.found) >= MAX_FOUND:
                    continue
                self.found[key] = {**item, "key": key, "rule": rule["id"], "kind": rule.get("label") or rule["id"],
                                   "topic": topic, "seen_at": now if now is not None else time.time()}

    # ── decisions ───────────────────────────────────────────────────────────
    def ignore(self, key: str, on: bool = True) -> None:
        with self._lock:
            if on and key not in self.ignored:
                self.ignored.append(key)
            if not on and key in self.ignored:
                self.ignored.remove(key)
            self._save()

    def set_enabled(self, on: bool) -> None:
        with self._lock:
            self.enabled = bool(on)
            self._save()

    def listing(self, devices: List[Any], template_exists: Callable[[str], bool]) -> List[Dict]:
        """Every found device with its state: new | in_mbg | template_missing | ignored."""
        known_topics = {}
        for d in devices:
            m = getattr(d, "mqtt_in", None) or {}
            known_topics[(str(m.get("topic") or ""), json.dumps(m.get("vars") or {}, sort_keys=True))] = d.id
        out = []
        with self._lock:
            items = [dict(v) for v in self.found.values()]
            ignored = set(self.ignored)
        for it in items:
            c = it["connection"]
            dev = known_topics.get((c["topic"], json.dumps(c.get("vars") or {}, sort_keys=True)))
            if dev:
                it.update(state="in_mbg", device=dev)
            elif it["key"] in ignored:
                it["state"] = "ignored"
            elif not template_exists(it["template"]):
                it["state"] = "template_missing"
            else:
                it["state"] = "new"
            out.append(it)
        return sorted(out, key=lambda x: (x["state"] != "new", x["kind"], x["name"].lower()))

    def get(self, key: str) -> Optional[Dict]:
        with self._lock:
            v = self.found.get(key)
            return dict(v) if v else None

    # ── listening ───────────────────────────────────────────────────────────
    def start(self, broker: Dict) -> None:
        """Listen on the gateway's own broker ({broker, port, username,
        password, tls}). Never raises."""
        if not self.enabled or not broker.get("broker"):
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.error = "paho-mqtt is not installed"
            return
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if broker.get("username"):
            c.username_pw_set(broker["username"], broker.get("password") or "")
        if broker.get("tls"):
            try:
                c.tls_set()
            except Exception as e:  # noqa: BLE001 — fail closed, no cleartext
                self.error = f"TLS: {e}"
                return

        def on_connect(client, _u, _f, rc, _p=None):
            self.connected = not getattr(rc, "is_failure", False)
            self.error = "" if self.connected else f"broker refused: {rc}"
            if self.connected:
                for r in self.rules():
                    client.subscribe(r["topic"], 1)

        def on_message(_c, _u, msg):
            try:
                self.handle(msg.topic, msg.payload)
            except Exception:  # noqa: BLE001 — never kill paho's thread
                logger.exception("discovery: %s", msg.topic)

        c.on_connect = on_connect
        c.on_disconnect = lambda *a: setattr(self, "connected", False)
        c.on_message = on_message
        try:
            c.connect_async(broker["broker"], int(broker.get("port") or 1883), 60)
            c.loop_start()
            self._client = c
        except Exception as e:  # noqa: BLE001
            self.error = f"cannot reach {broker.get('broker')}: {e}"

    def stop(self) -> None:
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
