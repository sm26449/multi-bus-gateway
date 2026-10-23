# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""A device's meters, each in a role (meter_links.py): a UPS with a meter on
its input and one on its output, a heat pump with one on its supply. Roles
from a fixed catalog, one link per role, no loop through the meters, the
earlier single link read forever and still written while it is all a device
has, a hand-edited block that never takes the device off, and a meter that
cannot be deleted while a device names it."""
import json

import yaml

from tests.test_devices_api import make_app, needs_tc
from tests.test_linked_meter import _row

IN = {"role": "input", "device": "em-in", "power": "power_active_total", "energy": "energy_active_import"}
OUT = {"role": "output", "device": "em-out", "power": "power_active_total"}


def _devices(tmp_path, client, ids=("ups", "em-in", "em-out")):
    for did in ids:
        r = client.post("/api/devices", json={"id": did, "enabled": False,
                                              "connection": {"protocol": "tcp", "host": "192.0.2.9"}})
        assert r.status_code in (200, 201), r.text
    for did in ids:
        f = tmp_path / "devices" / did / "selected_registers.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"version": "1.0", "poll_groups": {}, "registers": [
            _row(0, "power_active_total", "W"), _row(2, "energy_active_import", "kWh")]}))


def _stored(tmp_path, did):
    raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
    return next(d for d in raw["devices"] if d["id"] == did)


def _put(client, did, meters, **q):
    url = f"/api/devices/{did}/meters" + ("?dry_run=true" if q.get("dry_run") else "")
    return client.put(url, json={"meters": meters})


@needs_tc
def test_the_roles_are_a_fixed_catalog(tmp_path):
    _cfg, client = make_app(tmp_path)
    cat = client.get("/api/meter-roles").json()
    avail = {r["id"] for r in cat["roles"] if r["available"]}
    assert avail == {"consumption", "input", "output"}
    assert {"production", "ac_side", "dc_side", "primary", "secondary"} <= {r["id"] for r in cat["roles"]}
    assert next(r for r in cat["roles"] if r["id"] == "input")["pairs_with"] == "output"
    assert cat["signs"] == ["auto", "as_is", "invert"]


@needs_tc
def test_a_ups_names_its_input_and_output_meters(tmp_path):
    cfg, client = make_app(tmp_path)
    _devices(tmp_path, client)
    r = _put(client, "ups", [IN, OUT])
    assert r.status_code == 200 and r.json()["status"] == "updated", r.text
    stored = _stored(tmp_path, "ups")
    assert [m["role"] for m in stored["meters"]] == ["input", "output"] and "energy_meter" not in stored
    view = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "ups")
    assert [m["device"] for m in view["meters"]] == ["em-in", "em-out"]
    assert view["meters"][1] == {**OUT, "sign": "auto"}
    # the same list again changes nothing
    assert _put(client, "ups", [IN, OUT]).json()["status"] == "unchanged"
    # a dry run checks and stores nothing
    r = _put(client, "ups", [IN], dry_run=True)
    assert r.json() == {"status": "valid", "would": "update", "device": "ups",
                        "meters": [{**IN, "sign": "auto"}],
                        "options": {"min_load_w": 100.0, "tolerance_pct": 2.0, "max_skew_s": None}}
    assert len(_stored(tmp_path, "ups")["meters"]) == 2


@needs_tc
def test_what_a_link_may_not_be(tmp_path):
    _cfg, client = make_app(tmp_path)
    _devices(tmp_path, client)
    cases = [
        ([IN, {**IN, "device": "em-out"}], "meters[1].role", "one link per role"),
        ([{**IN, "role": "dc_side"}], "meters[0].role", "not available yet"),
        ([{**IN, "role": "boss"}], "meters[0].role", "not a role"),
        ([{"role": "consumption", "device": "em-in", "power": "power_active_total"}, OUT],
         "meters[0].role", "the input meter is what it takes"),
        ([{**IN, "device": "ups"}], "meters[0].device", "its own meter"),
        ([{**IN, "device": "nope"}], "meters[0].device", "no device 'nope'"),
        ([{**IN, "power": "voltage"}], "meters[0].power", "'voltage' is not a measurement of em-in"),
        ([{"role": "input", "device": "em-in"}], "meters[0].power", "power field, its energy counter"),
        ([{**IN, "sign": "up"}], "meters[0].sign", "not one of auto"),
        ([{**IN, "colour": "red"}], "meters[0]", "unknown key"),
    ]
    for meters, path, frag in cases:
        r = _put(client, "ups", meters)
        assert r.status_code == 422, (meters, r.text)
        fields = r.json()["detail"]["fields"]
        assert path in fields and frag in fields[path], (meters, fields)
    assert "meters" not in _stored(tmp_path, "ups")


@needs_tc
def test_the_meters_never_loop_back(tmp_path):
    _cfg, client = make_app(tmp_path)
    _devices(tmp_path, client, ids=("m-a", "m-b", "m-c", "m-d"))
    pa = {"role": "consumption", "power": "power_active_total"}
    for did, meter in (("m-a", "m-b"), ("m-b", "m-c"), ("m-c", "m-d")):
        assert _put(client, did, [{**pa, "device": meter}]).status_code == 200
    # m-d measured by m-a: m-d → m-a → m-b → m-c → m-d
    r = _put(client, "m-d", [{**pa, "device": "m-a"}])
    assert r.status_code == 422
    assert r.json()["detail"]["fields"]["meters[0].device"] == \
        "the meters would loop: m-d → m-a → m-b → m-c → m-d"
    # two devices may share one meter: m-a and m-c both measured by m-d
    assert _put(client, "m-a", [{**pa, "device": "m-d"}]).status_code == 200
    from multibus.meter_links import served_by
    assert sorted(d for d, _r, _i in served_by(_cfg, "m-d")) == ["m-a", "m-c"]


@needs_tc
def test_the_earlier_link_reads_the_same_and_is_still_written(tmp_path):
    """Golden: a stored `energy_meter` gives exactly what it gave before
    `meters` existed; one consumption link with the power read as positive is
    written back in that form, so an older image keeps it after a rollback."""
    cfg, client = make_app(tmp_path)
    _devices(tmp_path, client, ids=("hp-1", "meter-a"))
    r = client.put("/api/devices/hp-1/energy-meter",
                   json={"device": "meter-a", "power": "power_active_total", "energy": "energy_active_import"})
    assert r.status_code == 200, r.text
    assert _stored(tmp_path, "hp-1")["energy_meter"] == {
        "device": "meter-a", "power": "power_active_total", "energy": "energy_active_import"}
    from multibus.linked_meter import linked_calcs
    hp = cfg.get_device("hp-1")
    assert linked_calcs(cfg, hp) == [
        {"name": "power_electric", "label": "Electric power (meter-a)", "unit": "W", "category": "energy",
         "poll_group": "normal", "decimals": 0, "expr": "abs(meter-a.power_active_total)", "_linked": True},
        {"name": "energy_electric", "label": "Electric energy (meter-a)", "unit": "kWh", "category": "energy",
         "poll_group": "normal", "expr": "meter-a.energy_active_import", "_linked": True}]
    view = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "hp-1")
    assert view["meters"] == [{"role": "consumption", "device": "meter-a", "power": "power_active_total",
                               "energy": "energy_active_import", "sign": "auto"}]
    # the same link through /meters: still written as energy_meter
    assert _put(client, "hp-1", view["meters"]).json()["status"] == "unchanged"
    # a sign other than auto needs the list form, and is read as said
    r = _put(client, "hp-1", [{**view["meters"][0], "sign": "invert"}])
    assert r.status_code == 200, r.text
    st = _stored(tmp_path, "hp-1")
    assert "energy_meter" not in st and st["meters"][0]["sign"] == "invert"
    assert linked_calcs(cfg, cfg.get_device("hp-1"))[0]["expr"] == "-(meter-a.power_active_total)"
    # back to the plain link: the earlier form again
    assert _put(client, "hp-1", [{**view["meters"][0]}]).status_code == 200
    st = _stored(tmp_path, "hp-1")
    assert "meters" not in st and st["energy_meter"]["device"] == "meter-a"
    # and none
    assert _put(client, "hp-1", []).status_code == 200
    st = _stored(tmp_path, "hp-1")
    assert "meters" not in st and "energy_meter" not in st


@needs_tc
def test_the_earlier_route_sets_only_the_consumption_link(tmp_path):
    _cfg, client = make_app(tmp_path)
    _devices(tmp_path, client)
    assert _put(client, "ups", [IN, OUT]).status_code == 200
    # consumption next to input / output is refused, in the earlier words
    r = client.put("/api/devices/ups/energy-meter", json={"device": "em-in", "power": "power_active_total"})
    assert r.status_code == 422 and "input meter is what it takes" in json.dumps(r.json())
    # {} leaves the input / output links alone
    assert client.put("/api/devices/ups/energy-meter", json={}).status_code == 200
    assert [m["role"] for m in _stored(tmp_path, "ups")["meters"]] == ["input", "output"]


@needs_tc
def test_a_link_never_gives_a_name_the_device_already_reads(tmp_path):
    _cfg, client = make_app(tmp_path)
    _devices(tmp_path, client, ids=("hp-1", "meter-a"))
    f = tmp_path / "devices" / "hp-1" / "selected_registers.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"version": "1.0", "poll_groups": {}, "registers": [_row(0, "power_electric", "W")]}))
    r = _put(client, "hp-1", [{"role": "consumption", "device": "meter-a", "power": "power_active_total"}])
    assert r.status_code == 422
    assert "already reads power_electric" in r.json()["detail"]["fields"]["meters[0].role"]


@needs_tc
def test_an_edit_keeps_the_links_and_restarts_nothing(tmp_path):
    cfg, client = make_app(tmp_path)
    _devices(tmp_path, client)
    assert _put(client, "ups", [IN, OUT]).status_code == 200
    body = {"name": "ups", "enabled": False, "connection": {"protocol": "tcp", "host": "192.0.2.9"}}
    r = client.put("/api/devices/ups", json=body)
    assert r.status_code == 200 and r.json()["status"] == "unchanged", r.text
    assert len(cfg.get_device("ups").meters) == 2
    # the links are not part of what the device runs on
    from multibus import api as api_mod  # noqa: F401
    import dataclasses
    before = cfg.get_device("ups")
    after = dataclasses.replace(before, meters=[])
    skip = ('name', 'energy_meter', 'meters')
    strip = lambda c: {k: v for k, v in dataclasses.asdict(c).items() if k not in skip}  # noqa: E731
    assert strip(before) == strip(after)


@needs_tc
def test_a_meter_named_by_a_device_is_not_deleted(tmp_path):
    _cfg, client = make_app(tmp_path)
    _devices(tmp_path, client)
    assert _put(client, "ups", [IN, OUT]).status_code == 200
    r = client.delete("/api/devices/em-out")
    assert r.status_code == 409, r.text
    deps = r.json()["detail"]["dependents"]["blocking"]
    link = next(d for d in deps if d["type"] == "meter_link")
    assert link["id"] == "ups" and link["where"] == "meters.output"
    assert link["ref"] == {"type": "device", "id": "ups", "tab": "meters"}
    # once the UPS no longer names it, it goes
    assert _put(client, "ups", [IN]).status_code == 200
    assert client.delete("/api/devices/em-out").status_code == 200


def test_a_hand_edited_block_never_takes_the_device_off(tmp_path):
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml="""
devices:
  - id: ups
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.9}
    meters: [7, {role: input, device: em-in, power: p}, {role: input, device: em-x, power: p},
             {role: boss, device: em-in}, {role: output, device: em-out, power: p, sign: sideways}]
  - id: ups2
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.9}
    meters: "em-in"
