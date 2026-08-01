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
"""Composite virtual meters: cross-device sources + staleness policies.

The convention under test (docs/design → the aggregator evaluation):
  * absence must NEVER be encodable as a plausible measurement (no 0/false)
  * legacy instances (no on_stale) keep the pre-composite READ semantics —
    gap keeps last words, individual reads are never refused (Victron EM24 in
    production depends on this) — but since 3.3.4 the watchdog is fail-CLOSED
    per row: every row that resolves to a value must be fresh (row bound →
    source bound → instance bound) or the whole meter stops responding
  * fail     → reads touching a stale register are refused (Modbus exception)
  * sentinel → SunSpec NA words (float NaN, int16 0x8000, uint16 0xFFFF, …)
  * hold     → last value up to max_hold_s, then behaves like fail
  * sum      → quality of the worst input; never a partial sum
  * server-up (policy modes): at least one fresh live source; all-stale stops
"""
import math
import struct
import time

from multibus.encoder import RegisterEncoder
from multibus.virtual_meter import RegisterDef, Template, VirtualMeter
from multibus.virtual_meter_manager import make_multi_provider


def T(rows, **kw):
    return Template(id="tc", name="composite-test", transport={"port": 19999}, registers=rows)


def live(addr, src, typ="float", stale=None):
    return RegisterDef(addr=addr, type=typ, source_kind="live", source=src,
                       stale_after_s=stale)


def words_at(vm, addr):
    with vm._lock:
        return dict(vm._regs_out).get(addr)


def f32(words):
    return struct.unpack(">f", struct.pack(">HH", *words))[0]


# ── provider: multi-store resolution ─────────────────────────────────────────

def make_stores(now):
    _iso = "2026-01-01T00:00:00"   # display only; freshness reads 'mono'
    prim = {1: {"name": "P", "value": 100.0, "timestamp": _iso, "mono": now}}
    dev2 = {1: {"name": "temp", "value": 21.5, "timestamp": _iso, "mono": now},
            2: {"name": "a.b", "value": 7.0, "timestamp": _iso, "mono": now}}
    return prim, dev2


def test_multi_provider_resolution():
    now = time.monotonic()
    prim, dev2 = make_stores(now)
    p = make_multi_provider(prim, {"ble1": dev2}, prim, "umg512",
                            bounds_for=lambda d: 60.0 if d == "ble1" else None)
    assert p("P")[0] == 100.0                       # bare → own store
    v, ts, bound = p("ble1.temp")                   # dotted → other device + its bound
    assert v == 21.5 and bound == 60.0
    assert p("umg512.P")[0] == 100.0                # explicit primary works too
    # unknown prefix → the dot belongs to the register name (looked up bare)
    p2 = make_multi_provider(dev2, {}, prim, "umg512")
    assert p2("a.b")[0] == 7.0
    assert p("ble1.nope") is None                   # unknown register in known device
    assert p("ghost.x") is None                     # unknown device AND no bare match


# ── legacy semantics pinned (golden) ─────────────────────────────────────────

def test_legacy_gap_keeps_last_words_and_instance_watchdog():
    now = time.monotonic()
    vals = {"A": (10.0, now)}
    vm = VirtualMeter(T([live(0, "A")]), lambda n: vals.get(n),
                      stale_after_s=15, on_stale="legacy")
    newest = vm._rebuild_block()
    first = words_at(vm, 0)
    assert f32(first) == 10.0 and newest == now
    vals.clear()                                     # source vanishes
    newest2 = vm._rebuild_block()
    assert words_at(vm, 0) is None                   # gap: block simply not rewritten
    assert newest2 == 0.0                            # watchdog will stop the server
    assert vm._unavail_spans == []                   # legacy never refuses reads


def test_legacy_sum_gap_on_missing_member():
    now = time.monotonic()
    vals = {"A": (1.0, now), "B": (2.0, now - 999)}
    reg = RegisterDef(addr=0, type="float", source_kind="sum", source=["A", "B"])
    vm = VirtualMeter(T([reg]), lambda n: vals.get(n), on_stale="legacy")
    assert vm._rebuild_block() == now                # legacy: newest member ts
    del vals["B"]
    vm._rebuild_block()
    # gap — no partial sum was written (the entry stays at its last value)


