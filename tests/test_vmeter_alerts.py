# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""VM-ALERT-1: a virtual meter that stops serving, serves held values, or loses
its consumer raises an alert (and clears it) through the AlertManager — on
2026-11-11 the GX lost its grid meter for 25 min and nothing alerted."""
import time
from types import SimpleNamespace

import pytest

from multibus.outputs.alertd_client import type_for
from multibus.outputs.alerts import AlertManager
from multibus.outputs.vmeter_alerts import (DEFAULT_VMETER_THRESHOLDS, VMeterAlertTracker,
                                            thresholds_from, validate_thresholds)
from multibus.virtual_meter import RegisterDef, Template, VirtualMeter
from tests.test_devices_api import make_app, needs_tc

TH = dict(DEFAULT_VMETER_THRESHOLDS)


def _tracker():
    fired = []
    return VMeterAlertTracker(lambda *a, **k: fired.append((a, k))), fired


def _s(serving="ok", idle=1.0, vid="em24"):
    return {"id": vid, "name": "EM24", "serving": serving, "idle_s": idle}


# ── the tracker (fake clock) ────────────────────────────────────────────────

def test_not_serving_fires_after_its_limit_and_clears_once():
    tr, fired = _tracker()
    tr.update(_s("not_serving"), TH, 100.0)
    tr.update(_s("not_serving"), TH, 129.0)
    assert fired == []
    tr.update(_s("not_serving"), TH, 130.0)
    tr.update(_s("not_serving"), TH, 400.0)                  # still down: no repeat
    assert len(fired) == 1
    (sev, key, src, msg), kw = fired[0]
    assert (sev, key, src) == ("error", "vmeter:em24:not_serving", "vMeter EM24")
    assert "not serving" in msg and kw == {"transition": True}
    tr.update(_s("ok"), TH, 401.0)
    tr.update(_s("ok"), TH, 402.0)
    assert len(fired) == 2
    assert fired[1][0][:2] == ("info", "vmeter:em24:not_serving")


def test_a_short_outage_does_not_alert():
    tr, fired = _tracker()
    tr.update(_s("not_serving"), TH, 100.0)
    tr.update(_s("ok"), TH, 110.0)
    tr.update(_s("not_serving"), TH, 120.0)
    tr.update(_s("not_serving"), TH, 140.0)                  # 20 s of the new outage
    assert fired == []


def test_held_fires_after_its_limit():
    tr, fired = _tracker()
    tr.update(_s("held"), TH, 0.0)
    tr.update(_s("held"), TH, 19.0)
    assert fired == []
    tr.update(_s("held"), TH, 20.0)
    assert fired[0][0][:2] == ("warn", "vmeter:em24:held")
    # hold expired → not serving: held clears, not_serving starts its own clock
    tr.update(_s("not_serving"), TH, 21.0)
    assert fired[1][0][:2] == ("info", "vmeter:em24:held")


def test_consumer_lost_and_back():
    tr, fired = _tracker()
    tr.update(_s(idle=5.0), TH, 0.0)
    tr.update(_s(idle=120.0), TH, 1.0)
    assert fired == []
    tr.update(_s(idle=121.0), TH, 2.0)
    assert fired[0][0][:2] == ("warn", "vmeter:em24:consumer")
    tr.update(_s(idle=0.5), TH, 3.0)
    assert fired[1][0][:2] == ("info", "vmeter:em24:consumer")


def test_consumer_not_judged_while_not_serving_nor_before_any_request():
    tr, fired = _tracker()
    tr.update(_s(idle=None), TH, 0.0)                        # never polled: nobody wired
    tr.update(_s("not_serving", idle=500.0), TH, 1.0)        # its own alert explains it
    assert [f for f in fired if f[0][1].endswith(":consumer")] == []


def test_a_zero_limit_switches_a_condition_off():
    tr, fired = _tracker()
    th = {**TH, "held_s": 0.0, "consumer_idle_s": 0.0}
    for t in range(0, 300, 10):
        tr.update(_s("held", idle=999.0), th, float(t))
    assert fired == []


def test_retain_forgets_a_removed_meter_without_a_clear():
    tr, fired = _tracker()
    tr.update(_s("not_serving"), TH, 0.0)
    tr.update(_s("not_serving"), TH, 30.0)
    tr.retain([])
    tr.update(_s("ok"), TH, 31.0)
    assert len(fired) == 1


def test_thresholds_parse_and_validate():
    assert thresholds_from({"held_s": 5, "bogus": 1, "not_serving_s": -1}) == {
        **TH, "held_s": 5.0}
    assert validate_thresholds({"held_s": 5, "not_serving_s": 0}) == []
    assert validate_thresholds({"held_s": -1})
    assert validate_thresholds({"held_s": True})
    assert validate_thresholds({"nope": 1})
    assert validate_thresholds("x")


def test_alertd_routes_vmeter_keys_to_their_own_fired_type():
    from multibus.outputs.alertd_catalog import types
    for k in ("vmeter:em24:not_serving", "vmeter:em24:held", "vmeter:em24:consumer"):
        assert type_for(k) == ("mbg_vmeter", "fire")
    assert "mbg_vmeter" in types()


def test_alert_manager_config_defaults_and_overrides():
    am = AlertManager({"enabled": True})
    assert am.sig_vmeter is True and am.vmeter_thresholds == TH
    am.configure({"signals": {"vmeter": False}, "vmeter": {"consumer_idle_s": 300}})
    assert am.sig_vmeter is False and am.vmeter_thresholds["consumer_idle_s"] == 300.0
    assert am.status()["signals"]["vmeter"] is False


# ── the meter's serving mode ────────────────────────────────────────────────

def _vm(vals, on_stale="hold"):
    rows = [RegisterDef(addr=0x00, type="float", source_kind="live", source="power")]
    vm = VirtualMeter(Template(id="tv", name="t", transport={"port": 19997}, registers=rows),
                      lambda n: vals.get(n), stale_after_s=15, on_stale=on_stale, max_hold_s=20)
    vm._alive = lambda: True
    return vm


def test_serving_mode_ok_held_not_serving():
    now = time.monotonic()
    vals = {"power": (230.0, now)}
    vm = _vm(vals)
    vm._rebuild_block()
    assert vm.serving_mode() == "ok"
    vals["power"] = (230.0, now - 20)                        # stale, inside the hold
    vm._rebuild_block()
    assert vm.serving_mode() == "held"
    vals["power"] = (230.0, now - 60)                        # beyond the hold
    vm._last_good[0x00] = (vm._last_good[0x00][0], now - 60)
    vm._rebuild_block()
    assert vm.serving_mode() == "not_serving"
    vm._alive = lambda: False
    assert vm.serving_mode() == "not_serving"


def test_sentinel_only_is_not_serving():
    vm = _vm({"power": (230.0, time.monotonic() - 99)}, on_stale="sentinel")
    vm._rebuild_block()
    assert vm.serving_mode() == "not_serving"


# ── through the harvester, the way the 2026-11-11 outage ran ───────────────

class _Script:
    """The harvester's stop event, scripted: before each pass it sets the clock
    and the meter's mode for that pass; after the last pass it stops."""

    def __init__(self, passes, t, mgr):
        self.passes, self.t, self.mgr = list(passes), t, mgr

    def is_set(self):
        if not self.passes:
            return True
        self.t[0], self.mgr.mode = self.passes.pop(0)
        return False

    def wait(self, _s):
        return False