""")
    ups, ups2 = cfg.get_device("ups"), cfg.get_device("ups2")
    assert ups is not None and ups2 is not None
    assert ups.meters == [{"role": "input", "device": "em-in", "power": "p", "sign": "auto"}]
    assert ups2.meters == []
    assert "ups2.meters" in cfg.ignored_blocks
    assert {k for k in cfg.ignored_blocks if k.startswith("ups.")} == \
        {"ups.meters[0]", "ups.meters[2]", "ups.meters[3]", "ups.meters[4]"}


GX = """
devices:
  - id: gx
    name: Victron GX
    template: victron_gx_mqtt
    enabled: false
    connection: {protocol: mqtt, broker: 192.0.2.10, port: 1883, topic: 'N/c0ffee/#', vars: {portal: c0ffee}}
    components:
      - {type: battery, instance: 288}
  - id: shunt-meter
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.11}
"""


@needs_tc
def test_a_component_may_hold_a_link(tmp_path):
    cfg, client = make_app(tmp_path, GX)
    f = tmp_path / "devices" / "shunt-meter" / "selected_registers.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"version": "1.0", "poll_groups": {}, "registers": [_row(0, "power_dc", "W")]}))
    r = _put(client, "gx-battery-288", [{"role": "input", "device": "shunt-meter", "power": "power_dc"}])
    assert r.status_code == 200, r.text
    gx = _stored(tmp_path, "gx")
    assert gx["components"][0]["meters"][0]["device"] == "shunt-meter"
    assert cfg.get_device("gx-battery-288").meters[0]["role"] == "input"


# ── after the code review ───────────────

def test_any_garbage_in_a_hand_edited_link_drops_the_link_never_the_device(tmp_path):
    """ML-MJ1: a list or a mapping where a name belongs raised TypeError in the
    parse, and the whole device was skipped at load."""
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml="""
devices:
  - id: ups
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.9}
    meters: [{role: [input], device: em-in}, {role: {a: 1}, device: em-in}, {role: input, device: [em-in]},
             {role: output, device: em-out, power: [p]}, {role: input, device: em-in, power: p}]
