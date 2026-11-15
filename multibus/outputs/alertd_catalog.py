# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The catalog of MBG's alertd notification types and the YAML pack MBG exports for an
operator to import into alertd (``POST /v1/config/import?apply=true``) — ADR-0043 D4/D6.

One template per MBG ``type`` slug (the slugs ``alertd_client.type_for`` produces). The
template renders from the structured context MBG sends (``{source}``, ``{message}``,
``{severity}``, ``{key}``, ``{host}`` + any ``device_id/field/value/unit`` an emitter adds).
alertd owns routing, throttling (``min_repeat_min``), snooze and escalation once imported.

EVENT types are fired by MBG directly. CONDITION types (latency, buffer backlog, per-register
thresholds) are, per the hybrid decision, meant to be evaluated by **alertd rules** over MBG's
MQTT values — their templates are shipped here so a rule can render, but the variables + rules
are deployment-specific (they depend on the site's MQTT topics and chosen thresholds) and are
left for the operator to author; until then those conditions still flow over the public
``<prefix>/alert`` MQTT channel as before. Keeping the pack honest beats fabricating topics.
"""

# type slug → (severity, icon, title_template, message_template, min_repeat_min, kind)
# kind: "event" (MBG fires it) | "condition" (template only; alertd rule renders it).
_TEMPLATES = [
    ("mbg_device",         "warning",  "🔌", "{source}: {message}",
     "{message}", 15, "event"),
    ("mbg_sink",           "warning",  "🔗", "Sink {source}: {message}",
     "{message}", 15, "event"),
    ("mbg_influx_auth",    "critical", "🔑", "InfluxDB rejects writes",
     "{message}", 30, "event"),
    ("mbg_influx_dropped", "warning",  "🗑️", "InfluxDB dropped points",
     "{message}", 30, "event"),
    ("mbg_device_alarm",   "critical", "🚨", "{source}: {message}",
     "{message} ({key})", 15, "event"),
    ("mbg_bridge",         "warning",  "🚌", "Bus {source}: {message}",
     "{message}", 15, "event"),
    ("mbg_vmeter",         "critical", "🧮", "{source}: {message}",
     "{message} ({key})", 15, "event"),
    ("mbg_config",         "critical", "⚙️", "Gateway config: {message}",
     "{message}", 60, "event"),
    ("mbg_gateway",        "warning",  "🛰️", "Gateway: {message}",
     "{source} — {message}", 15, "event"),
    ("mbg_event",          "warning",  "🔔", "{source}: {message}",
     "{message} ({key})", 15, "event"),
    ("mbg_test",           "info",     "🧪", "MBG test alert",
     "{message}", 0, "event"),
    # conditions — template ships; the rule is the operator's (deployment-specific)
    ("mbg_latency",        "warning",  "🐢", "High read latency: {source}",
     "{message}", 30, "condition"),
    ("mbg_influx_buffer",  "warning",  "📦", "InfluxDB buffer backlog",
     "{message}", 30, "condition"),
    ("mbg_threshold",      "warning",  "📈", "{source}: {message}",
     "{message} ({key})", 15, "condition"),
]

DEFAULT_CHANNELS = ("ui_default", "banner_default")


def catalog() -> list:
    """The MBG type catalog as plain dicts (for introspection/UI/tests)."""
    return [{"type": t, "severity": sev, "icon": icon, "title_template": title,
             "message_template": msg, "min_repeat_min": rep, "kind": kind}
            for (t, sev, icon, title, msg, rep, kind) in _TEMPLATES]


def types() -> set:
    """Every MBG type slug the pack defines."""
    return {row[0] for row in _TEMPLATES}


def build_pack(channels=DEFAULT_CHANNELS, source: str = "multi-bus-gateway") -> dict:
    """The importable alertd config pack: a template per MBG type + routes to each of
    ``channels``. Pure (no I/O). The pack assumes the channels already exist in alertd
    (``ui_default``/``banner_default`` are alertd's bootstrap defaults)."""
    chans = list(channels) or list(DEFAULT_CHANNELS)
    templates, routes = [], []
    for row in _TEMPLATES:
        t, sev, icon, title, msg, rep, kind = row
        templates.append({
            "type": t, "severity": sev, "icon": icon,
            "title_template": title, "message_template": msg,
            "min_repeat_min": rep, "category": "mbg",
            "description": f"Multi-Bus Gateway {kind} alert ({t}).",
            "enabled": True,
        })
        for ch in chans:
            routes.append({"template": t, "channel": ch, "severity_min": "info"})
    # Only keys alertd's import schema accepts — no extra top-level keys (it rejects
    # unknown ones). Provenance goes in the YAML header comment (to_yaml), not here.
    return {
        "version": "1.0",
        "templates": templates,
        "routes": routes,
    }


def to_yaml(channels=DEFAULT_CHANNELS, source: str = "multi-bus-gateway") -> str:
    """The pack as a YAML string ready to POST to alertd /v1/config/import."""
    import yaml  # lazy: only the exporter needs it
    header = (
        "# Multi-Bus Gateway → Klopot notification pack (https://klopot.dev).\n"
        "# Import:  POST /v1/config/import?apply=true  (Content-Type: application/yaml)\n"
        "# Assumes the listed channels already exist in Klopot (ui_default/banner_default\n"
        "# are its bootstrap defaults). EVENT templates are fired by MBG; CONDITION ones\n"
        "# (latency/buffer/threshold) need an operator-authored rule over the site's MQTT.\n\n"
    )
    return header + yaml.safe_dump(build_pack(channels, source), sort_keys=False,
                                   allow_unicode=True, default_flow_style=False)
