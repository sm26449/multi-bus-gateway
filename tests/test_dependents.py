# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Nothing is deleted while something uses it: a rule, a sequence, another
device's formula, a device it measures, a virtual meter, a template — the
API refuses (409) with the list, through every delete path. Disabling is
the way out and is never refused; no safety action goes through the check."""
import json
from pathlib import Path

import pytest
import yaml

from multibus import dependents as deps_mod
from multibus.dependents import Sources, dangling, dependents_of, named_in
from tests.test_commands import _Inverter
from tests.test_commands_api import _app as _plant_app
from tests.test_devices_api import needs_tc
from tests.test_rules import OV

EM = {"id": "em-hall", "name": "Hall meter", "template": "eastron_sdm630", "enabled": False,
      "connection": {"protocol": "tcp", "host": "192.0.2.41", "port": 502, "unit_id": 1}}


def _blocking(r):
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert d["errors"] and "disable" in d["errors"][0]
    return sorted((x["type"], x["id"], x["where"]) for x in d["dependents"]["blocking"])


@pytest.fixture(autouse=True)
def _units_stop(monkeypatch):
    # the test unit's facade stops like a client does (an edit restarts it)
    from tests.test_commands_api import _Facade
    monkeypatch.setattr(_Facade, "disconnect", lambda self: None, raising=False)


def _plant(tmp_path, *, rule=True):
    cfg, app, client = _plant_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    if rule:
        assert client.post("/api/rules", json=OV).status_code == 200
    return cfg, app, client


# ── what a formula names ─────────────────────────────────────────────────────

def test_a_formula_names_a_device_as_written_never_a_piece_of_it():
    ids = {'pv-u1', 'pv', 'meter2'}
    assert named_in('max(pv-u1.voltage_l1_n, pv-u1.voltage_l2_n)', ids) == {'pv-u1'}
    assert named_in('grid_power-meter2.p', ids) == {'meter2'}             # a subtraction
    assert named_in('sun_elevation("pv") > 5', ids) == {'pv'}             # a place
    assert named_in("attr('pv-u1', 'rated_ac_kw') * 2", ids) == {'pv-u1'}
    assert named_in('pv-u1.x +', ids) == {'pv-u1'}                       # broken: still named
    assert named_in('pv2.x + other.y', ids) == set()


# ── rules ────────────────────────────────────────────────────────────────────

@needs_tc
def test_a_rule_blocks_deleting_the_installation_it_commands_and_reads(tmp_path):
    cfg, app, client = _plant(tmp_path)
    assert client.delete("/api/devices/pv-u1").status_code == 422      # a unit: as before
    got = _blocking(client.delete("/api/endpoints/pv"))
    assert ('rule_target', 'ov-u1', 'target') in got and ('rule_input', 'ov-u1', 'signal') in got
    assert cfg.get_raw_endpoint("pv") is not None
    # the way the UI asks first: the same answer, nothing removed
    assert client.delete("/api/endpoints/pv?dry_run=true").status_code == 409
    # the rule gone, the installation goes
    assert client.delete("/api/rules/ov-u1").status_code == 200
    r = client.delete("/api/endpoints/pv?dry_run=true")
    assert r.status_code == 200 and r.json()["would"] == "delete"
    assert {d["id"] for d in r.json()["dependents"]["follows"]} >= {"pv-u1", "pv-u2"}
    assert cfg.get_raw_endpoint("pv") is not None                       # a dry run removed nothing


@needs_tc
def test_an_edit_that_takes_away_a_unit_a_rule_uses_is_refused_at_its_field(tmp_path):
    cfg, app, client = _plant(tmp_path)
    ep = client.get("/api/endpoints/pv").json()
    groups = json.loads(json.dumps(ep["groups"]))
    one = dict(groups[0], units=[2])                     # pv-u1 would go
    r = client.put("/api/endpoints/pv", json={"groups": [one]})
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert "groups[0].units" in d["fields"] and "pv-u1" in d["errors"][0]
    assert {u.id for u in cfg.endpoint_devices("pv")} == {"pv-u1", "pv-u2"}
    # a unit nothing uses may go
    r = client.put("/api/endpoints/pv", json={"groups": [dict(groups[0], units=[1])]})
    assert r.status_code == 200, r.text
    assert {u.id for u in cfg.endpoint_devices("pv")} == {"pv-u1"}


@needs_tc
def test_disabling_a_group_or_the_installation_is_never_refused(tmp_path):
    cfg, app, client = _plant(tmp_path)
    ep = client.get("/api/endpoints/pv").json()
    g = dict(json.loads(json.dumps(ep["groups"][0])), enabled=False)
    r = client.put("/api/endpoints/pv", json={"groups": [g]})
    assert r.status_code == 200, r.text
    assert {u.id for u in cfg.endpoint_devices("pv")} == {"pv-u1", "pv-u2"}   # still there, not polling
    assert client.put("/api/endpoints/pv", json={"enabled": False}).status_code == 200


@needs_tc
def test_a_rule_targeting_a_group_blocks_renaming_it_away(tmp_path):
    cfg, app, client = _plant(tmp_path, rule=False)
    grp = dict(OV, id='grp', target={'endpoint': 'pv', 'group': 'inverters', 'command': 'power_limit'},
               signal='pv-u2.voltage_l1_n')
    assert client.post("/api/rules", json=grp).status_code == 200, client.post("/api/rules", json=grp).text
    g = dict(cfg.get_raw_endpoint("pv")["groups"][0], id='inv2')     # the stored group, renamed
    r = client.put("/api/endpoints/pv", json={"groups": [g]})
    assert r.status_code == 409 and "grp" in r.text, r.text


@needs_tc
def test_a_rule_in_shadow_disabled_or_reading_a_standalone_device_blocks_it(tmp_path):
    cfg, app, client = _plant(tmp_path, rule=False)
    assert client.post("/api/devices", json=EM).status_code == 200
    reads = dict(OV, id='reads-em', signal='em-hall.voltage_l1_n')
    assert client.post("/api/rules", json=reads).status_code == 200
    assert _blocking(client.delete("/api/devices/em-hall")) == [('rule_input', 'reads-em', 'signal')]
    assert client.post("/api/rules/reads-em/enable", json={"enabled": False}).status_code == 200
    assert client.delete("/api/devices/em-hall").status_code == 409      # disabled still names it
    assert client.delete("/api/rules/reads-em").status_code == 200
    assert client.delete("/api/devices/em-hall").status_code == 200


# ── sequences, formulas, meters, virtual meters ──────────────────────────────

@needs_tc
def test_a_sequence_step_blocks_the_device_it_commands_or_reads(tmp_path):
    cfg, app, client = _plant(tmp_path, rule=False)
    assert client.post("/api/devices", json=EM).status_code == 200
    Path(app.state.sequence_store.path).write_text(yaml.safe_dump({"sequences": [
        {"id": "start", "steps": [{"command": {"device": "pv-u1", "name": "power_limit"}},
                                  {"wait_until": "em-hall.power_active_total > 1", "timeout_s": 10}],
         "on_abort": []}]}))
    assert _blocking(client.delete("/api/devices/em-hall")) == [('sequence_step', 'start', 'steps[1].wait_until')]
    assert ('sequence_step', 'start', 'steps[0].command') in _blocking(client.delete("/api/endpoints/pv"))


@needs_tc
def test_a_disabled_devices_formula_blocks_the_device_it_reads_and_writes_nothing(tmp_path):
    cfg, app, client = _plant(tmp_path, rule=False)
    assert client.post("/api/devices", json=EM).status_code == 200
    assert client.post("/api/devices", json=dict(EM, id="em-two", name="Two")).status_code == 200
    cfg.save_calculated("em-two", [{"name": "twice", "expr": "em-hall.power_active_total * 2"}])
    good = list(Path(cfg.device_registers_path("em-two")).parent.glob("*.good"))
    assert _blocking(client.delete("/api/devices/em-hall")) == [('calculated', 'em-two', 'calculated.twice')]
    assert list(Path(cfg.device_registers_path("em-two")).parent.glob("*.good")) == good
    # its own formula reading itself is not a dependent
    cfg.save_calculated("em-two", [{"name": "self", "expr": "em-two.power_active_total * 2"}])
    assert client.delete("/api/devices/em-two?dry_run=true").status_code == 200


@needs_tc
def test_a_meter_that_measures_a_device_is_not_deleted_under_it(tmp_path):
    cfg, app, client = _plant(tmp_path, rule=False)
    assert client.post("/api/devices", json=EM).status_code == 200
    assert client.post("/api/devices", json=dict(EM, id="hp", name="Heat pump")).status_code == 200
    cfg.set_energy_meter("hp", {"device": "em-hall", "power": "power_active_total"})
    assert _blocking(client.delete("/api/devices/em-hall")) == [('meter_link', 'hp', 'meters.consumption')]
    got = client.get("/api/devices/em-hall/dependents").json()
    assert got["blocking"][0]["ref"] == {"type": "device", "id": "hp", "tab": "meters"}


@needs_tc
def test_a_parent_is_blocked_by_what_uses_one_of_its_components(tmp_path):
    from tests.test_device_components import _app as _gx_app
    cfg, c = _gx_app(tmp_path)
    cfg.save_calculated("umg512", [{"name": "bank_soc", "expr": "gx-battery-288.soc * 1"}])
    r = c.delete("/api/devices/gx")
    got = _blocking(r)
    assert got == [('calculated', 'umg512', 'calculated.bank_soc')]
    follows = {d["id"] for d in r.json()["detail"]["dependents"]["follows"] if d["type"] == "component"}
    assert follows == {"gx-battery-288", "gx-battery-512", "ruuvi-in"}
    assert c.delete("/api/devices/gx/components/gx-battery-288").status_code == 409
    assert c.delete("/api/devices/gx/components/gx-battery-512").status_code == 200
    cfg.save_calculated("umg512", [])
    assert c.delete("/api/devices/gx").status_code == 200


# ── templates ────────────────────────────────────────────────────────────────

@needs_tc
def test_a_template_read_by_a_disabled_device_or_an_installation_is_not_deleted(tmp_path):
    cfg, app, client = _plant(tmp_path, rule=False)
    tpl = {"device_template": {"id": "my_em", "name": "My meter", "vendor": "x", "model": "y",
                               "registers": [{"address": 0, "name": "power_active_total", "unit": "W",
                                              "data_type": "float32"}]}}
    assert client.post("/api/device-templates", json=tpl).status_code == 200
    assert client.post("/api/devices", json=dict(EM, template="my_em")).status_code == 200
    got = _blocking(client.delete("/api/device-templates/my_em"))
    assert got == [('device', 'em-hall', 'template')]
    assert client.delete("/api/device-templates/my_em?dry_run=true").status_code == 409
    # a built-in one is read-only first, whatever uses it
    assert client.delete("/api/device-templates/fronius_sunspec_inverter").status_code == 422
    assert client.delete("/api/devices/em-hall").status_code == 200
    r = client.delete("/api/device-templates/my_em?dry_run=true")
    assert r.status_code == 200 and r.json()["would"] == "delete"
    assert client.get("/api/device-templates/my_em").status_code == 200   # still there
    assert client.delete("/api/device-templates/my_em").status_code == 200


def test_an_installation_group_and_source_name_the_template_they_read_with():
    class _Cfg:
        devices = []
        endpoints = [{"id": "pv", "name": "PV", "template": "t_site",
                      "groups": [{"id": "inverters", "template": "t_inv",
                                  "sources": [{"id": "a", "template": "t_src"}]}]}]
    src = Sources(config=_Cfg())
    for tid, where in (("t_site", "template"), ("t_inv", "groups[0].template"),
                       ("t_src", "groups[0].sources[0].template")):
        b = dependents_of('template', tid, src)['blocking']
        assert [(d['id'], d['where']) for d in b] == [("pv", where)], tid


# ── what is never refused ────────────────────────────────────────────────────

@needs_tc
def test_no_safety_action_goes_through_the_check(tmp_path, monkeypatch):
    cfg, app, client = _plant(tmp_path)
    assert client.post("/api/devices", json=EM).status_code == 200

    def _boom(*a, **k):
        raise AssertionError("a safety action asked what depends on it")
    monkeypatch.setattr(deps_mod, "dependents_of", _boom)
    monkeypatch.setattr(deps_mod, "units_dependents", _boom)
    for path, body in (("/api/rules/ov-u1/mode", {"mode": "shadow"}),
                       ("/api/rules/ov-u1/enable", {"enabled": False}),
                       ("/api/rules/ov-u1/enable", {"enabled": True})):
        r = client.post(path, json=body)
        assert r.status_code == 200, (path, r.text)
    r = client.put("/api/devices/em-hall", json=dict(EM, enabled=True))
    assert r.status_code == 200, r.text
    r = client.put("/api/devices/em-hall", json=dict(EM, enabled=False))
    assert r.status_code == 200, r.text
    ep = client.get("/api/endpoints/pv").json()
    g = dict(json.loads(json.dumps(ep["groups"][0])), enabled=False)
    assert client.put("/api/endpoints/pv", json={"groups": [g]}).status_code == 200
    assert client.delete("/api/rules/ov-u1").status_code == 200


def test_only_the_guarded_routes_remove_a_device_an_installation_or_a_template():
    """No MQTT topic, OPC UA method or other route deletes one: every call
    that removes them sits in a delete handler that asks first."""
    import ast
    root = Path(__file__).resolve().parents[1] / "multibus"
    removers = ("remove_raw_device", "delete_endpoint", "remove_component", "delete_user")
    # the faces a delete could arrive through other than the guarded HTTP routes
    faces = [f for f in root.rglob("*.py")
             if f.name.startswith("mqtt_") or "opcua" in f.parts or f.name in ("rules_runtime.py", "sequences.py")]
    assert any(f.name == "mqtt_input.py" for f in faces) and any("opcua" in f.parts for f in faces)
    for f in faces:
        t = f.read_text(encoding="utf-8")
        assert not any(r + "(" in t for r in removers), f
    # inside the gateway's routes: only the handlers that check first
    owners = {}

    class _Calls(ast.NodeVisitor):
        stack = []

        def visit_FunctionDef(self, fn):
            self.stack.append(fn.name)
            self.generic_visit(fn)
            self.stack.pop()
        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            if isinstance(node.func, ast.Attribute) and node.func.attr in removers:
                owners.setdefault(node.func.attr, set()).add(self.stack[-1])   # the innermost
            self.generic_visit(node)
    for f in (root / "api.py", root / "routes" / "device_templates.py"):
        _Calls().visit(ast.parse(f.read_text(encoding="utf-8")))
    assert owners == {"remove_raw_device": {"_delete_device_locked"},
                      "remove_component": {"_delete_component_locked"},
                      "delete_endpoint": {"_delete_endpoint_locked"},
                      "delete_user": {"delete_device_template"}}, owners


# ── a bad block never drops a device ─────────────────────────────────────────

def test_a_malformed_block_is_ignored_not_the_device(tmp_path):
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml="""
devices:
  - id: em-odd
    name: Odd
    template: eastron_sdm630
    connection: { protocol: tcp, host: 192.0.2.42, port: 502, unit_id: 1 }
    energy_meter: "not a mapping"
    rest_push: 5
    pq_recorder: [1, 2]
