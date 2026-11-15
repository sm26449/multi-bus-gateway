# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""R2D-VBMS-3 (design §4.1, §4.6, §4.9): a Virtual BMS sends the limits of the packs it
actually counts on, a pack that dropped comes back slowly, a flapping pack is held out, and
the current limits ramp up after every (re)start. The ``test_vbms3_*`` tests are the
reviewer's probes (two-day review 2026-11-12), inverted: they reproduced the defect."""
import copy

import pytest

from multibus.outputs.vbms_admission import LimitRamp, PackAdmission, derate
from multibus.outputs.vbms_manager import VbmsInstance, VbmsManager
from multibus.outputs.vbms_registry import structural_errors
from tests.test_vbms_manager import _ROWS, CONFIG, FakeTx, _load

CCL, DCL = "charge_current_limit", "discharge_current_limit"


class _Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def _pack(store, dev, **extra):
    store.update({(dev, "soc"): 80, (dev, "pack_voltage"): 53.0})
    store.update({(dev, k): v for k, v in extra.items()})


def _drop(store, dev):
    for key in [k for k in store if k[0] == dev]:
        del store[key]


def _three(**over):
    """A 3-pack bank with a 150 A bank limit on each side (the reviewer's probe config)."""
    cfg = {**CONFIG["virtual_bms"][0], "packs": ["a", "b", "c"],
           "bounds": {CCL: [0, 150], DCL: [0, 150]},
           "floor": {CCL: 150, DCL: 150, "charge_enable": 1, "discharge_enable": 1}, **over}
    store, clock = {}, _Clock()
    m = VbmsManager({"virtual_bms": [cfg]}, lambda d, f: store.get((d, f)), _load,
                    tx_factory=FakeTx, clock=clock)
    return m, m.instances["deye-vbms"], store, clock


def _settle(m, inst, clock, seconds, step=1.0):
    """Tick the executor path for ``seconds``; return the last values sent."""
    v = None
    for _ in range(int(seconds / step)):
        clock.t += step
        v = m.tx_values(inst)
    return v


# ── the reviewer's probes, inverted ─────────────────────────────────────────────────────

def test_vbms3_a_lost_pack_no_longer_leaves_the_full_bank_limits():
    m, inst, store, clock = _three()
    for dev in ("a", "b"):                               # c is gone (stale / deleted device)
        _pack(store, dev)
    v = _settle(m, inst, clock, 120)                     # past the ramp
    assert m.state(inst)["data_state"] == "ok" and m.state(inst)["packs_live"] == 2
    assert v[CCL] == 100 and v[DCL] == 100               # 2 of 3 packs: 150 × 2/3


def test_vbms3_the_return_from_silence_is_ramped_not_at_full_limits():
    t = [100.0]
    store = {}
    m = VbmsManager(copy.deepcopy(CONFIG), lambda d, f: store.get((d, f)), _load,
                    tx_factory=FakeTx, clock=lambda: t[0])
    inst = m.instances["deye-vbms"]
    m.tx_values(inst)
    t[0] += 3600                                         # an hour of silence
    assert m.tx_values(inst) is None
    store.update({("seplos-adr1", "soc"): 80, ("seplos-adr1", "pack_voltage"): 53.0})
    assert m.tx_values(inst) is None                     # ONE fresh sample of ONE pack, an hour
    for _ in range(59):                                  # after the build: it is a RETURN, so it
        t[0] += 1                                        # serves return_after_s first
        assert m.tx_values(inst) is None
    t[0] += 1
    v = m.tx_values(inst)
    assert v[CCL] == 0 and v[DCL] == 0                   # admitted: the ramp starts at 0 (§4.9)
    for _ in range(60):
        t[0] += 1
        v = m.tx_values(inst)
    assert v[CCL] == 10 and v[DCL] == 20                 # 1 of 2 packs: half the floor


# ── per-pack capability (§4.6) ──────────────────────────────────────────────────────────

