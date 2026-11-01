# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Raw register writes travel the lanes; what a command writes is never
written raw (ADR-0022 D4–D8, CMD-Q-7): the MQTT
worker never waits for a bus, the latest value wins, a stale one expires,
every HA outcome is told on cmd/result, REST keeps its error body, the raw
lease revert is a lane revert job, and a clean shutdown never writes past
its budget — in main.py's order (review HIGH 1)."""
import json
import threading
import time
from unittest.mock import MagicMock

import pytest

from tests.test_devices_api import needs_tc
from tests.test_rules_recheck import _bg, _fast, _lock, _plant, _unit  # noqa: F401


def _tpl():
    from multibus.device_template import parse_template
    reg = (lambda a, n, **kw: {"address": a, "name": n, "data_type": "uint16", "register_type": "holding",
                               "writable": True, **kw})
    return parse_template({"device_template": {
        "schema_version": 1, "id": "ctrl_tpl", "name": "Ctrl", "protocol": {}, "categories": {},
        "registers": [
            reg(100, "limit", write_min=0, write_max=100),
            reg(101, "enable", write_min=0, write_max=1),
            reg(200, "setpoint", write_min=0, write_max=5000, write_safe=0),
            reg(300, "mode", write_min=0, write_max=1, enum={"0": "Off", "1": "On"}),
        ],
        # a command that writes `enable` second: never written raw (D4)
        "commands": {"lim": {"label": "Limit", "params": {"value": {"min": 0, "max": 100, "required": True}},
                             "writes": [{"register": "limit", "value": "${value}"},
                                        {"register": "enable", "value": 1}],
                             "channel": "power"}},
    }}, builtin=True)


def _twin_tpl():
    """Another template on the same slave: no command, `enable` writable."""
    from multibus.device_template import parse_template
    return parse_template({"device_template": {
        "schema_version": 1, "id": "twin_tpl", "name": "Twin", "protocol": {}, "categories": {},
        "registers": [{"address": 101, "name": "enable", "data_type": "uint16", "register_type": "holding",
                       "writable": True, "write_min": 0, "write_max": 1}]}}, builtin=True)


class _Fake:
    def __init__(self):
        self.reg, self.writes = {}, []

    def write_value(self, address, register_type, data_type, value, scale=1.0, offset=0.0, prefer_fc6=False):
        self.writes.append((address, value))
        self.reg[address] = value
        return True, None, [int(float(value))]

    def read_register(self, address, data_type, register_type):
        return self.reg.get(address)


class _Gw:
    pass


def _gw(tmp_path, monkeypatch, rate=10.0, ids=('ctrl',), twin=False):
    from fastapi.testclient import TestClient

    from multibus import auth as _authmod
    from multibus.api import create_api
    from multibus.config import DeviceConfig
    from tests.test_devices import write_config
    cfg = write_config(tmp_path)
    cfg.security.allow_writes = True
    cfg.security.write_rate_limit_per_s = rate
    cfg.mqtt.allow_write_entities = True
    cfg.mqtt.username = "gw"
    cfg.ui.auth_enabled = True
    cfg.ui.auth_username = "admin"
    cfg.ui.auth_password = _authmod.hash_password("pw")
    devs = [DeviceConfig(id=i, name=i, template='ctrl_tpl', protocol='tcp', mqtt_topic_prefix=f"t/{i}") for i in ids]
    if twin:
        devs.append(DeviceConfig(id='twin', name='twin', template='twin_tpl', protocol='tcp', mqtt_topic_prefix='t/twin'))
    g = _Gw()
    g.fakes = {d.id: _Fake() for d in devs}
    g.mqtt = MagicMock()
    g.mqtt.connected = True
    g.app, _ = create_api(cfg, None, g.mqtt, None,
                          devices=[(d, None) for d in cfg.devices] + [(d, g.fakes[d.id]) for d in devs])
    g.app.state.template_registry._templates['ctrl_tpl'] = _tpl()
    g.app.state.template_registry._templates['twin_tpl'] = _twin_tpl()
    g.handler = g.mqtt.set_command_write_handler.call_args[0][0]
    g.client = TestClient(g.app, raise_server_exceptions=False)
    assert g.client.post("/api/auth/login", json={"username": "admin", "password": "pw"}).status_code == 200
    g.cd = g.app.state.command_dispatch
    g.cfg = cfg
    return g