""")
    d = cfg.get_device("em-odd")
    assert d is not None and d.energy_meter == {} and d.rest_push == {} and d.pq_recorder == {}


# ── after an import or a restore ─────────────────────────────────────────────

def test_what_a_restored_configuration_names_and_does_not_have_is_said():
    class _Cfg:
        devices = [type("D", (), {"id": "pv-u1"})()]
        endpoints = [{"id": "pv"}]
    rules = {"a": dict(OV, id="a", target={"device": "pv-u9", "command": "power_limit"},
                       signal="pv-u9.voltage_l1_n"),
             "b": dict(OV, id="b")}
    seqs = [{"id": "s", "steps": [{"command": {"device": "gone", "name": "x"}}]}]
    from multibus import expressions
    old = expressions._device_ids
    expressions._device_ids = lambda: {"pv-u1", "pv"}
    try:
        got = {(d["type"], d["id"], d["missing"]) for d in dangling(Sources(
            config=_Cfg(), rules=lambda: rules, sequences=lambda: seqs))}
    finally:
        expressions._device_ids = old
    assert got == {("rule_target", "a", "pv-u9"), ("rule_input", "a", "pv-u9"),
                   ("sequence_step", "s", "gone")}


@needs_tc
def test_the_used_by_answer_is_readable_and_names_how_to_open_each(tmp_path):
    cfg, app, client = _plant(tmp_path)
    r = client.get("/api/endpoints/pv/dependents")
    assert r.status_code == 200
    b = r.json()["blocking"]
    assert {(d["type"], d["of"]) for d in b} >= {("rule_target", "pv-u1"), ("rule_input", "pv-u1")}
    assert all(d["ref"] == {"type": "rule", "id": "ov-u1"} for d in b)
    assert client.get("/api/devices/nope/dependents").status_code == 404
    assert client.get("/api/device-templates/nope/dependents").status_code == 404
    t = client.get("/api/device-templates/fronius_sunspec_inverter/dependents").json()
    assert [(d["type"], d["id"]) for d in t["blocking"]] == [("installation_source", "pv")]


@needs_tc
def test_one_pass_counts_what_uses_every_device_and_installation(tmp_path):
    from multibus.dependents import dependents_index
    cfg, app, client = _plant(tmp_path)
    assert client.post("/api/devices", json=EM).status_code == 200
    rules = {'ov-u1': dict(OV), 'em': dict(OV, id='em', signal='em-hall.voltage_l1_n')}
    got = dependents_index(Sources(config=cfg, rules=lambda: rules))
    assert got.get('pv-u1') == 2 and got.get('em-hall') == 1 and 'pv-u2' not in got


@needs_tc
def test_the_counts_for_a_list_come_in_one_answer(tmp_path):
    cfg, app, client = _plant(tmp_path)
    r = client.get("/api/dependents")
    assert r.status_code == 200, r.text
    counts = r.json()["counts"]
    assert counts.get("pv-u1", 0) >= 1 and "nope" not in counts
    # an installation counts what uses any of its units, each user once
    assert counts.get("pv") == counts["pv-u1"]



# ── review fixes (DP-M1..M3, DP-m*) ──────────────────────────────────────────

def _vm_stub(tmp_path, client, rows_yaml, instance=None):
    tdir = tmp_path / "vmt"
    tdir.mkdir(exist_ok=True)
    (tdir / "comp.yaml").write_text(
        "template:\n  id: comp\n  name: Comp\n  transport: { port: 1503 }\n  registers:\n" + rows_yaml)

    class _Stub:
        templates_dir = tdir

        def _load_cfg(self):
            return {"instances": [dict({"template": "comp", "port": 1503}, **(instance or {}))]}
    client.app.state.vmeter_manager = _Stub()
    return tdir


@needs_tc
def test_a_virtual_meters_failover_candidate_is_a_dependent(tmp_path):
    cfg, app, client = _plant(tmp_path, rule=False)
    assert client.post("/api/devices", json=EM).status_code == 200
    _vm_stub(tmp_path, client, '    - { addr: 0, type: float, source: { combined: ["umg512.power_active_total", '
                               '"em-hall.power_active_total"] } }\n')
    assert _blocking(client.delete("/api/devices/em-hall")) == [('virtual_meter', 'comp', 'source')]
    _vm_stub(tmp_path, client, '    - { addr: 0, type: float, source: { failover: ["em-hall.p"] } }\n')
    assert client.delete("/api/devices/em-hall?dry_run=true").status_code == 409


@needs_tc
def test_a_source_that_cannot_be_read_refuses_the_delete(tmp_path):
    cfg, app, client = _plant(tmp_path, rule=False)
    assert client.post("/api/devices", json=EM).status_code == 200
    # a virtual meter template that does not load
    tdir = _vm_stub(tmp_path, client, "    - { addr: 0, type: float, source: { live: em-hall.p } }\n")
    (tdir / "comp.yaml").write_text("template: [not, a, template")
    r = client.delete("/api/devices/em-hall")
    assert r.status_code == 503 and "comp" in r.text and cfg.get_device("em-hall") is not None
    client.app.state.vmeter_manager = None
    # a calculated-fields file that does not parse
    path = Path(cfg.device_registers_path("umg512"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json")
    r = client.delete("/api/devices/em-hall?dry_run=true")
    assert r.status_code == 503 and "umg512" in r.text
    path.write_text('{"registers": [], "calculated": []}')
    # rules.yaml that does not load
    rt = app.state.rules_runtime
    rt.store.path.write_text("rules: [unclosed")
    rt.load()
    r = client.delete("/api/devices/em-hall?dry_run=true")
    assert r.status_code == 503 and "rules.yaml" in r.text
    rt.store.path.write_text("rules: []\n")
    rt.load()
    assert client.delete("/api/devices/em-hall?dry_run=true").status_code == 200


@needs_tc
def test_dropping_a_unit_of_a_group_a_rule_commands_is_allowed_and_said(tmp_path):
    cfg, app, client = _plant(tmp_path, rule=False)
    grp = dict(OV, id='grp', target={'endpoint': 'pv', 'group': 'inverters', 'command': 'power_limit'},
               signal='max(pv-u1.voltage_l1_n, pv-u2.voltage_l1_n)')
    assert client.post("/api/rules", json=grp).status_code == 200
    # the rule READS pv-u2: that unit cannot go; pv-u1 is only commanded as one of the group
    grp2 = dict(grp, signal='pv-u2.voltage_l1_n')
    assert client.put("/api/rules/grp", json=grp2).status_code == 200
    g = cfg.get_raw_endpoint("pv")["groups"][0]
    r = client.put("/api/endpoints/pv", json={"groups": [dict(g, units=[2])]})
    assert r.status_code == 200, r.text
    w = r.json()["warnings"]
    assert [(x["type"], x["id"], x["of"]) for x in w] == [("rule_target", "grp", "pv-u1")]
    r = client.put("/api/endpoints/pv", json={"groups": [dict(g, units=[1])]})
    assert r.status_code == 409 and "pv-u2" in r.text                 # read by name: refused
    # a unit's own answer says the group target as a warning, never as blocking
    d = client.get("/api/devices/pv-u2/dependents").json()
    assert ("rule_input", "grp") in {(x["type"], x["id"]) for x in d["blocking"]}
    assert [(x["type"], x["id"]) for x in d["warnings"]] == [("rule_target", "grp")]


@needs_tc
def test_moving_a_unit_out_of_the_group_a_rule_commands_is_said(tmp_path):
    cfg, app, client = _plant(tmp_path, rule=False)
    grp = dict(OV, id='grp', target={'endpoint': 'pv', 'group': 'inverters', 'command': 'power_limit'},
               signal='sun_elevation("pv") > 100')
    assert client.post("/api/rules", json=grp).status_code == 200, client.post("/api/rules", json=grp).text
    g = cfg.get_raw_endpoint("pv")["groups"][0]
    other = dict(g, id='spare', units=[2])
    r = client.put("/api/endpoints/pv", json={"groups": [dict(g, units=[1]), other]})
    assert r.status_code == 200, r.text
    assert [(x["id"], x["of"]) for x in r.json()["warnings"]] == [("grp", "pv-u2")]


def test_places_are_every_argument_of_the_sun_and_rule_ids_are_not_devices():
    ids = {'pv', 'umg512', 'pv-u1'}
    assert named_in('is_day(0, "pv")', ids) == {'pv'}
    assert named_in('is_day(5, \'pv\') and sun_azimuth("pv-u1") > 90', ids) == {'pv', 'pv-u1'}
    assert named_in('attr("umg512", "pv")', ids) == {'umg512'}          # attr: the first only
    assert named_in('run_today_h("umg512") > 1', ids) == set()
    assert named_in('run_today_h("umg512") > 1 +', ids) == set()        # broken: the same


def test_a_component_templates_formula_is_walked():
    from multibus.dependents import Index

    class _T:
        def __init__(self, tid, expr, comp=''):
            self.id, self.name, self.component_of = tid, tid, comp
            self.calculated = [{'name': 'x', 'expr': expr}]

    class _D:
        def __init__(self, did):
            self.id, self.name, self.endpoint_id, self.component_of = did, did, '', ''

    class _Cfg:
        devices = [_D('em-hall'), _D('gx')]
        endpoints = []

        def load_calculated(self, _id):
            return []
    src = Sources(config=_Cfg(), templates=lambda: [_T('gx_mqtt:battery', 'em-hall.p * {device}.q', 'gx_mqtt')])
    got = dependents_of('device', 'em-hall', src)['blocking']
    assert [(d['type'], d['id']) for d in got] == [('template_formula', 'gx_mqtt:battery')]
    assert Index(src).by_name.get('em-hall')


@needs_tc
def test_the_count_a_list_shows_is_the_refusal_a_delete_gives(tmp_path):
    from tests.test_device_components import _app as _gx_app
    cfg, c = _gx_app(tmp_path)
    cfg.save_calculated("umg512", [{"name": "bank_soc", "expr": "gx-battery-288.soc * 1"}])
    counts = c.get("/api/dependents").json()["counts"]
    for did in ("gx", "gx-battery-288", "gx-battery-512"):
        r = c.delete(f"/api/devices/gx/components/{did}?dry_run=true") if did != "gx" \
            else c.delete("/api/devices/gx?dry_run=true")
        n = len({(x["ref"]["type"], x["id"]) for x in r.json()["detail"]["dependents"]["blocking"]}) \
            if r.status_code == 409 else 0
        assert counts.get(did, 0) == n, (did, counts, r.text)
    assert counts["gx"] == 1                                    # its component's user, rolled up


@needs_tc
def test_a_dry_run_on_a_unit_answers_as_the_delete_does(tmp_path):
    cfg, app, client = _plant(tmp_path)
    a = client.delete("/api/devices/pv-u1?dry_run=true")
    b = client.delete("/api/devices/pv-u1")
    assert a.status_code == b.status_code == 422 and a.json() == b.json()


@needs_tc
def test_more_safety_actions_never_ask(tmp_path, monkeypatch):
    cfg, app, client = _plant(tmp_path)

    def _boom(*a, **k):
        raise AssertionError("a safety action asked what depends on it")
    monkeypatch.setattr(deps_mod, "Index", _boom)
    monkeypatch.setattr(deps_mod, "dependents_of", _boom)
    r = client.post("/api/devices/pv-u1/write-lock", json={"locked": True})
    assert r.status_code == 200, r.text
    assert client.post("/api/devices/pv-u1/write-lock", json={"locked": False}).status_code == 200
    r = client.post("/api/sequences/none/cancel")
    assert r.status_code in (200, 404, 409), r.text                     # answered, never by the check


@needs_tc
def test_a_restore_says_what_it_names_and_does_not_have(tmp_path):
    import io as _io
    import zipfile
    from tests.test_devices_api import make_app
    _cfg, client = make_app(tmp_path)
    rules = yaml.safe_dump({"rules": [dict(OV, id="gone", target={"device": "pv-u9", "command": "power_limit"})]})
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("config.yaml", "modbus:\n  host: 192.0.2.10\n")
        z.writestr("rules.yaml", rules)
        z.writestr("manifest.json", '{"backup_version": 1}')
    r = client.post("/api/config/import", content=buf.getvalue(), headers={"Content-Type": "application/zip"})
    assert r.status_code == 200, r.text
    assert ("rule_target", "gone", "pv-u9") in {(d["type"], d["id"], d["missing"]) for d in r.json()["dangling"]}
    ev = client.app.state.event_log
    assert any("pv-u9" in str(e) for e in ev.recent(50)), ev.recent(5)


@needs_tc
def test_an_ignored_block_is_said_on_status_and_in_the_event_log(tmp_path):
    from tests.test_devices_api import make_app
    cfg, client = make_app(tmp_path, extra_yaml="""