def test_with_per_pack_limits_the_bank_limit_is_what_the_admitted_packs_carry():
    m, inst, store, clock = _three(pack_limits={CCL: 40, DCL: 60, "share_factor": 1.0})
    for dev in ("a", "b", "c"):
        _pack(store, dev)
    assert _settle(m, inst, clock, 120)[CCL] == 120      # 3 × 40 < 150
    _drop(store, "c")
    v = m.tx_values(inst)                                # the drop is immediate, no slew down
    assert v[CCL] == 80 and v[DCL] == 120


def test_the_share_factor_and_the_weakest_live_reading_bound_the_bank():
    m, inst, store, clock = _three(pack_limits={CCL: 40}, field_map={
        "soc": "soc", "pack_voltage": "voltage", "pack_ccl": "ccl"})
    _pack(store, "a", pack_ccl=40)
    _pack(store, "b", pack_ccl=10)                       # b's BMS allows only 10 A now
    _pack(store, "c", pack_ccl=40)
    v = _settle(m, inst, clock, 120)
    assert v[CCL] == 27                                  # min(3 × 10 × 0.9, 90) = 27


@pytest.mark.parametrize("bad", ["x", float("nan"), -5])
def test_a_per_pack_reading_that_is_not_a_number_counts_as_zero(bad):
    out = derate({CCL: 100}, [{"ccl": 50}, {"ccl": bad}], 2, {})
    assert out[CCL] == 0


def test_derate_never_raises_a_limit_and_keeps_an_untouched_value():
    assert derate({CCL: 20}, [{}, {}], 2, {CCL: 100}) == {CCL: 20}
    assert derate({CCL: 20}, [{}], 3, {}) == {CCL: 6.6}    # rounded DOWN, never up


def test_alarm_bits_of_a_pack_that_is_not_admitted_are_still_sent():
    m, inst, store, clock = _three(field_map={"soc": "soc", "pack_voltage": "voltage",
                                              "alarms": "alarm_flags"})
    for dev in ("a", "b"):
        _pack(store, dev, alarms=0)
    store[("c", "alarms")] = 4                           # c: no SOC/voltage, but an alarm
    assert m.tx_values(inst)["alarm_flags"] == 4


# ── the return state machine (§4.1) ─────────────────────────────────────────────────────

def test_a_dropped_pack_returns_only_after_return_after_s_without_a_break():
    m, inst, store, clock = _three(recovery={"return_after_s": 30})
    for dev in ("a", "b", "c"):
        _pack(store, dev)
    _settle(m, inst, clock, 100)
    _drop(store, "c")
    clock.t += 1
    m.tx_values(inst)
    _pack(store, "c")
    _settle(m, inst, clock, 29)
    assert m.state(inst)["admission"]["c"]["state"] == "returning"
    assert m.state(inst)["packs_live"] == 2
    _drop(store, "c")                                    # a break restarts the wait
    clock.t += 1
    m.tx_values(inst)
    _pack(store, "c")
    _settle(m, inst, clock, 29)
    assert m.state(inst)["admission"]["c"]["state"] == "returning"
    _settle(m, inst, clock, 2)
    assert m.state(inst)["admission"]["c"]["state"] == "in"


def test_a_returning_pack_ramps_up_and_the_rest_is_not_ramped_down():
    m, inst, store, clock = _three(recovery={"return_after_s": 10, "slew_a_per_s": 2})
    for dev in ("a", "b", "c"):
        _pack(store, dev)
    assert _settle(m, inst, clock, 100)[CCL] == 150
    _drop(store, "c")
    assert _settle(m, inst, clock, 1)[CCL] == 100        # down at once
    _pack(store, "c")
    assert _settle(m, inst, clock, 10)[CCL] == 100       # returning: not counted yet
    assert _settle(m, inst, clock, 1)[CCL] == 102        # admitted after 10 s, then +2 A/s
    assert _settle(m, inst, clock, 5)[CCL] == 112
    assert _settle(m, inst, clock, 60)[CCL] == 150


