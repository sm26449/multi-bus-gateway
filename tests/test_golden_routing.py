"""Characterization (golden) tests for the poller→sinks hot path.

These pin the BYTE-IDENTICAL routing contract before the architectural
refactor (calc-engine extraction, DeviceRegistry): the exact arguments each
publisher receives for the primary vs. a secondary device, the value-store
shape, and the calculated-register injection. Any refactor of create_api()
must keep every assertion here green WITHOUT editing this file — if one of
these fails, the wire output changed, not just the code layout.

The primary contract (legacy invisible-migration): topic_prefix/bucket/
device_tag are None and device_id is "" — publishers fall back to their own
config, which is what keeps janitza/umg512/* topics and Influx tags identical.
"""
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from multibus.api import create_api
from multibus.config import SelectedRegister

from tests.test_devices import write_config
from tests.test_devices_api import needs_tc

CALC_BASE = 8_000_000

SECONDARY_YAML = """
devices:
  - id: em24
    enabled: true
    connection: {protocol: tcp, host: 192.0.2.9, port: 1502, unit_id: 2}
    mqtt: {topic_prefix: meters/em24}
    influxdb: {bucket: warehouse, device_tag: em24tag}
"""


def build(tmp_path, extra_yaml="", calculated=None):
    """App with fake clients (capture the wired data_callback) + mock sinks.

    Lifespan is NOT entered, so main_loop['loop'] stays None and the WS branch
    is skipped — the golden scope is the publisher/store contract.
    """
    cfg = write_config(tmp_path, extra_yaml=extra_yaml)
    if calculated:
        cfg.save_calculated("umg512", calculated)   # before create_api → _load_calc
    mqtt, influx = MagicMock(), MagicMock()
    # real dicts so the background event-harvester thread doesn't trip over
    # MagicMock comparisons ('>' between MagicMock and int) while tests run
    mqtt.get_stats.return_value = {"connected": True}
    influx.get_stats.return_value = {"connected": True, "buffer_points": 0,
                                     "dropped_total": 0}
    fakes = {d.id: SimpleNamespace(publish_callback=None) for d in cfg.devices}
    devices = [(d, fakes[d.id]) for d in cfg.devices]
    app, _ = create_api(cfg, fakes[cfg.primary_device.id], mqtt, influx, devices=devices)
    return cfg, app, fakes, mqtt, influx


def reg(address, name, label="L", unit="V"):
    return SelectedRegister(address=address, name=name, label=label, unit=unit,
                            data_type="float", poll_group="realtime")


def batch(*regs_vals):
    return {r.address: {"value": v, "register": r} for r, v in regs_vals}


ISO_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


# ── primary: the byte-identical contract ─────────────────────────────────────

@needs_tc
def test_golden_primary_publisher_args_and_store(tmp_path):
    cfg, app, fakes, mqtt, influx = build(tmp_path)
    cb = fakes["umg512"].publish_callback
    assert cb is not None                      # create_api wired the callback

    data = batch((reg(19000, "_ULN1"), 231.5), (reg(19002, "_ULN2"), 232.0))
    cb("realtime", data)

    # MQTT: exactly the batch, topic_prefix=None → publisher's own config
    # (this None IS the invisible-migration guarantee for janitza/umg512/*)
    assert mqtt.publish_register_data.call_count == 1
    (pg, d), kw = mqtt.publish_register_data.call_args
    assert pg == "realtime" and d is data
    assert kw == {"topic_prefix": None}

    # InfluxDB: bucket/tag None (publisher config), device_id "" (legacy)
    assert influx.write_register_data.call_count == 1
    (pg, d), kw = influx.write_register_data.call_args
    assert pg == "realtime" and d is data
    assert kw == {"bucket": None, "device_tag": None, "device_id": ""}

    # store shape: exactly these keys, ISO timestamp
    item = app.state.current_values[19000]
    assert set(item) == {"value", "name", "label", "unit", "poll_group", "timestamp"}
    assert item["value"] == 231.5 and item["name"] == "_ULN1"
    assert item["poll_group"] == "realtime" and ISO_TS.match(item["timestamp"])
    # device_values[primary] is an ALIAS of current_values (api.py wires the
    # primary store into the per-device map so store_for(primary) works) — the
    # same dict object, not a copy and not a separate store.
    assert app.state.device_values["umg512"] is app.state.current_values


