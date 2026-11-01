# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""ADR-0027 (lot L2): one device-type catalog linked to templates, and a
device's identity as attributes.

- the catalog: built-ins and user types, served, changed by an admin, never
  deleted while used (a tombstoned device, a template, a group included);
- a device has a type; the wire `kind` stays built-in (a user type's
  `behaves_like`), and is unchanged for every device that existed before;
- templates declare a kind and may list user types; the `type` filter;
- identity (description, location, labels, nameplate) as an attribute,
  checked, published in meta, the serial number held back by default;
- a device created with its attributes, all or nothing;
- group roles checked against the catalog.
"""
import copy
import json
import pathlib
from types import SimpleNamespace

import pytest
import yaml

from multibus import attribute_catalog as cat
from multibus import device_types as dtypes
from multibus.device_kind import KINDS, attribute_kind, device_kind
from tests.test_devices_api import make_app, needs_tc
from tests.test_unchanged_save import DEVICES, PLANT

FIX = pathlib.Path(__file__).parent / "fixtures"
TPL_DIR = pathlib.Path(__file__).parent.parent / "multibus" / "device_templates"

TYPES = """
device_types:
  - id: chiller
    labels: {en: Chiller, ro: Răcitor}
    icon: snow
    behaves_like: heat_pump
  - id: microinv
    labels: {en: Micro-inverter, ro: Microinvertor}
    icon: sun
    behaves_like: inverter
  - id: fancoil
    labels: {en: Fan coil}
    icon: fan
