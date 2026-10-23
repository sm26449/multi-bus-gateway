"""fresh(), count_fresh(), absent(), age() and steps(): what a formula can say
about the freshness of its inputs, and a step function — in the expression
language shared by calculated fields and rules."""
import time
from types import SimpleNamespace

import pytest

from multibus import expressions as ex

VALUES = {'a': 10.0, 'b': 20.0, 'c': 30.0}
STALE = {'c'}
AGES = {'a': 2.0, 'b': 7.5, 'c': 400.0}


def _ev(expr, values=VALUES, stale=STALE, ages=AGES):
    ok, err, _r = ex.validate_expression(expr)
    assert ok, err
    return ex.evaluate(expr, values.get,
                       stale=(lambda n: n in stale or n not in values) if stale is not None else None,
                       age=(lambda n: ages.get(n)) if ages is not None else None)


def test_fresh_says_whether_every_input_is_there_and_fresh():
    assert _ev("fresh(a)") == 1
    assert _ev("fresh(a + b)") == 1
    assert _ev("fresh(a + c)") == 0                 # c is stale
    assert _ev("fresh(nope)") == 0                  # not there at all


def test_count_fresh_counts_its_fresh_arguments():
    assert _ev("count_fresh(a, b, c)") == 2
    assert _ev("count_fresh(c, nope)") == 0


def test_absent_publishes_nothing_this_round():
    assert _ev("a if fresh(a) else absent()") == 10.0
    with pytest.raises(ex.MissingValue):
        _ev("c if fresh(c) else absent()")


def test_age_is_the_oldest_input():
    assert _ev("age(a)") == 2.0
    assert _ev("age(a + b)") == 7.5
    with pytest.raises(ex.MissingValue):
        _ev("age(a)", ages=None)                     # no measurement times known


def test_steps_takes_the_highest_threshold_reached():
    derate = "steps(soc, 0, 15, 30, 25, 50, 40, 75, 60, 100)"
    for soc, want in ((10, 0), (15, 30), (24.9, 30), (25, 50), (59, 75), (60, 100), (95, 100)):
        assert _ev(derate, values={'soc': soc}) == want, soc


def test_steps_is_checked_on_save():
    assert ex.validate_expression("steps(x, 0, 10)")[0] is False         # a threshold without a value
    ok, err, _ = ex.validate_expression("steps(x, 0, 20, 1, 10, 2)")
    assert not ok and 'rise' in err
    assert ex.validate_expression("fresh(a, b)")[0] is False
    assert ex.validate_expression("absent(1)")[0] is False
    assert ex.validate_expression("count_fresh()")[0] is False


def test_without_staleness_fresh_only_asks_for_a_value():
    assert _ev("fresh(c)", stale=None) == 1
    assert _ev("fresh(nope)", stale=None) == 0


def test_a_calculated_value_gated_on_fresh_does_not_inherit_a_stale_inputs_age():
    """A formula that skips a stale input must not be stamped with its time."""
    from multibus.calc_engine import CalcEngine, CALC_ADDR_BASE
    now = time.monotonic()
    store = {
        1: {'name': 'live', 'value': 5.0, 'timestamp': '2026-10-20T19:00:00', 'mono': now - 1, 'interval': 5},
        2: {'name': 'dead', 'value': 9.0, 'timestamp': '2020-01-01T00:00:00', 'mono': now - 3600, 'interval': 5},
    }
    cfg = SimpleNamespace(load_calculated=lambda d: [
        {'name': 'sum', 'expr': 'live + (dead if fresh(dead) else 0)'},
        {'name': 'n', 'expr': 'count_fresh(live, dead)'},
        {'name': 'live_age', 'expr': 'age(live)'},
    ])
    eng = CalcEngine(cfg, store_for=lambda d: store, publishers=lambda: (None, None))
    eng.load('dev')
    eng.run('dev', 'normal', store, topic_prefix='', bucket=None, device_tag=None,
            device_id='dev', mqtt_on=False, influx_on=False)
    by = {e['name']: e for e in store.values() if e.get('calculated')}
    assert by['sum']['value'] == 5.0 and by['sum']['timestamp'] == '2026-10-20T19:00:00'
    assert by['n']['value'] == 1
    assert 0.5 < by['live_age']['value'] < 5
    assert CALC_ADDR_BASE in store


# -- what a copy/paste brings (written as code points: the source stays ASCII) --

U = chr


@pytest.mark.parametrize('expr, says', [
    ('a ' + U(0x2212) + ' b', 'type -'),                         # typographic minus
    ('a' + U(0x00A0) + '+ b', 'a normal space'),                 # no-break space
    ('a' + U(0x200B) + '+b', 'invisible'),                       # zero-width space
    ('between(' + U(0x201C) + '10:00' + U(0x201D) + ', "16:00")', 'type "'),   # curly quotes
    ('a ' + U(0x00D7) + ' 2', 'type *'),
    ('a ' + U(0x2264) + ' 3', 'type <='),
    ('soc_' + U(0x0430) + ' > 5', 'not a plain letter'),         # Cyrillic a, looks Latin
    (U(0x021B) + 'int' + U(0x0103) + ' + 1', 'not a plain letter'),   # diacritics
])
def test_a_pasted_formula_says_which_character_and_what_to_type(expr, says):
    ok, err, _r = ex.validate_expression(expr)
    assert not ok and says in err and 'position' in err, err


def test_a_formula_on_several_lines_is_one_formula():
    assert ex.validate_expression("a +\n\tb")[0] is True
    assert ex.evaluate("a +\n\tb", {'a': 1, 'b': 2}.get) == 3


def test_topics_refuse_invisible_and_lookalike_spaces_but_keep_real_names():
    from multibus.text_check import topic_issue
    assert topic_issue("zigbee2mqtt/Buc" + U(0x0103) + "t" + U(0x0103) + "rie senzor") is None   # a real name
    assert 'invisible' in topic_issue("a/b" + U(0x200B))
    assert 'normal space' in topic_issue("a/b" + U(0x00A0) + "c")
    assert 'starts or ends' in topic_issue("a/b ")


def test_a_template_refuses_a_pasted_name_or_topic():
    from multibus.device_template import extra_errors
    t = {'device_template': {'schema_version': 1, 'id': 't_paste', 'name': 'T', 'protocol': {'transports': ['mqtt']},
                             'registers': [{'address': 1, 'name': 'soc_' + U(0x0430), 'data_type': 'float', 'topic': 'a/soc'},
                                           {'address': 2, 'name': 'p', 'data_type': 'float', 'topic': 'a/p' + U(0x200B)}]}}
    errs = ' | '.join(extra_errors(t))
    assert 'not a plain letter' in errs and 'invisible' in errs, errs