class _Mgr:
    def __init__(self):
        self.mode, self.idle = "ok", 1.0

    def overview(self):
        return []

    def alert_samples(self):
        return [{"id": "em24_av53", "name": "EM24", "serving": self.mode, "idle_s": self.idle}]


def _harvest(am, mgr, passes, monkeypatch, active=True):
    import multibus.outputs.alert_harvest as ah
    from multibus.outputs.alert_harvest import EventHarvester
    t = [0.0]
    monkeypatch.setattr(ah, "_clock", lambda: t[0], raising=False)
    EventHarvester(
        config=SimpleNamespace(), app=SimpleNamespace(state=SimpleNamespace(vmeter_manager=mgr)),
        alert_mgr=am, event_log=SimpleNamespace(add=lambda *a, **k: None),
        registry=SimpleNamespace(pairs=lambda: [], store_for=lambda _d: {}),
        gw_role=SimpleNamespace(active=active),
        threshold_engine=SimpleNamespace(retain=lambda _s: None),
        modbus_client=None, _pub=SimpleNamespace(mqtt=None, influx=None),
        harvester_stop=_Script(passes, t, mgr), client_is_live=lambda _c: True,
        _display_of=lambda _d: {}, _unit_alarms=lambda _d: {"active": []}).run()


def test_harvester_alerts_the_outage_through_the_alert_manager(monkeypatch):
    am = AlertManager({"enabled": True, "mqtt": False})
    _harvest(am, _Mgr(), [(1000.0, "held"), (1020.0, "held"), (1021.0, "not_serving"),
                          (1051.0, "not_serving"), (1500.0, "ok")], monkeypatch)
    keys = [(a["severity"], a["key"]) for a in list(am._recent)]   # fired order
    assert keys == [("warn", "vmeter:em24_av53:held"), ("info", "vmeter:em24_av53:held"),
                    ("error", "vmeter:em24_av53:not_serving"),
                    ("info", "vmeter:em24_av53:not_serving")]
    assert all(am.alertd.should_fire(k) for _s, k in keys)       # fired to alertd too