"""

UDP = {"id": "new1", "name": "New", "template": "eastron_sdm630",
       "connection": {"protocol": "tcp", "host": "192.0.2.91", "port": 502, "unit_id": 1}}


@pytest.fixture(autouse=True)
def _no_user_types_left_behind():
    yield
    dtypes.set_user_types([])


def _stored_types(cfg):
    return yaml.safe_load(cfg.config_path.read_text()).get("device_types")


def _stored(cfg, did):
    return copy.deepcopy(next(d for d in yaml.safe_load(cfg.config_path.read_text())["devices"] if d["id"] == did))


def _dev(client, did):
    return next(x for x in client.get("/api/devices").json()["devices"] if x["id"] == did)


# ── 1. the catalog ───────────────────────────────────────────────────────────

@needs_tc
def test_the_catalog_serves_builtins_and_user_types(tmp_path):
    _cfg, client = make_app(tmp_path, TYPES)
    r = client.get("/api/device-types")
    assert r.status_code == 200, r.text
    body = r.json()
    types = {t["id"]: t for t in body["types"]}
    assert [t["id"] for t in body["types"] if t["builtin"]] == list(KINDS)
    assert "converter" in KINDS and types["converter"]["labels"]["en"]
    assert types["meter"]["templates"] > 0 and types["meter"]["icon_class"] == "bi-speedometer2"
    assert types["chiller"] == {**types["chiller"], "builtin": False, "behaves_like": "heat_pump",
                                "icon": "snow", "icon_class": "bi-snow",
                                "labels": {"en": "Chiller", "ro": "Răcitor"}}
    # a chiller is offered the heat pump maps
    assert types["chiller"]["templates"] == types["heat_pump"]["templates"] >= 1
    assert types["fancoil"]["behaves_like"] == "other" and types["fancoil"]["icon"] == "fan"
    assert "rating" in types["inverter"]["recommended_attributes"]
    assert "rating" in types["microinv"]["recommended_attributes"]
    assert set(body["icons"]) >= set(KINDS) and "fan" in body["icons"]


@needs_tc
def test_a_user_type_is_added_changed_and_deleted_by_an_admin(tmp_path):
    cfg, client = make_app(tmp_path)
    r = client.post("/api/device-types", json={"id": "boiler", "labels": {"en": "Boiler", "ro": "Boiler"},
                                               "icon": "fire", "behaves_like": "other"})
    assert r.status_code == 200, r.text
    assert r.json()["type"]["id"] == "boiler"
    assert _stored_types(cfg) == [{"id": "boiler", "labels": {"en": "Boiler", "ro": "Boiler"},
                                   "icon": "fire", "behaves_like": "other"}]
    r = client.put("/api/device-types/boiler", json={"labels": {"en": "Water boiler", "ro": "Boiler de apă"},
                                                     "icon": "water", "behaves_like": "heat_pump"})
    assert r.status_code == 200, r.text
    got = {t["id"]: t for t in client.get("/api/device-types").json()["types"]}["boiler"]
    assert got["labels"]["ro"] == "Boiler de apă" and got["behaves_like"] == "heat_pump"
    assert client.put("/api/device-types/nope", json={"labels": {"en": "x"}}).status_code == 404
    assert client.put("/api/device-types/meter", json={"labels": {"en": "x"}}).status_code == 422
    assert client.delete("/api/device-types/boiler").status_code == 200
    assert not _stored_types(cfg)
    assert client.delete("/api/device-types/boiler").status_code == 404


@needs_tc
@pytest.mark.parametrize("body,field", [
    ({"id": "meter", "labels": {"en": "M"}}, "id"),
    ({"id": "system", "labels": {"en": "M"}}, "id"),                # an alias of site
    ({"id": "Bad Id", "labels": {"en": "M"}}, "id"),
    ({"id": "x1", "labels": {"ro": "Doar ro"}}, "labels.en"),
    ({"id": "x1", "labels": {"en": "x" * 41}}, "labels.en"),
    ({"id": "x1", "labels": {"en": "M"}, "icon": "unicorn"}, "icon"),
    ({"id": "x1", "labels": {"en": "M"}, "behaves_like": "toaster"}, "behaves_like"),
    ({"id": "chiller", "labels": {"en": "Again"}}, "id"),
])
def test_a_type_that_clashes_or_is_malformed_is_refused_at_its_field(tmp_path, body, field):
    cfg, client = make_app(tmp_path, TYPES)
    before = cfg.config_path.read_text()
    r = client.post("/api/device-types", json=body)
    assert r.status_code == 422, r.text
    assert field in r.json()["detail"]["fields"], r.json()
    assert cfg.config_path.read_text() == before


@needs_tc
def test_operator_and_viewer_cannot_change_the_catalog(tmp_path):
    from fastapi.testclient import TestClient
    from tests.test_attributes import _api
    _cfg, app, _client = _api(tmp_path)                      # login on
    st = app.state.ctx.service_tokens
    anon = TestClient(app, raise_server_exceptions=False)
    r = anon.post("/api/device-types", json={"id": "boiler", "labels": {"en": "B"}},
                  headers={"Authorization": f"Bearer {st.create('x-admin', 'admin')}"})
    assert r.status_code == 200, r.text                        # how an EMS adds one
    for role in ("viewer", "operator"):
        h = {"Authorization": f"Bearer {st.create(f'x-{role}', role)}"}
        assert anon.get("/api/device-types", headers=h).status_code == 200
        r = anon.post("/api/device-types", json={"id": "boiler2", "labels": {"en": "B"}}, headers=h)
        assert r.status_code in (401, 403), (role, r.status_code)


@needs_tc
def test_a_type_in_use_is_not_deleted_and_the_users_are_named(tmp_path):
    cfg, client = make_app(tmp_path, TYPES + DEVICES + PLANT)
    # a device
    assert client.patch("/api/devices/udp/organise", json={"type": "chiller"}).status_code == 200
    r = client.delete("/api/device-types/chiller")
    assert r.status_code == 409, r.text
    assert "udp" in r.json()["detail"]["devices"]
    # a tombstoned device still holds it (it can be restored)
    assert client.delete("/api/devices/udp").status_code == 200
    r = client.delete("/api/device-types/chiller")
    assert r.status_code == 409 and "udp" in r.json()["detail"]["devices"], r.text
    assert client.delete("/api/devices/restorable/udp").status_code == 200
    # a template that lists it
    tpl = json.loads((TPL_DIR / "eastron_sdm120.json").read_text())
    tpl["device_template"].update(id="my_chiller_meter", name="Chiller meter", types=["chiller"])
    r = client.post("/api/device-templates", json={**tpl, "create": True})
    assert r.status_code == 200, r.text
    r = client.delete("/api/device-types/chiller")
    assert r.status_code == 409 and r.json()["detail"]["templates"] == ["my_chiller_meter"], r.text
    assert client.delete("/api/device-templates/my_chiller_meter").status_code == 200
    # an installation group whose role it is
    ep = client.get("/api/endpoints/pv").json()
    groups = [dict(g) for g in json.loads(json.dumps(_raw_groups(cfg, "pv")))]
    groups[0]["role"] = "microinv"
    r = client.put("/api/endpoints/pv", json={"name": ep.get("name", "PV"), "groups": groups})
    assert r.status_code == 200, r.text
    r = client.delete("/api/device-types/microinv")
    assert r.status_code == 409 and r.json()["detail"]["groups"] == ["pv/inverters"], r.text
    assert client.delete("/api/device-types/chiller").status_code == 200


def _raw_groups(cfg, pid):
    return next(p for p in yaml.safe_load(cfg.config_path.read_text())["endpoints"] if p["id"] == pid)["groups"]


# ── 2. a device has a type; the wire kind stays built-in ────────────────────

@needs_tc
def test_the_wire_kind_of_a_user_type_is_its_behaves_like(tmp_path):
    cfg, client = make_app(tmp_path, TYPES + DEVICES)
    r = client.patch("/api/devices/udp/organise", json={"type": "microinv"})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "inverter" and r.json()["type"] == "microinv"
    assert _stored(cfg, "udp")["kind"] == "microinv"            # the stored key stays `kind`
    d = _dev(client, "udp")
    assert (d["kind"], d["kind_source"], d["type"], d["type_label"]) == \
        ("inverter", "override", "microinv", "Micro-inverter")
    m = client.app.state.attributes.meta("device", "udp")
    assert (m["kind"], m["type"], m["type_label"]) == ("inverter", "microinv", "Micro-inverter")
    # behaves like `other`: the wire says other
    assert client.patch("/api/devices/udp/organise", json={"type": "fancoil"}).status_code == 200
    d = _dev(client, "udp")
    assert (d["kind"], d["type"]) == ("other", "fancoil")
    # `kind` is still accepted, and reads the same catalog
    assert client.patch("/api/devices/udp/organise", json={"kind": "chiller"}).json()["type"] == "chiller"
    r = client.patch("/api/devices/udp/organise", json={"type": "toaster"})
    assert r.status_code == 422 and "type" in r.json()["detail"]["fields"]
    r = client.patch("/api/devices/udp/organise", json={"type": "ups", "kind": "meter"})
    assert r.status_code == 422


def test_the_derivation_reads_user_types():
    dtypes.set_user_types(yaml.safe_load(TYPES)["device_types"])

    class Reg:
        def get(self, _):
            return SimpleNamespace(id="m", kind="meter", registers=[], calculated=[])
    dev = SimpleNamespace(kind="microinv", role="", template="m", sources=[], component_of="")
    assert device_kind(dev, Reg()) == ("inverter", "override")
    assert dtypes.device_type(dev, Reg()) == "microinv"
    assert attribute_kind(dev, Reg()) == "inverter"
    unit = SimpleNamespace(kind="", role="chiller", template="m", sources=[], component_of="")
    assert device_kind(unit, Reg()) == ("heat_pump", "role")
    assert dtypes.device_type(unit, Reg()) == "chiller"
    # a role naming a user type: its behaves_like, and only a kind meta has
    # always published (a heat pump never was one: null, as for its template)
    assert attribute_kind(unit, Reg()) is None
    mi = SimpleNamespace(kind="", role="microinv", template="m", sources=[], component_of="")
    assert attribute_kind(mi, Reg()) == "inverter"
    plain =SimpleNamespace(kind="", role="", template="m", sources=[], component_of="")
    assert dtypes.device_type(plain, Reg()) == "meter"


@needs_tc
def test_a_stored_kind_reads_as_the_type_and_a_user_type_survives_a_reload(tmp_path):
    stored = DEVICES.replace("    name: UDP meter\n", "    name: UDP meter\n    kind: chiller\n") \
                    .replace("    name: ASCII meter\n", "    name: ASCII meter\n    kind: ups\n")
    cfg, client = make_app(tmp_path, TYPES + stored)
    assert (_dev(client, "udp")["type"], _dev(client, "udp")["kind"]) == ("chiller", "heat_pump")
    assert (_dev(client, "asc")["type"], _dev(client, "asc")["kind"]) == ("ups", "ups")
    assert "udp.kind" not in (getattr(cfg, "ignored_blocks", {}) or {})
    # a save keeps the catalog and the stored kinds as they were
    cfg.save_yaml_config()
    assert [t["id"] for t in _stored_types(cfg)] == ["chiller", "microinv", "fancoil"]
    assert _stored(cfg, "udp")["kind"] == "chiller"


@needs_tc
def test_a_new_device_may_say_its_type(tmp_path):
    cfg, client = make_app(tmp_path, TYPES)
    r = client.post("/api/devices", json={**UDP, "type": "microinv"})
    assert r.status_code == 200, r.text
    assert _stored(cfg, "new1")["kind"] == "microinv" and _dev(client, "new1")["type"] == "microinv"
    r = client.post("/api/devices", json={**UDP, "id": "new2", "type": "toaster"})
    assert r.status_code == 422 and "type" in r.json()["detail"]["fields"]


@needs_tc
def test_a_meter_only_device_may_be_a_user_type_that_is_not_a_meter(tmp_path):
    from tests.test_meter_links import IN, OUT, _devices
    _cfg, client = make_app(tmp_path, TYPES)
    _devices(tmp_path, client, ids=("em-in", "em-out"))
    body = {"id": "boil", "name": "Boiler", "connection": {"protocol": "none"}, "meters": [IN, OUT]}
    r = client.post("/api/devices", json={**body, "type": "fancoil"})
    assert r.status_code == 200, r.text
    assert _dev(client, "boil")["type"] == "fancoil"
    r = client.post("/api/devices", json={**body, "id": "boil2", "type": "meter"})
    assert r.status_code == 422


# ── the wire kind of everything that existed stays the same ─────────────────

def test_every_bundled_template_gives_the_wire_kinds_it_gave_before():
    """tests/fixtures/l2_wire_kinds_golden.json: (device kind, source,
    attribute/meta kind) of a device made from each bundled template, dumped
    by 912a192, the commit before this lot."""
    from multibus.device_template import load_template
    golden = json.loads((FIX / "l2_wire_kinds_golden.json").read_text())
    tpls = {t.id: t for t in (load_template(str(p), builtin=True) for p in sorted(TPL_DIR.glob("*.json")))}

    class Reg:
        def get(self, tid):
            return tpls.get(tid)
    assert set(golden) <= set(tpls)
    for tid, want in golden.items():
        dev = SimpleNamespace(kind="", role="", template=tid, sources=[], component_of="")
        assert [*device_kind(dev, Reg()), attribute_kind(dev, Reg())] == want, tid


@needs_tc
def test_the_prod_shaped_gateway_publishes_the_same_wire_kinds(tmp_path):
    from tests.test_device_kind import _prod_app
    golden = json.loads((FIX / "kind_parent_golden.json").read_text())
    cfg, app, client = _prod_app(tmp_path)
    att = app.state.attributes
    for d in cfg.devices:
        m = att.meta("device", d.id)
        assert m.get("kind") == golden["meta"][d.id].get("kind"), d.id
        assert m["type"] and m["type_label"], d.id
    old = {x["id"]: x["kind"] for x in golden["devices"] if "kind" in x}
    now = {x["id"]: x["kind"] for x in client.get("/api/devices").json()["devices"]}
    for did, k in old.items():
        assert now[did] == k, did


# ── 3. templates linked to types ────────────────────────────────────────────

def test_every_bundled_template_declares_its_kind():
    for p in sorted(TPL_DIR.glob("*.json")):
        t = json.loads(p.read_text())["device_template"]
        assert t.get("kind") in KINDS, p.name


@needs_tc
def test_the_type_filter_returns_the_templates_for_the_type(tmp_path):
    _cfg, client = make_app(tmp_path, TYPES)

    def ids(**p):
        r = client.get("/api/device-templates", params=p)
        assert r.status_code == 200, r.text
        return [t["id"] for t in r.json()["templates"]]
    meters = ids(type="meter")
    assert "eastron_sdm630" in meters and "huawei_sun2000" not in meters
    assert ids(type="chiller") == ids(type="heat_pump") and "samsung_mim_b19n" in ids(type="chiller")
    assert set(ids(type="microinv")) == set(ids(type="inverter"))
    tpl = json.loads((TPL_DIR / "eastron_sdm120.json").read_text())
    tpl["device_template"].update(id="chiller_meter", name="Chiller meter", types=["chiller"])
    assert client.post("/api/device-templates", json={**tpl, "create": True}).status_code == 200
    assert "chiller_meter" in ids(type="chiller") and "chiller_meter" in ids(type="meter")
    assert "chiller_meter" not in ids(type="heat_pump")
    got = client.get("/api/device-templates/chiller_meter").json()["device_template"]
    assert got["types"] == ["chiller"]
    r = client.get("/api/device-templates", params={"type": "toaster"})
    assert r.status_code == 422 and "type" in r.json()["detail"]["fields"]


def test_a_templates_types_are_slugs_and_its_kind_any_builtin():
    from multibus.device_template import validate_template
    base = json.loads((TPL_DIR / "eastron_sdm120.json").read_text())
    good = copy.deepcopy(base)
    good["device_template"].update(kind="drive", types=["chiller", "fan_coil"])
    assert validate_template(good) == []
    for bad in (["Bad Type"], "chiller", [1], ["x"] * 33):
        b = copy.deepcopy(base)
        b["device_template"]["types"] = bad
        assert any("types" in e for e in validate_template(b)), bad
    b = copy.deepcopy(base)
    b["device_template"]["kind"] = "toaster"
    assert any("kind" in e for e in validate_template(b))


# ── 4. identity is an attribute ─────────────────────────────────────────────

def test_identity_is_checked_lengths_label_slugs_and_the_date():
    ok, errs, _w = cat.validate("identity", {
        "description": "Pump room heat pump.\nServiced yearly.", "location": "garage, cabinet 2",
        "labels": ["hvac", "floor-1", "hvac"], "manufacturer": "Samsung", "model": "AE160",
        "serial_number": "SN-0042", "firmware": "1.2.3", "installed_on": "2024-05-17"}, level="device")
    assert errs == [], errs
    assert ok["labels"] == ["hvac", "floor-1"]                           # once each, in order
    assert cat.validate("identity", {"location": "roof"}, level="installation")[1] == []
    for value, field in (({"description": "x" * 501}, "description"),
                         ({"location": "x" * 121}, "location"),
                         ({"labels": ["Not A Slug"]}, "labels"),
                         ({"labels": "hvac"}, "labels"),
                         ({"labels": [f"l{i}" for i in range(17)]}, "labels"),
                         ({"labels": ["x" * 33]}, "labels"),
                         ({"installed_on": "2024-02-30"}, "installed_on"),
                         ({"installed_on": "17.05.2024"}, "installed_on"),
                         ({"serial_number": "a​b"}, "serial_number"),
                         ({"colour": "red"}, "colour")):
        _ok, errs, _w = cat.validate("identity", value, level="device")
        assert any(f"identity.{field}" in e for e in errs), (value, errs)
    assert cat.validate("identity", {"location": "roof"}, level="site")[1]       # not on the plant


def test_recommended_attributes_follow_the_builtin_kind():
    assert "rating" in cat.recommended_for("inverter")
    assert "battery" in cat.recommended_for("battery")
    assert cat.recommended_for("other") == []
    assert cat.catalog_view()["categories"]["rating"]["recommended_for"]


@needs_tc
def test_meta_carries_the_type_its_label_and_identity_without_the_serial(tmp_path):
    cfg, client = make_app(tmp_path, TYPES + DEVICES)
    ident = {"description": "Main meter", "location": "cabinet 2", "labels": ["grid", "main"],
             "manufacturer": "Fronius", "model": "Smart Meter 65A", "serial_number": "SN123",
             "installed_on": "2023-03-01"}
    r = client.put("/api/attributes/devices/udp/identity", json=ident)
    assert r.status_code == 200, r.text
    att = client.app.state.attributes
    m = att.meta("device", "udp")
    assert m["type"] == "meter" and m["type_label"] == "Meter"
    assert m["attributes"]["identity"] == {k: v for k, v in ident.items() if k != "serial_number"}
    assert "identity.serial_number" not in att.flat("device", "udp")       # OPC UA neither
    # a signed-in reader sees it
    assert client.get("/api/attributes/devices/udp").json()["attributes"]["identity"]["serial_number"] == "SN123"
    # published only when the setting says so
    r = client.put("/api/attributes/settings", json={"publish_serials": True})
    assert r.status_code == 200, r.text
    assert att.meta("device", "udp")["attributes"]["identity"]["serial_number"] == "SN123"
    assert client.put("/api/attributes/settings", json={"publish_serials": "yes"}).status_code == 422
    assert client.get("/api/attributes").json()["settings"]["publish_serials"] is True


@needs_tc
def test_labels_and_types_are_filtered_in_the_device_list(tmp_path):
    cfg, client = make_app(tmp_path, TYPES + DEVICES)
    assert client.put("/api/attributes/devices/udp/identity", json={"labels": ["grid", "main"]}).status_code == 200
    assert client.put("/api/attributes/devices/rs/identity", json={"labels": ["lab"]}).status_code == 200
    assert client.patch("/api/devices/asc/organise", json={"type": "chiller"}).status_code == 200

    def ids(**p):
        r = client.get("/api/devices", params=p)
        assert r.status_code == 200, r.text
        return {d["id"] for d in r.json()["devices"]}
    assert ids(label="grid") == {"udp"} and ids(label="grid,lab") == {"udp", "rs"}
    assert ids(type="chiller") == {"asc"} and "asc" not in ids(type="heat_pump")
    assert _dev(client, "udp")["labels"] == ["grid", "main"]
    assert "labels" not in _dev(client, "hp")
    assert "udp" in ids(q="main")                                     # a label is searched too
    assert client.get("/api/devices", params={"type": "toaster"}).status_code == 422


@needs_tc
def test_create_with_attributes_is_all_or_nothing(tmp_path):
    cfg, client = make_app(tmp_path, TYPES)
    bad = {**UDP, "attributes": {"identity": {"labels": ["Not A Slug"]}, "rating": {"rated_ac_kw": 5}}}
    r = client.post("/api/devices", json=bad)
    assert r.status_code == 422, r.text
    assert any("identity.labels" in e for e in r.json()["detail"]["errors"])
    assert cfg.get_device("new1") is None
    assert "new1" not in (client.get("/api/attributes").json()["device"])
    r = client.post("/api/devices", json={**UDP, "attributes": {"nope": {}}})
    assert r.status_code == 422 and cfg.get_device("new1") is None
    r = client.post("/api/devices", json={**UDP, "attributes": "x"})
    assert r.status_code == 422 and cfg.get_device("new1") is None
    good = {**UDP, "attributes": {"identity": {"location": "garage", "labels": ["lab"]},
                                  "grid_connection": {"role": "sub_meter"}}}
    r = client.post("/api/devices?dry_run=true", json=good)
    assert r.status_code == 200 and cfg.get_device("new1") is None
    assert "new1" not in client.get("/api/attributes").json()["device"]
    r = client.post("/api/devices", json=good)
    assert r.status_code == 200, r.text
    held = client.get("/api/attributes/devices/new1").json()["attributes"]
    assert held == {"identity": {"location": "garage", "labels": ["lab"]}, "grid_connection": {"role": "sub_meter"}}


# ── group roles from the catalog ────────────────────────────────────────────

@needs_tc
def test_group_roles_are_checked_against_the_catalog(tmp_path):
    cfg, client = make_app(tmp_path, TYPES + PLANT)
    groups = _raw_groups(cfg, "pv")

    def put(role):
        g = copy.deepcopy(groups)
        g[0]["role"] = role
        return client.put("/api/endpoints/pv", json={"name": "PV", "groups": g})
    r = put("toaster")
    assert r.status_code == 422 and "groups[0].role" in json.dumps(r.json()), r.text
    assert put("other").status_code == 422
    assert put("System").status_code == 422                       # stored as the catalog says it, or not at all
    assert put("microinv").status_code == 200
    assert _raw_groups(cfg, "pv")[0]["role"] == "microinv"
    unit = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "pv-u1")
    assert (unit["kind"], unit["type"]) == ("inverter", "microinv")
    assert put("converter").status_code == 200


@needs_tc
def test_a_hand_written_role_already_stored_is_kept_by_an_unrelated_edit(tmp_path):
    cfg, client = make_app(tmp_path, PLANT.replace("role: meter", "role: grid"))
    groups = _raw_groups(cfg, "pv")
    r = client.put("/api/endpoints/pv", json={"name": "PV renamed", "groups": groups})
    assert r.status_code == 200, r.text
    assert _raw_groups(cfg, "pv")[1]["role"] == "grid"


# ── snapshots carry the catalog (it lives in config.yaml) ───────────────────

@needs_tc
def test_a_snapshot_restore_brings_the_user_types_back(tmp_path):
    cfg, client = make_app(tmp_path, TYPES)
    store = client.app.state.snapshots if hasattr(client.app.state, "snapshots") else None
    raw = cfg.config_path.read_text()
    assert "device_types:" in raw
    assert client.delete("/api/device-types/fancoil").status_code == 200
    cfg.config_path.write_text(raw)
    cfg.load()
    assert "fancoil" in {t["id"] for t in client.get("/api/device-types").json()["types"]}
    del store


# ── review fixes (lot L2, second review) ────────────────────────────────────

FLAT = """
endpoints:
  - id: flat
    name: Flat
    enabled: false
    template: eastron_sdm630
    role: tankx
    connection: {protocol: tcp, host: 192.0.2.50, port: 502}
    units: [1, 2]