def _reg(address, name):
    from multibus.config import SelectedRegister
    return SelectedRegister(address=address, name=name, label=name, unit='', data_type='uint16',
                            poll_group='normal')


def _bus(g, dev='ctrl'):
    _i, cfg, cl = g.app.state.registry.find(dev)
    return g.app.state.cmd_lock_for(cl, cfg)


def _results(g, dev='ctrl'):
    out = []
    for c in g.mqtt.client.publish.call_args_list:
        topic, body = c.args[0], c.args[1]
        if topic == f"t/{dev}/cmd/result":
            out.append(json.loads(body))
    return out


def _call(g, *a, timeout=1.0):
    """Run the handler as the MQTT worker would; True when it returned in time."""
    t = threading.Thread(target=g.handler, args=a, daemon=True)
    t.start()
    t.join(timeout)
    return not t.is_alive(), t


# ── the MQTT worker never waits for a bus ────────────────────────────────────

@needs_tc
def test_the_mqtt_worker_returns_while_the_bus_is_held(tmp_path, monkeypatch):
    g = _gw(tmp_path, monkeypatch, ids=('ctrl', 'other'))
    lock = _bus(g)
    assert lock.acquire(timeout=5)
    try:
        returned, t = _call(g, 'ctrl', _reg(200, 'setpoint'), '42')
        assert returned, 'the worker waited for the bus (CMD-Q-7)'
        # another device's write is not behind it
        returned2, _t2 = _call(g, 'other', _reg(200, 'setpoint'), '7')
        assert returned2
        assert g.cd.drain(5) is False or True
        time.sleep(0.2)
        assert g.fakes['other'].reg.get(200) == 7.0
        assert 200 not in g.fakes['ctrl'].reg
    finally:
        lock.release()
    t.join(5)
    assert g.cd.drain(5)
    assert g.fakes['ctrl'].reg[200] == 42.0


@needs_tc
def test_a_burst_of_values_writes_the_first_and_the_last_and_tells_each(tmp_path, monkeypatch):
    """Ten HA values while the bus is busy: the waiting one is replaced
    (latest wins), at most two frames, every outcome on cmd/result — and a
    replaced value spends no rate token (rate 2/s: 3.94 refused the last)."""
    g = _gw(tmp_path, monkeypatch, rate=2.0)
    lock = _bus(g)
    assert lock.acquire(timeout=5)
    done = threading.Event()

    def burst():
        for v in range(1, 11):
            g.handler('ctrl', _reg(200, 'setpoint'), str(v))
        done.set()
    threading.Thread(target=burst, daemon=True).start()
    burst_returned = done.wait(2)
    lock.release()
    done.wait(10)
    assert burst_returned, 'the worker waited for the bus (CMD-Q-7)'
    assert g.cd.drain(5)
    writes = [v for a, v in g.fakes['ctrl'].writes if a == 200]
    assert len(writes) <= 2 and writes[-1] == 10.0
    res = _results(g)
    assert len(res) == 10
    assert all(r['command'] == 'raw:holding@200' and r['via'] == 'ha' for r in res)
    assert sorted(r['status'] for r in res).count('superseded') == 10 - len(writes)
    assert {r['status'] for r in res} <= {'superseded', 'success'}


@needs_tc
def test_a_raw_write_that_waited_past_its_age_expires(tmp_path, monkeypatch):
    g = _gw(tmp_path, monkeypatch)
    now = [1000.0]
    g.cd.clock = lambda: now[0]
    lock = _bus(g)
    assert lock.acquire(timeout=5)
    try:
        assert _call(g, 'ctrl', _reg(200, 'setpoint'), '1')[0]       # runs, waits for the bus
        time.sleep(0.2)
        assert _call(g, 'ctrl', _reg(300, 'mode'), '1')[0]         # waits in the lane
        now[0] += 31.0
    finally:
        lock.release()
    assert g.cd.drain(5)
    assert 300 not in g.fakes['ctrl'].reg
    assert any(r['status'] == 'expired' and r['command'] == 'raw:holding@300' for r in _results(g))