def test_a_standby_does_not_judge_virtual_meters(monkeypatch):
    am = AlertManager({"enabled": True, "mqtt": False})
    _harvest(am, _Mgr(), [(0.0, "not_serving"), (100.0, "not_serving")], monkeypatch,
             active=False)
    assert [a for a in am.recent(20) if a["key"].startswith("vmeter:")] == []


def test_the_signal_switch_silences_virtual_meter_alerts(monkeypatch):
    am = AlertManager({"enabled": True, "mqtt": False, "signals": {"vmeter": False}})
    _harvest(am, _Mgr(), [(0.0, "not_serving"), (100.0, "not_serving")], monkeypatch)
    assert [a for a in am.recent(20) if a["key"].startswith("vmeter:")] == []


@needs_tc
@pytest.mark.parametrize("bad", [{"held_s": -1}, {"nope": 3}, "x"])
def test_alerts_api_validates_vmeter_limits(tmp_path, bad):
    _cfg, client = make_app(tmp_path)
    r = client.post("/api/config/alerts", json={"vmeter": bad})
    assert r.status_code == 422


@needs_tc
def test_alerts_api_round_trips_vmeter_limits(tmp_path):
    _cfg, client = make_app(tmp_path)
    got = client.get("/api/config/alerts").json()
    assert got["vmeter"] == TH and got["signals"]["vmeter"] is True
    r = client.post("/api/config/alerts", json={"vmeter": {"held_s": 45}})
    assert r.status_code == 200, r.text
    assert client.get("/api/config/alerts").json()["vmeter"]["held_s"] == 45.0


# ── per-meter limits (VM-ALERT-2): a consumer that sleeps at night ─────────

def test_overrides_keep_only_named_valid_keys():
    from multibus.outputs.vmeter_alerts import overrides_from
    assert overrides_from({"consumer_idle_s": 0}) == {"consumer_idle_s": 0.0}
    assert overrides_from({"consumer_idle_s": -1, "held_s": "x", "bogus": 5,
                           "not_serving_s": True}) == {}
    assert overrides_from(None) == {} and overrides_from([1]) == {}


class _TwoMgr(_Mgr):
    def alert_samples(self):
        return [{"id": "em24_av53", "name": "EM24", "serving": "ok", "idle_s": 500.0},
                {"id": "fronius_ts_native", "name": "DM", "serving": "ok", "idle_s": 500.0,
                 "alerts": {"consumer_idle_s": 0.0}}]


def test_a_meter_can_switch_its_consumer_alert_off_alone(monkeypatch):
    am = AlertManager({"enabled": True, "mqtt": False})
    _harvest(am, _TwoMgr(), [(0.0, "ok"), (10.0, "ok")], monkeypatch)
    keys = {a["key"] for a in am.recent(20) if a["key"].startswith("vmeter:")}
    assert keys == {"vmeter:em24_av53:consumer"}          # the other one stays silent


def test_the_manager_carries_an_instances_alert_limits(monkeypatch, tmp_path):
    import multibus.outputs.virtual_meter_manager as vmm
    started = []

    class _VM:
        def __init__(self, t, *_a, **_k):
            self.t, self.on_stale = t, "hold"

        def start(self):
            started.append(self)

        def serving_mode(self):
            return "ok"

        def idle_s(self):
            return 1.0

    monkeypatch.setattr(vmm, "VirtualMeter", _VM)
    monkeypatch.setattr(vmm, "load_template",
                        lambda _p: SimpleNamespace(id="dm", name="DM", transport={}))
    mgr = vmm.VirtualMeterManager({}, config_path=str(tmp_path / "vm.yaml"),
                                  templates_dir=str(tmp_path))
    monkeypatch.setattr(mgr, "_template_path", lambda _t: tmp_path / "dm.json")
    monkeypatch.setattr(mgr, "_may_serve", lambda _i: True)
    mgr._start_one({"template": "dm", "alerts": {"consumer_idle_s": 0, "bogus": 1}})
    assert started[0].alert_overrides == {"consumer_idle_s": 0.0}
    monkeypatch.setattr(mgr, "_snap", lambda: started)
    assert mgr.alert_samples()[0]["alerts"] == {"consumer_idle_s": 0.0}