"""


@needs_tc
def test_a_type_id_already_used_as_a_role_is_refused_and_where_is_named(tmp_path):
    from tests.test_device_kind import _prod_app
    _cfg, _app, client = _prod_app(tmp_path)                    # holds the hand-written role `grid`
    r = client.post("/api/device-types", json={"id": "grid", "labels": {"en": "Grid"}})
    assert r.status_code == 422, r.text
    assert "odd/d" in r.json()["detail"]["fields"]["id"], r.json()
    (tmp_path / "flat").mkdir()
    _cfg, client = make_app(tmp_path / "flat", FLAT)
    r = client.post("/api/device-types", json={"id": "tankx", "labels": {"en": "Tank"}})
    assert r.status_code == 422 and "flat" in r.json()["detail"]["fields"]["id"], r.text


@needs_tc
def test_meta_says_the_new_kind_at_once_after_a_type_change(tmp_path):
    _cfg, client = make_app(tmp_path, TYPES + DEVICES)
    att = client.app.state.attributes
    assert att.meta("device", "udp")["kind"] == "meter"           # warm the cached map
    assert client.patch("/api/devices/udp/organise", json={"type": "microinv"}).status_code == 200
    assert att.meta("device", "udp")["kind"] == "inverter"
    r = client.put("/api/device-types/microinv", json={"behaves_like": "battery"})
    assert r.status_code == 200, r.text
    assert att.meta("device", "udp")["kind"] == "battery"


@needs_tc
def test_a_type_held_only_in_a_raw_entry_or_a_component_is_in_use(tmp_path):
    from tests.test_device_components import GX_YAML
    cfg, client = make_app(tmp_path, TYPES + GX_YAML)
    assert client.patch("/api/devices/ruuvi-in/organise", json={"type": "fancoil"}).status_code == 200
    # the component's own entry holds it; dropped from the built list, the raw entry still does
    cfg.devices = [d for d in cfg.devices if d.id != "ruuvi-in"]
    r = client.delete("/api/device-types/fancoil")
    assert r.status_code == 409 and "ruuvi-in" in r.json()["detail"]["devices"], r.text


def test_the_meter_only_model_says_the_types_words_never_its_id():
    dtypes.set_user_types(yaml.safe_load(TYPES)["device_types"])
    assert dtypes.meter_only_model(SimpleNamespace(kind="fancoil", protocol="none")) == "Meter-only (Fan coil)"
    assert dtypes.meter_only_model(SimpleNamespace(kind="ups", protocol="none")) == "Meter-only (UPS)"
    assert dtypes.meter_only_model(SimpleNamespace(kind="", protocol="none")) == "Meter-only (device)"


@needs_tc
def test_a_type_a_meter_only_device_holds_cannot_become_a_meter(tmp_path):
    from tests.test_meter_links import IN, OUT, _devices
    _cfg, client = make_app(tmp_path, TYPES)
    _devices(tmp_path, client, ids=("em-in", "em-out"))
    r = client.post("/api/devices", json={"id": "boil", "name": "Boiler", "connection": {"protocol": "none"},
                                          "meters": [IN, OUT], "type": "fancoil"})
    assert r.status_code == 200, r.text
    r = client.put("/api/device-types/fancoil", json={"behaves_like": "meter"})
    assert r.status_code == 409 and "boil" in json.dumps(r.json()), r.text
    assert client.put("/api/device-types/fancoil", json={"behaves_like": "ups"}).status_code == 200


def test_a_role_is_read_as_its_builtin_kind():
    dtypes.set_user_types(yaml.safe_load(TYPES)["device_types"])
    assert dtypes.role_is(SimpleNamespace(role="microinv"), "inverter")
    assert dtypes.role_is(SimpleNamespace(role="inverter"), "inverter")
    assert not dtypes.role_is(SimpleNamespace(role="chiller"), "inverter")


@needs_tc
def test_a_failed_create_leaves_an_older_deleted_device_restorable(tmp_path):
    cfg, client = make_app(tmp_path, TYPES)
    assert client.post("/api/devices", json=UDP).status_code == 200
    assert client.delete("/api/devices/new1").status_code == 200
    assert "new1" in {x["id"] for x in cfg.list_deleted_devices()}

    def boom(*a, **kw):
        raise OSError("disk full")
    client.app.state.attributes.put_many = boom
    r = client.post("/api/devices", json={**UDP, "name": "Again", "attributes": {"identity": {"location": "x"}}})
    assert r.status_code == 503, r.text
    assert cfg.get_device("new1") is None
    assert "new1" not in {d.get("id") for d in yaml.safe_load(cfg.config_path.read_text()).get("devices") or []}
    kept = cfg.load_deleted_device("new1")
    assert kept and kept.get("name") == "New"                    # the older device, untouched


@needs_tc
def test_identity_location_follows_the_location_switch_and_labels_join_for_opcua(tmp_path):
    _cfg, client = make_app(tmp_path, DEVICES)
    att = client.app.state.attributes
    assert client.put("/api/attributes/devices/udp/identity",
                      json={"location": "garage", "labels": ["grid", "main"]}).status_code == 200
    assert att.meta("device", "udp")["attributes"]["identity"]["location"] == "garage"
    assert att.flat("device", "udp")["identity.labels"] == "grid, main"
    assert client.put("/api/attributes/settings", json={"location_publish": "off"}).status_code == 200
    ident = att.meta("device", "udp")["attributes"]["identity"]
    assert "location" not in ident and ident["labels"] == ["grid", "main"]
    assert "identity.location" not in att.flat("device", "udp")


@needs_tc
def test_create_with_attributes_audits_the_user(tmp_path):
    from tests.test_attributes import _api, _login
    cfg, _app, client = _api(tmp_path)
    _login(client, "admin", "pw")
    r = client.post("/api/devices", json={**UDP, "attributes": {"identity": {"location": "garage"}}})
    assert r.status_code == 200, r.text
    lines = [json.loads(x) for x in (cfg.config_path.parent / "audit.jsonl").read_text().splitlines() if x.strip()]
    mine = [x for x in lines if x.get("action") == "attributes set" and "new1" in str(x.get("target"))]
    assert mine and mine[-1]["user"] == "admin", mine


@needs_tc
def test_a_flat_installations_role_is_checked_when_it_changes_and_kept_otherwise(tmp_path):
    cfg, client = make_app(tmp_path, FLAT)

    def stored():
        return next(p for p in yaml.safe_load(cfg.config_path.read_text())["endpoints"] if p["id"] == "flat")
    assert client.put("/api/endpoints/flat", json={"name": "Flat renamed"}).status_code == 200
    assert stored().get("role") == "tankx"                        # hand-written, kept
    r = client.put("/api/endpoints/flat", json={"name": "Flat", "role": "toaster"})
    assert r.status_code == 422 and "role" in json.dumps(r.json()), r.text
    assert client.put("/api/endpoints/flat", json={"name": "Flat", "role": "meter"}).status_code == 200
    assert stored()["role"] == "meter"