devices:
  - id: em-odd
    name: Odd
    template: eastron_sdm630
    enabled: false
    connection: { protocol: tcp, host: 192.0.2.42, port: 502, unit_id: 1 }
    rest_push: 5
""")
    st = client.get("/api/status").json()["config"]
    assert st["ignored_blocks"] == [{"where": "em-odd.rest_push", "why": "rest_push 5 is not a mapping: ignored"}]
    assert any("em-odd" in str(e) and "rest_push" in str(e) for e in client.app.state.event_log.recent(20))


def test_three_hundred_devices_are_judged_quickly():
    """A delete, a dry run and the list counts at 300 devices × 5 formulas,
    100 rules and broken formulas among them: the target is 50 ms; the bound
    here is generous for a slow CI runner, the time measured is printed."""
    import time
    from multibus.dependents import dependents_index

    class _D:
        def __init__(self, did, ep=''):
            self.id, self.name, self.endpoint_id, self.component_of = did, did, ep, ''
            self.group_id = 'units' if ep else ''
            self.energy_meter = {}
    devs = [_D(f"dev-{i}") for i in range(280)] + [_D(f"site-u{i}", 'site') for i in range(1, 21)]

    class _Cfg:
        devices = devs
        endpoints = [{"id": "site"}]
    calcs = {d.id: [{"name": f"c{k}", "expr": f"dev-{(i + k) % 280}.power * 2 + site-u1.p"
                     + (" +" if k == 4 else "")} for k in range(5)] for i, d in enumerate(devs)}
    rules = {f"r{i}": dict(OV, id=f"r{i}", target={"device": f"dev-{i}", "command": "x"},
                           signal=f"max(dev-{i}.v, dev-{(i + 1) % 280}.v, gone-{i}.v)" + (" +" if i % 3 else ""))
             for i in range(100)}
    src = Sources(config=_Cfg(), rules=lambda: rules, calculated=lambda did: calcs[did])
    times = {}
    for name, fn in (("device", lambda: dependents_of('device', 'dev-7', src)),
                     ("installation", lambda: dependents_of('installation', 'site', src)),
                     ("index", lambda: dependents_index(src))):
        t0 = time.perf_counter()
        fn()
        times[name] = (time.perf_counter() - t0) * 1000
    print("dependents at 300 devices (ms):", {k: round(v, 1) for k, v in times.items()})
    assert all(v < 1000 for v in times.values()), times
