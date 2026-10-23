# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Rules phase 3: several rules on one target. Channels (what commands
control together), the referee (priority, min, max), the channel's own
protection whoever writes, and safety above manual — on a Delta VFD-EL and
on the Fronius inverters."""
import json

import pytest

from multibus import clock
from multibus.arbiter import Candidate, ChannelState, winner
from multibus.commands import assign_channels, parse_command_def
from tests.test_commands import _Inverter
from tests.test_commands_api import _Facade
from tests.test_delta_vfd_el import FREQ, _Drive
from tests.test_devices import write_config
from tests.test_endpoints import needs_tc
from tests.test_rules_api import _Clock, _rt, _rule_app

DRIVE_YAML = """
security: { allow_writes: true }
devices:
  - id: vfd1
    name: Pool pump drive
    template: delta_vfd_el
    connection: { protocol: tcp, host: 192.0.2.10, port: 502, unit_id: 1 }
    commands:
      - { name: run_at }
      - { name: run }
      - { name: set_frequency }
"""
ALWAYS = 'between("00:00", "24:00")'


def _drive_app(tmp_path):
    from fastapi.testclient import TestClient

    from multibus import auth as _authmod
    from multibus.api import create_api
    cfg = write_config(tmp_path, extra_yaml=DRIVE_YAML)
    cfg.ui.auth_enabled = True
    cfg.ui.auth_username = "admin"
    cfg.ui.auth_password = _authmod.hash_password("pw")
    drive = _Drive()
    devices = [(d, _Facade(drive) if d.id == 'vfd1' else None) for d in cfg.devices]
    app, _ = create_api(cfg, None, None, None, devices=devices)
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/api/auth/login", json={"username": "admin", "password": "pw"})
    ck = _Clock()
    rt = _rt(app, ck)
    clock.set_timezone('UTC')                    # the app set the installation's zone at start
    return app, client, rt, drive, ck


def _rule(rid, layer, when, then, otherwise=None, **over):
    r = {'id': rid, 'mode': 'armed', 'layer': layer, 'target': {'device': 'vfd1', 'command': 'run_at'},
         'kind': 'condition', 'when': when, 'then': {'params': {'value': then}} if then is not None else None,
         'else': {'params': {'value': otherwise}} if otherwise is not None else None,
         'timing': {'every_s': 2, 'debounce': 1, 'min_interval_s': 0, 'reassert_s': 0}}
    r.update(over)
    return r


@pytest.fixture(autouse=True)
def _utc():
    clock.set_timezone('UTC')
    yield
    clock.set_now(None)


def _freqs(drive):
    return [v[0] / 100 for a, v in drive.writes if a == FREQ]       # 2001H is 0.01 Hz


# ── the pure parts ───────────────────────────────────────────────────────────

def test_commands_that_write_a_common_register_are_one_channel():
    defs = {k: parse_command_def(k, v) for k, v in {
        'run': {'writes': [{'register': 'cw', 'value': 1}]},
        'run_at': {'writes': [{'register': 'f', 'value': 1}, {'register': 'cw', 'value': 6}]},
        'set_frequency': {'writes': [{'register': 'f', 'value': 1}]},
        'stop': {'alias': {'command': 'run', 'params': {'value': 0}}},
        'reset': {'writes': [{'register': 'fault', 'value': 2}]},
        'mode': {'writes': [{'register': 'm', 'value': 1}], 'channel': 'motor'},
        'other': {'writes': [{'register': 'x', 'value': 1}], 'channel': 'motor'},
    }.items()}
    ch = assign_channels(defs)
    assert ch['run'] == ch['run_at'] == ch['set_frequency'] == ch['stop'] == 'run+run_at+set_frequency'
    assert ch['reset'] == 'reset' and ch['mode'] == ch['other'] == 'motor'


def test_the_referee_by_priority_and_by_value():
    a, b, c = Candidate('ov', 90, 70), Candidate('export', 50, 60), Candidate('scada', 50, 80)
    assert winner([a, b, c], 'priority').rule == 'ov'
    assert winner([a, b, c], 'min').rule == 'export' and winner([a, b, c], 'max').rule == 'scada'
    assert winner([Candidate('x', 70, 1), Candidate('y', 70, 2, forced=True)], 'priority').rule == 'y'


def test_the_channel_protection_counts_starts_and_stops_whoever_wrote():
    ch = ChannelState()
    cfg = {'min_on_s': 60, 'min_off_s': 30, 'max_starts_per_h': 2}
    assert ch.hold(cfg, True, 0) is None
    ch.note(True, 0)
    assert 'minimum run time' in ch.hold(cfg, False, 10)
    ch.note(False, 70)
    assert 'minimum stop time' in ch.hold(cfg, True, 80)
    ch.note(True, 100)
    ch.note(False, 200)
    assert 'starts in the last hour (max 2)' in ch.hold(cfg, True, 300)
    assert ch.hold(cfg, None, 300) is None                 # a set point neither starts nor stops


# ── rules on the drive ───────────────────────────────────────────────────────

@needs_tc
def test_safety_wins_the_drive_and_the_schedule_takes_it_back(tmp_path):
    app, client, rt, drive, ck = _drive_app(tmp_path)
    clock.set_now(lambda: 10 * 3600.0)
    ck.t = 10 * 3600.0                                              # 10:00
    assert client.post("/api/rules", json=_rule('quiet', 'safety', 'hour() < 12', 0)).status_code == 200
    assert client.post("/api/rules", json=_rule('day', 'schedule', ALWAYS, 25)).status_code == 200
    rt.tick()
    assert _freqs(drive) == [0]                                     # only safety wrote
    d = list(rt.decisions['day'])[0]
    assert d['action'] == 'yield' and 'yields to quiet' in d['reason']
    assert rt.states[('day', 'vfd1')].yielding_to == 'quiet'
    view = client.get("/api/channels/vfd1").json()['channels']
    motor = next(c for c in view if c['channel'] == 'motor')
    assert motor['winner'] == 'quiet' and [r['rule'] for r in motor['rules']] == ['quiet', 'day']
    clock.set_now(lambda: 13 * 3600.0)
    ck.t = 13 * 3600.0                                              # 13:00: the quiet rule has no opinion
    rt.tick()
    assert _freqs(drive)[-1] == 25 and rt.states[('day', 'vfd1')].yielding_to is None


@needs_tc
def test_two_rules_at_one_priority_on_a_priority_channel_are_refused(tmp_path):
    app, client, rt, drive, ck = _drive_app(tmp_path)
    assert client.post("/api/rules", json=_rule('a', 'schedule', ALWAYS, 25)).status_code == 200
    r = client.post("/api/rules", json=_rule('b', 'schedule', ALWAYS, 30))
    assert r.status_code == 422 and 'same' not in r.text and 'priority 50' in r.text
    r = client.post("/api/rules", json=_rule('b', 'schedule', ALWAYS, 30, mode='shadow'))
    assert r.status_code == 200                                     # in shadow it may exist; arming it is refused
    assert client.post("/api/rules/b/mode", json={'mode': 'armed'}).status_code == 422


@needs_tc
def test_a_force_lifts_a_rule_to_manual_over_the_schedule(tmp_path):
    app, client, rt, drive, ck = _drive_app(tmp_path)
    assert client.post("/api/rules", json=_rule('day', 'schedule', ALWAYS, 25)).status_code == 200
    assert client.post("/api/rules", json=_rule('solar', 'optimize', ALWAYS, 40)).status_code == 200
    rt.tick()
    assert _freqs(drive) == [25]                                    # schedule (50) over optimize (30)
    assert client.post("/api/rules/solar/force", json={'value': 45, 'seconds': 600}).status_code == 200
    rt.tick()
    assert _freqs(drive)[-1] == 45                                  # forced: manual (70)


@needs_tc
def test_a_safety_rule_cannot_be_paused_from_outside_but_a_schedule_can(tmp_path):
    app, client, rt, drive, ck = _drive_app(tmp_path)
    assert client.post("/api/rules", json=_rule('day', 'schedule', ALWAYS, 25)).status_code == 200
    rt.tick()
    r = client.post("/api/devices/vfd1/commands/run", json={'value': 0})       # another command, same channel
    assert r.status_code == 409 and "owned by rule 'day'" in r.json()['reason']
    r = client.post("/api/devices/vfd1/commands/run", json={'value': 0, 'override_s': 600})
    assert r.status_code == 200 and rt.states[('day', 'vfd1')].paused_until is not None
    client.post("/api/rules", json=_rule('stop-hot', 'safety', ALWAYS, 0))
    rt.tick()
    r = client.post("/api/devices/vfd1/commands/run_at", json={'value': 30, 'override_s': 600})
    assert r.status_code == 409 and "safety rule 'stop-hot'" in r.json()['reason']


@needs_tc
def test_the_channel_protection_holds_an_outside_start_too(tmp_path):
    app, client, rt, drive, ck = _drive_app(tmp_path)                # template: min_off_s 5
    assert client.post("/api/devices/vfd1/commands/run_at", json={'value': 25}).status_code == 200
    assert client.post("/api/devices/vfd1/commands/run_at", json={'value': 0}).status_code == 200
    r = client.post("/api/devices/vfd1/commands/run_at", json={'value': 25})
    assert r.status_code == 409 and r.json()['held'] and 'minimum stop time' in r.json()['reason']
    r = client.post("/api/devices/vfd1/commands/set_frequency", json={'value': 30})
    assert r.status_code == 200                                     # a set point neither starts nor stops


# ── limits on the inverters: the lowest goes out ─────────────────────────────

@needs_tc
def test_two_limits_on_a_min_channel_send_the_lowest(tmp_path):
    inv = _Inverter(sf=-2)
    cfg, app, client, clock_, rt = _rule_app(tmp_path, {'pv-u1': inv})
    client.delete("/api/rules/ov-u1")
    base = {'mode': 'armed', 'target': {'device': 'pv-u1', 'command': 'power_limit'}, 'kind': 'condition',
            'when': ALWAYS, 'else': None, 'params': {'revert_s': 0},
            'timing': {'every_s': 2, 'debounce': 1, 'min_interval_s': 0, 'reassert_s': 0}}
    assert client.post("/api/rules", json=dict(base, id='export', then={'params': {'value': 70}})).status_code == 200
    assert client.post("/api/rules", json=dict(base, id='scada', then={'params': {'value': 60}})).status_code == 200
    rt.tick(clock_())
    clock_.t += 2
    rt.tick(clock_())
    assert inv.mem[40232] == 6000                                   # 60 % at SF -2
    assert rt.states[('export', 'pv-u1')].yielding_to == 'scada'
    assert json.dumps(client.get("/api/channels/pv-u1").json()).count('"winner": "scada"') == 1


# ── a device that does not answer holds nobody ──────────────────────────────

@needs_tc
def test_a_device_that_does_not_answer_does_not_hold_the_rules_or_other_devices(tmp_path):
    import threading
    import time as _time
    inv_ok, inv_slow = _Inverter(sf=-2), _Inverter(sf=-2)
    gate = threading.Event()
    real = inv_slow.write

    def stuck(*a, **k):                     # the second inverter's link hangs
        gate.wait(10)
        return real(*a, **k)
    inv_slow.write = stuck
    cfg, app, client, clock_, rt = _rule_app(tmp_path, {'pv-u1': inv_ok, 'pv-u2': inv_slow})
    client.delete("/api/rules/ov-u1")
    base = {'mode': 'armed', 'kind': 'condition', 'when': ALWAYS, 'else': None, 'params': {'revert_s': 0},
            'then': {'params': {'value': 60}},
            'timing': {'every_s': 2, 'debounce': 1, 'min_interval_s': 0, 'reassert_s': 0}}
    for dev in ('pv-u2', 'pv-u1'):
        r = client.post("/api/rules", json=dict(base, id=f'cap-{dev}', target={'device': dev, 'command': 'power_limit'}))
        assert r.status_code == 200, r.text
    t0 = _time.monotonic()
    rt.tick(clock_())
    took = _time.monotonic() - t0
    assert inv_ok.mem[40232] == 6000                       # the healthy inverter was written in the tick
    assert took < 7                                        # the tick waited its budget, not the hung link
    clock_.t += 2
    t1 = _time.monotonic()
    rt.tick(clock_())                                      # the hung one has failures? no — it is still in flight
    assert _time.monotonic() - t1 < 1.5
    d = [x for x in rt.decisions['cap-pv-u2'] if x['action'] in ('run', 'reassert')]
    assert d and d[0]['result'] in ('sent', 'queued')
    gate.set()


def test_the_watchdog_says_when_the_rules_stop_ticking():
    from multibus.rules_runtime import RulesRuntime
    rt = RulesRuntime.__new__(RulesRuntime)
    alerts = []
    rt.rules = {'r': type('R', (), {'enabled': True, 'timing': type('T', (), {'every_s': 2})(), 'id': 'r'})()}
    rt.errors = {}
    rt._mono = lambda: 0.0
    rt._alert = lambda sev, key, msg: alerts.append((sev, key, msg))
    rt._last_tick_mono = 0.0
    assert rt.check_stall(now=20) is None                 # under 30 s: fine
    assert 'has not run for 40 s' in rt.check_stall(now=40) and alerts[-1][0] == 'error'
    assert rt.check_stall(now=50) is None and len(alerts) == 1          # said once
    rt._last_tick_mono = 50.0
    rt.check_stall(now=51)
    assert alerts[-1] == ('info', 'rules:stalled', 'the rules engine runs again')