# ── legacy: fail-closed per row (3.3.4) ──────────────────────────────────────

def test_legacy_one_stale_row_closes_the_gate():
    """P1 (2026-07-31 audit): A fresh + B expired must NOT keep the server up
    serving B's old words as live — one stale row fails the instance closed."""
    now = time.monotonic()
    vals = {"A": (10.0, now), "B": (20.0, now - 999)}     # B stale
    vm = VirtualMeter(T([live(0, "A"), live(2, "B")]),
                      lambda n: vals.get(n), stale_after_s=15, on_stale="legacy")
    newest = vm._rebuild_block()
    assert newest == now                              # display ts still newest
    assert f32(words_at(vm, 2)) == 20.0               # words encoded (reads never refused)
    assert vm._legacy_all_fresh is False              # ...but the supervisor will stop
    vals["B"] = (20.0, time.monotonic())              # source recovers
    vm._rebuild_block()
    assert vm._legacy_all_fresh is True


def test_legacy_row_bound_cascade():
    """Legacy judges each row on ITS bound (row → source → instance) — a slow
    source with its own threshold must not flap the meter."""
    now = time.monotonic()
    # row-level bound: 40s old with stale=60 is fresh despite instance bound 15
    vals = {"SLOW": (5.0, now - 40)}
    vm = VirtualMeter(T([live(0, "SLOW", stale=60)]), lambda n: vals.get(n),
                      stale_after_s=15, on_stale="legacy")
    vm._rebuild_block()
    assert vm._legacy_all_fresh is True
    # source-level bound: provider's 3rd element carries the device threshold
    vm = VirtualMeter(T([live(0, "X")]), lambda n: (5.0, now - 40, 60.0),
                      stale_after_s=15, on_stale="legacy")
    vm._rebuild_block()
    assert vm._legacy_all_fresh is True


def test_legacy_missing_row_keeps_gap_but_fresh_rows_gate():
    """A MISSING row keeps the pinned gap contract (no verdict contribution);
    the gate closes only when nothing at all is fresh."""
    now = time.monotonic()
    vals = {"A": (10.0, now)}                        # B absent entirely
    vm = VirtualMeter(T([live(0, "A"), live(2, "B")]),
                      lambda n: vals.get(n), stale_after_s=15, on_stale="legacy")
    vm._rebuild_block()
    assert words_at(vm, 2) is None                    # gap for the missing row
    assert vm._legacy_all_fresh is True               # fresh A keeps the gate open
    vals.clear()                                      # everything vanishes
    vm._rebuild_block()
    assert vm._legacy_all_fresh is False              # nothing fresh → gate closed


# ── derived bound from the poll-group interval (3.4) ─────────────────────────

def test_lookup_derives_bound_from_poll_interval():
    """A store entry carries the producing group's poll interval; the provider
    derives bound = 2.5 × interval. No interval (push source / legacy entry)
    → None, so the instance bound applies."""
    from multibus.virtual_meter_manager import _lookup
    now = time.monotonic()
    store = {1: {"name": "E", "value": 7.0, "mono": now, "interval": 60},
             2: {"name": "F", "value": 8.0, "mono": now}}
    assert _lookup(store, "E") == (7.0, now, 150.0)
    assert _lookup(store, "F") == (8.0, now, None)


