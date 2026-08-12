# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Redundant-source failover: a virtual-meter row backed by an ordered list of
sources serves the first FRESH one and auto-switches when it goes stale — the
reliability guard for the ESS feed. All-stale still degrades through the row's
on_stale policy exactly as a single source would."""
import struct
import time

from multibus.virtual_meter import RegisterDef, Template, VirtualMeter
from multibus.virtual_meter_manager import VirtualMeterManager


def T(rows):
    return Template(id="tf", name="failover-test", transport={"port": 19998}, registers=rows)


def failover(addr, srcs, typ="float", stale=None):
    return RegisterDef(addr=addr, type=typ, source_kind="failover",
                       source=srcs, stale_after_s=stale)


def words_at(vm, addr):
    with vm._lock:
        return dict(vm._regs_out).get(addr)


def f32(words):
    return struct.unpack(">f", struct.pack(">HH", *words))[0]


def _vm(rows, vals, on_stale="fail", stale_after_s=15):
    return VirtualMeter(T(rows), lambda n: vals.get(n),
                        stale_after_s=stale_after_s, on_stale=on_stale)


# ── selection ────────────────────────────────────────────────────────────────

def test_serves_primary_when_fresh():
    now = time.monotonic()
    vals = {"A": (230.0, now), "B": (231.0, now)}      # both fresh → primary wins
    vm = _vm([failover(0, ["A", "B"])], vals)
    vm._rebuild_block()
    assert f32(words_at(vm, 0)) == 230.0
    assert vm._failover_active[0] == "A"


def test_switches_to_fallback_when_primary_stale():
    now = time.monotonic()
    vals = {"A": (230.0, now - 999), "B": (231.0, now)}   # A stale, B fresh
    vm = _vm([failover(0, ["A", "B"])], vals)
    vm._rebuild_block()
    assert f32(words_at(vm, 0)) == 231.0                  # served the fallback
    assert vm._failover_active[0] == "B"
    assert vm._unavail_spans == []                        # meter stays UP


def test_skips_a_missing_candidate():
    now = time.monotonic()
    vals = {"B": (231.0, now)}                            # A absent entirely
    vm = _vm([failover(0, ["A", "B"])], vals)
    vm._rebuild_block()
    assert f32(words_at(vm, 0)) == 231.0
    assert vm._failover_active[0] == "B"


def test_all_stale_fails_closed_under_fail_policy():
    now = time.monotonic()
    vals = {"A": (230.0, now - 999), "B": (231.0, now - 999)}   # both stale
    vm = _vm([failover(0, ["A", "B"])], vals)
    vm._rebuild_block()
    assert words_at(vm, 0) is None                        # not served
    assert vm._unavail_spans != []                        # row refused (fail policy)


def test_switch_back_to_primary_on_recovery():
    now = time.monotonic()
    vals = {"A": (230.0, now - 999), "B": (231.0, now)}
    vm = _vm([failover(0, ["A", "B"])], vals)
    vm._rebuild_block()
    assert vm._failover_active[0] == "B"                  # on fallback
    vals["A"] = (240.0, time.monotonic())                # primary recovers
    vm._rebuild_block()
    assert f32(words_at(vm, 0)) == 240.0
    assert vm._failover_active[0] == "A"                  # switched back


# ── observability: a switch logs one event ───────────────────────────────────

def test_switch_records_a_failover_event():
    now = time.monotonic()
    vals = {"A": (230.0, now), "B": (231.0, now)}
    vm = _vm([failover(0, ["A", "B"])], vals)
    vm._rebuild_block()                                   # bind primary (silent)
    vals["A"] = (230.0, now - 999)                        # primary goes stale
    vm._rebuild_block()                                   # → failover to B (warn)
    vals["A"] = (232.0, time.monotonic())                # recover
    vm._rebuild_block()                                   # → back to A (info)
    kinds = [(e["level"], e["kind"]) for e in vm.stats.events if e["kind"] == "failover"]
    assert ("warn", "failover") in kinds
    assert ("info", "failover") in kinds
    # the very first bind must NOT have emitted an event
    assert len(kinds) == 2


# ── template validation + editor round-trip ──────────────────────────────────

def test_load_template_rejects_non_list_failover(tmp_path):
    from multibus.virtual_meter import load_template
    p = tmp_path / "bad.yaml"
    p.write_text(
        "template:\n  id: bad\n  name: bad\n  transport: {port: 19997}\n"
        "  registers:\n    - { addr: 0, type: float, source: { failover: 'A' } }\n")
    try:
        load_template(str(p))
        assert False, "expected a validation error"
    except ValueError as e:
        assert "failover" in str(e)


def test_editor_normalizes_and_dumps_failover(tmp_path):
    mgr = VirtualMeterManager.__new__(VirtualMeterManager)   # no full init needed
    norm, err = mgr._normalize_registers(
        [{"addr": 0, "type": "float", "source_kind": "failover",
          "source": "A, B ,C"}], "big")
    assert err is None
    assert norm[0]["source_kind"] == "failover"
    assert norm[0]["source"] == ["A", "B", "C"]           # comma string → list
    text = mgr._dump_template("tf", "t", "big",
                              {"type": "tcp", "port": 1502, "unit_id": 1,
                               "bind": "0.0.0.0"}, norm)
    assert "failover: [" in text
    # and it must re-parse into a failover RegisterDef
    import yaml
    from multibus.virtual_meter import _parse_source
    reg = yaml.safe_load(text)["template"]["registers"][0]
    assert _parse_source(reg) == ("failover", ["A", "B", "C"])


def test_empty_failover_list_rejected():
    mgr = VirtualMeterManager.__new__(VirtualMeterManager)
    _, err = mgr._normalize_registers(
        [{"addr": 0, "type": "float", "source_kind": "failover", "source": " , "}], "big")
    assert err and "failover needs at least one" in err
