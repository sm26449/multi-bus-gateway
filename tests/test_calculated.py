"""Calculated registers: safe expression evaluator + config persistence + API."""
import pytest

from multibus import expressions as ex
from multibus.config import Config

from tests.test_devices_api import make_app, needs_tc


# ---- evaluator safety -----------------------------------------------------------

@pytest.mark.parametrize("expr", [
    "__import__('os')",
    "os.system('rm -rf /')",         # Call on an Attribute → not a whitelisted func
    "[x for x in range(3)]",         # comprehension
    "lambda: 1",                     # lambda
    "'a string'",                    # non-numeric constant
    "a.b.c",                         # attribute chain (only <dev>.<reg> allowed)
    "unknown_func(1)",               # function not in whitelist
    "",                              # empty
])
def test_validate_rejects(expr):
    ok, err, _refs = ex.validate_expression(expr)
    assert ok is False and err


@pytest.mark.parametrize("expr", [
    "_P_SUM3 / _S_SUM3",
    "min(a, b) + 3",
    "a if a > 0 else 0",
    "pow(x, 2) + sqrt(y)",
    "fronius._P + _P_L1",            # cross-device ref
])
def test_validate_accepts(expr):
    ok, err, _refs = ex.validate_expression(expr)
    assert ok is True and err is None


def test_refs_collection():
    ok, _e, refs = ex.validate_expression("janitza._P + _S - avg(a, b)")
    assert ok
    assert set(refs) == {"janitza._P", "_S", "a", "b"}


@pytest.mark.parametrize("expr", [
    "_G_ULN[1] / _G_ULN[2]",
    "_ILN[0] + _ILN[1] + _ILN[2]",
    "fronius._P[0] * 2",
    "avg(_PLN[0], _PLN[1], _PLN[2])",
])
def test_bracket_register_names_accepted(expr):
    ok, err, _refs = ex.validate_expression(expr)
    assert ok is True and err is None


def test_bracket_ref_collection_and_eval():
    ok, _e, refs = ex.validate_expression("_ILN[0] + _ILN[1]")
    assert ok and set(refs) == {"_ILN[0]", "_ILN[1]"}
    ns = {"_ILN[0]": 5.0, "_ILN[1]": 3.0}
    assert ex.evaluate("_ILN[0] * 2 + _ILN[1]", lambda n: ns.get(n)) == 13.0


def test_non_constant_subscript_rejected():
    ok, err, _refs = ex.validate_expression("_ILN[x]")
    assert ok is False and err


@pytest.mark.parametrize("expr", [
    "round(_P, 1, 2)",        # too many args
    "clamp(_P, 0)",           # too few args
    "min(_P)",                # min needs >= 2
    "round(_P, ndigits=1)",   # keyword arg
])
def test_validate_rejects_bad_arity(expr):
    ok, err, _refs = ex.validate_expression(expr)
    assert ok is False and err


def test_exponent_bounded_at_eval():
    ok, _e, _r = ex.validate_expression("2 ** 100")   # syntactically fine
    assert ok
    with pytest.raises(ex.ExpressionError):            # 100 > MAX_POW_EXP (64)
        ex.evaluate("2 ** 100", lambda n: None)
    assert ex.evaluate("2 ** 8", lambda n: None) == 256


def test_nested_pow_magnitude_bounded():
    # each exponent stays at 64 (under the cap) but the base grows super-
    # exponentially — must be rejected on the second level BEFORE allocation.
    ok, _e, _r = ex.validate_expression("((2**64)**64)**64")
    assert ok                                         # syntactically valid
    with pytest.raises(ex.ExpressionError):
        ex.evaluate("(2**64)**64", lambda n: None)    # 2^64 base, exp 64 → too large
    assert ex.evaluate("2 ** 64", lambda n: None) == 2 ** 64   # single level still fine


def test_stateful_prev_and_dt():
    # validation: prev() needs a single reference; dt is allowed and not a ref
    ok, _e, refs = ex.validate_expression("(_E - prev(_E)) / dt * 3600")
    assert ok and set(refs) == {"_E"}          # dt is not a measurement ref
    bad, err, _ = ex.validate_expression("prev(1 + 2)")
    assert bad is False and err
    # first run: no prev history -> MissingValue (caller snapshots + skips)
    with pytest.raises(ex.MissingValue):
        ex.evaluate("_E - prev(_E)", lambda n: {"_E": 100.0}.get(n))
    # next run: prev + dt available -> average power from a Wh counter
    val = ex.evaluate("(_E - prev(_E)) / dt * 3600",
                      lambda n: {"_E": 100.0}.get(n),
                      prev_resolve=lambda r: {"_E": 40.0}.get(r), dt=10.0)
    assert val == (100.0 - 40.0) / 10.0 * 3600


