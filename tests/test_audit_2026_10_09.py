# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The audit of everything added since 3.91 (the heat pump work), 2026-10-09:
one test per failure scenario it found, each reproducing what went wrong."""
import json
import threading
import time

from multibus import expressions as ex
from multibus.rules import RuleState, parse_rule_def, validate_rule_def
from tests.test_devices_api import needs_tc


# ── commands: refresh and leases ─────────────────────────────────────────────

def _timers(monkeypatch):
    import threading as _th
    timers = []

    class _T:
        def __init__(self, every, fn):
            self.every, self.fn, self.cancelled, self.daemon = every, fn, False, True
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True
    monkeypatch.setattr(_th, "Timer", _T)
    return timers


@needs_tc
def test_a_refresh_waiting_for_the_bus_never_puts_back_what_a_newer_command_replaced(tmp_path, monkeypatch):
    """The refresh timer fired and was about to write the 60 % limit again;
    the safe value went out first. The refresh must then write nothing (it
    used to write 60 % back and keep it alive forever)."""
    from tests.test_commands_api import _Inverter, _app
    timers = _timers(monkeypatch)
    inv = _Inverter(sf=-2)
    cfg, app, client = _app(tmp_path, {'pv-u1': inv})
    tpl = app.state.template_registry.get('fronius_sunspec_inverter')
    monkeypatch.setitem(tpl.commands['power_limit'], 'refresh_s', 1800)
    assert client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 60}).status_code == 200
    refresh = [t for t in timers if t.every == 1800][-1]
    # the race: the timer passed its own check, then the safe command runs
    # before it reaches the bus — reproduced by running the safe command
    # from inside the timer's device lookup
    reg = app.state.registry
    orig, done = reg.find, []

    def find(did):
        if not done and threading.current_thread() is threading.main_thread():
            done.append(1)
            client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 100})
        return orig(did)
    monkeypatch.setattr(reg, "find", find)
    refresh.fn()
    assert done and inv.writes[-1][1][0] == 10000          # the safe value is the last word
    assert not [t for t in timers if t.every == 1800 and not t.cancelled and t is not refresh]


@needs_tc
def test_a_refresh_does_not_renew_the_dead_man(tmp_path, monkeypatch):
    from tests.test_commands_api import _Inverter, _app
    timers = _timers(monkeypatch)
    inv = _Inverter(sf=-2)
    cfg, app, client = _app(tmp_path, {'pv-u1': inv})
    tpl = app.state.template_registry.get('fronius_sunspec_inverter')
    monkeypatch.setitem(tpl.commands['power_limit'], 'refresh_s', 1800)
    assert client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 60, "lease_s": 600}).status_code == 200
    mgr = app.state.lease_manager
    key = ('pv-u1', 'command', 40232)
    gen = mgr._leases[key]['gen']
    [t for t in timers if t.every == 1800][-1].fn()
    assert len(inv.writes) == 2                             # the setting was written again…
    assert mgr._leases[key]['gen'] == gen                   # …and the lease still runs out on time


@needs_tc
def test_a_deleted_device_keeps_no_refresh(tmp_path, monkeypatch):
    from tests.test_commands_api import _Inverter, _app
    timers = _timers(monkeypatch)
    inv = _Inverter(sf=-2)
    cfg, app, client = _app(tmp_path, {'pv-u1': inv})
    tpl = app.state.template_registry.get('fronius_sunspec_inverter')
    monkeypatch.setitem(tpl.commands['power_limit'], 'refresh_s', 1800)
    client.post("/api/devices/pv-u1/commands/power_limit", json={"value": 60})
    t = [t for t in timers if t.every == 1800][-1]
    # the device stops: the refresh ends instead of retrying forever
    dev = next(d for d in cfg.devices if d.id == 'pv-u1')
    monkeypatch.setattr(dev, "enabled", False)
    n = len(inv.writes)
    t.fn()
    assert len(inv.writes) == n and len([x for x in timers if x.every == 1800]) == 1


def test_a_masked_state_is_verified_by_its_code_not_through_the_mask_again():
    from types import SimpleNamespace as NS
    from multibus.commands import _check
    reg = NS(enum={'0': 'Off', '1': 'On'}, mask=0xFF00, shift=8, bits=None)
    assert _check({'read': 'x', 'expect': 1}, 'On', {}, reg) is None
    assert _check({'read': 'x', 'expect': 1}, 'Off', {}, reg) is not None


# ── rules: the latch ────────────────────────────────────────────────────────

def _latched():
    raw = {'id': 'buffer', 'mode': 'armed', 'kind': 'condition', 'target': {'device': 'hp', 'command': 'power'},
           'when': 'hp.t < 25', 'release': 'hp.t > 35',
           'then': {'params': {'value': 1}}, 'else': {'params': {'value': 0}},
           'timing': {'debounce': 1, 'min_interval_s': 0}, 'stale_after_s': 30}
    assert validate_rule_def(raw) == []
    return parse_rule_def(raw)


def test_after_a_restart_in_the_dead_band_the_rule_waits_instead_of_switching_off():
    """Water at 30 °C, heating on, the gateway restarts: the rule does not
    know its latch — it must not send power 0."""
    st = RuleState(_latched())
    for i in range(4):
        d = st.evaluate(float(i * 2), (False, False), 0.5, actual=1.0, actual_age_s=1.0, actual_stale_s=60)
        assert d.want is None and d.action not in ('run', 'shadow'), d
    d = st.evaluate(10.0, (False, True), 0.5, actual=1.0, actual_age_s=1.0, actual_stale_s=60)
    assert d.want == {'value': 0}                         # a threshold decides


def test_overlapping_thresholds_keep_what_we_have_instead_of_flapping():
    st = RuleState(_latched())
    st.evaluate(0.0, (True, False), 0.5)                  # on
    states = [st.evaluate(2.0 * i, (True, True), 0.5).state for i in range(1, 5)]
    assert states == ['true'] * 4


def test_a_rule_that_keeps_failing_shows_it():
    from multibus.rules_runtime import RulesRuntime
    rt = RulesRuntime.__new__(RulesRuntime)
    rt._lock = threading.Lock()
    rt.errors, rt.tick_errors, rt.alert_mgr = {}, {}, None
    rt._clock = rt._mono = lambda: 100.0
    r = _latched()
    rt.rules = {r.id: r}

    def boom(rule, now, mono):
        raise ValueError("Unknown format code 'g' for object of type 'str'")
    rt._tick_rule = boom
    rt.tick()
    assert 'evaluation failed' in rt.tick_errors['buffer']


def test_a_numeric_string_read_back_does_not_crash_the_drift_message():
    st = RuleState(_latched())
    st.evaluate(0.0, (True, False), 0.5, actual='0', actual_age_s=1.0, actual_stale_s=60)
    st.commanded = {'value': 1}
    d = st.evaluate(4.0, (True, False), 0.5, actual='0', actual_age_s=1.0, actual_stale_s=60)
    assert d is not None


# ── reading ─────────────────────────────────────────────────────────────────

def _poller(regs, setup=None, max_read=0):
    from multibus.modbus_client import RegisterPoller
    from multibus.register_parser import RegisterParser
    from types import SimpleNamespace as NS
    conn = NS(config=NS(max_gap=10, illegal_registers=[]))
    p = RegisterPoller.__new__(RegisterPoller)
    p.registers, p.parser, p.connection, p.setup, p.max_read = regs, RegisterParser('big'), conn, setup, max_read
    p._tag = ''
    return p


def _reg(addr, name):
    from multibus.config import SelectedRegister
    return SelectedRegister(address=addr, name=name, label=name, unit='', data_type='uint16', poll_group='normal')


def test_registers_the_device_always_answers_never_share_a_read_with_setup_ones():
    """Writes off (the default): the setup is not written and 4-28 / 82-96
    are refused. The rows around them must still be read."""
    from multibus.device_setup import SetupRunner
    setup = SetupRunner(None, [{'name': 'o', 'address': 6000, 'values': [1], 'provides': {'from': 4, 'to': 28}},
                               {'name': 'i', 'address': 7000, 'values': [1], 'provides': {'from': 82, 'to': 96}}])
    regs = [_reg(a, f"r{a}") for a in (2, 4, 10, 28, 52, 53, 66, 79, 82, 90, 96)]
    groups = _poller(regs, setup)._create_read_groups()
    side = lambda a: 'o' if 4 <= a <= 28 else ('i' if 82 <= a <= 96 else 'base')   # noqa: E731
    for g in groups:                                  # no read mixes the two kinds
        assert len({side(r.address) for r in g['registers']}) == 1, (g['start'], g['end'])
    base = [g for g in groups if side(g['start']) == 'base']
    assert {r.name for g in base for r in g['registers']} == {'r2', 'r52', 'r53', 'r66', 'r79'}


def test_the_templates_read_limit_is_honoured():
    regs = [_reg(a, f"r{a}") for a in range(0, 60)]
    groups = _poller(regs, max_read=32)._create_read_groups()
    assert max(g['count'] for g in groups) <= 32


def test_a_refusal_is_judged_on_this_threads_own_read():
    from multibus.modbus_client import ModbusConnection
    c = ModbusConnection.__new__(ModbusConnection)
    c.__dict__.update(_ev_lock=threading.Lock(), error_counts={})
    c._read_tl().kind = None
    other = threading.Thread(target=lambda: (c._read_tl(), setattr(c._read_tl(), 'kind', 'exception_2')))
    other.start()
    other.join()
    assert c.last_read_error() is None                  # another poller's refusal does not leak in


def test_writes_keep_their_gap_even_when_two_arrive_together(monkeypatch):
    from multibus.modbus_client import ModbusConnection
    c = ModbusConnection.__new__(ModbusConnection)
    c.__dict__.update(min_write_gap_s=0.3, _retired=False)
    stamps = []

    def now(*a, **k):
        stamps.append(time.monotonic())
        time.sleep(0.01)
        return True, None
    monkeypatch.setattr(c, "_write_now", now)
    ts = [threading.Thread(target=lambda: c.write(1, values=[1])) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    stamps.sort()
    assert all(b - a >= 0.28 for a, b in zip(stamps, stamps[1:])), stamps


def test_a_retired_connection_refuses_to_write():
    from multibus.modbus_client import ModbusConnection
    c = ModbusConnection.__new__(ModbusConnection)
    c.__dict__.update(min_write_gap_s=0, _retired=True)
    assert c.write(1, values=[1]) == (False, "connection retired (reconfigured)")


def test_after_a_reconnect_the_setup_writes_through_the_new_connection():
    from multibus.config import ModbusConfig
    from multibus.modbus_client import ModbusClient
    cli = ModbusClient(ModbusConfig(host='127.0.0.1', port=1), [], {}, setup_steps=[
        {'name': 's', 'address': 6000, 'values': [1], 'provides': {'from': 4, 'to': 5}}], min_write_gap_s=2)
    old = cli.connection
    cli.pollers = []
    cli.start_polling = lambda: None
    cli.connection.connect = lambda: False
    from multibus import modbus_client as mc
    orig = mc.ModbusConnection.connect
    mc.ModbusConnection.connect = lambda self: False
    try:
        cli._reconnect_locked()
    finally:
        mc.ModbusConnection.connect = orig
    assert cli.connection is not old and cli.setup.connection is cli.connection
    assert cli.connection.min_write_gap_s == 2 and cli.connection.on_recovered


def test_a_model_that_lacks_the_registers_is_not_rewritten_every_two_minutes(monkeypatch):
    from multibus import device_setup as ds
    t = [0.0]
    monkeypatch.setattr(ds.time, "monotonic", lambda: t[0])
    monkeypatch.setattr(ds.time, "sleep", lambda s: None)
    writes = []
    conn = type("C", (), {"write": lambda self, a, **k: (writes.append(a), (True, None))[1]})()
    r = ds.SetupRunner(conn, [{'name': 's', 'address': 6000, 'values': [1], 'provides': {'from': 4, 'to': 5}}])
    r.ensure()
    gaps = []
    for _ in range(5):
        before = len(writes)
        while len(writes) == before:                    # refused again: when may it be rewritten?
            t[0] += 30
            r.invalidate("refused")
            r.ensure()
        gaps.append(t[0])
    steps = [b - a for a, b in zip(gaps, gaps[1:])]
    assert steps == sorted(steps) and steps[-1] > steps[0]   # backs off


def test_with_writes_off_the_setup_says_so_once():
    from multibus import device_setup as ds
    r = ds.SetupRunner(None, [{'name': 's', 'address': 6000, 'values': [1]}], 'hp', gate=lambda: "writes are disabled")
    r._next_try = 0
    r.ensure()
    assert r._gate_said == "writes are disabled"


# ── expressions ─────────────────────────────────────────────────────────────

def test_a_subtraction_stays_a_subtraction_unless_it_names_a_device():
    ex.set_device_ids_provider(lambda: ['meter2', 'samsung-dvm', '3150-iem', 'heat__pump'])
    try:
        ok, _e, refs = ex.validate_expression("grid_power-meter2.power")
        assert ok and refs == ['grid_power', 'meter2.power']              # a minus, as it always was
        vals = {'samsung-dvm.heat': 9.0, '3150-iem.power': 3000.0, 'heat__pump.x': 5.0, 'heat-pump.x': 99.0}
        res = lambda n: vals.get(ex.unprepare_ref(n))                     # noqa: E731
        assert ex.evaluate("samsung-dvm.heat / (3150-iem.power / 1000)", res) == 3.0
        assert ex.evaluate("heat__pump.x", res) == 5.0                     # its own value, not heat-pump's
    finally:
        ex.set_device_ids_provider(None)


def test_first_prefers_a_live_estimate_to_a_frozen_meter():
    tree = ex.compile_expression("first(power_electric, power_electric_estimated)")
    vals = {'power_electric': 4000.0, 'power_electric_estimated': 3291.0}
    stale = lambda n: n == 'power_electric'                               # noqa: E731
    assert ex.evaluate_tree(tree, vals.get, stale=stale) == 3291.0
    assert ex.evaluate_tree(tree, vals.get, stale=lambda n: True) == 4000.0   # none fresh: the first that has one
    assert ex.evaluate("first(a / b, 5)", {'a': 1, 'b': 0}.get) == 5          # a math error passes to the next


def test_linked_fields_run_in_a_group_the_device_actually_polls():
    from types import SimpleNamespace as NS
    from multibus.linked_meter import linked_calcs
    meter = NS(id='m', name='M')
    dev = NS(id='hp', energy_meter={'device': 'm', 'power': 'p'})
    regs = {'m': [NS(name='p', unit='W', poll_group='realtime')], 'hp': [NS(name='x', poll_group='slow'),
                                                                          NS(name='y', poll_group='slow')]}
    cfg = NS(devices=[meter, dev], unit_registers=lambda d: regs[d.id])
    assert linked_calcs(cfg, dev)[0]['poll_group'] == 'slow'


# ── templates ───────────────────────────────────────────────────────────────

def _tpl(**blocks):
    return {"device_template": {"schema_version": 1, "id": "aud_t", "name": "Aud",
                                "protocol": {"transports": ["tcp"]}, "poll_groups": {"normal": {"interval": 5}},
                                "registers": [{"address": 0, "name": "t", "data_type": "int16"},
                                              {"address": 1, "name": "onoff", "data_type": "uint16",
                                               "writable": True, "write_allowed": [0, 1]}],
                                "commands": {"power": {"params": {"value": {"allowed": [0, 1]}},
                                                       "writes": [{"register": "onoff", "value": "${value}"}]}},
                                **blocks}}


def test_a_malformed_rule_example_is_an_answer_not_a_crash():
    from multibus.device_template import validate_template
    for bad in ({"id": "x1", "target": "power"}, {"id": ["a"], "target": {"device": "{device}", "command": "power"}},
                {"id": "x2", "target": {"device": "{device}", "command": {"a": 1}}}):
        errs = validate_template(_tpl(rules=[bad]))
        assert errs and all(isinstance(e, str) for e in errs), bad


def test_a_rule_example_with_a_broken_expression_is_refused():
    from multibus.device_template import validate_template
    p = {"id": "b", "kind": "condition", "target": {"device": "{device}", "command": "power"},
         "when": "{device}.t >>> ((", "then": {"params": {"value": 1}}}
    assert any("when" in e for e in validate_template(_tpl(rules=[p])))


def test_a_rule_example_gets_a_valid_id_whatever_the_device_id():
    from multibus.device_template import rule_preset_for
    from multibus.rules import validate_rule_def as v
    p = {"id": "buffer", "kind": "condition", "target": {"device": "{device}", "command": "power"},
         "when": "{device}.t < 25", "then": {"params": {"value": 1}}}
    for did in ("3em-pump", "a" * 60):
        r = rule_preset_for(p, did)
        assert not [e for e in v(r) if e.startswith('id')], (did, r['id'])


def test_a_calculated_fields_section_must_exist_and_setup_words_must_be_whole():
    from multibus.device_template import validate_template
    errs = validate_template(_tpl(categories={"water": {"label": "Water"}},
                                  calculated=[{"name": "c", "expr": "t", "category": "bogus"}]))
    assert any("category 'bogus'" in e for e in errs)
    errs = validate_template(_tpl(setup=[{"name": "s", "address": 6000, "values": [1.7]}]))
    assert any("values[0]" in e for e in errs)


def test_update_from_template_drops_decoding_the_template_dropped():
    from types import SimpleNamespace as NS
    from multibus.template_refresh import REGISTER_KEYS, _refresh_rows
    t = NS(name="x", label="X", unit="", description="", category="", scale=1, offset=0, aggregates=None,
           enum=None, bits=None, mask=None, shift=None, nan=None)
    row = [{"name": "x", "enum": {"0": "Off"}, "mask": 255}]
    assert _refresh_rows(row, {"x": t}, REGISTER_KEYS) == 1
    assert "enum" not in row[0] and "mask" not in row[0]


# ── outputs and persistence ─────────────────────────────────────────────────

def test_a_reseed_over_another_devices_file_keeps_nothing_of_it(tmp_path):
    from tests.test_devices import write_config
    cfg = write_config(tmp_path)
    p = cfg.device_registers_path('dev-x')
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"version": "1.0", "registers": [{"name": "old"}],
                             "calculated": [{"name": "cop", "expr": "old*2"}],
                             "energy_fields": [{"name": "old_e"}], "poll_groups": {"normal": {"interval": 600}}}))
    cfg.save_device_registers('dev-x', [{"address": 0, "name": "new", "data_type": "uint16"}], replace=True)
    d = json.loads(p.read_text())
    assert "calculated" not in d and "energy_fields" not in d and d["poll_groups"]["normal"]["interval"] != 600


def test_a_stale_alarm_field_is_unknown_not_cleared_and_a_flap_is_one_alarm():
    from multibus.alarm_alerts import AlarmTracker
    fired, clock = [], [0.0]
    t = AlarmTracker(lambda *a, **k: fired.append(a[0]), clock=lambda: clock[0])
    decl = [{"field": "err", "severity": "danger"}]
    act = [{"field": "err", "label": "ERR", "severity": "danger", "value": 101}]
    t.update("hp", "HP", decl, act, known={"err"})
    clock[0] = 300.0
    t.update("hp", "HP", decl, [], known=set())          # gone stale: say nothing
    for i in range(10):                                  # flapping 0 ↔ 101 every 5 s
        clock[0] = 305.0 + 5 * i
        t.update("hp", "HP", decl, [] if i % 2 else act, known={"err"})
    assert fired == ['error']


def test_an_alarm_the_template_no_longer_declares_is_forgotten():
    from multibus.alarm_alerts import AlarmTracker
    t = AlarmTracker(lambda *a, **k: None)
    t.update("hp", "HP", [{"field": "err"}], [{"field": "err", "label": "E", "severity": "danger", "value": 1}])
    t.retain({"hp"}, seen=set(), evaluated={"hp"})
    assert t.state == {}


def test_history_reads_one_series_across_measurements():
    import inspect
    from multibus import influxdb_publisher as ip
    src = inspect.getsource(ip.InfluxDBPublisher.query_history)
    assert '|> group()' in src and 'sort(columns: ["_time"])' in src


def test_section_names_fold_accents():
    from multibus.influxdb_publisher import _measurement_slug
    assert _measurement_slug("Températures") == "temperatures"


@needs_tc
def test_a_row_ticked_from_the_catalog_keeps_its_table_and_states(tmp_path):
    """The catalog tick sends the editor's defaults (holding, no states):
    a new row takes its table and decoding from the template."""
    from tests.test_measurements_save import _app, _device
    cfg, pub, client = _app(tmp_path)
    f = _device(cfg, client, tmp_path, template="samsung_mim_b19n")
    shown = client.get("/api/registers/selected?device=hp").json()["registers"]
    new = dict(shown[0], address=52, name="iu0_power", label="On/off", unit="", enum=None, register_type="holding")
    assert client.post("/api/registers/selected?device=hp", json=shown + [new]).status_code == 200
    row = next(r for r in json.loads(f.read_text())["registers"] if r["name"] == "iu0_power")
    assert row.get("enum") and row["enum"].get("1") == "On"


@needs_tc
def test_a_device_measured_only_in_kvah_keeps_its_energy_tab(tmp_path):
    from tests.test_devices_api import make_app
    _cfg, client = make_app(tmp_path)
    client.post("/api/devices", json={"id": "kv-meter", "enabled": False, "connection": {"protocol": "tcp", "host": "192.168.1.9"}})
    f = tmp_path / "devices" / "kv-meter" / "selected_registers.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"version": "1.0", "poll_groups": {}, "registers": [
        {"address": 0, "name": "s_total", "label": "S", "unit": "kVAh", "data_type": "float", "poll_group": "normal",
         "mqtt": {"enabled": True, "topic": ""}, "influxdb": {"enabled": True, "measurement": "", "tags": {}}, "ui": {}}]}))
    dev = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "kv-meter")
    assert dev["energy_fields"] == 1