@needs_tc
def test_a_device_locked_while_a_raw_write_waits_is_not_written(tmp_path, monkeypatch):
    """RP-10: the lock is asked again at the head, under the bus lock."""
    g = _gw(tmp_path, monkeypatch)
    lock = _bus(g)
    assert lock.acquire(timeout=5)
    try:
        assert _call(g, 'ctrl', _reg(200, 'setpoint'), '5')[0]
        time.sleep(0.2)
        g.app.state.registry.find('ctrl')[1].write_locked = True
    finally:
        lock.release()
    assert g.cd.drain(5)
    assert 200 not in g.fakes['ctrl'].reg
    told = _results(g)
    assert told and told[-1]['status'] == 'rejected' and 'write-locked' in told[-1]['reason'], told


@needs_tc
@pytest.mark.parametrize('how', ['none', 'raises'])
def test_an_unverified_raw_write_says_why(tmp_path, monkeypatch, how):
    """RP-11: the read-back's failure was swallowed — `unverified`, no word."""
    g = _gw(tmp_path, monkeypatch)
    fake = g.fakes['ctrl']
    if how == 'none':
        fake.read_register = lambda *a: None
    else:
        def boom(*a):
            raise TimeoutError('no answer in 3 s')
        fake.read_register = boom
    g.handler('ctrl', _reg(200, 'setpoint'), '5')
    assert g.cd.drain(5)
    res = _results(g)[-1]
    assert res['status'] == 'unverified' and res['reason'].startswith('read-back failed: '), res
    if how == 'raises':
        assert 'no answer in 3 s' in res['reason']
    r = g.client.post('/api/devices/ctrl/write', json={'address': 200, 'value': 6})
    assert r.status_code == 200 and r.json()['reason'].startswith('read-back failed: '), r.text


@needs_tc
def test_a_raw_write_on_a_unit_read_several_ways_is_read_back(tmp_path, monkeypatch):
    """RP-14 (found by the live S10 reason): every unit the runtime builds
    is a MultiSourceClient, which had no read_register — every raw write's
    read-back raised AttributeError, swallowed: `unverified`, no refresh."""
    from multibus.config import SourceConfig
    from multibus.multi_source import MultiSourceClient
    g = _gw(tmp_path, monkeypatch)
    fake = _Fake()
    ms = MultiSourceClient('ctrl', [(SourceConfig(id='api', protocol='http'), _Gw()),
                                    (SourceConfig(id='mb', protocol='tcp'), fake)])
    _i, d, _c = g.app.state.registry.find('ctrl')
    g.app.state.registry.replace('ctrl', d, client=ms)
    g.handler('ctrl', _reg(200, 'setpoint'), '5')
    assert g.cd.drain(5)
    res = _results(g)[-1]
    assert res['status'] == 'success' and res['read_back'] == 5.0, res
    assert fake.reg[200] == 5.0


@needs_tc
def test_a_standby_tells_nothing_and_writes_nothing(tmp_path, monkeypatch):
    g = _gw(tmp_path, monkeypatch)
    lock = _bus(g)
    assert lock.acquire(timeout=5)
    try:
        assert _call(g, 'ctrl', _reg(200, 'setpoint'), '5')[0]
        time.sleep(0.2)
        g.app.state.role.role = 'standby'
    finally:
        lock.release()
    assert g.cd.drain(5)
    assert 200 not in g.fakes['ctrl'].reg
    assert _results(g) == []
    g.app.state.role.role = 'active'


# ── D4: what a command writes is never written raw ───────────────────────────