""")
    ups = cfg.get_device("ups")
    assert ups is not None
    assert ups.meters == [{"role": "input", "device": "em-in", "power": "p", "sign": "auto"}]
    # every dropped entry is said, not only the last one (ML-m6)
    assert {k for k in cfg.ignored_blocks if k.startswith("ups.")} == \
        {"ups.meters[0]", "ups.meters[1]", "ups.meters[2]", "ups.meters[3]"}


def test_meters_beside_a_disagreeing_earlier_link_win_and_it_is_said(tmp_path):
    """ML-m5: after a rollback an older version may have edited energy_meter."""
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml="""
devices:
  - id: hp
    enabled: false
    connection: {protocol: tcp, host: 192.0.2.9}
    energy_meter: {device: old-meter, power: p}
    meters: [{role: consumption, device: new-meter, power: p, sign: invert}]
""")
    assert cfg.get_device("hp").meters[0]["device"] == "new-meter"
    assert "disagrees with meters" in cfg.ignored_blocks["hp.energy_meter"]


@needs_tc
def test_a_restored_device_keeps_its_meters_and_its_kind(tmp_path):
    """ML-MJ2: the restore put back a fixed list of own keys, without meters
    (and without the newer kind)."""
    cfg, client = make_app(tmp_path)
    _devices(tmp_path, client)
    assert _put(client, "ups", [IN, OUT]).status_code == 200
    assert client.patch("/api/devices/ups/organise", json={"kind": "ups"}).status_code == 200
    assert client.delete("/api/devices/ups").status_code == 200
    r = client.post("/api/devices/ups/restore")
    assert r.status_code == 200, r.text
    st = _stored(tmp_path, "ups")
    assert [m["role"] for m in st["meters"]] == ["input", "output"] and st["kind"] == "ups"
    assert [m["device"] for m in cfg.get_device("ups").meters] == ["em-in", "em-out"]


@needs_tc
def test_a_unit_of_an_installation_is_not_a_meter(tmp_path):
    """ML-MJ3a: a unit's id changes with its installation."""
    from tests.test_unchanged_save import PLANT
    _cfg, client = make_app(tmp_path, PLANT)
    _devices(tmp_path, client)
    r = _put(client, "ups", [{"role": "input", "device": "pv-u1", "power": "power_active_total"}])
    assert r.status_code == 422
    assert "unit of the installation 'pv'" in r.json()["detail"]["fields"]["meters[0].device"]


