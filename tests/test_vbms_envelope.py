# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The Virtual BMS control envelope and its configuration schema (FINDINGS WK7-SAFE-3,
WK7-CFG-1…4, WK7-UI-1): no path puts an unbounded, non-finite or unrepresentable number on
the wire, and a configuration that cannot run is refused before anything is stored."""
import math
import threading

import pytest

from multibus.outputs.vbms import FrameValueError, VbmsRuntime, VirtualBms, encode_value
from multibus.outputs.vbms_control import VbmsControl
from multibus.outputs.vbms_registry import validate_instance
from tests.test_vbms_crud import _new, client  # noqa: F401 — the fixture
from tests.test_vbms_manager import FakeTx, _load

BOUNDS = {"charge_current_limit": (0, 80), "discharge_current_limit": (0, 180)}


# ── the control plane ────────────────────────────────────────────────────────────────────

def test_a_floor_is_clamped_by_the_same_envelope_as_a_command():
    c = VbmsControl(BOUNDS, {"charge_current_limit": 999, "discharge_current_limit": 40})
    assert c.resolve()["charge_current_limit"] == 80


def test_a_field_without_bounds_cannot_be_commanded_and_keeps_its_floor():
    c = VbmsControl(BOUNDS, {"charge_voltage_limit": 57.6})
    with pytest.raises(ValueError, match="no configured bounds"):
        c.command("cvl", 500)
    assert c.resolve()["charge_voltage_limit"] == 57.6


def test_a_bounded_voltage_is_commandable_inside_its_envelope():
    c = VbmsControl({**BOUNDS, "charge_voltage_limit": (48, 58.4)}, {"charge_voltage_limit": 57.6})
    assert c.command("cvl", 500) == 58.4


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "nan", "inf", "x"])
def test_a_command_that_is_not_a_finite_number_is_refused(bad):
    c = VbmsControl(BOUNDS, {"charge_current_limit": 20})
    with pytest.raises(ValueError):
        c.command("ccl", bad)
    assert c.resolve()["charge_current_limit"] == 20


def test_a_floor_that_is_not_finite_is_not_used():
    c = VbmsControl(BOUNDS, {"charge_current_limit": float("nan"), "discharge_current_limit": 40})
    out = c.resolve()
    assert "charge_current_limit" not in out and out["discharge_current_limit"] == 40


def test_enables_are_always_commandable_and_are_0_or_1():
    c = VbmsControl({}, {"charge_enable": 7})
    assert c.resolve()["charge_enable"] == 1
    assert c.command("charge_enable", 0) == 0


# ── the wire ─────────────────────────────────────────────────────────────────────────────

def _row(name):
    return next(r for r in _load("x") if r.get("name") == name)


def test_a_value_that_does_not_fit_its_field_is_refused_not_masked():
    row = _row("charge_voltage_limit")
    with pytest.raises(FrameValueError, match="does not fit"):
        encode_value(bytearray(8), row, 100000)
    with pytest.raises(FrameValueError):
        encode_value(bytearray(8), row, -1)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "abc"])
def test_a_value_that_is_not_a_finite_number_is_refused(bad):
    with pytest.raises(FrameValueError):
        encode_value(bytearray(8), _row("charge_current_limit"), bad)


def test_a_frame_set_with_an_unrepresentable_value_is_not_sent_at_all():
    vbms = VirtualBms(_load("x"), name="vb")
    vbms.arm()
    tx = FakeTx("fake")
    stop = threading.Event()
    sets = iter([{"soc": 50, "charge_voltage_limit": 100000}, {"soc": 50, "charge_voltage_limit": 57.6}])

    def values():
        try:
            return next(sets)
        except StopIteration:
            stop.set()
            return None
    rt = VbmsRuntime(vbms, tx, values, period_s=0)
    assert rt.run(stop) == 1                            # the bad set: nothing; the good one: sent
    assert len(tx.sent) == len(vbms.can_ids())          # exactly one whole set on the wire


# ── the schema (pure validation) ─────────────────────────────────────────────────────────

_REG = [{"name": "can1", "availability": "available", "publisher": None, "bitrate": 500000,
         "readers": []}]
_SRC = [{"id": "pack1", "name": "Pack 1"}, {"id": "pack2", "name": "Pack 2"}]


def _check(**over):
    inst = {"id": "vb", "profile": "p", "channel": "can1", "packs": ["pack1", "pack2"], **over}
    return validate_instance(inst, existing_ids=[], registry=_REG, sources=_SRC,
                             profile_is_emulator=lambda n: True, device_ids=["pack1", "pack2", "inv"])


def test_a_plain_instance_is_valid():
    assert _check() == []
    assert _check(bounds={"charge_current_limit": [0, 80], "charge_voltage_limit": [48, 58.4]},
                  floor={"charge_current_limit": 20, "charge_voltage_limit": 57.6},
                  period_s=1, min_packs=2, fallback={"hold_s": 5}) == []


@pytest.mark.parametrize("over,needle", [
    ({"bounds": {"charge_current_limit": 80}}, "[lo, hi]"),
    ({"bounds": {"charge_current_limit": [80, 0]}}, "above hi"),
    ({"bounds": {"charge_current_limit": [-5, 80]}}, "0 or more"),
    ({"bounds": {"charge_current_limit": [0, math.inf]}}, "finite"),
    ({"bounds": {"charge_current_limit": [0, True]}}, "finite"),
    ({"bounds": {"speed": [0, 1]}}, "not a control field"),
    ({"bounds": "x"}, "mapping"),
    ({"floor": {"charge_current_limit": math.nan}}, "finite"),
    ({"floor": {"charge_current_limit": -1}}, "0 or more"),
    ({"bounds": {"charge_current_limit": [0, 80]}, "floor": {"charge_current_limit": 999}}, "outside its bounds"),
    ({"floor": {"charge_enable": 2}}, "0 or 1"),
    ({"period_s": 0}, "period_s"), ({"period_s": -1}, "period_s"), ({"period_s": "fast"}, "period_s"),
    ({"period_s": math.inf}, "period_s"),
    ({"command_max_age_s": 0}, "command_max_age_s"),
    ({"min_packs": 0}, "min_packs"), ({"min_packs": 3}, "min_packs"), ({"min_packs": 1.5}, "min_packs"),
    ({"fallback": {"hold_s": -1}}, "hold_s"), ({"fallback": 5}, "fallback"),
    ({"field_map": ["soc"]}, "field_map"), ({"field_map": {}}, "field_map"),
    ({"fields": {"send_optional": "yes"}}, "fields"),
    ({"packs": ["pack1", "pack1"]}, "more than once"),
    ({"packs": "pack1"}, "list of device ids"),
    ({"id": "inv"}, "already a device"),
    ({"id": "pack1"}, "own output"),
])
def test_what_cannot_run_is_refused(over, needle):
    errors = _check(**over)
    assert any(needle in e for e in errors), errors


# ── through the routes: nothing is stored unless all of it is ────────────────────────────

def test_a_malformed_instance_is_a_422_and_nothing_changes(client):  # noqa: F811
    for bad in ({"bounds": {"charge_current_limit": 80}}, {"period_s": 0},
                {"packs": ["pack1", "pack1"]}, {"id": "pack1"}, {"field_map": []}):
        r = client.post("/api/vbms", json=_new(**bad))
        assert r.status_code == 422, (bad, r.text)
    assert client.cfg.virtual_bms == [] and client.cfg.saves == 0 and client.mgr.instances == {}


def test_a_save_that_fails_changes_neither_memory_nor_the_manager(client):  # noqa: F811
    assert client.post("/api/vbms", json=_new()).status_code == 200
    before = [dict(x) for x in client.cfg.virtual_bms]
    inst_before = client.mgr.instances[before[0]["id"]]

    def boom():
        raise OSError("disk full")
    client.cfg.save_yaml_config = boom
    iid = before[0]["id"]
    assert client.post("/api/vbms", json=_new(id="second", channel="can0")).status_code == 503
    assert client.patch(f"/api/vbms/{iid}", json={"name": "renamed"}).status_code == 503
    assert client.delete(f"/api/vbms/{iid}").status_code == 503
    assert client.cfg.virtual_bms == before
    assert list(client.mgr.instances) == [iid] and client.mgr.instances[iid] is inst_before


def test_an_edit_keeps_the_bounds_it_did_not_mention(client):  # noqa: F811
    """The form knows the two current ceilings; saving it must not erase a voltage envelope
    that was set through the API (WK7-UI-1)."""
    full = {"charge_current_limit": [0, 80], "discharge_current_limit": [0, 180],
            "charge_voltage_limit": [48, 58.4], "discharge_voltage_limit": [44, 50]}
    r = client.post("/api/vbms", json=_new(bounds=full, floor={"charge_voltage_limit": 57.6}))
    assert r.status_code == 200, r.text
    iid = client.cfg.virtual_bms[0]["id"]
    r = client.patch(f"/api/vbms/{iid}", json={
        "bounds": {"charge_current_limit": [0, 60], "discharge_current_limit": [0, 100]},
        "floor": {"charge_current_limit": 0, "discharge_current_limit": 0}})
    assert r.status_code == 200, r.text
    stored = client.cfg.virtual_bms[0]
    assert stored["bounds"]["charge_current_limit"] == [0, 60]
    assert stored["bounds"]["charge_voltage_limit"] == [48, 58.4]
    assert stored["bounds"]["discharge_voltage_limit"] == [44, 50]
    assert stored["floor"]["charge_voltage_limit"] == 57.6
    # an explicit null is how a bound is taken away
    r = client.patch(f"/api/vbms/{iid}", json={"bounds": {"discharge_voltage_limit": None}})
    assert r.status_code == 200, r.text
    assert "discharge_voltage_limit" not in client.cfg.virtual_bms[0]["bounds"]


# ── R2D-VBMS-2: the schema is one, whichever way a config arrives ────────────────────────

BAD = [{"period_s": 0}, {"packs": ["seplos-adr1", "seplos-adr1"]},
       {"bounds": {"charge_current_limit": 80}},
       {"bounds": {"charge_voltage_limit": [48, 58.4]}, "floor": {"charge_voltage_limit": 999}}]


@pytest.mark.parametrize("over", BAD)
def test_an_instance_that_fails_the_schema_is_never_built(over):
    from multibus.outputs.vbms_manager import VbmsInstance
    from tests.test_vbms_manager import CONFIG, _ROWS
    with pytest.raises(ValueError, match="invalid virtual_bms"):
        VbmsInstance({**CONFIG["virtual_bms"][0], **over}, _ROWS)


@pytest.mark.parametrize("over", BAD)
def test_a_manager_leaves_out_an_instance_that_fails_the_schema(over):
    from multibus.outputs.vbms_manager import VbmsManager
    from tests.test_vbms_manager import CONFIG, _read
    m = VbmsManager({"virtual_bms": [{**CONFIG["virtual_bms"][0], **over}]}, _read, _load)
    assert m.instances == {}                       # not built → cannot be armed


@pytest.mark.parametrize("over", BAD + [{"id": "Not A Slug"}])
def test_a_bundle_with_such_an_instance_is_refused_before_anything_is_written(over):
    import io
    import zipfile

    import yaml

    from multibus.lifecycle.snapshots import validate_bundle
    from tests.test_vbms_manager import CONFIG
    doc = {"mqtt": {"enabled": False}, "virtual_bms": [{**CONFIG["virtual_bms"][0], **over}]}
    b = io.BytesIO()
    with zipfile.ZipFile(b, 'w') as z:
        z.writestr('config.yaml', yaml.safe_dump(doc))
    errors = validate_bundle(zipfile.ZipFile(io.BytesIO(b.getvalue())))
    assert any("virtual_bms" in e for e in errors), errors


def test_a_bundle_with_a_valid_instance_passes_and_a_clashing_id_does_not():
    import io
    import zipfile

    import yaml

    from multibus.lifecycle.snapshots import validate_bundle
    from tests.test_vbms_manager import CONFIG

    def check(doc):
        b = io.BytesIO()
        with zipfile.ZipFile(b, 'w') as z:
            z.writestr('config.yaml', yaml.safe_dump(doc))
        return [e for e in validate_bundle(zipfile.ZipFile(io.BytesIO(b.getvalue()))) if "virtual_bms" in e]
    inst = CONFIG["virtual_bms"][0]
    assert check({"mqtt": {"enabled": False}, "virtual_bms": [inst]}) == []
    assert check({"mqtt": {"enabled": False}, "virtual_bms": [inst, inst]})
    assert check({"mqtt": {"enabled": False}, "devices": [{"id": inst["id"]}], "virtual_bms": [inst]})