def test_derived_bound_is_capped():
    """3.4.1: a misconfigured huge poll interval must NOT relax the freshness
    gate to hours — the auto-derived bound is capped at MAX_DERIVED_STALE_S, so
    a dead source can't read 'fresh' to a control loop indefinitely."""
    from multibus.virtual_meter_manager import (_lookup, MAX_DERIVED_STALE_S,
                                                 GROUP_STALE_MULT)
    now = time.monotonic()
    # 24h poll interval → 2.5×86400 = 216000s uncapped; must clamp to the cap
    store = {1: {"name": "E", "value": 7.0, "mono": now, "interval": 86400}}
    _v, _ts, bound = _lookup(store, "E")
    assert bound == MAX_DERIVED_STALE_S
    # a normal slow-group interval stays below the cap (unchanged behaviour)
    store2 = {1: {"name": "E", "value": 7.0, "mono": now, "interval": 60}}
    assert _lookup(store2, "E")[2] == 150.0 < MAX_DERIVED_STALE_S
    # end-to-end THROUGH the real derived path: a source 20 min stale in a 24h
    # group is NOT fresh (the cap bites, so the gate closes)
    from multibus.virtual_meter_manager import make_provider
    stale_store = {1: {"name": "E", "value": 7.0, "mono": now - 1200,
                       "interval": 86400}}
    vm = VirtualMeter(T([live(0, "E")]), make_provider(stale_store),
                      stale_after_s=15, on_stale="legacy")
    vm._rebuild_block()
    assert vm._legacy_all_fresh is False
    # ...whereas the SAME 20-min-old value WOULD be fresh uncapped (216000s bound)
    fresh_store = {1: {"name": "E", "value": 7.0, "mono": now - 1200,
                       "interval": 86400}}
    # sanity: without the cap it would be fresh — confirm the cap is what closed it
    assert (now - fresh_store[1]["mono"]) < GROUP_STALE_MULT * 86400


def test_legacy_slow_group_row_auto_bound():
    """End-to-end: a 60s slow-group row aged 40s must NOT close the legacy
    gate despite a 15s instance bound (the flapping seen at the 3.3.4 deploy);
    genuinely stale (> 2.5× interval) still fails closed."""
    from multibus.virtual_meter_manager import make_provider
    now = time.monotonic()
    store = {1: {"name": "FAST", "value": 1.0, "mono": now, "interval": 0.25},
             2: {"name": "WH", "value": 2.0, "mono": now - 40, "interval": 60}}
    vm = VirtualMeter(T([live(0, "FAST"), live(2, "WH")]), make_provider(store),
                      stale_after_s=15, on_stale="legacy")
    vm._rebuild_block()
    assert vm._legacy_all_fresh is True          # 40s < 150s derived bound
    store[2]["mono"] = now - 200                 # beyond 2.5× interval
    vm._rebuild_block()
    assert vm._legacy_all_fresh is False


def test_derived_bound_never_tightens_below_instance():
    """A fast row's cadence bound (2.5×0.25s = 0.625s) must not override the
    15s instance floor — one hiccup on a realtime row must not flap the meter.
    An EXPLICIT row stale_after_s may still tighten."""
    from multibus.virtual_meter_manager import make_provider
    now = time.monotonic()
    store = {1: {"name": "FAST", "value": 1.0, "mono": now - 5, "interval": 0.25}}
    vm = VirtualMeter(T([live(0, "FAST")]), make_provider(store),
                      stale_after_s=15, on_stale="legacy")
    vm._rebuild_block()
    assert vm._legacy_all_fresh is True          # 5s < 15s instance floor
    vm2 = VirtualMeter(T([live(0, "FAST", stale=2)]), make_provider(store),
                       stale_after_s=15, on_stale="legacy")
    vm2._rebuild_block()
    assert vm2._legacy_all_fresh is False        # explicit row bound tightens


def test_multi_provider_combines_device_and_cadence_bounds():
    """Dotted sources: the device threshold and the cadence bound may both
    apply — the provider returns the looser (both exist to avoid false-stale)."""
    now = time.monotonic()
    dev2 = {1: {"name": "temp", "value": 21.5, "timestamp": "2026-01-01T00:00:00",
                "mono": now, "interval": 100}}
    p = make_multi_provider({}, {"ble1": dev2}, {}, "umg512",
                            bounds_for=lambda d: 60.0 if d == "ble1" else None)
    v, ts, bound = p("ble1.temp")
    assert v == 21.5 and bound == 250.0          # max(2.5×100, 60)


# ── policy: fail ──────────────────────────────────────────────────────────────