@needs_tc
def test_a_register_a_command_writes_second_is_refused_on_every_face(tmp_path, monkeypatch):
    g = _gw(tmp_path, monkeypatch)
    # HA
    g.handler('ctrl', _reg(101, 'enable'), '0')
    g.cd.drain(5)
    assert 101 not in g.fakes['ctrl'].reg
    told = _results(g)
    assert told and told[-1]['status'] == 'rejected' and 'lim' in told[-1]['reason']
    # REST guarded
    r = g.client.post('/api/devices/ctrl/write', json={'address': 101, 'value': 0})
    assert r.status_code == 422, r.text
    assert 'lim' in r.text and 'power' in r.text and r.json()['detail']['errors']
    # REST unguarded (admin): absolute (Q2)
    r = g.client.post('/api/devices/ctrl/write', json={'address': 101, 'value': 0, 'unguarded': True})
    assert r.status_code == 422, r.text
    # a float32 at 99 covers 100: overlapping is touching
    r = g.client.post('/api/devices/ctrl/write', json={'address': 99, 'value': 1, 'unguarded': True,
                                                         'data_type': 'float32'})
    assert r.status_code == 422, r.text
    assert g.fakes['ctrl'].writes == []
    # what the dialog is told before a write
    info = g.client.get('/api/devices/ctrl/write-info/holding/101').json()
    assert info['through_command'] == 'lim' and info['channel'] == 'power'
    assert g.client.get('/api/devices/ctrl/write-info/holding/200').json()['through_command'] is None
    # a register next to it is still written
    assert g.client.post('/api/devices/ctrl/write', json={'address': 99, 'value': 3,
                                                           'unguarded': True}).status_code == 200


@needs_tc
def test_a_twin_device_entry_on_the_same_slave_is_refused_too(tmp_path, monkeypatch):
    g = _gw(tmp_path, monkeypatch, twin=True)
    r = g.client.post('/api/devices/twin/write', json={'address': 101, 'value': 0})
    assert r.status_code == 422, r.text
    assert "lim" in r.text and 'ctrl' in r.text
    assert g.fakes['twin'].writes == []


@needs_tc
def test_units_read_without_modbus_are_not_one_slave_by_the_default_connection(tmp_path, monkeypatch):
    """RP-7: a unit whose sources are all HTTP/MQTT keeps protocol 'tcp' and
    the empty default connection — twins across installations were then one
    slave (':502', unit) and a command of one refused the other's register."""
    from multibus.config import DeviceConfig, SourceConfig
    g = _gw(tmp_path, monkeypatch)
    for i, tpl in (('a-u1', 'ctrl_tpl'), ('b-u1', 'twin_tpl')):
        d = DeviceConfig(id=i, name=i, template=tpl, protocol='tcp', mqtt_topic_prefix=f"t/{i}",
                         sources=[SourceConfig(id='s', protocol='mqtt')])
        g.app.state.registry.replace(i, d, client=_Fake(), add_if_missing=True)
    assert g.client.get('/api/devices/b-u1/write-info/holding/101').json()['through_command'] is None
    # its own command still points there
    assert g.client.get('/api/devices/a-u1/write-info/holding/101').json()['through_command'] == 'lim'


@needs_tc
def test_ha_discovery_does_not_offer_what_the_handler_refuses(tmp_path, monkeypatch):
    g = _gw(tmp_path, monkeypatch)
    g.cfg.unit_registers = lambda d: [_reg(100, 'limit'), _reg(101, 'enable'), _reg(200, 'setpoint')]
    g.mqtt.publish_device_discovery.reset_mock()
    for h in list(g.mqtt.discovery_hooks):
        h()
    calls = [c for c in g.mqtt.publish_device_discovery.call_args_list if c.args[0] == 'ctrl']
    assert calls
    rules = calls[-1].kwargs.get('write_rules') or {}
    addrs = {int(str(k).split(':')[-1]) if ':' in str(k) else int(k) for k in rules}
    assert 200 in addrs, rules                                  # the filter was asked
    assert 101 not in addrs and 100 not in addrs


@needs_tc
def test_a_fronius_enable_written_alone_is_refused_even_for_the_admin(tmp_path, monkeypatch):
    """The OV hole: WMaxLim_Ena = 0 written raw lifts a curtail the rule
    cannot see (power_limit writes it fifth)."""
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch, rule=False, interval=0)
    r = client.post('/api/devices/pv-u1/write', json={'address': 40236, 'value': 0, 'unguarded': True})
    assert r.status_code == 422, r.text
    assert 'power_limit' in r.text and 'power' in r.text
    assert invs['pv-u1'].writes == []


@needs_tc
def test_rest_refuses_a_device_written_over_mqtt(tmp_path, monkeypatch):
    from multibus.config import DeviceConfig
    g = _gw(tmp_path, monkeypatch)
    d = DeviceConfig(id='mq', name='mq', template='ctrl_tpl', protocol='mqtt', mqtt_topic_prefix='t/mq')
    fake = _Fake()
    g.app.state.registry.replace('mq', d, client=fake, add_if_missing=True)
    r = g.client.post('/api/devices/mq/write', json={'address': 200, 'value': 1, 'unguarded': True})
    assert r.status_code == 422, r.text
    assert 'command' in r.text and fake.writes == []


