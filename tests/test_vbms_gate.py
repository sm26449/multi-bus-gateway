# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The Virtual BMS transmit gate is LIVE (FINDINGS WK7-SAFE-1): the master write switch and
the gateway role are obeyed by an executor that is already running — not only when arming.
Fake transmitter throughout; nothing here touches a bus."""
import threading

import pytest

from multibus.outputs.vbms import VbmsRuntime, VirtualBms
from multibus.outputs.vbms_manager import VbmsGateClosed, VbmsManager
from tests.test_vbms_boot import _boot
from tests.test_vbms_manager import CONFIG, FakeTx, _load, _read


class CountingTx(FakeTx):
    def __init__(self, channel):
        super().__init__(channel)
        self.cv = threading.Condition()
        self.count = 0

    def send(self, can_id, data):
        with self.cv:
            self.count += 1
            self.cv.notify_all()

    def wait_for_frames(self, n=1, timeout=2):
        with self.cv:
            return self.cv.wait_for(lambda: self.count >= n, timeout=timeout)

    def quiet_for(self, seconds=0.15):
        """True when no further frame arrives for ``seconds``."""
        with self.cv:
            before = self.count
            return not self.cv.wait_for(lambda: self.count > before, timeout=seconds)


# ── the executor loop ────────────────────────────────────────────────────────────────────

def test_the_loop_asks_the_gate_before_every_frame_set_and_stops_on_a_reason():
    answers = iter([None, None, "the master write switch is off"])
    vbms = VirtualBms(_load("x"), name="vb")
    vbms.arm()
    tx = FakeTx("fake")
    rt = VbmsRuntime(vbms, tx, lambda: {"soc": 50}, period_s=0, gate=lambda: next(answers))
    sets = rt.run(threading.Event())
    assert sets == 2
    assert rt.revoked == "the master write switch is off"
    assert vbms.armed is False and tx.closed is True


def test_a_closed_gate_sends_nothing_at_all():
    vbms = VirtualBms(_load("x"), name="vb")
    vbms.arm()
    tx = FakeTx("fake")
    assert VbmsRuntime(vbms, tx, lambda: {}, period_s=0, gate=lambda: "standby").run(
        threading.Event()) == 0
    assert tx.sent == [] and vbms.armed is False


# ── the manager ──────────────────────────────────────────────────────────────────────────

def _manager(gate):
    txs = []

    def factory(channel):
        txs.append(CountingTx(channel))
        return txs[-1]
    m = VbmsManager(CONFIG, _read, _load, tx_factory=factory, gate=gate)
    iid = next(iter(m.instances))
    m.instances[iid].period_s = 0.005
    return m, iid, txs


def test_arming_is_refused_while_the_gate_is_closed():
    m, iid, txs = _manager(lambda: "the gateway is standby")
    with pytest.raises(VbmsGateClosed, match="standby"):
        m.arm(iid)
    assert m.instances[iid].armed is False and txs == []


def test_a_running_executor_goes_silent_when_the_gate_closes_and_stays_disarmed():
    state = {"reason": None}
    m, iid, txs = _manager(lambda: state["reason"])
    try:
        m.arm(iid)
        assert txs[0].wait_for_frames(1)
        state["reason"] = "the master write switch is off"
        m.instances[iid]._thread.join(timeout=2)
        assert m.instances[iid].running is False and m.instances[iid].armed is False
        assert txs[0].closed is True and txs[0].quiet_for()
        state["reason"] = None                          # the switch comes back on …
        assert txs[0].quiet_for() and m.instances[iid].armed is False   # … no auto re-arm
    finally:
        m.stop_all()


def test_revoke_disarms_every_armed_instance_synchronously():
    m, iid, txs = _manager(lambda: None)
    try:
        m.arm(iid)
        assert txs[0].wait_for_frames(1)
        assert m.revoke("the gateway became standby") == [iid]
        assert m.instances[iid].armed is False and m.instances[iid].running is False
        assert txs[0].closed is True and txs[0].quiet_for()
        assert m.revoke("again") == []
    finally:
        m.stop_all()


# ── through the real app: the two switches every hardware write obeys ────────────────────

def _armed_app(tmp_path):
    app, _ = _boot(tmp_path)
    mgr = app.state.vbms_manager
    mgr.instances['vb'].period_s = 0.005
    txs = []

    def factory(channel):
        txs.append(CountingTx(channel))
        return txs[-1]
    mgr._tx_factory = factory
    app.state.ctx.config.security.allow_writes = True
    mgr.arm('vb')
    assert txs[0].wait_for_frames(1)
    return app, mgr, txs


def test_turning_the_master_switch_off_silences_a_running_virtual_bms(tmp_path):
    app, mgr, txs = _armed_app(tmp_path)
    try:
        app.state.ctx.config.security.allow_writes = False     # any path: the loop reads it live
        mgr.instances['vb']._thread.join(timeout=2)
        assert mgr.instances['vb'].armed is False and txs[0].closed and txs[0].quiet_for()
    finally:
        mgr.stop_all()


def test_becoming_standby_disarms_a_running_virtual_bms_before_set_returns(tmp_path):
    app, mgr, txs = _armed_app(tmp_path)
    try:
        app.state.role.set('standby', 1, 'test')
        assert mgr.instances['vb'].armed is False and mgr.instances['vb'].running is False
        assert txs[0].closed and txs[0].quiet_for()
        app.state.role.set('active', 2, 'test')
        assert mgr.instances['vb'].armed is False and txs[0].quiet_for()   # explicit re-arm only
    finally:
        mgr.stop_all()
        app.state.role.set('active', 3, 'test-cleanup')


def test_a_standby_gateway_refuses_to_arm(tmp_path):
    app, _ = _boot(tmp_path)
    mgr = app.state.vbms_manager
    app.state.ctx.config.security.allow_writes = True
    app.state.role.set('standby', 1, 'test')
    try:
        with pytest.raises(VbmsGateClosed):
            mgr.arm('vb')
        assert mgr.instances['vb'].armed is False
    finally:
        app.state.role.set('active', 2, 'test-cleanup')


def test_the_security_route_revokes_when_it_turns_writes_off(tmp_path):
    from fastapi.testclient import TestClient
    app, mgr, txs = _armed_app(tmp_path)
    try:
        r = TestClient(app).post('/api/config/security', json={"allow_writes": False})
        assert r.status_code == 200, r.text
        assert mgr.instances['vb'].armed is False and mgr.instances['vb'].running is False
    finally:
        mgr.stop_all()


# ── WK7-SAFE-2: a bank that cannot be vouched for (design §4.1) ──────────────────────────

class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _bank(store, **over):
    """A manager over a mutable store, a fake clock and the 2-pack fixture config."""
    cfg = {"virtual_bms": [{**CONFIG["virtual_bms"][0], **over}]}
    clock = _Clock()
    m = VbmsManager(cfg, lambda dev, field: store.get((dev, field)), _load,
                    tx_factory=FakeTx, clock=clock)
    return m, m.instances["deye-vbms"], clock


def _store(*packs):
    out = {}
    for dev in packs:
        out.update({(dev, "soc"): 80, (dev, "pack_voltage"): 53.0, (dev, "battery_current"): 5.0})
    return out


def test_min_packs_defaults_to_all_but_one():
    _m, inst, _c = _bank({})
    assert inst.min_packs == 1                           # 2 packs → max(1, 2-1)
    _m, inst, _c = _bank({}, packs=["a", "b", "c", "d"])
    assert inst.min_packs == 3
    _m, inst, _c = _bank({}, packs=["a", "b", "c", "d"], min_packs=4)
    assert inst.min_packs == 4


def test_enough_packs_sends_the_limits_of_the_packs_it_has():
    m, inst, clock = _bank(_store("seplos-adr1"))        # 1 of 2 is still vouched for
    v = m.tx_values(inst)
    assert m.state(inst)["data_state"] == "ok"
    assert v["charge_current_limit"] == 0                # the ramp starts at 0 (R2D-VBMS-3)
    for _ in range(30):
        clock.t += 1
        v = m.tx_values(inst)
    assert v["charge_current_limit"] == 10 and v["discharge_current_limit"] == 20   # 1 of 2
    assert v["charge_enable"] == 1 and v["discharge_enable"] == 1


def test_no_live_pack_sends_zero_limits_and_no_enables_during_the_hold():
    store = {}
    m, inst, clock = _bank(store)
    v = m.tx_values(inst)
    assert v is not None and m.state(inst)["data_state"] == "hold"
    assert (v["charge_current_limit"], v["discharge_current_limit"]) == (0, 0)
    assert (v["charge_enable"], v["discharge_enable"]) == (0, 0)
    m.command("deye-vbms", "charge_current_limit", 60)   # an EMS command cannot lift it
    assert m.tx_values(inst)["charge_current_limit"] == 0


def test_after_the_hold_the_executor_is_silent_and_returns_when_packs_do():
    store = {}
    m, inst, clock = _bank(store, fallback={"hold_s": 5})
    assert m.tx_values(inst) is not None                 # hold
    clock.t += 5.1
    assert m.tx_values(inst) is None                     # silence
    assert m.state(inst)["data_state"] == "silent"
    assert m.state(inst)["values"]["charge_current_limit"] == 0   # telemetry still says 0
    store.update(_store("seplos-adr1", "seplos-adr2"))   # never seen before: admitted at once
    v = m.tx_values(inst)
    assert v is not None and m.state(inst)["data_state"] == "ok"
    assert v["charge_current_limit"] == 0                # … but through the ramp (R2D-VBMS-3)
    store.clear()
    assert m.tx_values(inst) is not None                 # a NEW failure gets its own hold


def test_a_stricter_min_packs_fails_when_one_pack_drops():
    m, inst, _c = _bank(_store("seplos-adr1"), min_packs=2)
    assert m.state(inst)["data_state"] == "hold"
    assert m.tx_values(inst)["discharge_current_limit"] == 0


def test_silence_puts_no_frame_on_the_bus():
    """End to end through the executor: an armed instance with no live pack sends zero-limit
    frames during the hold and then NOTHING — never a frame set of zeros."""
    store = {}
    cfg = {"virtual_bms": [{**CONFIG["virtual_bms"][0], "fallback": {"hold_s": 0}}]}
    txs = []

    def factory(channel):
        txs.append(CountingTx(channel))
        return txs[-1]
    m = VbmsManager(cfg, lambda dev, field: store.get((dev, field)), _load, tx_factory=factory)
    m.instances["deye-vbms"].period_s = 0.005
    try:
        m.arm("deye-vbms")
        assert txs[0].quiet_for(0.2) and txs[0].count == 0
        assert m.instances["deye-vbms"].running is True          # armed, alive, saying nothing
        store.update(_store("seplos-adr1", "seplos-adr2"))
        assert txs[0].wait_for_frames(1)                         # the packs are back → it speaks
    finally:
        m.stop_all()


def test_the_loop_never_sends_an_empty_value_set():
    vbms = VirtualBms(_load("x"), name="vb")
    vbms.arm()
    tx = FakeTx("fake")
    stop = threading.Event()
    answers = iter([None, {}, None])

    def values():
        try:
            return next(answers)
        except StopIteration:
            stop.set()
            return None
    VbmsRuntime(vbms, tx, values, period_s=0).run(stop)
    assert tx.sent == []


# ── R2D-VBMS-1: one bad cycle is not the end of the executor ─────────────────────────────

class _FlakyTx(FakeTx):
    """Raises on the first send of its first set, then works."""
    def __init__(self, channel):
        super().__init__(channel)
        self.fail_next = True

    def send(self, can_id, data):
        if self.fail_next:
            self.fail_next = False
            raise OSError(105, "No buffer space available")
        super().send(can_id, data)


def test_a_transient_send_error_costs_one_cycle_not_the_executor():
    vbms = VirtualBms(_load("x"), name="vb")
    vbms.arm()
    tx = _FlakyTx("fake")
    stop = threading.Event()
    ticks = iter(range(3))

    def values():
        if next(ticks, None) is None:
            stop.set()
            return None
        return {"soc": 50}
    rt = VbmsRuntime(vbms, tx, values, period_s=0)
    assert rt.run(stop) == 2                       # the first set failed, the next two went out
    assert rt.failures == 1 and vbms.armed is True
    assert len(tx.sent) == 2 * len(vbms.can_ids())


def test_a_value_source_that_raises_costs_one_cycle_not_the_executor():
    vbms = VirtualBms(_load("x"), name="vb")
    vbms.arm()
    tx = FakeTx("fake")
    stop = threading.Event()
    calls = {"n": 0}

    def values():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TypeError("can't convert type 'str' to numerator/denominator")
        if calls["n"] > 3:
            stop.set()
            return None
        return {"soc": 50}
    rt = VbmsRuntime(vbms, tx, values, period_s=0)
    assert rt.run(stop) == 2 and rt.failures == 1 and vbms.armed is True


def test_a_non_numeric_pack_value_does_not_kill_a_running_instance():
    store = _store("seplos-adr1", "seplos-adr2")
    txs = []

    def factory(channel):
        txs.append(CountingTx(channel))
        return txs[-1]
    m = VbmsManager(CONFIG, lambda dev, field: store.get((dev, field)), _load, tx_factory=factory)
    inst = m.instances["deye-vbms"]
    inst.period_s = 0.005
    try:
        m.arm("deye-vbms")
        assert txs[0].wait_for_frames(1)
        store[("seplos-adr1", "pack_voltage")] = "n/a"           # a pack starts answering text
        deadline = threading.Event()
        for _ in range(100):                                     # until a cycle has failed on it
            if getattr(inst.runtime, "failures", 0):
                break
            deadline.wait(0.01)
        assert inst.runtime.failures >= 1
        assert txs[0].quiet_for(0.15)                            # nothing goes out meanwhile
        assert inst.running is True and inst.armed is True       # alive, waiting for a number
        assert "values" in m.status()["deye-vbms"]["last_error"]
        store[("seplos-adr1", "pack_voltage")] = 53.0
        before = txs[0].count
        assert txs[0].wait_for_frames(before + 1)                # and it speaks again by itself
    finally:
        m.stop_all()


def test_a_transmitter_that_will_not_open_leaves_it_armed_idle_with_the_cause_shown():
    """The designed state for a port still in listen-only: armed, not running (the UI's
    "Armed · not transmitting") — and now the status says why, not only the log."""
    class DeadTx(FakeTx):
        def open(self):
            raise OSError("no such device")
    m = VbmsManager(CONFIG, _read, _load, tx_factory=DeadTx)
    inst = m.instances["deye-vbms"]
    m.arm("deye-vbms")
    inst._thread.join(timeout=2)
    assert inst.running is False and inst.armed is True
    assert "would not open" in m.status()["deye-vbms"]["last_error"]


def test_an_executor_that_crashes_does_not_stay_armed(monkeypatch):
    """If the loop itself dies (its per-cycle handling did not hold), nothing will restart it:
    the instance must not read "armed" for ever."""
    def crash(self, stop):
        raise MemoryError("the loop died")
    monkeypatch.setattr(VbmsRuntime, "run", crash)
    m = VbmsManager(CONFIG, _read, _load, tx_factory=FakeTx)
    inst = m.instances["deye-vbms"]
    m.arm("deye-vbms")
    inst._thread.join(timeout=2)
    assert inst.running is False and inst.armed is False
    assert "crashed" in m.status()["deye-vbms"]["last_error"]


# ── R2D-LIFE-1: nothing can be armed while a reload is in flight ─────────────────────────

def test_a_frozen_manager_refuses_to_arm_until_it_is_thawed():
    m, iid, txs = _manager(lambda: None)
    try:
        m.arm(iid)
        assert txs[0].wait_for_frames(1)
        assert m.freeze("the configuration is being imported/restored") == [iid]
        assert m.instances[iid].armed is False
        with pytest.raises(VbmsGateClosed, match="imported/restored"):
            m.arm(iid)
        assert m.instances[iid].armed is False
        m.thaw()
        m.arm(iid)
        assert m.instances[iid].armed is True
    finally:
        m.stop_all()


def test_an_arm_that_arrives_during_an_import_is_refused_and_nothing_is_armed_after(tmp_path):
    import io
    import zipfile

    import yaml
    from fastapi.testclient import TestClient
    app, mgr, txs = _armed_app(tmp_path)
    cfg = app.state.ctx.config
    cfg.save_yaml_config()
    doc = yaml.safe_load(cfg.config_path.read_text())
    b = io.BytesIO()
    with zipfile.ZipFile(b, 'w') as z:
        z.writestr('config.yaml', yaml.safe_dump(doc))
    refused = []
    orig = cfg.load

    def load_with_a_racing_arm():
        try:
            mgr.arm('vb')                       # what an arm request would do, mid-reload
        except VbmsGateClosed as e:
            refused.append(str(e))
        return orig()
    cfg.load = load_with_a_racing_arm
    try:
        r = TestClient(app).post('/api/config/import?lock_writes=false', content=b.getvalue())
        assert r.status_code == 200, r.text
        assert refused and mgr.instances['vb'].armed is False and mgr.instances['vb'].running is False
        mgr.arm('vb')                           # thawed once the reload is over
        assert mgr.instances['vb'].armed is True
    finally:
        mgr.stop_all()


def test_a_reload_that_fails_still_reconciles_and_thaws(tmp_path):
    from fastapi.testclient import TestClient
    app, mgr, txs = _armed_app(tmp_path)
    cfg = app.state.ctx.config
    cfg.save_yaml_config()
    import io
    import zipfile

    import yaml
    doc = yaml.safe_load(cfg.config_path.read_text())
    doc['virtual_bms'] = []
    b = io.BytesIO()
    with zipfile.ZipFile(b, 'w') as z:
        z.writestr('config.yaml', yaml.safe_dump(doc))
    app.state.template_registry.reload = lambda: (_ for _ in ()).throw(RuntimeError("after the load"))
    try:
        r = TestClient(app).post('/api/config/import?lock_writes=false', content=b.getvalue())
        assert r.status_code == 500, r.text
        assert mgr.instances == {}              # the published config has none: none is armable
        assert mgr._frozen is None
    finally:
        mgr.stop_all()