def test_three_drops_in_the_window_hold_the_pack_until_an_operator_clears_it():
    m, inst, store, clock = _three(recovery={"return_after_s": 5})
    for dev in ("a", "b", "c"):
        _pack(store, dev)
    _settle(m, inst, clock, 10)
    for _ in range(3):
        _drop(store, "c")
        _settle(m, inst, clock, 1)
        _pack(store, "c")
        _settle(m, inst, clock, 10)
    assert m.state(inst)["admission"]["c"]["state"] == "held"
    _settle(m, inst, clock, 3600)
    assert m.state(inst)["admission"]["c"]["state"] == "held"    # time alone does not clear it
    assert m.clear_pack("deye-vbms", "c") is True
    assert m.clear_pack("deye-vbms", "c") is False
    _settle(m, inst, clock, 4)
    assert m.state(inst)["admission"]["c"]["state"] == "returning"   # still serves return_after_s
    _settle(m, inst, clock, 2)
    assert m.state(inst)["admission"]["c"]["state"] == "in"


def test_drops_outside_the_flap_window_do_not_hold():
    a = PackAdmission(["p"], return_after_s=5, flap_drops=3, flap_window_s=600)
    t = 0.0
    a.observe(["p"], t)
    for _ in range(5):
        t += 400
        a.observe([], t)
        t += 10
        a.observe(["p"], t)
        t += 10
        a.observe(["p"], t)
    assert a.state["p"] == "in"


def test_a_pack_admitted_back_counts_for_min_packs_only_after_its_return():
    m, inst, store, clock = _three(min_packs=3, fallback={"hold_s": 5},
                                   recovery={"return_after_s": 30})
    for dev in ("a", "b", "c"):
        _pack(store, dev)
    _settle(m, inst, clock, 5)
    _drop(store, "c")
    _settle(m, inst, clock, 10)
    assert m.tx_values(inst) is None                     # failure past the hold → silent
    _pack(store, "c")
    _settle(m, inst, clock, 20)
    assert m.tx_values(inst) is None                     # c is back, but not yet trusted
    v = _settle(m, inst, clock, 12)
    assert v is not None and v[CCL] == 2                 # ok again at 30 s, one tick of 2 A/s


# ── the ramp (§4.9) ─────────────────────────────────────────────────────────────────────

def test_a_rearm_enters_through_the_ramp():
    m, inst, store, clock = _three()
    for dev in ("a", "b", "c"):
        _pack(store, dev)
    assert _settle(m, inst, clock, 100)[CCL] == 150
    m.stop_instance(inst)                                # what disarm and a re-arm both do
    clock.t += 1
    assert m.tx_values(inst)[CCL] == 0


def test_a_gap_between_ticks_is_never_a_jump():
    r = LimitRamp(2.0, max_dt_s=2.0)
    assert r.apply({CCL: 100}, 0.0)[CCL] == 0
    assert r.apply({CCL: 100}, 3600.0)[CCL] == 4         # dt capped at 2 s
    assert r.apply({CCL: 1}, 3601.0)[CCL] == 1           # decreases at once
    assert r.view({CCL: 100})[CCL] == 1


def test_telemetry_shows_the_ramped_limit_not_the_target():
    m, inst, store, clock = _three()
    for dev in ("a", "b", "c"):
        _pack(store, dev)
    _settle(m, inst, clock, 5)                           # 2 A/s × 4 s after the first tick
    assert m.state(inst)["values"][CCL] == 8
    assert m.preview(inst)["sends"][CCL] == 8


# ── the schema (shared structural validation, R2D-VBMS-2) ───────────────────────────────

BAD = [{"recovery": {"return_after_s": 0}}, {"recovery": {"return_after_s": "60"}},
       {"recovery": {"flap_drops": 0}}, {"recovery": {"flap_drops": True}},
       {"recovery": {"slew_a_per_s": 0}}, {"recovery": {"slew_a_per_s": float("inf")}},
       {"recovery": {"flap_window_s": 10}}, {"recovery": {"return_after": 5}},
       {"recovery": [1]}, {"pack_limits": {CCL: -1}}, {"pack_limits": {CCL: float("nan")}},
       {"pack_limits": {"share_factor": 1.2}}, {"pack_limits": {"cvl": 50}},
       {"pack_limits": 40}]