@needs_tc
def test_a_field_must_read_power_or_energy(tmp_path):
    """ML-MJ3b: a current, a kVA or a unitless register was taken as power."""
    _cfg, client = make_app(tmp_path)
    _devices(tmp_path, client)
    f = tmp_path / "devices" / "em-in" / "selected_registers.json"
    f.write_text(json.dumps({"version": "1.0", "poll_groups": {}, "registers": [
        _row(0, "current_l1", "A"), _row(2, "power_apparent_total", "kVA"), _row(4, "count", ""),
        _row(6, "power_active_total", "kW"), _row(8, "energy_active_import", "MWh")]}))
    for field, name, frag in (("power", "current_l1", "reads A"), ("power", "power_apparent_total", "reads kVA"),
                              ("power", "count", "reads no unit"), ("energy", "power_active_total", "reads kW")):
        r = _put(client, "ups", [{"role": "input", "device": "em-in", field: name}])
        assert r.status_code == 422, (field, name)
        assert frag in r.json()["detail"]["fields"][f"meters[0].{field}"]
    ok = _put(client, "ups", [{"role": "input", "device": "em-in", "power": "power_active_total",
                               "energy": "energy_active_import"}])
    assert ok.status_code == 200, ok.text


@needs_tc
def test_null_is_not_none(tmp_path):
    """ML-m2: a client bug sending null wiped every link."""
    _cfg, client = make_app(tmp_path)
    _devices(tmp_path, client)
    assert _put(client, "ups", [IN, OUT]).status_code == 200
    r = client.put("/api/devices/ups/meters", json={"meters": None})
    assert r.status_code == 422 and "send []" in r.json()["detail"]["fields"]["meters"]
    assert len(_stored(tmp_path, "ups")["meters"]) == 2


