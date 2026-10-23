"""The sun in formulas and rules, and attr() (docs/attributes.md, phase 2).

The sun's position is checked against values the NOAA calculator and pvlib
give; during development it agreed with pvlib within 0.014° of elevation
above the horizon and 0.05° of azimuth, over random times and places."""
from datetime import datetime, timezone

import pytest

from multibus import clock, expressions as ex, sun

PARIS = (48.8566, 2.3522)          # an example place, never a real installation's


def _t(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_the_sun_where_the_almanac_puts_it():
    e, a = sun.position(*PARIS, _t(2026, 6, 21, 11, 51))           # near solar noon, solstice
    assert 64.3 < e < 64.9 and 175 < a < 185
    e, _a = sun.position(*PARIS, _t(2026, 6, 21, 0, 0))
    assert e < -15                                                  # night
    e, a = sun.position(*PARIS, _t(2026, 3, 20, 6, 0))              # equinox morning: east-ish
    assert 80 < a < 100
    e, _a = sun.position(0.0, 0.0, _t(2026, 3, 20, 12, 7))          # equator, equinox noon
    assert e > 88.5


def test_polar_day_and_night_give_an_elevation():
    e_winter, _ = sun.position(69.65, 18.96, _t(2026, 12, 21, 11, 0))
    e_summer, _ = sun.position(69.65, 18.96, _t(2026, 6, 21, 23, 0))
    assert e_winter < 0 < e_summer
    e, a = sun.position(90.0, 0.0, _t(2026, 6, 21, 12, 0))          # the pole: no error
    assert 0 < e < 30 and 0 <= a < 360


@pytest.fixture
def places():
    ex.set_place_provider(lambda p: {'site': PARIS, 'pv': PARIS}.get(p),
                          lambda: {'site', 'pv', 'umg512'})
    ex.set_attr_provider(lambda t, c, f: {('umg512', 'grid_connection', 'max_export_kw'): 45,
                                          ('umg512', 'grid_connection', 'role'): 'connection_point'}.get((t, c, f)))
    yield
    ex.set_place_provider(None, None)
    ex.set_attr_provider(None)
    clock.set_now(None)


def _ev(expr, values=None):
    ok, err, _r = ex.validate_expression(expr)
    assert ok, err
    return ex.evaluate(expr, (values or {}).get)


def test_sun_functions_in_formulas(places):
    clock.set_now(lambda: _t(2026, 6, 21, 11, 51).timestamp())
    assert 64.3 < _ev('sun_elevation()') < 64.9
    assert _ev('sun_elevation("pv")') == _ev('sun_elevation()')
    assert 175 < _ev('sun_azimuth("pv")') < 185
    assert _ev('is_day()') is True and _ev('is_day(70)') is False and _ev('is_day(5, "pv")') is True
    clock.set_now(lambda: _t(2026, 6, 21, 0, 0).timestamp())
    assert _ev('is_day()') is False
    assert _ev('1 if is_day(5, "pv") else 0') == 0


def test_a_place_without_a_location_has_no_value(places):
    ex.set_place_provider(lambda p: None, lambda: {'site', 'pv'})
    with pytest.raises(ex.MissingValue):
        _ev('sun_elevation("pv")')


def test_places_and_attributes_are_checked_on_save(places):
    assert 'no installation or device' in ex.validate_expression('sun_elevation("nope")')[1]
    assert ex.validate_expression('sun_elevation(pv)')[0] is False                 # not in quotes
    assert ex.validate_expression('is_day("pv", 5)')[0] is False                   # degrees first
    assert 'not an attribute category' in ex.validate_expression('attr("umg512", "grid", "x")')[1]
    assert 'has no' in ex.validate_expression('attr("umg512", "grid_connection", "nope")')[1]
    assert ex.validate_expression('attr("umg512", "grid_connection")')[0] is False


def test_attr_reads_a_number_and_text_is_no_value(places):
    assert _ev('attr("umg512", "grid_connection", "max_export_kw") * 1000 + p',
               {'p': -42000}) == 3000                       # 3 kW of room under the cap
    with pytest.raises(ex.MissingValue):
        _ev('attr("umg512", "grid_connection", "role")')
    with pytest.raises(ex.MissingValue):
        _ev('attr("umg512", "grid_connection", "phases")')   # not set


def test_a_formula_on_the_sun_alone_reads_no_values(places):
    ok, _err, refs = ex.validate_expression('is_day(5, "pv")')
    assert ok and refs == []                                # a rule on it is timeless, like one on the clock


def test_the_service_answers_places_and_summed_pv_inputs(tmp_path):
    from multibus.attributes import AttributeService
    svc = AttributeService(tmp_path / 'a.yaml', devices=lambda: [('pv-u1', 'pv', 'inverter', 2)],
                           installations=lambda: ['pv'])
    assert svc.place_location('pv-u1') is None
    svc.put('site', '', 'location', {'latitude_deg': PARIS[0], 'longitude_deg': PARIS[1]})
    assert svc.place_location('pv-u1') == PARIS and svc.place_location('site') == PARIS
    row = {'mppt': 1, 'modules': 40, 'module_wp': 355, 'azimuth_deg': 180, 'tilt_deg': 40}
    svc.put('device', 'pv-u1', 'pv_input', [row, {**row, 'mppt': 2, 'modules': 20}])
    assert svc.attr_value('pv-u1', 'pv_input', 'kwp') == 21.3            # the inverter's total
    assert svc.attr_value('pv-u1', 'pv_input', 'tilt_deg') == 40          # shared by every row
    assert svc.attr_value('pv-u1', 'pv_input', 'mppt') is None            # rows differ, no sum
    assert svc.place_ids() == {'site', 'pv', 'pv-u1'}


def test_is_day_turns_at_sunrise_not_minutes_before(places):
    """Paris, 21 June 2026: sunrise about 03:47 UTC (true elevation −0.833°)."""
    clock.set_now(lambda: _t(2026, 6, 21, 3, 41).timestamp())
    assert _ev('is_day()') is False
    clock.set_now(lambda: _t(2026, 6, 21, 3, 53).timestamp())
    assert _ev('is_day()') is True


def test_odd_arguments_are_refused_or_missing(places):
    assert ex.validate_expression('is_day(true)')[0] is False
    assert ex.validate_expression('sun_elevation(5)')[0] is False
    assert ex.validate_expression('attr("umg512", "grid_connection", 3)')[0] is False
    assert ex.validate_expression('is_day("pv", "pv")')[0] is False
    assert ex.validate_expression('sun_elevation("site")')[0] is True
    assert ex.validate_expression('sun_elevation("nope")', check_places=False)[0] is True   # a template
    ex.set_attr_provider(lambda t, c, f: True)
    with pytest.raises(ex.MissingValue):
        _ev('attr("umg512", "grid_connection", "phases") + 1')