@needs_tc
def test_golden_secondary_routes_to_its_own_sinks_and_store(tmp_path):
    cfg, app, fakes, mqtt, influx = build(tmp_path, extra_yaml=SECONDARY_YAML)
    cb = fakes["em24"].publish_callback
    data = batch((reg(100, "P_TOTAL", unit="W"), -5794.0))
    cb("normal", data)

    (_pg, _d), kw = mqtt.publish_register_data.call_args
    assert kw == {"topic_prefix": "meters/em24"}
    (_pg, _d), kw = influx.write_register_data.call_args
    assert kw == {"bucket": "warehouse", "device_tag": "em24tag", "device_id": "em24"}

    # store isolation: value in device_values['em24'], NOT in current_values
    assert app.state.device_values["em24"][100]["value"] == -5794.0
    assert 100 not in app.state.current_values


@needs_tc
def test_golden_sink_failure_isolated(tmp_path):
    # one sink throwing must not skip the other sink or the store update
    cfg, app, fakes, mqtt, influx = build(tmp_path)
    mqtt.publish_register_data.side_effect = RuntimeError("broker down")
    cb = fakes["umg512"].publish_callback
    cb("realtime", batch((reg(19000, "_ULN1"), 230.0)))
    assert influx.write_register_data.call_count == 1
    assert app.state.current_values[19000]["value"] == 230.0


# ── calculated registers: injection + routing ────────────────────────────────

@needs_tc
def test_golden_calc_injection_and_routing(tmp_path):
    cfg, app, fakes, mqtt, influx = build(tmp_path, calculated=[{
        "name": "CALC_2X", "label": "Doubled", "unit": "V",
        "expr": "_ULN1 * 2", "poll_group": "realtime", "decimals": 1,
    }])
    fakes["umg512"].publish_callback("realtime", batch((reg(19000, "_ULN1"), 100.25)))

    # injected at the synthetic base address, flagged calculated, decimals applied
    item = app.state.current_values[CALC_BASE]
    assert item["name"] == "CALC_2X" and item["value"] == 200.5
    assert item["calculated"] is True and item["unit"] == "V"

    # routed as a SECOND publish to the same sinks with the same routing args
    assert mqtt.publish_register_data.call_count == 2
    (pg, calc_d), kw = mqtt.publish_register_data.call_args
    assert pg == "realtime" and kw == {"topic_prefix": None}
    assert list(calc_d) == [CALC_BASE]
    assert calc_d[CALC_BASE]["value"] == 200.5
    assert calc_d[CALC_BASE]["register"].name == "CALC_2X"
    assert influx.write_register_data.call_count == 2
    _a, kw = influx.write_register_data.call_args
    assert kw == {"bucket": None, "device_tag": None, "device_id": ""}


@needs_tc
def test_golden_calc_missing_input_publishes_nothing_extra(tmp_path):
    cfg, app, fakes, mqtt, influx = build(tmp_path, calculated=[{
        "name": "CALC_BAD", "expr": "NO_SUCH_REG * 2", "poll_group": "realtime",
    }])
    fakes["umg512"].publish_callback("realtime", batch((reg(19000, "_ULN1"), 1.0)))
    assert mqtt.publish_register_data.call_count == 1       # raw batch only
    assert CALC_BASE not in app.state.current_values


@needs_tc
def test_golden_calc_prev_dt_stateful(tmp_path):
    # prev()/dt across two polls — pins the per-entry _state contract
    cfg, app, fakes, mqtt, influx = build(tmp_path, calculated=[{
        "name": "CALC_DELTA", "expr": "E - prev(E)", "poll_group": "realtime",
    }])
    e = reg(20000, "E", unit="Wh")
    cb = fakes["umg512"].publish_callback
    cb("realtime", batch((e, 1000.0)))                      # first: no prev → skip
    assert CALC_BASE not in app.state.current_values
    cb("realtime", batch((e, 1042.0)))                      # second: delta
    assert app.state.current_values[CALC_BASE]["value"] == 42.0
