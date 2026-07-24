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
"""Generate ESPHome node YAML from a gateway device template (Device Builder).

The differentiator of the Builder section: pick a Modbus device template +
a register subset, and get (a) firmware YAML for an ESP32/ESP8266 that polls
the meter over RS485 and publishes each value to MQTT, and (b) the exact
PAIRED gateway artifacts — a user device-template and an /api/devices payload
— whose topics match the firmware byte-for-byte. The node and the gateway are
two halves of one contract, both derived from the same source template, so
"adopt" is zero-configuration.

Topic contract (we control BOTH ends, so it is explicit, not inferred from
ESPHome's object_id sanitization): every sensor gets
    state_topic: <prefix>/<safe_reg_name>/state        (plain scalar payload)
and availability is ESPHome's stock LWT on <prefix>/status (online/offline).
The paired template rows carry the same topic with NO json_path — the
MQTT-input driver consumes whole-payload scalars natively (mqtt_input.py).

Data-type mapping (gateway template → modbus_controller value_type):
    float/float32→FP32  int16/short→S_WORD  uint16→U_WORD
    int32→S_DWORD  uint32→U_DWORD  int64/long64→S_QWORD  uint64→U_QWORD
    (little-endian templates get the _R variants; FP64/double and string
    inputs are reported as skipped — modbus_controller has no equivalent.)
Scale keeps the gateway semantic (engineering = raw / scale) via a
`multiply: 1/scale` filter, so the node publishes engineering units and the
paired template rows use scale=1.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

_VALUE_TYPES = {
    'float': 'FP32', 'float32': 'FP32',
    'int16': 'S_WORD', 'short': 'S_WORD', 'uint16': 'U_WORD',
    'int32': 'S_DWORD', 'uint32': 'U_DWORD',
    'int64': 'S_QWORD', 'long64': 'S_QWORD', 'uint64': 'U_QWORD',
}
# _R variants exist for multi-register types only
_R_CAPABLE = {'FP32', 'S_DWORD', 'U_DWORD', 'S_QWORD', 'U_QWORD'}

_REGISTER_TYPES = {'holding': 'holding', 'input': 'read',
                   'coil': 'coil', 'discrete': 'discrete_input'}

_NODE_NAME_RE = re.compile(r'^[a-z0-9]([a-z0-9-]{0,22}[a-z0-9])?$')
_PIN_RE = re.compile(r'^GPIO[0-9]{1,2}$')

# registers per modbus_controller command range; ESPHome batches consecutive
# addresses itself, this only bounds our per-sensor emission
_MAX_SENSORS = 200


def safe_topic_name(name: str) -> str:
    """Register name → topic segment (deterministic on both ends)."""
    s = re.sub(r'[^A-Za-z0-9_-]+', '_', str(name or '')).strip('_')
    return s or 'reg'


def _yq(s: str) -> str:
    """Quote a YAML scalar defensively (labels/units may hold anything)."""
    return '"' + str(s).replace('\\', '\\\\').replace('"', '\\"') + '"'


def _fmt_multiply(scale: float) -> str:
    """1/scale with enough digits to round-trip float32-scaled values."""
    inv = 1.0 / float(scale)
    txt = f"{inv:.10g}"
    return txt


def generate_node(payload: Dict[str, Any], template, poll_groups: Dict,
                  mqtt_defaults: Dict[str, Any]) -> Dict[str, Any]:
    """Build the firmware YAML + paired gateway artifacts.

    payload (validated here, ValueError on bad input):
      node:  name (mDNS-safe), friendly_name?, platform esp32|esp8266,
             board?, wifi_ap_fallback? (default true)
      uart:  tx_pin, rx_pin, flow_control_pin?, baud_rate, parity, stop_bits
      modbus: unit_id (1..247)
      mqtt:  broker?, port?, username?, topic_prefix?  (defaults from the
             gateway's own northbound broker — node lands on the same bus)
      registers: [names] — subset of template register names ('' = defaults)

    Returns {yaml, node_yaml_name, topics, secrets, warnings,
             device_template, device_payload}.
    """
    node = dict(payload.get('node') or {})
    uart = dict(payload.get('uart') or {})
    modb = dict(payload.get('modbus') or {})
    mqtt = dict(payload.get('mqtt') or {})
    wanted = payload.get('registers') or []

    # ---- node identity ---------------------------------------------------------
    name = str(node.get('name', '') or '').strip().lower()
    if not _NODE_NAME_RE.match(name):
        raise ValueError("node.name must be mDNS-safe: lowercase letters, "
                         "digits, hyphens, max 24 chars (e.g. hall-meter)")
    friendly = str(node.get('friendly_name', '') or '').strip() or name
    platform = str(node.get('platform', 'esp32') or 'esp32').lower()
    if platform not in ('esp32', 'esp8266'):
        raise ValueError("node.platform must be esp32 or esp8266")
    board = str(node.get('board', '') or '').strip() or (
        'esp32dev' if platform == 'esp32' else 'd1_mini')
    if not re.match(r'^[a-z0-9_-]{2,40}$', board):
        raise ValueError(f"node.board looks invalid: {board!r}")

    # ---- RS485 UART ------------------------------------------------------------
    tx = str(uart.get('tx_pin', 'GPIO17') or 'GPIO17').upper()
    rx = str(uart.get('rx_pin', 'GPIO16') or 'GPIO16').upper()
    flow = str(uart.get('flow_control_pin', '') or '').upper()
    for label, pin in (('tx_pin', tx), ('rx_pin', rx)) + (
            (('flow_control_pin', flow),) if flow else ()):
        if not _PIN_RE.match(pin):
            raise ValueError(f"uart.{label} must be like GPIO17 (got {pin!r})")
    try:
        baud = int(uart.get('baud_rate', 9600))
    except (TypeError, ValueError):
        raise ValueError("uart.baud_rate must be an integer")
    if baud not in (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200):
        raise ValueError(f"uart.baud_rate {baud} is not a standard rate")
    parity = str(uart.get('parity', 'NONE') or 'NONE').upper()
    if parity not in ('NONE', 'EVEN', 'ODD'):
        raise ValueError("uart.parity must be NONE, EVEN or ODD")
    stop_bits = int(uart.get('stop_bits', 1) or 1)
    if stop_bits not in (1, 2):
        raise ValueError("uart.stop_bits must be 1 or 2")

    try:
        unit_id = int(modb.get('unit_id', 1))
    except (TypeError, ValueError):
        raise ValueError("modbus.unit_id must be an integer")
    if not 1 <= unit_id <= 247:
        raise ValueError("modbus.unit_id must be 1..247")

    # ---- MQTT (defaults: the gateway's own broker) -------------------------------
    broker = str(mqtt.get('broker', '') or mqtt_defaults.get('broker', '') or '')
    if not broker:
        raise ValueError("mqtt.broker is required (the gateway has no MQTT "
                         "broker configured to inherit)")
    try:
        port = int(mqtt.get('port', mqtt_defaults.get('port', 1883)) or 1883)
    except (TypeError, ValueError):
        raise ValueError("mqtt.port must be an integer")
    username = str(mqtt.get('username', mqtt_defaults.get('username', '')) or '')
    has_password = bool(mqtt_defaults.get('password')) or bool(mqtt.get('password_secret'))
    prefix = str(mqtt.get('topic_prefix', '') or f"esphome/{name}").strip('/')
    if not re.match(r'^[A-Za-z0-9_/-]+$', prefix):
        raise ValueError("mqtt.topic_prefix may contain letters, digits, _-/ only")

    # ---- register selection ------------------------------------------------------
    regs = list(template.registers)
    by_name = {r.name: r for r in regs}
    if wanted:
        missing = [n for n in wanted if n not in by_name]
        if missing:
            raise ValueError(f"unknown register name(s): {', '.join(missing[:5])}")
        chosen = [by_name[n] for n in wanted]
    else:
        chosen = [r for r in regs if getattr(r, 'defaults', None)] or regs
    if not chosen:
        raise ValueError("no registers selected")
    warnings: List[str] = []
    if len(chosen) > _MAX_SENSORS:
        warnings.append(f"selection truncated to {_MAX_SENSORS} registers "
                        f"({len(chosen)} requested) — an ESP node cannot "
                        "reasonably poll more")
        chosen = chosen[:_MAX_SENSORS]

    proto = getattr(template, 'protocol', {}) or {}
    byte_order = str(proto.get('byte_order', 'big') or 'big').lower()
    little = byte_order.startswith('little')

    # poll groups → controller base interval + per-sensor skip_updates
    def _interval(g: str) -> int:
        pg = poll_groups.get(g)
        iv = getattr(pg, 'interval', None) if pg is not None else None
        return int(iv or {'realtime': 1, 'normal': 5, 'slow': 60}.get(g, 5))
    used = sorted({_interval(getattr(r, 'poll_group', '') or 'normal')
                   for r in chosen})
    base_s = max(1, used[0])

    # ---- sensors -------------------------------------------------------------------
    sensors: List[str] = []
    paired_rows: List[Dict[str, Any]] = []
    topics: List[str] = []
    seen_topics = set()
    for r in chosen:
        vt = _VALUE_TYPES.get(str(r.data_type or 'float').lower())
        rt = _REGISTER_TYPES.get(str(getattr(r, 'register_type', 'holding')
                                     or 'holding').lower())
        if vt is None or rt is None:
            warnings.append(f"{r.name}: data_type {r.data_type!r} has no "
                            "modbus_controller equivalent — skipped")
            continue
        if rt in ('coil', 'discrete_input') and vt != 'U_WORD':
            vt = 'U_WORD'
        if little and vt in _R_CAPABLE:
            vt += '_R'
        seg = safe_topic_name(r.name)
        if seg in seen_topics:
            warnings.append(f"{r.name}: topic collision on '{seg}' — skipped")
            continue
        seen_topics.add(seg)
        topic = f"{prefix}/{seg}/state"
        topics.append(topic)
        label = str(getattr(r, 'label', '') or r.name)
        unit = str(getattr(r, 'unit', '') or '')
        scale = float(getattr(r, 'scale', 1.0) or 1.0)
        group_s = _interval(getattr(r, 'poll_group', '') or 'normal')
        skip = max(0, round(group_s / base_s) - 1)
        lines = [
            "  - platform: modbus_controller",
            "    modbus_controller_id: mbg_device",
            f"    name: {_yq(label)}",
            f"    id: reg_{seg.lower()}"[:64],
            f"    state_topic: {_yq(topic)}",
            f"    register_type: {rt}",
            f"    address: {int(r.address)}",
            f"    value_type: {vt}",
        ]
        if unit:
            lines.append(f"    unit_of_measurement: {_yq(unit)}")
        lines.append(f"    accuracy_decimals: {2 if vt.startswith(('FP',)) or scale != 1.0 else 0}")
        if skip:
            lines.append(f"    skip_updates: {skip}")
        if scale != 1.0:
            lines += ["    filters:", f"      - multiply: {_fmt_multiply(scale)}"]
        sensors.append("\n".join(lines))

        paired_rows.append({
            "address": int(r.address), "name": r.name, "label": label,
            "unit": unit, "data_type": "float",
            "poll_group": getattr(r, 'poll_group', '') or 'normal',
            "category": getattr(r, 'category', '') or '',
            "topic": topic, "scale": 1.0, "defaults": {},
        })
    if not sensors:
        raise ValueError("no usable registers after type mapping: "
                         + "; ".join(warnings[-3:]))

    # ---- firmware YAML -------------------------------------------------------------
    secrets = {"wifi_ssid": None, "wifi_password": None, "ota_password": None}
    mqtt_lines = [f"mqtt:",
                  f"  broker: {_yq(broker)}",
                  f"  port: {port}"]
    if username:
        mqtt_lines.append(f"  username: {_yq(username)}")
    if has_password:
        mqtt_lines.append("  password: !secret mqtt_password")
        secrets["mqtt_password"] = None
    mqtt_lines += [f"  topic_prefix: {_yq(prefix)}",
                   "  discovery: false"]

    y: List[str] = [
        "# Generated by Multi-Bus Gateway (Device Builder)",
        f"# Source template: {template.id} — register subset: {len(sensors)}",
        "# The paired gateway device consumes exactly these topics.",
        "",
        "esphome:",
        f"  name: {name}",
        f"  friendly_name: {_yq(friendly)}",
        "",
        f"{platform}:",
        f"  board: {board}",
        "",
        "wifi:",
        "  ssid: !secret wifi_ssid",
        "  password: !secret wifi_password",
    ]
    if node.get('wifi_ap_fallback', True):
        y += ["  ap:",
              f"    ssid: {_yq(name + ' fallback')}",
              "", "captive_portal:"]
    y += [
        "",
        "logger:",
        "",
        "ota:",
        "  - platform: esphome",
        "    password: !secret ota_password",
        "",
        *mqtt_lines,
        "",
        "uart:",
        "  id: uart_rs485",
        f"  tx_pin: {tx}",
        f"  rx_pin: {rx}",
        f"  baud_rate: {baud}",
        f"  parity: {parity}",
        f"  stop_bits: {stop_bits}",
        "",
        "modbus:",
        "  id: rs485_bus",
        "  uart_id: uart_rs485",
    ]
    if flow:
        y.append(f"  flow_control_pin: {flow}")
    y += [
        "",
        "modbus_controller:",
        "  - id: mbg_device",
        "    modbus_id: rs485_bus",
        f"    address: {unit_id}",
        f"    update_interval: {base_s}s",
        "",
        "sensor:",
        *sensors,
        "",
    ]

    # ---- paired gateway artifacts ----------------------------------------------------
    slow_s = used[-1]
    stale_after = min(max(3 * slow_s, 15), 3600)
    tpl_id = f"esphome_{name.replace('-', '_')}"
    device_template = {"device_template": {
        "id": tpl_id,
        "name": f"ESPHome node: {friendly}",
        "vendor": "ESPHome",
        "model": template.id,
        "version": "1",
        "description": (f"Paired map for the generated node '{name}' "
                        f"(source template: {template.id}). Values arrive "
                        "in engineering units; topics match the firmware."),
        "source_document": "generated by Device Builder",
        "protocol": {"transports": ["mqtt"]},
        "registers": paired_rows,
    }}
    device_payload = {
        "id": name,
        "name": friendly,
        "template": tpl_id,
        "enabled": True,
        "connection": {
            "protocol": "mqtt",
            "broker": mqtt_defaults.get('broker') or broker,
            "port": int(mqtt_defaults.get('port') or port),
            "username": mqtt_defaults.get('username') or "",
            "password": mqtt_defaults.get('password') or "",
            "tls": False,
            "topic": f"{prefix}/#",
            "stale_after_s": stale_after,
        },
    }

    return {
        "yaml": "\n".join(y),
        "node_yaml_name": f"{name}.yaml",
        "topics": topics,
        "secrets": secrets,
        "warnings": warnings,
        "device_template": device_template,
        "device_payload": device_payload,
    }


def merge_secrets(existing: str, needed: Dict[str, Optional[str]]) -> Tuple[str, List[str]]:
    """Append MISSING keys to a secrets.yaml body; never touch existing lines.

    `needed` maps key → value (None = placeholder the user must fill).
    Returns (new_content, added_keys)."""
    present = set()
    for line in (existing or '').splitlines():
        m = re.match(r'^([A-Za-z0-9_]+)\s*:', line)
        if m:
            present.add(m.group(1))
    added = []
    out = (existing or '').rstrip('\n')
    for key in sorted(needed):
        if key in present:
            continue
        val = needed[key]
        rendered = _yq(val) if val is not None else '"CHANGE_ME"'
        comment = "" if val is not None else "   # TODO: fill in"
        out += ("\n" if out else "") + f"{key}: {rendered}{comment}"
        added.append(key)
    return (out + "\n" if out else ""), added
