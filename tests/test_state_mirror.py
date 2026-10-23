# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The mirror an active gateway hands its standby partner: the values of the
devices it polls, every rule's state, every channel's protection — moved
from one process's monotonic clock to the other's by their ages."""
import threading
from types import SimpleNamespace

import pytest

from multibus.arbiter import ChannelState
from multibus.rules import RuleState, parse_rule_def
from multibus.state_mirror import export_values, import_values

from tests.test_devices_api import needs_tc


def _rule():
    return parse_rule_def({"id": "pump", "label": "Pump", "kind": "steps", "enabled": True, "mode": "shadow",
                           "signal": "voltage", "stale_after_s": 60, "every_s": 2,
                           "target": {"command": "power_limit", "param": "value"},
                           "steps": [{"at": 253, "value": 60, "label": "High"}],
                           "release_below": 250, "normal": {"value": 100}})


def test_a_rules_state_moves_to_another_clock_by_its_ages():
    a = RuleState(_rule())
    a.state, a.want, a.since, a.commanded = 'true', {'hz': 30}, 1_800_000_000.0, {'hz': 30}
    a.last_cmd_ts, a.on_since, a._rest_until = 990.0, 400.0, 1100.0      # A's clock: now = 1000
    a._starts.extend([100.0, 700.0])
    a._deb_target, a._sample_ts, a.on_today_s, a.paused_until = ('true', 1), 999.0, 1234.5, 1_800_000_600.0
    d = a.export_mirror(1000.0)
    b = RuleState(_rule())
    b.import_mirror(d, 50.0)                                            # B's clock: now = 50
    assert (b.state, b.want, b.commanded, b.since, b.paused_until) == ('true', {'hz': 30}, {'hz': 30},
                                                                      1_800_000_000.0, 1_800_000_600.0)
    assert b.last_cmd_ts == pytest.approx(40.0) and b.on_since == pytest.approx(-550.0)
    assert b._rest_until == pytest.approx(150.0)                         # 100 s from now, still
    assert list(b._starts) == pytest.approx([-850.0, -250.0])
    assert b._deb_target == ('true', 1) and b.on_today_s == 1234.5
    assert b._sample_ts is None and b._deb_sample_ts is None             # its next sample is new


def test_a_channels_protection_survives_the_move():
    a = ChannelState()
    for t in (100.0, 900.0, 950.0):
        a.note(True, t)
        a.note(False, t + 10)
    a.note(True, 990.0)
    b = ChannelState.from_mirror(a.export_mirror(1000.0), 5.0)
    assert b.on is True and b.starts_last_h(5.0) == a.starts_last_h(1000.0) == 4
    cfg = {'min_on_s': 60, 'max_starts_per_h': 4}
    assert b.hold(cfg, False, 5.0) == a.hold(cfg, False, 1000.0)         # 50 s to go before stopping, on both


def test_the_runtime_takes_what_it_knows_and_counts_the_rest():
    from multibus.rules_runtime import RulesRuntime
    rt = RulesRuntime.__new__(RulesRuntime)
    rt._lock, rt._mono, rt.states, rt.rules = threading.RLock(), (lambda: 10.0), {}, {'pump': _rule()}
    st = RuleState(_rule())
    st.state = 'true'
    data = {'states': {'pump\tvfd1': st.export_mirror(500.0), 'gone\tvfd1': {}},
            'channels': {'vfd1\tmotor': ChannelState().export_mirror(500.0)}}
    got = rt.import_mirror(data)
    assert got == {'states': 1, 'states_left_out': 1, 'channels': 1}
    assert rt.states[('pump', 'vfd1')].state == 'true' and ('vfd1', 'motor') in rt.channels
    assert set(rt.export_mirror()['states']) == {'pump\tvfd1'}


# ── values ──

def _setup():
    polled = SimpleNamespace(id='meter', sources=[SimpleNamespace(protocol='tcp')], protocol='tcp')
    tapped = SimpleNamespace(id='tap', sources=[SimpleNamespace(protocol='rtu_tap')], protocol='rtu_tap')
    stores = {'meter': {}, 'tap': {}}
    reg = SimpleNamespace(store_for=stores.get, ensure_store=lambda d: stores.setdefault(d, {}))
    return SimpleNamespace(devices=[polled, tapped]), reg, stores


def test_values_travel_with_their_age_and_only_for_what_is_polled(monkeypatch):
    from multibus import state_mirror
    cfg, reg, stores = _setup()
    stores['meter'][100] = {'value': 230.1, 'name': 'v', 'ts': 1000.0, 'mono': 95.0}
    stores['meter'][101] = {'value': 5, 'name': 'old', 'ts': 900.0, 'mono': 10.0}
    stores['tap'][7] = {'value': 1, 'name': 'heard', 'ts': 1000.0, 'mono': 95.0}
    monkeypatch.setattr(state_mirror.time, 'monotonic', lambda: 100.0)
    out = export_values(cfg, reg)
    assert set(out) == {'meter'} and out['meter']['100']['age_s'] == 5.0 and 'mono' not in out['meter']['100']
    assert set(export_values(cfg, reg, since=950.0)['meter']) == {'100'}          # only what is new
    cfg2, reg2, stores2 = _setup()
    stores2['meter'][101] = {'value': 6, 'name': 'old', 'ts': 950.0, 'mono': 1.0}  # B holds it newer
    monkeypatch.setattr(state_mirror.time, 'monotonic', lambda: 7000.0)
    got = import_values(cfg2, reg2, {**out, 'nope': {}})
    assert got == {'values': 1, 'kept_newer': 1, 'devices_unknown': 1}
    assert stores2['meter'][100]['mono'] == pytest.approx(6995.0) and stores2['meter'][101]['value'] == 6


# ── the API ──

@needs_tc
def test_the_mirror_is_read_from_the_active_and_taken_only_by_a_standby(tmp_path):
    from tests.test_operator_role import _build_clients
    c = _build_clients(tmp_path)
    r = c.admin.post("/api/auth/service-tokens", json={"name": "mbg-ha", "role": "ha"})
    ha = {"Authorization": f"Bearer {r.json()['token']}"}
    from fastapi.testclient import TestClient
    svc = TestClient(c.app, raise_server_exceptions=False)
    m = svc.get("/api/state/mirror", headers=ha)
    assert m.status_code == 200 and {'t', 'values', 'rules', 'leases', 'sequences'} <= set(m.json())
    body = {'t': 1.0, 'values': {}, 'rules': {}, 'leases': [{'device': 'umg512', 'remaining_s': 9}], 'sequences': []}
    assert svc.post("/api/state/mirror", json=body, headers=ha).status_code == 409   # active: it gives
    svc.put("/api/role", json={"role": "standby", "epoch": 1}, headers=ha)
    r = svc.post("/api/state/mirror", json=body, headers=ha)
    assert r.status_code == 200 and r.json()['took']['leases'] == 1
    assert c.app.state.peer_mirror['leases'][0]['device'] == 'umg512'
    assert c.viewer.post("/api/state/mirror", json=body).status_code == 403


@needs_tc
def test_a_channels_protection_outlives_a_restart(tmp_path, monkeypatch):
    """A motor started 30 s before a restart, two minutes down: its minimum run
    time and its starts of the hour still count when the gateway is back."""
    from multibus import rules_runtime as rr
    from tests.test_devices_api import make_app
    _cfg, client = make_app(tmp_path)
    rt = client.app.state.rules_runtime
    rt._mono = lambda: 1000.0
    rt._chan_state('vfd1', 'motor').note(True, 970.0)
    monkeypatch.setattr(rr.time, 'time', lambda: 5000.0)
    rt._save_state()
    monkeypatch.setattr(rr.time, 'time', lambda: 5120.0)                 # down for 120 s
    _cfg2, client2 = make_app(tmp_path)                                  # the same config folder
    back = client2.app.state.rules_runtime
    back.__dict__.pop('channels', None)
    back._mono = lambda: 20.0
    back.load()
    st = back.channels[('vfd1', 'motor')]
    assert st.on is True and st.on_since == pytest.approx(20.0 - 150.0) and st.starts_last_h(20.0) == 1
    assert st.hold({'min_on_s': 300}, False, 20.0)                       # still inside its minimum run
