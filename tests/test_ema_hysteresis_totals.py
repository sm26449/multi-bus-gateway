"""ema() and hysteresis() remember between evaluations; an installation's
totals read in a formula like a device's values (docs/templates-advanced.md)."""
import math

import pytest

from multibus import expressions as ex


def _ev(expr, values, memo, monkey=None):
    ok, err, _r = ex.validate_expression(expr)
    assert ok, err
    return ex.evaluate(expr, values.get, memo=memo)


def test_ema_starts_at_the_value_then_follows_it_by_its_time_constant(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(ex.time, 'monotonic', lambda: t[0])
    memo = {}
    assert _ev('ema(p, 60)', {'p': 100}, memo) == 100
    t[0] += 60                                       # one time constant later, a step to 200
    assert _ev('ema(p, 60)', {'p': 200}, memo) == pytest.approx(100 + 100 * (1 - math.exp(-1)))
    t[0] += 600                                      # ten more: it has arrived
    assert _ev('ema(p, 60)', {'p': 200}, memo) == pytest.approx(200, abs=0.01)


def test_ema_without_a_memory_is_the_value_itself():
    assert ex.evaluate('ema(p, 60)', {'p': 42}.get) == 42      # a preview


def test_hysteresis_holds_between_its_thresholds():
    memo = {}
    seq = [(10, 0), (25, 0), (31, 1), (25, 1), (21, 1), (20, 0), (25, 0), (30, 1)]
    for x, want in seq:
        assert _ev('hysteresis(t, 20, 30)', {'t': x}, memo) == want, x


def test_two_calls_in_one_formula_remember_apart():
    memo = {}
    e = 'hysteresis(a, 0, 10) + 2 * hysteresis(b, 0, 10)'
    assert _ev(e, {'a': 11, 'b': 5}, memo) == 1
    assert _ev(e, {'a': 5, 'b': 11}, memo) == 3


def test_bad_arguments_are_refused_on_save():
    assert 'time constant' in ex.validate_expression('ema(p, 0)')[1]
    assert ex.validate_expression('ema(p)')[0] is False
    assert 'low must be under high' in ex.validate_expression('hysteresis(t, 30, 20)')[1]
    assert ex.validate_expression('hysteresis(t, 20)')[0] is False


def test_an_installations_totals_read_like_a_devices(tmp_path):
    from multibus import endpoint_aggregator as ea
    from multibus.calc_engine import CalcEngine
    import time as _t
    ea._latest_totals['battery-bank'] = ({'pack_average_soc': 84.2, 'status': 'online'},
                                         _t.time(), _t.monotonic(), 10.0)
    ex.set_device_ids_provider(lambda: ['umg512', 'battery-bank'])
    try:
        eng = CalcEngine.__new__(CalcEngine)
        eng._store_for = lambda dev: None                 # no device by that id
        resolve = eng.resolver({})
        assert ex.evaluate('battery-bank.pack_average_soc - 4.2', resolve) == pytest.approx(80.0)
        with pytest.raises(ex.MissingValue):
            ex.evaluate('battery-bank.status', resolve)      # text: no number
        assert ex.validate_expression('battery-bank.pack_average_soc')[2] == ['battery-bank.pack_average_soc']
    finally:
        ea._latest_totals.pop('battery-bank', None)
        ex.set_device_ids_provider(None)
