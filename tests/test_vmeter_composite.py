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
  * legacy instances (no on_stale) keep the EXACT pre-composite semantics —
    gap keeps last words, one instance-level watchdog (Victron EM24 in
    production depends on this)
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
    prim = {1: {"name": "P", "value": 100.0,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))}}
    dev2 = {1: {"name": "temp", "value": 21.5,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))},
            2: {"name": "a.b", "value": 7.0,     # register name that CONTAINS a dot
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))}}
    return prim, dev2


def test_multi_provider_resolution():
    now = time.time()
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
    now = time.time()
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
    now = time.time()
    vals = {"A": (1.0, now), "B": (2.0, now - 999)}
    reg = RegisterDef(addr=0, type="float", source_kind="sum", source=["A", "B"])
    vm = VirtualMeter(T([reg]), lambda n: vals.get(n), on_stale="legacy")
    assert vm._rebuild_block() == now                # legacy: newest member ts
    del vals["B"]
    vm._rebuild_block()
    # gap — no partial sum was written (the entry stays at its last value)


# ── policy: fail ──────────────────────────────────────────────────────────────

def test_fail_marks_span_unavailable_and_recovers():
    now = time.time()
    vals = {"A": (10.0, now), "B": (20.0, now - 120)}     # B stale
    vm = VirtualMeter(T([live(0, "A"), live(2, "B")]),
                      lambda n: vals.get(n), stale_after_s=15, on_stale="fail")
    newest = vm._rebuild_block()
    assert f32(words_at(vm, 0)) == 10.0              # fresh row served
    assert vm._unavail_spans == [(2, 4)]             # stale float spans 2 registers
    assert newest == now                             # any-fresh → server stays up
    assert vm._quality == {"fresh": 1, "stale": 1, "missing": 0}
    vals["B"] = (20.0, time.time())                  # source recovers
    vm._rebuild_block()
    assert vm._unavail_spans == []
    assert f32(words_at(vm, 2)) == 20.0


def test_fail_all_stale_stops_server_signal():
    vals = {"A": (10.0, time.time() - 999)}
    vm = VirtualMeter(T([live(0, "A")]), lambda n: vals.get(n),
                      stale_after_s=15, on_stale="fail")
    assert vm._rebuild_block() == 0.0                # no fresh source → supervisor stops


def test_per_row_bound_overrides_instance():
    now = time.time()
    vals = {"SLOW": (5.0, now - 40)}                 # 40s old
    # instance bound 15s would call it stale; the row allows 60s (BLE-style)
    vm = VirtualMeter(T([live(0, "SLOW", stale=60)]), lambda n: vals.get(n),
                      stale_after_s=15, on_stale="fail")
    assert vm._rebuild_block() > 0
    assert f32(words_at(vm, 0)) == 5.0
    assert vm._quality["fresh"] == 1


def test_source_bound_from_provider_used():
    now = time.time()
    # provider supplies a 60s bound (the source device's own threshold)
    vm = VirtualMeter(T([live(0, "X")]), lambda n: (5.0, now - 40, 60.0),
                      stale_after_s=15, on_stale="fail")
    assert vm._rebuild_block() > 0                   # judged by the source bound


# ── policy: sentinel ─────────────────────────────────────────────────────────

def test_sentinel_words_by_type():
    now = time.time()
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
    now = time.time()
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
    now = time.time()
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
    now = time.time()
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
    now = time.time()
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
    now = time.time()
    vm = VirtualMeter(T([live(0, "A")]), lambda n: (1.5, now),
                      stale_after_s=15, on_stale="sentinel")
    j = vm.json_view()
    assert j["complete"] is True and j["stale_fields"] == []
    assert j["values"]["A"]["value"] == 1.5


def test_json_view_sum_worst_member():
    now = time.time()
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
    now = time.time()
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