@pytest.mark.parametrize("over", BAD)
def test_a_bad_return_policy_is_refused_and_never_built(over):
    cfg = {**CONFIG["virtual_bms"][0], **over}
    assert structural_errors(cfg)
    with pytest.raises(ValueError, match="invalid virtual_bms"):
        VbmsInstance(cfg, _ROWS)


def test_the_defaults_are_the_designs():
    inst = VbmsInstance(CONFIG["virtual_bms"][0], _ROWS)
    assert inst.admission.return_after_s == 60 and inst.admission.flap_drops == 3
    assert inst.admission.flap_window_s == 600 and inst.ramp.slew == 2
    assert structural_errors({**CONFIG["virtual_bms"][0],
                              "recovery": {"return_after_s": 5, "flap_drops": 1,
                                           "flap_window_s": 60, "slew_a_per_s": 0.1},
                              "pack_limits": {CCL: 0, DCL: 100, "share_factor": 0.5}}) == []


# ── the operator's clear (design §3.4) ──────────────────────────────────────────────────

from tests.test_vbms_crud import _new, client  # noqa: E402,F401 — the fixture


def test_the_clear_route_readmits_only_a_held_pack(client):  # noqa: F811
    assert client.post("/api/vbms", json=_new()).status_code == 200
    assert client.post("/api/vbms/deye-vbms/packs/nope/clear").status_code == 404
    assert client.post("/api/vbms/deye-vbms/packs/pack1/clear").status_code == 409   # not held
    adm = client.mgr.instances["deye-vbms"].admission
    adm.state["pack1"] = "held"
    r = client.post("/api/vbms/deye-vbms/packs/pack1/clear")
    assert r.status_code == 200 and adm.state["pack1"] == "out"


def test_an_edit_keeps_the_return_policy_keys_it_did_not_mention(client):  # noqa: F811
    body = _new(recovery={"return_after_s": 120, "flap_drops": 2})
    assert client.post("/api/vbms", json=body).status_code == 200
    r = client.patch("/api/vbms/deye-vbms", json={"recovery": {"return_after_s": 30}})
    assert r.status_code == 200, r.text
    assert client.cfg.virtual_bms[0]["recovery"] == {"return_after_s": 30, "flap_drops": 2}
    assert client.post("/api/vbms", json=_new(id="x", channel="can0",
                                              recovery={"return_after_s": 0})).status_code == 422


# ── review follow-up (2026-11-15) ───────────────────────────────────────────────────────

def test_a_pack_first_seen_after_the_start_grace_serves_return_after_s():
    m, inst, store, clock = _three(recovery={"return_after_s": 30})
    for dev in ("a", "b"):
        _pack(store, dev)
    _settle(m, inst, clock, 31)                          # c never answered during the grace
    _pack(store, "c")
    _settle(m, inst, clock, 1)
    assert m.state(inst)["admission"]["c"]["state"] == "returning"
    _settle(m, inst, clock, 30)
    assert m.state(inst)["admission"]["c"]["state"] == "in"


def test_a_pack_first_seen_inside_the_start_grace_is_admitted_at_once():
    m, inst, store, clock = _three(recovery={"return_after_s": 30})
    _settle(m, inst, clock, 20)
    for dev in ("a", "b", "c"):
        _pack(store, dev)
    _settle(m, inst, clock, 1)
    assert m.state(inst)["admission"]["c"]["state"] == "in"


def _persisted(tmp_path, **over):
    cfg = {**CONFIG["virtual_bms"][0], "packs": ["a", "b", "c"], **over}
    store, clock = {}, _Clock()
    m = VbmsManager({"virtual_bms": [cfg]}, lambda d, f: store.get((d, f)), _load,
                    tx_factory=FakeTx, clock=clock, state_path=tmp_path / "vbms_admission.json")
    return m, m.instances["deye-vbms"], store, clock