def test_fail_marks_span_unavailable_and_recovers():
    now = time.monotonic()
    vals = {"A": (10.0, now), "B": (20.0, now - 120)}     # B stale
    vm = VirtualMeter(T([live(0, "A"), live(2, "B")]),
                      lambda n: vals.get(n), stale_after_s=15, on_stale="fail")
    newest = vm._rebuild_block()
    assert f32(words_at(vm, 0)) == 10.0              # fresh row served
    assert vm._unavail_spans == [(2, 4)]             # stale float spans 2 registers
    assert newest == now                             # any-fresh → server stays up
    assert vm._quality == {"fresh": 1, "stale": 1, "missing": 0}
    vals["B"] = (20.0, time.monotonic())                  # source recovers
    vm._rebuild_block()
    assert vm._unavail_spans == []
    assert f32(words_at(vm, 2)) == 20.0


def test_fail_all_stale_stops_server_signal():
    vals = {"A": (10.0, time.time() - 999)}
    vm = VirtualMeter(T([live(0, "A")]), lambda n: vals.get(n),
                      stale_after_s=15, on_stale="fail")
    assert vm._rebuild_block() == 0.0                # no fresh source → supervisor stops


def test_per_row_bound_overrides_instance():
    now = time.monotonic()
    vals = {"SLOW": (5.0, now - 40)}                 # 40s old
    # instance bound 15s would call it stale; the row allows 60s (BLE-style)
    vm = VirtualMeter(T([live(0, "SLOW", stale=60)]), lambda n: vals.get(n),
                      stale_after_s=15, on_stale="fail")
    assert vm._rebuild_block() > 0
    assert f32(words_at(vm, 0)) == 5.0
    assert vm._quality["fresh"] == 1


def test_source_bound_from_provider_used():
    now = time.monotonic()
    # provider supplies a 60s bound (the source device's own threshold)
    vm = VirtualMeter(T([live(0, "X")]), lambda n: (5.0, now - 40, 60.0),
                      stale_after_s=15, on_stale="fail")
    assert vm._rebuild_block() > 0                   # judged by the source bound


# ── policy: sentinel ─────────────────────────────────────────────────────────

def test_sentinel_words_by_type():
    now = time.monotonic()
    vals = {"F": (1.0, now - 999), "I": (2.0, now - 999), "U": (3.0, now - 999)}
    vm = VirtualMeter(T([live(0, "F", "float"), live(2, "I", "int16"),
                         live(3, "U", "uint16")]),
                      lambda n: vals.get(n), stale_after_s=15, on_stale="sentinel")
    vm._rebuild_block()
    assert math.isnan(f32(words_at(vm, 0)))          # float → NaN
    assert words_at(vm, 2) == [0x8000]               # int16 → SunSpec NA
    assert words_at(vm, 3) == [0xFFFF]               # uint16 → SunSpec NA
    assert vm._unavail_spans == []                   # sentinel serves, never refuses


def test_sentinel_never_zero():
    enc = RegisterEncoder("big")
    for dt in ("int16", "uint16", "int32", "uint32", "int64", "uint64"):
        words = enc.sentinel_words(dt)
        assert any(w != 0 for w in words), f"{dt} sentinel must not look like 0"


# ── policy: hold ─────────────────────────────────────────────────────────────

def test_hold_serves_last_value_within_cap_then_fails():
    now = time.monotonic()
    vals = {"A": (10.0, now)}
    vm = VirtualMeter(T([live(0, "A")]), lambda n: vals.get(n),
                      stale_after_s=1, on_stale="hold", max_hold_s=3600)
    vm._rebuild_block()
    assert f32(words_at(vm, 0)) == 10.0
    vals["A"] = (10.0, now - 30)                     # stale, but within hold cap
    vm._rebuild_block()
    assert f32(words_at(vm, 0)) == 10.0              # held (bounded)
    assert vm._quality["stale"] == 1
    # age the last-good read past the cap (hold time runs from the last FRESH
    # source timestamp) → hold expires and behaves like fail
    w, _ts = vm._last_good[0]
    vm._last_good[0] = (w, now - 30)
    vm.max_hold_s = 5
    vm._rebuild_block()
    assert vm._unavail_spans == [(0, 2)]             # past cap → behaves like fail