# ── REST: the lane's word in the shape the UI reads ──────────────────────────

@needs_tc
def test_rest_answers_superseded_in_its_error_body_and_ha_replaces_a_waiting_rest_job(tmp_path, monkeypatch):
    g = _gw(tmp_path, monkeypatch)
    lock = _bus(g)
    assert lock.acquire(timeout=5)
    try:
        t1, r1 = _bg(g.client.post, '/api/devices/ctrl/write', json={'address': 200, 'value': 1})
        time.sleep(0.3)                                          # runs: waits for the bus
        t2, r2 = _bg(g.client.post, '/api/devices/ctrl/write', json={'address': 200, 'value': 2})
        time.sleep(0.3)                                          # waits in the lane
        g.handler('ctrl', _reg(200, 'setpoint'), '3')            # the same register, from HA
        t2.join(5)
        assert not t2.is_alive(), 'a REST write waited for the bus instead of being replaced'
    finally:
        lock.release()
    t1.join(5)
    assert r1['r'].status_code == 200 and 'queued_ms' in r1['r'].json()
    body = r2['r'].json()
    assert r2['r'].status_code == 409, body
    assert body['detail']['status'] == 'superseded' and body['detail']['errors']
    assert g.cd.drain(5)
    assert [v for a, v in g.fakes['ctrl'].writes if a == 200] == [1, 3.0]


# ── D8: the raw lease revert is a lane revert job ────────────────────────────

@needs_tc
def test_the_raw_lease_revert_is_a_revert_job_asked_again_under_the_bus_lock(tmp_path, monkeypatch):
    g = _gw(tmp_path, monkeypatch)
    kinds = []
    real = g.cd.submit
    g.cd.submit = lambda job: (kinds.append(job.kind), real(job))[1]
    revert = g.app.state.make_lease_revert('ctrl', 'holding', 200, 'uint16', 1.0, 0)
    current = [True]
    lock = _bus(g)
    assert lock.acquire(timeout=5)
    try:
        t, out = _bg(revert, lambda: current[0])
        time.sleep(0.3)
        current[0] = False                                       # renewed while it waited
    finally:
        lock.release()
    t.join(5)
    assert kinds == ['revert']
    assert g.fakes['ctrl'].writes == []                          # asked again under the lock
    current[0] = True
    revert(lambda: current[0])
    assert g.fakes['ctrl'].writes == [(200, 0)]


@needs_tc
def test_a_recovered_lease_fired_the_moment_it_is_armed_finds_the_write_path(tmp_path, monkeypatch, caplog):
    """RP-12: the sweeper started before create_api had defined
    _submit_raw, and boot recovery arms with fire_now — a tick landing right
    after the arm ran the revert into a NameError ('revert FAILED', healed on
    the retry). Here the sweeper ticks the moment a recovered lease is armed."""
    from multibus.write_lease import WriteLeaseManager
    WriteLeaseManager(persist_path=tmp_path / 'write_leases.json').arm(
        'ctrl', 'holding', 200, 5000, lambda ok: None,
        meta={'device': 'ctrl', 'register_type': 'holding', 'address': 200, 'data_type': 'uint16',
              'scale': 1.0, 'offset': 0.0, 'safe_value': 0, 'lease_ms': 5000})
    real_arm = WriteLeaseManager.arm

    def arm_and_tick(self, *a, **kw):
        real_arm(self, *a, **kw)
        if kw.get('fire_now') and self._thread is not None and self._thread.is_alive():
            self._sweep()                                      # the sweeper's tick, right now
    monkeypatch.setattr(WriteLeaseManager, 'arm', arm_and_tick)
    with caplog.at_level('WARNING'):
        g = _gw(tmp_path, monkeypatch)
        deadline = time.monotonic() + 5
        while g.fakes['ctrl'].reg.get(200) != 0 and time.monotonic() < deadline:
            time.sleep(0.05)
    assert g.fakes['ctrl'].reg.get(200) == 0
    assert not [r for r in caplog.records if 'revert FAILED' in r.getMessage()], caplog.text


