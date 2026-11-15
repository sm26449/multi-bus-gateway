# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The device-set reconciliation diff (ADR-0047 P2, `multibus/devices/reconcile.py`): a reload must
apply MEMBERSHIP changes (start a new device, stop a removed one), classify a retarget as a rebuild
and an output-only edit as a keep-client update, and make components follow their parent. These are
the cases today's `restart_devices` (registry-only) misses."""
from multibus.core.config.models import DeviceConfig, ModbusConfig, SourceConfig
from multibus.devices.reconcile import DeviceDiff, apply_diff, diff_devices


def _dev(did, host="1.1.1.1", port=502, name=None, component_of=""):
    return DeviceConfig(id=did, name=name or did, component_of=component_of,
                        sources=[SourceConfig(id="s", protocol="tcp",
                                              connection=ModbusConfig(host=host, port=port))])


def test_added_and_removed_are_detected():
    old = [_dev("a"), _dev("b")]
    new = [_dev("a"), _dev("c")]              # b removed, c added
    d = diff_devices(old, new)
    assert d.added == ["c"] and d.removed == ["b"]
    assert d.unchanged == ["a"]


def test_wire_change_is_a_rebuild():
    old = [_dev("a", host="1.1.1.1")]
    new = [_dev("a", host="9.9.9.9")]         # retarget → the running client would write the old wire
    d = diff_devices(old, new)
    assert d.rebuilt == ["a"] and d.updated == [] and d.unchanged == []


def test_non_wire_change_is_a_keep_client_update():
    old = [_dev("a", name="old-name")]
    new = [_dev("a", name="new-name")]        # same wire, other field changed
    d = diff_devices(old, new)
    assert d.updated == ["a"] and d.rebuilt == []


def test_identical_is_unchanged_and_empty():
    old = [_dev("a")]
    new = [_dev("a")]
    d = diff_devices(old, new)
    assert d.unchanged == ["a"] and d.is_empty() is True


def test_component_follows_a_rebuilt_parent():
    # the component's own config is unchanged, but it reads through the parent's connection
    old = [_dev("p", host="1.1.1.1"), _dev("c", component_of="p")]
    new = [_dev("p", host="9.9.9.9"), _dev("c", component_of="p")]
    d = diff_devices(old, new)
    assert "p" in d.rebuilt and "c" in d.rebuilt       # parent retargeted → component rebuilds too
    assert "c" not in d.unchanged


def test_component_of_a_removed_parent_is_removed():
    old = [_dev("p"), _dev("c", component_of="p")]
    new = [_dev("c", component_of="p")]       # parent gone but component left behind (bad state)
    d = diff_devices(old, new)
    assert "p" in d.removed and "c" in d.removed
    assert "c" not in d.unchanged and "c" not in d.updated


def test_buckets_are_disjoint_and_cover_every_id():
    old = [_dev("keep"), _dev("retarget", host="1.1.1.1"), _dev("edit", name="x"), _dev("gone")]
    new = [_dev("keep"), _dev("retarget", host="2.2.2.2"), _dev("edit", name="y"), _dev("fresh")]
    d = diff_devices(old, new)
    assert d.added == ["fresh"] and d.removed == ["gone"]
    assert d.rebuilt == ["retarget"] and d.updated == ["edit"] and d.unchanged == ["keep"]
    all_ids = d.added + d.removed + d.rebuilt + d.updated + d.unchanged
    assert sorted(all_ids) == sorted(["keep", "retarget", "edit", "gone", "fresh"])


# ── apply_diff orchestrator ──────────────────────────────────────────────────────────
def test_apply_diff_calls_each_op_and_orders_stop_before_start():
    calls = []
    d = DeviceDiff(added=["new"], removed=["gone"], rebuilt=["re"], updated=["up"])
    failed = apply_diff(
        d, stop=lambda i: calls.append(("stop", i)),
        rebuild=lambda ids: calls.append(("rebuild", tuple(ids))),
        update=lambda i: calls.append(("update", i)),
        start=lambda i: calls.append(("start", i)))
    assert failed == []
    assert calls == [("stop", "gone"), ("rebuild", ("re",)), ("update", "up"), ("start", "new")]
    # stop (free the wire) happens before start
    assert calls.index(("stop", "gone")) < calls.index(("start", "new"))


def test_apply_diff_isolates_a_failing_op_and_reports_it():
    done = []
    d = DeviceDiff(added=["a1", "a2"], removed=[], rebuilt=[], updated=[])

    def start(i):
        if i == "a1":
            raise RuntimeError("boom")
        done.append(i)

    failed = apply_diff(d, stop=lambda i: None, rebuild=lambda ids: None,
                        update=lambda i: None, start=start)
    assert done == ["a2"]                       # a1's failure did not abort a2
    assert failed == [("a1", "start", "boom")]


def test_apply_diff_no_rebuild_call_when_empty():
    rebuilt_calls = []
    d = DeviceDiff(updated=["u"])
    apply_diff(d, stop=lambda i: None, rebuild=lambda ids: rebuilt_calls.append(ids),
               update=lambda i: None, start=lambda i: None)
    assert rebuilt_calls == []                  # don't call restart_devices([]) needlessly