# ── sum in policy modes ──────────────────────────────────────────────────────

def test_policy_sum_uses_worst_member_and_never_partial():
    now = time.monotonic()
    vals = {"A": (1.0, now), "B": (2.0, now - 120)}
    reg = RegisterDef(addr=0, type="float", source_kind="sum", source=["A", "B"])
    vm = VirtualMeter(T([reg]), lambda n: vals.get(n),
                      stale_after_s=15, on_stale="fail")
    vm._rebuild_block()
    assert vm._unavail_spans == [(0, 2)]             # worst member is stale → no sum
    del vals["B"]                                    # member missing entirely
    vm._rebuild_block()
    assert vm._unavail_spans == [(0, 2)]             # still refused — never partial


# ── const rows unaffected by policies ────────────────────────────────────────

def test_const_rows_always_served():
    vm = VirtualMeter(T([RegisterDef(addr=0, type="uint16", source_kind="const",
                                     source=1651)]),
                      lambda n: None, on_stale="fail")
    vm._rebuild_block()
    assert words_at(vm, 0) == [1651]
    assert vm._unavail_spans == []


# ── end-to-end: the fail policy on the wire (real pymodbus server) ───────────

def test_fail_policy_on_the_wire():
    from pymodbus.client import ModbusTcpClient
    now = time.monotonic()
    vals = {"OK": (42.5, now), "DEAD": (7.0, now - 999)}
    t = Template(id="wire", name="wire-test", transport={"port": 19998, "unit_id": 1},
                 registers=[live(0, "OK"), live(2, "DEAD")])
    vm = VirtualMeter(t, lambda n: (vals[n][0], vals[n][1]) if n in vals else None,
                      stale_after_s=15, update_interval_s=0.2, on_stale="fail")
    vm.start()
    try:
        c = ModbusTcpClient("127.0.0.1", port=19998, timeout=2)
        for _ in range(40):
            time.sleep(0.2)
            if c.connect():
                r = c.read_holding_registers(address=0, count=2, slave=1)
                if not r.isError():
                    break
        else:
            raise AssertionError("composite meter did not come up")
        # fresh register reads fine
        assert abs(f32(r.registers) - 42.5) < 0.01
        # a read touching the STALE register is refused with a Modbus exception
        r2 = c.read_holding_registers(address=2, count=2, slave=1)
        assert r2.isError(), "stale register read must be refused"
        # a block read spanning fresh+stale is refused too (no partial truth)
        r3 = c.read_holding_registers(address=0, count=4, slave=1)
        assert r3.isError(), "block spanning a stale register must be refused"
        c.close()
    finally:
        vm.stop()


# ── device-delete guard (composite refs) ─────────────────────────────────────

def test_delete_guard_blocks_device_referenced_by_composite(tmp_path):
    from tests.test_devices_api import make_app, _HAS_TC
    import pytest as _pytest
    if not _HAS_TC:
        _pytest.skip("TestClient not installed")
    cfg, client = make_app(tmp_path)
    client.post("/api/devices", json={"id": "srcdev", "enabled": False,
                "connection": {"protocol": "tcp", "host": "192.0.2.9", "port": 1502,
                               "unit_id": 1}})
    # stub vmeter manager: one instance whose template references srcdev.X
    tdir = tmp_path / "templates"; tdir.mkdir()
    (tdir / "comp.yaml").write_text(
        "template:\n  id: comp\n  name: Comp\n  transport: { port: 1503 }\n"
        "  registers:\n    - { addr: 0, type: float, source: { live: \"srcdev.X\" } }\n")

    class _Stub:
        templates_dir = tdir
        def _load_cfg(self):
            return {"instances": [{"template": "comp", "port": 1503}]}

    client.app.state.vmeter_manager = _Stub()
    r = client.delete("/api/devices/srcdev")
    assert r.status_code == 422 and "comp" in str(r.json())      # blocked with the meter named
    # un-reference it → delete succeeds
    (tdir / "comp.yaml").write_text(
        "template:\n  id: comp\n  name: Comp\n  transport: { port: 1503 }\n"
        "  registers:\n    - { addr: 0, type: float, source: { live: \"other\" } }\n")
    assert client.delete("/api/devices/srcdev").status_code == 200