@needs_tc
def test_the_earlier_route_and_view_keep_a_sign(tmp_path):
    """ML-m1: a consumption link with sign invert read as "no meter" in the
    view, and a re-save through the earlier route turned it back to abs()."""
    cfg, client = make_app(tmp_path)
    _devices(tmp_path, client, ids=("hp-1", "meter-a"))
    link = {"role": "consumption", "device": "meter-a", "power": "power_active_total", "sign": "invert"}
    assert _put(client, "hp-1", [link]).status_code == 200
    view = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "hp-1")
    assert view["energy_meter"] == {"device": "meter-a", "power": "power_active_total", "sign": "invert"}
    r = client.put("/api/devices/hp-1/energy-meter", json={"device": "meter-a", "power": "power_active_total"})
    assert r.status_code == 200 and r.json()["energy_meter"]["sign"] == "invert"
    assert cfg.get_device("hp-1").meters[0]["sign"] == "invert"
    # a plain link keeps the earlier view shape (no sign key)
    assert _put(client, "hp-1", [{**link, "sign": "auto"}]).status_code == 200
    view = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "hp-1")
    assert view["energy_meter"] == {"device": "meter-a", "power": "power_active_total"}


@needs_tc
def test_the_earlier_route_says_which_field(tmp_path):
    """ML-m3: the 422 lost its "power: " prefix; a self-link said two things."""
    _cfg, client = make_app(tmp_path)
    _devices(tmp_path, client, ids=("hp-1", "meter-a"))
    r = client.put("/api/devices/hp-1/energy-meter", json={"device": "meter-a", "power": "nope"})
    assert r.json()["detail"]["errors"] == ["power: 'nope' is not a measurement of meter-a"]
    r = client.put("/api/devices/hp-1/energy-meter", json={"device": "hp-1", "power": "x"})
    assert r.json()["detail"]["errors"] == ["device: a device cannot be its own meter"]


@needs_tc
def test_a_name_the_device_reads_wins_over_a_link_at_runtime(tmp_path):
    """ML-m7: a register the device reads (added after its link) and the link
    both gave power_electric: two values of one name."""
    _cfg, client = make_app(tmp_path)
    _devices(tmp_path, client, ids=("hp-1", "meter-a"))
    assert client.put("/api/devices/hp-1/energy-meter",
                      json={"device": "meter-a", "power": "power_active_total"}).status_code == 200
    f = tmp_path / "devices" / "hp-1" / "selected_registers.json"
    f.write_text(json.dumps({"version": "1.0", "poll_groups": {}, "registers": [_row(0, "power_electric", "W")]}))
    ce = client.app.state.calc_engine
    ce.load("hp-1")
    assert not any(e["_reg"].name == "power_electric" for e in ce.store.get("hp-1", []))


class _Running:
    """A running client, as far as the device list asks it."""
    def get_stats(self):
        return {"connected": True}

    def data_health(self):
        return {"status": "ok"}


@needs_tc
def test_setting_the_kind_and_the_meters_never_restarts_the_device(tmp_path):
    """After the rebase both lots skip their own keys in _built_sig."""
    from fastapi.testclient import TestClient

    from multibus.api import create_api
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml="""
devices:
  - {id: ups, name: ups, enabled: false, connection: {protocol: tcp, host: 192.0.2.9}}
  - {id: em-in, enabled: false, connection: {protocol: tcp, host: 192.0.2.10}}
""")
    running = {d.id: _Running() for d in cfg.devices}
    app, _ = create_api(cfg, None, None, None, devices=[(d, running[d.id]) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    f = tmp_path / "devices" / "em-in" / "selected_registers.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"version": "1.0", "poll_groups": {}, "registers": [_row(0, "power_active_total", "W")]}))
    assert client.patch("/api/devices/ups/organise", json={"kind": "ups"}).status_code == 200
    r = _put(client, "ups", [{"role": "input", "device": "em-in", "power": "power_active_total"}])
    assert r.status_code == 200, r.text
    assert app.state.registry.find("ups")[2] is running["ups"]
    # the device page's save knows neither: both stay, a rename restarts nothing
    st = _stored(tmp_path, "ups")
    body = {k: v for k, v in st.items() if k not in ("kind", "meters")}
    r = client.put("/api/devices/ups", json={**body, "name": "UPS hall"})
    assert r.status_code == 200, r.text
    assert app.state.registry.find("ups")[2] is running["ups"]
    st = _stored(tmp_path, "ups")
    assert st["kind"] == "ups" and st["meters"][0]["role"] == "input"