def test_evaluate_arithmetic_and_funcs():
    ns = {"a": 2, "b": 3, "x": 3.0, "y": 16.0}
    assert ex.evaluate("2 + 3 * 4", lambda n: ns.get(n)) == 14
    assert ex.evaluate("pow(a, 10)", lambda n: ns.get(n)) == 1024
    assert ex.evaluate("sqrt(y)", lambda n: ns.get(n)) == 4.0
    assert ex.evaluate("a > 1 and b > 1", lambda n: ns.get(n)) is True


def test_evaluate_zero_division_is_expression_error():
    with pytest.raises(ex.ExpressionError):
        ex.evaluate("a / b", lambda n: {"a": 1, "b": 0}.get(n))


def test_evaluate_missing_value():
    with pytest.raises(ex.MissingValue):
        ex.evaluate("x + 1", lambda n: None)


# ---- API ------------------------------------------------------------------------

@needs_tc
def test_presets_and_functions(tmp_path):
    _cfg, client = make_app(tmp_path)
    presets = client.get("/api/calculated/presets").json()["presets"]
    assert any(p["id"] == "pf_total" for p in presets)
    fns = client.get("/api/calculated/functions").json()
    assert any(f["name"] == "avg" for f in fns["functions"]) and fns["operators"]


@needs_tc
def test_save_validates_and_persists(tmp_path):
    _cfg, client = make_app(tmp_path)
    # bad expression rejected
    bad = {"calculated": [{"name": "X", "expr": "__import__('os')"}]}
    assert client.post("/api/devices/umg512/calculated", json=bad).status_code == 422
    # duplicate name rejected
    dup = {"calculated": [{"name": "X", "expr": "1+1"}, {"name": "X", "expr": "2"}]}
    assert client.post("/api/devices/umg512/calculated", json=dup).status_code == 422
    # good expression saved + persisted
    good = {"calculated": [{"name": "PF_TOTAL", "label": "Power factor",
                            "unit": "", "expr": "_P_SUM3 / _S_SUM3",
                            "poll_group": "normal", "decimals": 3}]}
    r = client.post("/api/devices/umg512/calculated", json=good)
    assert r.status_code == 200 and r.json()["count"] == 1
    reloaded = Config(str(tmp_path / "config.yaml"))
    saved = reloaded.load_calculated("umg512")
    assert saved and saved[0]["name"] == "PF_TOTAL" and saved[0]["expr"] == "_P_SUM3 / _S_SUM3"


@needs_tc
def test_user_templates_save_list_delete(tmp_path):
    _cfg, client = make_app(tmp_path)
    assert client.get("/api/calculated/templates").json()["templates"] == []
    # bad expr rejected
    assert client.post("/api/calculated/templates",
                       json={"name": "Bad", "expr": "lambda: 1"}).status_code == 422
    # save one
    r = client.post("/api/calculated/templates",
                    json={"name": "My PF", "unit": "", "expr": "_P_SUM3 / _S_SUM3", "decimals": 3})
    assert r.status_code == 200
    tid = r.json()["id"]
    assert tid == "my-pf"
    # listed + persisted
    tpls = client.get("/api/calculated/templates").json()["templates"]
    assert len(tpls) == 1 and tpls[0]["expr"] == "_P_SUM3 / _S_SUM3"
    reloaded = Config(str(tmp_path / "config.yaml"))
    assert reloaded.load_calculated_templates()[0]["name"] == "My PF"
    # delete
    assert client.delete(f"/api/calculated/templates/{tid}").json()["templates"] == []


@needs_tc
def test_calculated_listed_in_history_picker(tmp_path):
    _cfg, client = make_app(tmp_path)
    good = {"calculated": [{"name": "PF_TOTAL", "unit": "",
                            "expr": "_P_SUM3 / _S_SUM3", "poll_group": "normal"}]}
    assert client.post("/api/devices/umg512/calculated", json=good).status_code == 200
    regs = client.get("/api/history/registers").json()["registers"]
    pf = next((r for r in regs if r["name"] == "PF_TOTAL"), None)
    assert pf is not None and pf.get("calculated") is True