# ── JSON view (Phase 3): same convention, HTTP shape ─────────────────────────

def test_json_view_good_stale_missing():
    now = time.monotonic()
    vals = {"OK": (42.5, now), "OLD": (7.0, now - 120)}
    vm = VirtualMeter(T([live(0, "OK"), live(2, "OLD"), live(4, "GONE"),
                         RegisterDef(addr=6, type="uint16", source_kind="const", source=1651)]),
                      lambda n: vals.get(n), stale_after_s=15, on_stale="fail")
    j = vm.json_view()
    assert j["complete"] is False and set(j["stale_fields"]) == {"OLD", "GONE"}
    ok = j["values"]["OK"]
    assert ok["quality"] == "good" and ok["value"] == 42.5 and ok["age_s"] is not None
    old = j["values"]["OLD"]
    assert old["value"] is None                       # NEVER the stale number in 'value'
    assert old["quality"] == "stale" and old["last_value"] == 7.0
    gone = j["values"]["GONE"]
    assert gone["quality"] == "missing" and gone["value"] is None and "last_value" not in gone
    assert j["values"]["addr_6"]["quality"] == "const"


def test_json_view_all_good_complete():
    now = time.monotonic()
    vm = VirtualMeter(T([live(0, "A")]), lambda n: (1.5, now),
                      stale_after_s=15, on_stale="sentinel")
    j = vm.json_view()
    assert j["complete"] is True and j["stale_fields"] == []
    assert j["values"]["A"]["value"] == 1.5


def test_json_view_sum_worst_member():
    now = time.monotonic()
    vals = {"A": (1.0, now), "B": (2.0, now - 999)}
    reg = RegisterDef(addr=0, type="float", source_kind="sum", source=["A", "B"])
    vm = VirtualMeter(T([reg]), lambda n: vals.get(n), stale_after_s=15, on_stale="fail")
    j = vm.json_view()
    e = j["values"]["addr_0"]
    assert e["value"] is None and e["quality"] == "stale"    # worst member governs


# ── in-band quality block ────────────────────────────────────────────────────

def _provider(values):
    def p(name):
        return values.get(name)          # (value, ts) sau None
    return p


def test_quality_block_words_fresh_and_stale():
    from multibus.virtual_meter import QUALITY_BASE
    now = time.monotonic()
    vals = {"a": (50.0, now), "b": (50.0, now)}
    vm = VirtualMeter(T([live(100, "a"), live(102, "b")]),
                      _provider(vals), on_stale="fail", quality_block=True)
    vm._rebuild_block()
    w = words_at(vm, QUALITY_BASE)
    assert w[0] == 1 and w[1] == 1                      # versiune, stare ok
    assert (w[2], w[3], w[4], w[5]) == (2, 0, 0, 2)     # fresh/stale/missing/total
    assert (w[6] << 16 | w[7]) <= 1                     # vârstă ~0s

    vals["b"] = (50.0, now - 999)          # b devine stale
    vm._rebuild_block()
    w = words_at(vm, QUALITY_BASE)
    assert w[1] == 2 and (w[2], w[3]) == (1, 1)         # degradat

    vals["a"] = (50.0, now - 999)          # totul stale
    vm._rebuild_block()
    w = words_at(vm, QUALITY_BASE)
    assert w[1] == 3 and w[2] == 0                      # stale, nimic fresh


