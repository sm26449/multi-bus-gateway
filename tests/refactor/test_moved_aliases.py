# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""S0 check (P0.2): a moved module's old import path must be the SAME object as
the new one, not a copy.

The refactor leaves ``sys.modules`` shims at the old paths
(``sys.modules[old] = import_module(new)``) so that the 213 ``monkeypatch`` /
``patch.object`` sites, the conftest global resets and tools/ov/diff_day.py keep
hitting the real module (structure audit §5.1). A ``from new import *`` shim
would silently break all of them while the tests still passed — testing nothing.
This check holds the invariant for every move as it lands.

MOVED is appended one (old, new) pair at a time by each phase-0 move step
(P0.4–P0.8). It is empty until the first move; the self-test below proves the
identity assertion itself works so the first real entry is already guarded.
"""
import importlib
import sys

import pytest

# (old_dotted_path, new_dotted_path) — appended per move step. Empty at P0.2.
MOVED: list[tuple[str, str]] = [
    # P0.4 / S1 — transports/can
    ("multibus.can_client", "multibus.transports.can.can_client"),
    ("multibus.can_interfaces", "multibus.transports.can.can_interfaces"),
    ("multibus.can_link", "multibus.transports.can.can_link"),
    # P0.4 / S2 — transports/http + transports/mqtt
    ("multibus.http_client", "multibus.transports.http.http_client"),
    ("multibus.mqtt_input", "multibus.transports.mqtt.mqtt_input"),
    ("multibus.mqtt_link", "multibus.transports.mqtt.mqtt_link"),
    # P0.4 / S3 — vendors
    ("multibus.seplos_v2", "multibus.vendors.seplos_v2"),
    ("multibus.pq_recorder", "multibus.vendors.pq_recorder"),
    ("multibus.backfill", "multibus.vendors.backfill"),
    # P0.5 / S4 — lifecycle
    ("multibus.config_mirror", "multibus.lifecycle.config_mirror"),
    ("multibus.dependents", "multibus.lifecycle.dependents"),
    ("multibus.snapshots", "multibus.lifecycle.snapshots"),
    ("multibus.state_mirror", "multibus.lifecycle.state_mirror"),
    # P0.5 / S5 — commissioning
    ("multibus.bus_scan", "multibus.commissioning.bus_scan"),
    ("multibus.component_discover", "multibus.commissioning.component_discover"),
    ("multibus.discovery", "multibus.commissioning.discovery"),
    ("multibus.mqtt_discovery", "multibus.commissioning.mqtt_discovery"),
    ("multibus.preview", "multibus.commissioning.preview"),
    # P0.5 / S6 — transports/serial
    ("multibus.bridge_diag", "multibus.transports.serial.bridge_diag"),
    ("multibus.bridge_guard", "multibus.transports.serial.bridge_guard"),
    ("multibus.bridges", "multibus.transports.serial.bridges"),
    ("multibus.bus_claims", "multibus.transports.serial.bus_claims"),
    ("multibus.serial_inventory", "multibus.transports.serial.serial_inventory"),
    ("multibus.serial_ports", "multibus.transports.serial.serial_ports"),
    # P0.5 / S7 — transports/modbus
    ("multibus.bus_trace", "multibus.transports.modbus.bus_trace"),
    ("multibus.device_setup", "multibus.transports.modbus.device_setup"),
    ("multibus.modbus_client", "multibus.transports.modbus.modbus_client"),
    ("multibus.rtu_tap", "multibus.transports.modbus.rtu_tap"),
    # P0.6 / S8 — control
    ("multibus.arbiter", "multibus.control.arbiter"),
    ("multibus.command_dispatch", "multibus.control.command_dispatch"),
    ("multibus.commands", "multibus.control.commands"),
    ("multibus.input_check", "multibus.control.input_check"),
    ("multibus.rules", "multibus.control.rules"),
    ("multibus.rules_runtime", "multibus.control.rules_runtime"),
    ("multibus.sequences", "multibus.control.sequences"),
    ("multibus.write_lease", "multibus.control.write_lease"),
    # P0.6 / S9 — outputs
    ("multibus.alarm_alerts", "multibus.outputs.alarm_alerts"),
    ("multibus.alerts", "multibus.outputs.alerts"),
    ("multibus.influxdb_publisher", "multibus.outputs.influxdb_publisher"),
    ("multibus.mqtt_publisher", "multibus.outputs.mqtt_publisher"),
    ("multibus.rest_push", "multibus.outputs.rest_push"),
    ("multibus.threshold_engine", "multibus.outputs.threshold_engine"),
    ("multibus.virtual_meter", "multibus.outputs.virtual_meter"),
    ("multibus.virtual_meter_manager", "multibus.outputs.virtual_meter_manager"),
    ("multibus.opcua", "multibus.outputs.opcua"),
    ("multibus.opcua.server", "multibus.outputs.opcua.server"),
    # P0.7 / S10 — codec
    ("multibus.register_parser", "multibus.codec.register_parser"),
    ("multibus.value_decode", "multibus.codec.value_decode"),
    ("multibus.encoder", "multibus.codec.encoder"),
    ("multibus.counter_filter", "multibus.codec.counter_filter"),
    ("multibus.can_signal", "multibus.codec.can_signal"),
    # P0.7 / S11 — catalog
    ("multibus.attribute_catalog", "multibus.catalog.attribute_catalog"),
    ("multibus.attributes", "multibus.catalog.attributes"),
    ("multibus.canonical_fields", "multibus.catalog.canonical_fields"),
    ("multibus.csv_import", "multibus.catalog.csv_import"),
    ("multibus.device_kind", "multibus.catalog.device_kind"),
    ("multibus.device_template", "multibus.catalog.device_template"),
    ("multibus.device_types", "multibus.catalog.device_types"),
    ("multibus.display", "multibus.catalog.display"),
    ("multibus.template_facts", "multibus.catalog.template_facts"),
    ("multibus.template_refresh", "multibus.catalog.template_refresh"),
    ("multibus.yaml_import", "multibus.catalog.yaml_import"),
    # P0.7 / S12 — devices
    ("multibus.bus_line", "multibus.devices.bus_line"),
    ("multibus.buses", "multibus.devices.buses"),
    ("multibus.calc_engine", "multibus.devices.calc_engine"),
    ("multibus.conn_advanced", "multibus.devices.conn_advanced"),
    ("multibus.connection_matrix", "multibus.devices.connection_matrix"),
    ("multibus.connection_spec", "multibus.devices.connection_spec"),
    ("multibus.device_patch", "multibus.devices.device_patch"),
    ("multibus.device_registry", "multibus.devices.device_registry"),
    ("multibus.device_runtime", "multibus.devices.device_runtime"),
    ("multibus.device_seed", "multibus.devices.device_seed"),
    ("multibus.device_summary", "multibus.devices.device_summary"),
    ("multibus.endpoint_aggregator", "multibus.devices.endpoint_aggregator"),
    ("multibus.linked_meter", "multibus.devices.linked_meter"),
    ("multibus.meter_links", "multibus.devices.meter_links"),
    ("multibus.meter_only", "multibus.devices.meter_only"),
    ("multibus.multi_source", "multibus.devices.multi_source"),
    ("multibus.save_merge", "multibus.devices.save_merge"),
    ("multibus.source_arbiter", "multibus.devices.source_arbiter"),
    ("multibus.topic_prefix", "multibus.devices.topic_prefix"),
    # P0.7 / S13 — core
    ("multibus.audit", "multibus.core.audit"),
    ("multibus.clock", "multibus.core.clock"),
    ("multibus.config", "multibus.core.config"),
    ("multibus.config_store", "multibus.core.config_store"),
    ("multibus.event_log", "multibus.core.event_log"),
    ("multibus.expressions", "multibus.core.expressions"),
    ("multibus.identity", "multibus.core.identity"),
    ("multibus.redact", "multibus.core.redact"),
    ("multibus.role", "multibus.core.role"),
    ("multibus.sun", "multibus.core.sun"),
    ("multibus.text_check", "multibus.core.text_check"),
    ("multibus.tombstone_store", "multibus.core.tombstone_store"),
]


@pytest.mark.parametrize("old,new", MOVED)
def test_old_path_is_the_same_object_as_new(old, new):
    old_mod = importlib.import_module(old)
    new_mod = importlib.import_module(new)
    assert old_mod is new_mod, (
        f"{old} is not the same module object as {new}; a sys.modules alias is "
        f"required (not 'from {new} import *'), or monkeypatching breaks silently")
    # package code must not depend on the shim: it imports the new path
    assert new in sys.modules


def _load_check_imports():
    import importlib.util
    from pathlib import Path
    p = Path(__file__).resolve().parents[2] / "tools" / "check_imports.py"
    spec = importlib.util.spec_from_file_location("mbg_check_imports_probe", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_no_package_module_imports_a_moved_old_path():
    """A shim serves tests/tools only — no module inside multibus may still
    import a moved (shimmed) old path, or it would break when the shim is
    removed (ADR-0034 / structure audit §6.4). Reuses check_imports' resolver."""
    if not MOVED:
        pytest.skip("no moves recorded yet")
    import ast
    from pathlib import Path

    import multibus
    ci = _load_check_imports()
    pkg = Path(multibus.__file__).resolve().parent
    mods = ci._collect(pkg)  # modname -> (is_pkg, path, names, knows)
    old_paths = {old for old, _ in MOVED}
    offenders = []
    for modname, (is_pkg, path, _n, _k) in mods.items():
        if modname in old_paths:
            continue  # the shim module itself
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                t = ci._resolve_from(modname, is_pkg, node)
                if t:
                    targets = [t] + [f"{t}.{a.name}" for a in node.names if a.name != "*"]
            for tg in targets:
                if any(tg == o or tg.startswith(o + ".") for o in old_paths):
                    offenders.append(f"{modname} ({path.name}:{node.lineno}) -> {tg}")
    assert not offenders, (
        "package code imports a moved (shimmed) old path:\n  " + "\n  ".join(offenders))


def test_alias_technique_gives_object_identity():
    """Validate the shim technique on a throwaway alias (no real move yet)."""
    real = importlib.import_module("json")
    old_name = "mbg_alias_selftest_old"
    sys.modules.pop(old_name, None)
    try:
        sys.modules[old_name] = importlib.import_module("json")
        aliased = importlib.import_module(old_name)
        assert aliased is real
        sentinel = object()
        setattr(aliased, "_mbg_probe", sentinel)
        assert getattr(real, "_mbg_probe", None) is sentinel  # setattr hits the real module
    finally:
        sys.modules.pop(old_name, None)
        if hasattr(real, "_mbg_probe"):
            delattr(real, "_mbg_probe")
