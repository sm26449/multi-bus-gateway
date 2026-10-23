# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""A device's kind (device_kind.py): one derivation for lists, filters and
attributes; an admin's override that never restarts the device and that no
edit drops; a stored value nobody can read never stops a device; the device
and template lists searched, filtered and sorted only when asked."""
import copy
import json
import pathlib
from types import SimpleNamespace

import pytest
import yaml

from multibus.device_kind import KINDS, OVERRIDABLE, attribute_kind, device_kind, normalize, template_kind
from tests.test_device_components import GX_YAML
from tests.test_devices_api import make_app, needs_tc
from tests.test_unchanged_save import DEVICES, PLANT


class _Reg:
    def __init__(self, **tpls):
        self.t = tpls

    def get(self, tid):
        return self.t.get(tid)


def _tpl(kind='', names=(), tid='x'):
    return SimpleNamespace(id=tid, kind=kind, registers=[SimpleNamespace(name=n) for n in names], calculated=[])


def _dev(**kw):
    base = dict(kind='', role='', template='', sources=[], component_of='')
    base.update(kw)
    return SimpleNamespace(**base)


# ── the derivation ───────────────────────────────────────────────────────────

def test_first_hit_wins_override_role_component_template_names_none():
    reg = _Reg(m=_tpl('meter', tid='m'), n=_tpl('', ('soc',), tid='n'), z=_tpl('', (), tid='z'),
               **{'gx:battery': _tpl('battery', tid='gx:battery')})
    assert device_kind(_dev(kind='ups', role='inverter', template='m'), reg) == ('ups', 'override')
    assert device_kind(_dev(role='inverter', template='m'), reg) == ('inverter', 'role')
    assert device_kind(_dev(template='gx:battery', component_of='gx'), reg) == ('battery', 'component')
    assert device_kind(_dev(template='m'), reg) == ('meter', 'template')
    assert device_kind(_dev(template='n'), reg) == ('battery', 'names')
    assert device_kind(_dev(template='z'), reg) == ('other', 'none')
    assert device_kind(_dev(), reg) == ('other', 'none')
    # a unit read through sources: its first source's template
    src = SimpleNamespace(template='m', protocol='tcp')
    assert device_kind(_dev(sources=[src]), reg) == ('meter', 'template')


def test_two_bundled_templates_without_a_kind_are_known_by_id():
    reg = _Reg()
    assert template_kind(None, 'samsung_mim_b19n') == ('heat_pump', 'template')
    assert template_kind(None, 'delta_vfd_el') == ('drive', 'template')
    assert device_kind(_dev(template='samsung_mim_b19n'), reg) == ('heat_pump', 'template')


def test_words_and_aliases():
    assert normalize('System') == 'site' and normalize(' heat-pump ') == 'heat_pump'
    assert normalize('vfd') == 'drive' and normalize('toaster') is None and normalize(42) is None
    assert 'other' in KINDS and 'other' not in OVERRIDABLE


def test_every_kind_a_template_may_declare_is_a_device_kind():
    from multibus.attribute_catalog import KINDS as ATTR
    assert set(ATTR) <= set(KINDS)


def test_the_attribute_kind_stays_what_it_was():
    # the attribute service published role → template kind → names, in its
    # own words; a heat pump or a drive was nothing to it, and stays nothing
    reg = _Reg(m=_tpl('meter', tid='m'), g=_tpl('site', ('soc',), tid='g'))
    assert attribute_kind(_dev(role='inverter', template='m'), reg) == 'inverter'
    assert attribute_kind(_dev(template='g'), reg) == 'site'
    assert attribute_kind(_dev(template='samsung_mim_b19n'), reg) is None
    assert attribute_kind(_dev(template='zz'), reg) is None
    assert attribute_kind(_dev(role='strange', template='m'), reg) == 'strange'


def test_a_broken_device_never_breaks_a_list():
    class Bad:
        def get(self, _):
            raise RuntimeError("boom")
    assert device_kind(_dev(template='m'), Bad()) == ('other', 'none')


# ── the gateway ──────────────────────────────────────────────────────────────

def _stored(cfg, did):
    return copy.deepcopy(next(d for d in yaml.safe_load(cfg.config_path.read_text())["devices"] if d["id"] == did))


def _dev_json(client, did, **params):
    return next(x for x in client.get("/api/devices", params=params).json()["devices"] if x["id"] == did)


@needs_tc
def test_devices_carry_their_kind_and_how_it_is_known(tmp_path):
    _cfg, client = make_app(tmp_path, DEVICES + PLANT.replace("endpoints:", "endpoints:", 1))
    got = {d["id"]: (d["kind"], d["kind_source"]) for d in client.get("/api/devices").json()["devices"]}
    assert got["hp"] == ("heat_pump", "template")
    assert got["rs"] == ("meter", "template")
    assert got["pv-u1"] == ("inverter", "role")
    assert next(v for k, v in got.items() if k.startswith("pv-") and k.endswith("240")) == ("meter", "role")
    assert got["bank-u1"] == ("battery", "template")
    primary = next(d for d in client.get("/api/devices").json()["devices"] if d["primary"])
    assert primary["kind"] == "meter"


@needs_tc
def test_a_component_is_the_kind_its_parent_says(tmp_path):
    cfg, client = make_app(tmp_path, GX_YAML)
    got = {d["id"]: (d["kind"], d["kind_source"]) for d in client.get("/api/devices").json()["devices"]}
    assert got["gx"] == ("site", "template")
    assert got["gx-battery-288"] == ("battery", "component")
    assert got["ruuvi-in"] == ("sensor", "component")
    # an override on a component lives in the component's own entry
    r = client.patch("/api/devices/ruuvi-in/organise", json={"kind": "meter"})
    assert r.status_code == 200 and r.json()["kind_source"] == "override", r.text
    comp = next(c for c in _stored(cfg, "gx")["components"] if c.get("id") == "ruuvi-in")
    assert comp["kind"] == "meter"
    assert _dev_json(client, "ruuvi-in")["kind"] == "meter"


@needs_tc
@pytest.mark.parametrize("kind", OVERRIDABLE)
def test_every_kind_can_be_set_and_null_derives_it_again(tmp_path, kind):
    cfg, client = make_app(tmp_path, DEVICES)
    r = client.patch("/api/devices/udp/organise", json={"kind": kind})
    assert r.status_code == 200 and r.json()["status"] == "updated", r.text
    assert _stored(cfg, "udp")["kind"] == kind
    assert (_dev_json(client, "udp")["kind"], _dev_json(client, "udp")["kind_source"]) == (kind, "override")
    r = client.patch("/api/devices/udp/organise", json={"kind": None})
    assert r.status_code == 200 and "kind" not in _stored(cfg, "udp")
    assert (_dev_json(client, "udp")["kind"], _dev_json(client, "udp")["kind_source"]) == ("meter", "template")


@needs_tc
def test_a_wrong_kind_is_refused_at_its_field_and_nothing_is_written(tmp_path):
    cfg, client = make_app(tmp_path, DEVICES)
    before = _stored(cfg, "udp")
    for body in ({"kind": "toaster"}, {"kind": "other"}, {"kind": 7}, {}):
        r = client.patch("/api/devices/udp/organise", json=body)
        assert r.status_code == 422 and "kind" in r.json()["detail"]["fields"], (body, r.text)
    assert _stored(cfg, "udp") == before
    # an alias is the same kind
    assert client.patch("/api/devices/udp/organise", json={"kind": "System"}).json()["kind"] == "site"


@needs_tc
def test_the_primary_and_a_unit_are_refused_in_words(tmp_path):
    _cfg, client = make_app(tmp_path, PLANT)
    primary = next(d for d in client.get("/api/devices").json()["devices"] if d["primary"])
    r = client.patch(f"/api/devices/{primary['id']}/organise", json={"kind": "ups"})
    assert r.status_code == 422 and "template" in r.json()["detail"]["fields"]["kind"]
    r = client.patch("/api/devices/pv-u1/organise", json={"kind": "ups"})
    assert r.status_code == 422 and "role" in r.json()["detail"]["fields"]["kind"]
    assert client.patch("/api/devices/nope/organise", json={"kind": "ups"}).status_code == 404


@needs_tc
def test_dry_run_and_the_same_kind_change_nothing(tmp_path):
    cfg, client = make_app(tmp_path, DEVICES)
    before = cfg.config_path.read_text()
    r = client.patch("/api/devices/udp/organise?dry_run=true", json={"kind": "ups"})
    assert r.json() == {"status": "valid", "would": "update"}
    assert cfg.config_path.read_text() == before
    r = client.patch("/api/devices/udp/organise", json={"kind": None})       # it has none: nothing to do
    assert r.json()["status"] == "unchanged" and cfg.config_path.read_text() == before


class _Client:
    """A running client, as far as the device list asks it."""
    def get_stats(self):
        return {"connected": True}

    def data_health(self):
        return {"status": "ok"}


@needs_tc
def test_setting_the_kind_never_restarts_the_device_and_an_edit_keeps_it(tmp_path):
    from fastapi.testclient import TestClient

    from multibus.api import create_api
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml=DEVICES)
    running = {d.id: _Client() for d in cfg.devices}
    app, _ = create_api(cfg, None, None, None, devices=[(d, running[d.id]) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    assert client.patch("/api/devices/udp/organise", json={"kind": "ups"}).status_code == 200
    _i, _d, c = app.state.registry.find("udp")
    assert c is running["udp"]                                   # the same client: not restarted
    # the device page's save does not know the kind: it stays
    stored = _stored(cfg, "udp")
    body = {k: v for k, v in stored.items() if k != "kind"}
    r = client.put("/api/devices/udp", json=body)
    assert r.status_code == 200 and r.json()["status"] == "unchanged", r.text
    assert _stored(cfg, "udp")["kind"] == "ups"
    # nor can a PUT that says another kind change it (its own route's)
    r = client.put("/api/devices/udp", json={**body, "name": "Renamed", "kind": "meter"})
    assert r.status_code == 200 and _stored(cfg, "udp")["kind"] == "ups"
    _i, _d, c = app.state.registry.find("udp")
    assert c is running["udp"]                                   # a rename is no restart either


@needs_tc
@pytest.mark.parametrize("bad", ["toaster", 42, "[ups]", "other"])
def test_a_kind_nobody_can_read_is_dropped_never_the_device(tmp_path, bad):
    stored = DEVICES.replace("    name: UDP meter\n", f"    name: UDP meter\n    kind: {bad}\n")
    cfg, client = make_app(tmp_path, stored)
    d = _dev_json(client, "udp")
    assert (d["kind"], d["kind_source"]) == ("meter", "template")
    assert "udp.kind" in cfg.ignored_blocks
    events = client.get("/api/events").json()
    assert any("udp" in str(e.get("message", "")) and "kind" in str(e.get("message", ""))
               for e in (events.get("events") if isinstance(events, dict) else events))


# ── the lists ────────────────────────────────────────────────────────────────

@needs_tc
def test_with_no_parameter_the_list_is_as_before(tmp_path):
    _cfg, client = make_app(tmp_path, DEVICES + PLANT)
    plain = client.get("/api/devices").json()
    blank = client.get("/api/devices", params={"q": "", "kind": "", "sort": ""}).json()
    assert plain == blank
    assert [d["id"] for d in plain["devices"]][0] == next(d["id"] for d in plain["devices"] if d["primary"])
    t_plain = client.get("/api/device-templates").json()
    assert t_plain == client.get("/api/device-templates", params={"q": ""}).json()


@needs_tc
def test_devices_are_searched_filtered_and_sorted(tmp_path):
    _cfg, client = make_app(tmp_path, DEVICES + PLANT)

    def ids(**p):
        r = client.get("/api/devices", params=p)
        assert r.status_code == 200, r.text
        return [d["id"] for d in r.json()["devices"]]

    assert ids(q="serial") == ["rs"]
    assert ids(q="192.0.2.30") == ["udp"]
    assert ids(q="samsung") == ["hp"]                       # the template's id and vendor are searched
    assert set(ids(kind="heat_pump,inverter")) == {"hp", "pv-u1", "pv-u2"}
    assert set(ids(protocol="ascii,udp")) == {"asc", "udp"}
    assert set(ids(installation="bank")) == {"bank-u1", "bank-u2"}
    assert set(ids(state="disabled")) >= {"hp", "rs", "asc", "udp"}
    assert "hp" not in ids(state="online,offline")
    assert ids(installation="bank", sort="-name") == sorted(ids(installation="bank"), reverse=True)
    names = [d["name"].lower() for d in client.get("/api/devices", params={"sort": "name"}).json()["devices"]]
    assert names == sorted(names)
    assert ids(q="serial meter") == ["rs"] and ids(q="serial nothere") == []


@needs_tc
def test_a_wrong_filter_is_said_at_its_parameter(tmp_path):
    _cfg, client = make_app(tmp_path, DEVICES)
    for params, field in (({"kind": "toaster"}, "kind"), ({"state": "asleep"}, "state"), ({"sort": "colour"}, "sort")):
        r = client.get("/api/devices", params=params)
        assert r.status_code == 422 and field in r.json()["detail"]["fields"], r.text
    r = client.get("/api/device-templates", params={"origin": "mine"})
    assert r.status_code == 422 and "origin" in r.json()["detail"]["fields"]


@needs_tc
def test_templates_carry_the_device_kind_and_are_filtered(tmp_path):
    _cfg, client = make_app(tmp_path)
    tpls = {t["id"]: t for t in client.get("/api/device-templates").json()["templates"]}
    # the template's own `kind` stays what it was (the wizard groups by it)
    assert tpls["samsung_mim_b19n"].get("kind", "") == ""
    assert tpls["samsung_mim_b19n"]["device_kind"] == "heat_pump"
    assert tpls["samsung_mim_b19n"]["device_kind_source"] == "template"
    assert "kind_source" not in tpls["samsung_mim_b19n"]           # it would read as the source of `kind`
    assert tpls["victron_gx_mqtt"]["device_kind"] == "site" and tpls["victron_gx_mqtt"]["transport"] == "mqtt"
    assert tpls["eastron_sdm630"]["used_by_count"] == len(tpls["eastron_sdm630"]["used_by"])

    def ids(**p):
        r = client.get("/api/device-templates", params=p)
        assert r.status_code == 200, r.text
        return [t["id"] for t in r.json()["templates"]]

    assert "samsung_mim_b19n" in ids(kind="heat_pump") and "eastron_sdm630" not in ids(kind="heat_pump")
    assert set(ids(protocol="mqtt")) >= {"victron_gx_mqtt"} and "eastron_sdm630" not in ids(protocol="mqtt")
    assert "eastron_sdm630" in ids(q="eastron 630")
    assert ids(origin="custom") == []
    vendors = [tpls[i].get("vendor", "").lower() for i in ids(sort="vendor")]
    assert vendors == sorted(vendors)


# ── against the gateway before this change (a frozen golden) ────────────────
# tests/fixtures/kind_parent_golden.json was dumped by the commit before the
# kind existed (33fbd70) from tests/fixtures/kind_prod_shaped.yaml: the device
# list, the template list, what the attribute service knows of each device and
# the `meta` it publishes. Nothing that was there may change; the only new keys
# are the ones named here.

FIX = pathlib.Path(__file__).parent / "fixtures"
# meters: the meter-links lot; meter_options / meters_status: its derived values;
# meter_options_own: the device's own thresholds (the device page sends them back)
NEW_DEVICE_KEYS = {"kind", "kind_source", "meters", "meter_options", "meter_options_own",
                   "meters_status", "running"}
NEW_TEMPLATE_KEYS = {"device_kind", "device_kind_source", "used_by_count"}
# templates bundled since the golden (no device in it uses them)
NEW_TEMPLATE_IDS = {"fronius_solar_api_meter"}
# values changed on purpose since the golden: three bundled Fronius files name
# their maker `manufacturer`, now listed as their vendor (findability review M7)
CHANGED_TEMPLATE_VALUES = {tid: {"vendor": "Fronius"} for tid in
                           ("fronius_solar_api_inverter", "fronius_solar_api_inverter_3p", "fronius_solar_api_site")}
# Delta VFD-EL 0.4.0: its alarm reads a calculated igbt_hot (ALM-1)
CHANGED_TEMPLATE_VALUES["delta_vfd_el"] = {"version": "0.4.0"}


def _prod_app(tmp_path):
    from fastapi.testclient import TestClient

    from multibus.api import create_api
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml=(FIX / "kind_prod_shaped.yaml").read_text())
    app, _ = create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    return cfg, app, TestClient(app, raise_server_exceptions=False)


def _norm(obj, tmp_path):
    return json.loads(json.dumps(obj, sort_keys=True, default=str).replace(str(tmp_path), "<tmp>"))


@needs_tc
def test_the_attributes_and_their_meta_are_byte_identical_to_before(tmp_path):
    golden = json.loads((FIX / "kind_parent_golden.json").read_text())
    cfg, app, _client = _prod_app(tmp_path)
    att = app.state.attributes
    assert _norm(sorted([list(x) for x in att._devices()]), tmp_path) == golden["attributes"]
    assert _norm({d.id: att.meta("device", d.id) for d in cfg.devices}, tmp_path) == golden["meta"]


@needs_tc
def test_the_lists_keep_every_value_they_had_and_add_only_what_is_named(tmp_path):
    golden = json.loads((FIX / "kind_parent_golden.json").read_text())
    _cfg, _app, client = _prod_app(tmp_path)
    for name, url, new in (("devices", "/api/devices", NEW_DEVICE_KEYS),
                           ("templates", "/api/device-templates", NEW_TEMPLATE_KEYS)):
        now = [x for x in _norm(client.get(url).json()[name], tmp_path)
               if name != "templates" or x["id"] not in NEW_TEMPLATE_IDS]
        assert [x["id"] for x in now] == [x["id"] for x in golden[name]]
        for old, cur in zip(golden[name], now):
            assert set(cur) - set(old) <= new, (name, cur["id"], set(cur) - set(old))
            if name == "templates":
                old = {**old, **CHANGED_TEMPLATE_VALUES.get(cur["id"], {})}
            assert {k: cur.get(k) for k in old} == old, (name, cur["id"])


@needs_tc
def test_prod_shaped_kinds(tmp_path):
    _cfg, _app, client = _prod_app(tmp_path)
    got = {d["id"]: (d["kind"], d["kind_source"]) for d in client.get("/api/devices").json()["devices"]}
    assert got["pv-u1"] == ("inverter", "role") and got["pv-u0"] == ("site", "role")
    assert got["pv-u240"] == ("meter", "role") and got["battery-bank-u1"] == ("battery", "template")
    assert got["victron-gx"] == ("site", "template") and got["victron-gx-temperature-22"] == ("sensor", "component")
    assert got["samsung-dvm"] == ("heat_pump", "template") and got["vfd"] == ("drive", "template")
    assert got["generic"] == ("other", "none")
    # hand-edited roles: the list speaks in kinds; a role it cannot read files
    # the unit by its template (the attributes keep the role as written — above)
    assert got["odd-u1"] == ("inverter", "role") and got["odd-u2"] == ("site", "role")
    assert got["odd-u4"] == ("meter", "template")


# ── units read through sources ───────────────────────────────────────────────

@needs_tc
def test_a_unit_read_through_sources_is_found_by_them(tmp_path):
    _cfg, _app, client = _prod_app(tmp_path)

    def ids(**p):
        r = client.get("/api/devices", params=p)
        assert r.status_code == 200, r.text
        return set(x["id"] for x in r.json()["devices"])

    pv = {"pv-u1", "pv-u2", "pv-u3", "pv-u4"}
    assert pv <= ids(q="solar_api") and pv <= ids(q="sunspec")
    assert pv | {"pv-u0"} <= ids(q="192.0.2.40")                 # the datalogger, in the URL and as host
    assert pv <= ids(q="fronius")                                # the source templates' vendor
    # the site unit is read over HTTP only: it is not a TCP device
    assert "pv-u0" not in ids(protocol="tcp") and "pv-u0" in ids(protocol="http")
    assert pv <= ids(protocol="tcp") and pv <= ids(protocol="http")
    assert {"pv-u240", "vfd", "samsung-dvm"} <= ids(protocol="modbus") and "pv-u0" not in ids(protocol="modbus")
    assert {"battery-bank-u1"} <= ids(protocol="rtu_tap")


@needs_tc
def test_a_viewer_never_finds_a_device_by_a_secret_in_its_url(tmp_path):
    from multibus.list_filters import device_search_text
    cfg, _app, _client = _prod_app(tmp_path)
    dev = cfg.get_device("pv-u1")
    src = next(s for s in dev.sources if s.protocol == "http")
    src.http["url"] = "http://user:hunter2@192.0.2.40/x?token=abc123"
    assert "hunter2" in device_search_text(dev, {"id": "pv-u1"})
    shown = device_search_text(dev, {"id": "pv-u1"}, redact=True)
    assert "hunter2" not in shown and "abc123" not in shown


# ── the rest of the contract ─────────────────────────────────────────────────

@needs_tc
def test_filters_read_aliases_and_one_protocol_vocabulary(tmp_path):
    _cfg, _app, client = _prod_app(tmp_path)
    a = client.get("/api/devices", params={"kind": "system"}).json()["devices"]
    assert {d["id"] for d in a} == {"victron-gx", "pv-u0", "odd-u2"}
    assert client.get("/api/devices", params={"kind": "heat-pump"}).status_code == 200
    for url in ("/api/devices", "/api/device-templates"):
        r = client.get(url, params={"protocol": "carrier-pigeon"})
        assert r.status_code == 422 and "protocol" in r.json()["detail"]["fields"], r.text
    # a device protocol on templates means the transport it is read by
    tcp = {t["id"] for t in client.get("/api/device-templates", params={"protocol": "tcp"}).json()["templates"]}
    assert "eastron_sdm630" in tcp and "victron_gx_mqtt" not in tcp


@needs_tc
def test_sort_is_strict_and_ties_stay_in_name_order(tmp_path):
    _cfg, _app, client = _prod_app(tmp_path)
    for bad in ("--name", "name-", "-", "na me"):
        r = client.get("/api/devices", params={"sort": bad})
        assert r.status_code == 422 and "sort" in r.json()["detail"]["fields"], bad
    rows = client.get("/api/devices", params={"sort": "-kind"}).json()["devices"]
    kinds = [d["kind"] for d in rows]
    assert kinds == sorted(kinds, reverse=True)
    for k in set(kinds):
        names = [d["name"].lower() for d in rows if d["kind"] == k]
        assert names == sorted(names), k                         # ascending, though the kind is descending


@needs_tc
def test_oversized_input_is_refused_in_words(tmp_path):
    _cfg, _app, client = _prod_app(tmp_path)
    r = client.get("/api/devices", params={"q": "x" * 201})
    assert r.status_code == 422 and "q" in r.json()["detail"]["fields"]
    r = client.get("/api/devices", params={"installation": ",".join(f"i{n}" for n in range(21))})
    assert r.status_code == 422 and "installation" in r.json()["detail"]["fields"]
    r = client.get("/api/devices", params={"kind": ",".join(["toaster"] * 10)})
    assert len(r.json()["detail"]["fields"]["kind"]) < 300            # the message does not echo everything


@needs_tc
def test_a_stored_kind_nobody_can_read_is_cleared_with_null(tmp_path):
    stored = DEVICES.replace("    name: UDP meter\n", "    name: UDP meter\n    kind: toaster\n")
    cfg, client = make_app(tmp_path, stored)
    r = client.patch("/api/devices/udp/organise", json={"kind": None})
    assert r.status_code == 200 and r.json()["status"] == "updated", r.text
    assert "kind" not in _stored(cfg, "udp")
    r = client.patch("/api/devices/udp/organise", json={"kind": None})
    assert r.json()["status"] == "unchanged"


@needs_tc
def test_a_restored_device_keeps_its_kind(tmp_path):
    cfg, client = make_app(tmp_path, DEVICES)
    assert client.patch("/api/devices/udp/organise", json={"kind": "ups"}).status_code == 200
    assert client.delete("/api/devices/udp").status_code == 200
    r = client.post("/api/devices/udp/restore")
    assert r.status_code == 200, r.text
    assert (_dev_json(client, "udp")["kind"], _dev_json(client, "udp")["kind_source"]) == ("ups", "override")


@needs_tc
def test_a_new_device_may_say_its_kind(tmp_path):
    cfg, client = make_app(tmp_path)
    body = {"id": "ups1", "name": "UPS", "template": "eastron_sdm630",
            "connection": {"protocol": "tcp", "host": "192.0.2.91", "port": 502, "unit_id": 1}}
    r = client.post("/api/devices", json={**body, "kind": "toaster"})
    assert r.status_code == 422 and "kind" in r.json()["detail"]["fields"]
    r = client.post("/api/devices?dry_run=true", json={**body, "kind": "UPS"})
    assert r.status_code == 200 and cfg.get_device("ups1") is None
    r = client.post("/api/devices", json={**body, "kind": "UPS"})
    assert r.status_code == 200, r.text
    assert _stored(cfg, "ups1")["kind"] == "ups" and _dev_json(client, "ups1")["kind_source"] == "override"