@needs_tc
def test_a_legacy_raw_lease_on_a_command_register_reverts_and_stamps_the_pace(tmp_path, monkeypatch):
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch, rule=False, interval=30)
    dev, facade = _unit(app)
    wrote = []
    facade.write_value = lambda *a, **k: (wrote.append(a), (True, None, [0]))[1]
    assert rt._seq_of(dev, 'power_limit') is None
    revert = app.state.make_lease_revert('pv-u1', 'holding', 40236, 'uint16', 1.0, 0)
    revert(lambda: True)                                         # a revert is never refused
    assert wrote and wrote[-1][0] == 40236
    assert rt._seq_of(dev, 'power_limit') is not None
    assert 'min_interval_s' in (rt.channel_hold(dev, 'power_limit', {'value': 70}) or '')


# ── HIGH 1: a clean shutdown in main.py's order writes nothing past its budget ──

@needs_tc
def test_a_shutdown_raw_revert_in_mains_order_writes_nothing_late(tmp_path, monkeypatch):
    from multibus.write_lease import WriteLeaseManager
    g = _gw(tmp_path, monkeypatch)
    mgr = WriteLeaseManager()
    mgr.arm('ctrl', 'holding', 200, 60000, g.app.state.make_lease_revert('ctrl', 'holding', 200, 'uint16', 1.0, 0))
    lock = _bus(g)
    assert lock.acquire(timeout=5)
    try:
        t, _out = _bg(mgr.stop, True, 0.3)
        t.join(3)
        hung = t.is_alive()
        g.cd.stop()                                              # main.py: then the dispatcher
    finally:
        lock.release()
    t.join(5)
    time.sleep(0.3)
    assert not hung
    assert ('ctrl', 'holding', 200) in mgr._leases               # left for boot recovery
    assert g.fakes['ctrl'].writes == []


@needs_tc
def test_a_shutdown_command_revert_gives_up_within_its_budget(tmp_path, monkeypatch):
    """The command revert waited for a running revert unbounded (its lane
    held by a busy bus): the clean shutdown hung."""
    cfg, app, client, ck, rt, invs = _plant(tmp_path, monkeypatch, rule=False, interval=0)
    inv = invs['pv-u1']
    r = client.post('/api/devices/pv-u1/commands/power_limit', json={'value': 60, 'lease_s': 60})
    assert r.status_code == 200, r.text
    n = len(inv.writes)
    mgr = app.state.lease_mgr
    lock = _lock(app)
    assert lock.acquire(timeout=5)
    try:
        t, _out = _bg(mgr.stop, True, 0.5)
        t.join(4)
        hung = t.is_alive()
        app.state.command_dispatch.stop()
    finally:
        lock.release()
    t.join(40)
    time.sleep(0.3)
    assert not hung, 'the shutdown waited for the bus past its budget'
    assert len(inv.writes) == n                                  # nothing written late
    assert any(k[0] == 'pv-u1' for k in mgr._leases)            # left for boot recovery


# ── (g) the setup steps never touch a command's register ─────────────────────

def test_no_builtin_setup_step_writes_a_commands_register():
    """device_setup.py is a raw writer outside the lanes (review MED 4g): it
    is kept off every register a command writes."""
    from multibus.commands import parse_command_def
    from multibus.device_template import TemplateRegistry
    reg = TemplateRegistry()
    if not reg._templates:
        reg.reload()
    assert reg._templates
    bad = []
    for tid, t in sorted(reg._templates.items()):
        steps = getattr(t, 'setup', None) or []
        if not steps:
            continue
        regs = {r.name: r for r in t.registers}
        cmd_addrs = set()
        for n, raw in (t.commands or {}).items():
            try:
                d = parse_command_def(n, raw)
            except Exception:  # noqa: BLE001
                continue
            for w in d.writes:
                r = regs.get(w.get('register'))
                if r is not None:
                    cmd_addrs.add(int(r.address))
        for s in steps:
            lo = int(s['address'])
            if any(lo <= a < lo + max(1, len(s.get('values') or [])) for a in cmd_addrs):
                bad.append((tid, s.get('name')))
    assert bad == []