def test_quality_block_no_word_tearing_under_concurrency():
    """3.4.2: under quality_block the sparse block writes a multi-word value
    word-by-word — a concurrent consumer read must not observe a half-updated
    value (P2 word-tearing). The write path (_push_to_ctx) and the read path
    both take _store_lock, so a reader sees a value whole (all-old or all-new).

    We drive the REAL _push_to_ctx against a sparse block whose per-word write
    is slowed to force the race window, and read under the SAME _store_lock the
    server's getValues uses. The writer always writes EQUAL words [v, v], so any
    read where hi != lo is a torn value. Without the shared lock this tears
    within milliseconds; with it, never."""
    import struct  # noqa: F401  (kept parallel to the encoder path)
    import threading
    import time as _t
    from pymodbus.datastore import ModbusSparseDataBlock

    now = time.monotonic()
    vm = VirtualMeter(T([live(0, "a")]), _provider({"a": (1.0, now)}),
                      quality_block=True, on_stale="legacy")
    block = ModbusSparseDataBlock({0: 0, 1: 0})

    def slow_set(addr, values, **_k):          # widen the tearing window
        block.values[addr] = values[0]
        _t.sleep(0.0003)
        if len(values) > 1:
            block.values[addr + 1] = values[1]
    block.setValues = slow_set
    vm._block = block

    torn, stop = [], threading.Event()

    def writer():
        v = 1
        while not stop.is_set():
            with vm._lock:
                vm._regs_out = [(0, [v, v])]   # equal words → a tear shows as hi!=lo
            vm._push_to_ctx()                  # real write path: holds _store_lock
            v = 2 if v == 1 else 1

    def reader():
        while not stop.is_set():
            with vm._store_lock:               # mirrors _instrumented_get's guard
                hi, lo = block.values[0], block.values[1]
            if hi != lo:
                torn.append((hi, lo))

    ts = [threading.Thread(target=writer), threading.Thread(target=reader),
          threading.Thread(target=reader)]
    for t in ts:
        t.start()
    _t.sleep(1.0)
    stop.set()
    for t in ts:
        t.join(timeout=2)
    assert not torn, f"observed {len(torn)} torn reads, e.g. {torn[:3]}"


def test_quality_block_legacy_state_zero_and_never_age():
    from multibus.virtual_meter import QUALITY_BASE
    vm = VirtualMeter(T([live(100, "a")]), _provider({}), quality_block=True)
    vm._rebuild_block()
    w = words_at(vm, QUALITY_BASE)
    assert w[0] == 1 and w[1] == 0                      # legacy → stare 0
    assert (w[6] << 16 | w[7]) == 0xFFFFFFFF            # nicio valoare vreodată


def test_quality_block_default_off_and_overlap_guard():
    from multibus.virtual_meter import QUALITY_BASE
    vm = VirtualMeter(T([live(100, "a")]), _provider({}))
    vm._rebuild_block()
    assert words_at(vm, QUALITY_BASE) is None           # default: absent
    # hartă care se suprapune cu blocul → blocul se dezactivează singur
    vm2 = VirtualMeter(T([live(QUALITY_BASE + 2, "a")]), _provider({}),
                       quality_block=True)
    assert vm2.quality_block is False


# ── P0: virtual meter is read-only (rejects consumer writes) ─────────────────

def test_readonly_slave_context_refuses_writes():
    """The real ReadOnlySlaveContext used by every vmeter must refuse write FCs
    (validate → False → ILLEGAL ADDRESS) and invoke the refusal callback, while
    reads pass through."""
    from pymodbus.datastore import ModbusSequentialDataBlock
    from multibus.virtual_meter import _read_only_slave_context, _WRITE_FCS

    refused = []
    RO = _read_only_slave_context()
    block = ModbusSequentialDataBlock(0, [7] * 8)
    ctx = RO(hr=block, ir=block, zero_mode=True,
             on_write_refused=lambda fc, a, c: refused.append((fc, a, c)))

    assert ctx.validate(3, 0, 2) is True            # FC3 read allowed
    assert ctx.validate(4, 0, 2) is True            # FC4 read allowed
    for fc in _WRITE_FCS:                            # 5,6,15,16,22,23 refused
        assert ctx.validate(fc, 0, 2) is False
    assert [r[0] for r in refused] == list(_WRITE_FCS)
    # a refused write never mutates the block
    assert block.getValues(0, 2) == [7, 7]