@needs_tc
def test_test_endpoint_live_preview(tmp_path):
    _cfg, client = make_app(tmp_path)
    st = client.app.state.current_values
    st[100] = {"value": 4000.0, "name": "_P_SUM3", "unit": "W", "timestamp": "t"}
    st[102] = {"value": 5000.0, "name": "_S_SUM3", "unit": "VA", "timestamp": "t"}
    r = client.post("/api/devices/umg512/calculated/test",
                    json={"expr": "_P_SUM3 / _S_SUM3"}).json()
    assert r["ok"] is True and abs(r["value"] - 0.8) < 1e-9
    # missing ref reported, not crashed
    r2 = client.post("/api/devices/umg512/calculated/test",
                     json={"expr": "_P_SUM3 / _NOPE"}).json()
    assert r2["ok"] is False and "_NOPE" in r2["missing"]


# ── P0: calc results inherit the oldest input timestamp (no staleness laundering) ──

def test_calc_result_inherits_oldest_input_timestamp():
    from types import SimpleNamespace
    from multibus.calc_engine import CalcEngine, CALC_ADDR_BASE
    # one store with two inputs of different age; the calc P = A + B must be
    # stamped with the OLDER of the two, not now()
    old_ts = "2020-01-01T00:00:00"
    new_ts = "2020-01-01T00:00:30"
    store = {
        1: {"name": "_A", "value": 10.0, "timestamp": new_ts},
        2: {"name": "_B", "value": 20.0, "timestamp": old_ts},   # the stale input
    }
    cfg = SimpleNamespace(load_calculated=lambda d: [{"name": "_P", "expr": "_A + _B"}])
    eng = CalcEngine(cfg, store_for=lambda d: store,
                     publishers=lambda: (None, None))
    eng.load("dev")
    eng.run("dev", "normal", store, topic_prefix="", bucket=None, device_tag=None,
            device_id="dev", mqtt_on=False, influx_on=False)
    calc = store[CALC_ADDR_BASE]
    assert calc["value"] == 30.0 and calc["calculated"] is True
    assert calc["timestamp"] == old_ts          # oldest input, NOT now()


def test_calc_with_no_timestamped_inputs_falls_back_to_now():
    from types import SimpleNamespace
    from datetime import datetime
    from multibus.calc_engine import CalcEngine, CALC_ADDR_BASE
    store = {1: {"name": "_A", "value": 5.0}}   # no timestamp
    cfg = SimpleNamespace(load_calculated=lambda d: [{"name": "_P", "expr": "_A * 2"}])
    eng = CalcEngine(cfg, store_for=lambda d: store, publishers=lambda: (None, None))
    eng.load("dev")
    eng.run("dev", "normal", store, topic_prefix="", bucket=None, device_tag=None,
            device_id="dev", mqtt_on=False, influx_on=False)
    calc = store[CALC_ADDR_BASE]
    assert calc["value"] == 10.0
    # a parseable recent ISO timestamp (fell back to now)
    assert datetime.fromisoformat(calc["timestamp"]).year >= 2020


# ── P1: calc registers evaluate on push-driven (mqtt) group ──────────────────

def test_calc_evaluates_on_mqtt_wildcard_group():
    from types import SimpleNamespace
    from multibus.calc_engine import CalcEngine, CALC_ADDR_BASE
    store = {1: {"name": "_A", "value": 5.0, "timestamp": "2020-01-01T00:00:00"}}
    cfg = SimpleNamespace(load_calculated=lambda d: [
        {"name": "_P", "expr": "_A * 2", "poll_group": "normal"}])   # NOT 'mqtt'
    eng = CalcEngine(cfg, store_for=lambda d: store, publishers=lambda: (None, None))
    eng.load("dev")
    # a push on the 'mqtt' group must still evaluate the 'normal'-group calc
    eng.run("dev", "mqtt", store, topic_prefix="", bucket=None, device_tag=None,
            device_id="dev", mqtt_on=False, influx_on=False)
    assert store[CALC_ADDR_BASE]["value"] == 10.0