def _flap_c(m, inst, store, clock):
    for dev in ("a", "b", "c"):
        _pack(store, dev)
    _settle(m, inst, clock, 10)
    for _ in range(3):
        _drop(store, "c")
        _settle(m, inst, clock, 1)
        _pack(store, "c")
        _settle(m, inst, clock, 10)
    assert m.state(inst)["admission"]["c"]["state"] == "held"


def test_a_held_pack_stays_held_across_a_rebuild(tmp_path):
    m, inst, store, clock = _persisted(tmp_path, recovery={"return_after_s": 5})
    _flap_c(m, inst, store, clock)
    m2, inst2, store2, clock2 = _persisted(tmp_path, recovery={"return_after_s": 5})   # a restart
    for dev in ("a", "b", "c"):
        _pack(store2, dev)
    _settle(m2, inst2, clock2, 100)
    assert m2.state(inst2)["admission"]["c"]["state"] == "held"
    assert m2.state(inst2)["packs_live"] == 2
    assert m2.clear_pack("deye-vbms", "c")
    m3, inst3, _s, _c = _persisted(tmp_path, recovery={"return_after_s": 5})
    assert m3.state(inst3)["admission"]["c"]["state"] != "held"    # the clear is persisted too


def test_recent_drops_survive_a_rebuild(tmp_path):
    m, inst, store, clock = _persisted(tmp_path, recovery={"return_after_s": 5})
    for dev in ("a", "b", "c"):
        _pack(store, dev)
    _settle(m, inst, clock, 10)
    for _ in range(2):                                   # two drops, then a restart
        _drop(store, "c")
        _settle(m, inst, clock, 1)
        _pack(store, "c")
        _settle(m, inst, clock, 10)
    m2, inst2, store2, clock2 = _persisted(tmp_path, recovery={"return_after_s": 5})
    for dev in ("a", "b", "c"):
        _pack(store2, dev)
    _settle(m2, inst2, clock2, 10)
    _drop(store2, "c")                                   # the third drop holds it
    _settle(m2, inst2, clock2, 1)
    assert m2.state(inst2)["admission"]["c"]["state"] == "held"


def test_a_damaged_state_file_holds_nothing_back_and_raises_nothing(tmp_path):
    (tmp_path / "vbms_admission.json").write_text("{not json")
    m, inst, store, clock = _persisted(tmp_path)
    for dev in ("a", "b", "c"):
        _pack(store, dev)
    _settle(m, inst, clock, 2)
    assert m.state(inst)["packs_live"] == 3


def test_a_known_zero_per_pack_limit_gives_zero_with_the_rest_unknown():
    out = derate({CCL: 100}, [{"ccl": 0}, {}], 2, {})
    assert out[CCL] == 0
    out = derate({CCL: 100}, [{"ccl": 20}, {}], 2, {})
    assert out[CCL] == 36                                # min(100 × 2/2, 2 × 20 × 0.9)


@pytest.mark.parametrize("over", [{"recovery": {"slew_a_per_s": None}},
                                  {"recovery": {"return_after_s": None}},
                                  {"pack_limits": {"share_factor": None}},
                                  {"pack_limits": {CCL: None}}])
def test_a_null_inside_the_return_policy_is_refused(over):
    assert structural_errors({**CONFIG["virtual_bms"][0], **over})


@pytest.mark.parametrize("bad", ["0x04", float("nan"), -1, 2.5, None.__class__])
def test_a_garbage_alarm_flag_reads_as_alarm_and_never_silences_the_bank(bad):
    m, inst, store, clock = _three(field_map={"soc": "soc", "pack_voltage": "voltage",
                                              "alarms": "alarm_flags"})
    for dev in ("a", "b"):
        _pack(store, dev, alarms=0)
    store[("c", "alarms")] = bad                         # an EXCLUDED pack answers garbage
    v = m.tx_values(inst)
    assert v is not None and v["alarm_flags"] != 0       # alarm set, frames still go out
    inst.vbms.build_frames(v)                            # and it fits its field
