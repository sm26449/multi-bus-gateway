# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The trim rule: nudge a value up while the signal is over `above`, down
while it is under `below`, within min..max — a drive that soaks up exactly
the solar surplus. Samples in, decisions out."""
from multibus.rules import RuleState, parse_rule_def, validate_rule_def

SURPLUS = {
    'id': 'vfd-trim', 'mode': 'armed',
    'target': {'device': 'vfd1', 'command': 'run_at'},
    'kind': 'trim',
    'signal': '-site.power_active_total',
    'above': 500, 'below': 100, 'step': 2, 'min': 15, 'max': 50, 'start': 15, 'off_value': 0,
    'stale_after_s': 60,
    'timing': {'every_s': 5, 'debounce': 2, 'min_interval_s': 10, 'reassert_s': 0},
    'on_stale': 'safe', 'on_disable': 'safe',
}


def _rule(**over):
    raw = dict(SURPLUS, **over)
    for k in [k for k, v in over.items() if v is None]:
        raw.pop(k)
    assert validate_rule_def(raw) == [], validate_rule_def(raw)
    r = parse_rule_def(raw)
    r.safe_params = {'value': 0}
    r.tolerance = 0.05
    return r


class _Clock:
    """Feeds one sample every 5 s and keeps the device's read-back in step
    with what was last sent (an armed rule whose commands succeed)."""

    def __init__(self, st, actual=None):
        self.st, self.t, self.actual = st, 0.0, actual

    def feed(self, *samples):
        out = []
        for v in samples:
            d = self.st.evaluate(self.t, v, 0.5, actual=self.actual, actual_age_s=1.0, actual_stale_s=60)
            if d.action in ('run', 'reassert'):
                self.actual = d.want['value']
            out.append(d)
            self.t += 5.0
        return out


def test_a_surplus_starts_the_drive_then_raises_it_step_by_step():
    c = _Clock(RuleState(_rule()), actual=0)
    d = c.feed(800, 800)                                          # debounce 2, then from off to start
    assert d[0].action == 'hold' and d[1].action == 'run' and d[1].want == {'value': 15} and d[1].state == 'raising'
    d = c.feed(800, 800, 800, 800)
    assert [x.want['value'] for x in d if x.action == 'run'] == [17, 19]   # one step per 10 s window
    assert c.actual == 19


def test_between_the_thresholds_it_holds():
    c = _Clock(RuleState(_rule()), actual=30)
    d = c.feed(300, 300, 300)
    assert all(x.action in ('idle', 'sweep') for x in d) and c.actual == 30 and d[-1].state == 'holding'


def test_an_import_lowers_it_to_min_then_off():
    c = _Clock(RuleState(_rule()), actual=17)                     # seeded from the read-back, no jump
    d = c.feed(*[-200] * 12)
    sent = [x.want['value'] for x in d if x.action == 'run']
    assert sent == [15, 0]                                        # 17 → 15 (min) → off
    assert d[-1].state == 'off'


def test_it_never_goes_over_max_and_says_so():
    c = _Clock(RuleState(_rule()), actual=49)
    d = c.feed(*[3000] * 10)
    assert [x.want['value'] for x in d if x.action == 'run'] == [50]
    assert d[-1].state == 'at max' and c.actual == 50


def test_gain_makes_the_step_proportional_and_step_caps_it():
    c = _Clock(RuleState(_rule(gain=0.004)), actual=20)
    d = c.feed(750, 750)                                          # 250 W past 500 × 0.004 = +1 Hz
    assert d[-1].want == {'value': 21}
    c2 = _Clock(RuleState(_rule(gain=0.004)), actual=20)
    d = c2.feed(5000, 5000)                                       # 4500 × 0.004 = 18 → capped at step 2
    assert d[-1].want == {'value': 22}


def test_a_one_sample_blip_does_not_move_it():
    c = _Clock(RuleState(_rule()), actual=30)
    d = c.feed(900, 300, 900, 300)
    assert not any(x.action == 'run' for x in d) and c.actual == 30


def test_a_stale_signal_asks_for_safe_and_restarts_from_the_read_back():
    st = RuleState(_rule())
    c = _Clock(st, actual=30)
    c.feed(800, 800)                                              # → 32
    d = st.evaluate(c.t + 120, None, None, actual=32, actual_age_s=1.0, actual_stale_s=60)
    assert d.state == 'stale' and d.want == {'value': 0}         # on_stale: safe = stop
    assert st._trim is not None
    c.t += 240
    c.actual = 0
    d = c.feed(300)                                               # back, between the thresholds
    assert st._trim == 0 and d[0].state == 'off'                 # re-seeded from the device (stopped)


def test_shadow_decides_and_writes_nothing():
    c = _Clock(RuleState(_rule(mode='shadow')), actual=0)
    d = c.feed(800, 800, 800, 800)
    shadow = [x for x in d if x.action not in ('hold', 'idle', 'sweep')]
    assert [x.action for x in shadow] == ['shadow'] and shadow[0].want == {'value': 15}
    assert d[-1].want == {'value': 15}                            # the device still holds 0: still one step from it
    assert c.actual == 0                                          # nothing reached the device


def test_without_off_it_stops_at_min():
    c = _Clock(RuleState(_rule(off_value=None)), actual=16)
    d = c.feed(*[-500] * 8)
    assert [x.want['value'] for x in d if x.action == 'run'] == [15] and d[-1].state == 'at min'


def test_the_operator_ceiling_caps_the_trim_too():
    st = RuleState(_rule())
    st.set_clamp(25, None)
    c = _Clock(st, actual=24)
    d = c.feed(*[3000] * 8)
    assert [x.want['value'] for x in d if x.action == 'run'] == [25]


def test_validation_says_what_is_wrong():
    def errs(**over):
        return validate_rule_def(dict(SURPLUS, **over))
    assert any('below: must be under above' in e for e in errs(below=600))
    assert any('step:' in e for e in errs(step=0))
    assert any('min: must be under max' in e for e in errs(min=60))
    assert any('off_value: under min' in e for e in errs(off_value=20))
    assert any('start: within' in e for e in errs(start=80))
    raw = dict(SURPLUS)
    raw.pop('step')
    assert any(e.startswith('step:') for e in validate_rule_def(raw))
    assert parse_rule_def(SURPLUS).to_dict()['off_value'] == 0                 # it survives a save


def test_in_shadow_each_would_be_step_starts_from_what_the_device_holds():
    """2026-10-20 on the plant: in shadow nothing lowers the export, so a trim
    that stepped from its own last would-be value walked down to min unseen —
    and arming sent that. A would-be step starts from the read-back."""
    c = _Clock(RuleState(_rule(mode='shadow')), actual=30)
    d = c.feed(*[-200] * 12)
    assert [x.want['value'] for x in d if x.action == 'shadow'] == [28]
    assert d[-1].want == {'value': 28} and d[-1].state == 'lowering'   # never 26, 24, … down to min
    assert c.actual == 30


def test_the_signal_unit_is_checked_and_survives_a_save():
    raw = dict(SURPLUS, signal_unit='W exported')
    assert validate_rule_def(raw) == []
    assert parse_rule_def(raw).to_dict()['signal_unit'] == 'W exported'
    assert any(e.startswith('signal_unit:') for e in validate_rule_def(dict(SURPLUS, signal_unit='x' * 41)))
    assert any(e.startswith('signal_unit:') for e in validate_rule_def(dict(SURPLUS, signal_unit=5)))
    assert 'signal_unit' not in parse_rule_def(SURPLUS).to_dict()


def test_after_a_restart_it_waits_for_the_read_back_instead_of_starting_at_min():
    """A gateway restarted under an armed trim reads the device a little
    later than the first tick: starting from min then would send the floor
    to a device at full value. With a read-back bound, it waits for it."""
    r = _rule(off_value=None, start=None)
    r.target['_readback'] = 'run_freq'
    st = RuleState(r)
    d = [st.evaluate(t, -200, 0.5, actual=None) for t in (0.0, 5.0, 10.0, 15.0)]
    assert not [x for x in d if x.action in ('run', 'reassert')]
    assert d[-1].action == 'hold' and 'read-back' in d[-1].reason and st._trim is None
    c = _Clock(st, actual=30)
    c.t = 20.0
    d = c.feed(-200, -200, -200)
    assert [x.want['value'] for x in d if x.action == 'run'] == [28]   # from what the device holds


def test_without_a_read_back_bound_it_starts_as_before():
    st = RuleState(_rule(off_value=None, start=None))
    d = [st.evaluate(t, -200, 0.5, actual=None) for t in (0.0, 5.0, 10.0)]
    assert st._trim is not None and any(x.action == 'run' for x in d)
