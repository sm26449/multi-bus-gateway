# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Endpoints (F0.3): one template + one endpoint + N unit ids → N materialized
devices, each with its own socket, managed THROUGH the endpoint.

Covers: config expansion (ids, substitutions, invalid entries), persistence
(endpoints stay in `endpoints:`, never leak into `devices:`), the CRUD guards on
materialized devices, the /api/endpoints routes, and boot-time template seeding.
"""
import json

import pytest

from multibus.config import Config

from tests.test_devices import write_config

try:
    from fastapi.testclient import TestClient
    HAVE_TC = True
except Exception:  # pragma: no cover
    HAVE_TC = False

needs_tc = pytest.mark.skipif(not HAVE_TC, reason="fastapi testclient unavailable")


ENDPOINT_YAML = """
endpoints:
  - id: fronius
    name: Fronius PV
    connection: { protocol: tcp, host: 192.0.2.9, port: 502 }
    units: [1, 2, {unit_id: 3, id: inv3, name: East roof}]
    mqtt: { topic_prefix: "mbg/fronius/inverter/${unit_id}", ha_discovery: false }
    influxdb: { bucket: fronius_shadow, device_tag: "inverter_${unit_id}" }
"""


# ── config-level expansion ───────────────────────────────────────────────────

def test_endpoint_expands_to_one_device_per_unit(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=ENDPOINT_YAML)
    made = cfg.endpoint_devices("fronius")
    assert [d.id for d in made] == ["fronius-u1", "fronius-u2", "inv3"]
    assert [d.connection.unit_id for d in made] == [1, 2, 3]
    assert all(d.endpoint_id == "fronius" for d in made)
    # every unit gets its OWN connection (socket-per-device is the point)
    assert made[0].connection is not made[1].connection
    assert made[0].connection.host == "192.0.2.9"


def test_endpoint_substitutions_and_defaults(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=ENDPOINT_YAML)
    u1, _u2, inv3 = cfg.endpoint_devices("fronius")
    assert u1.mqtt_topic_prefix == "mbg/fronius/inverter/1"
    assert u1.influxdb_bucket == "fronius_shadow"
    assert u1.influxdb_device_tag == "inverter_1"
    assert u1.name == "Fronius PV unit 1"
    assert u1.ha_discovery_enabled is False
    # per-unit overrides win
    assert inv3.id == "inv3" and inv3.name == "East roof"
    assert inv3.mqtt_topic_prefix == "mbg/fronius/inverter/3"


def test_endpoint_invalid_units_are_skipped_not_fatal(tmp_path):
    cfg = write_config(tmp_path, extra_yaml="""
endpoints:
  - id: p1
    connection: { protocol: tcp, host: 192.0.2.9 }
    units: [1, 1, "x", 999, 2]
""")
    # duplicate 1, non-numeric, out-of-range are skipped; 1 and 2 survive
    assert [d.connection.unit_id for d in cfg.endpoint_devices("p1")] == [1, 2]


def test_disabled_endpoint_materializes_disabled_units(tmp_path):
    cfg = write_config(tmp_path, extra_yaml="""
endpoints:
  - id: p1
    enabled: false
    connection: { protocol: tcp, host: 192.0.2.9 }
    units: [1, 2]
""")
    made = cfg.endpoint_devices("p1")
    assert len(made) == 2 and all(not d.enabled for d in made)


def test_endpoint_persistence_roundtrip_never_leaks_into_devices(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=ENDPOINT_YAML)
    cfg.save_yaml_config()
    import yaml as _yaml
    doc = _yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert len(doc["endpoints"]) == 1
    assert "devices" not in doc          # materialized units are derived
    cfg2 = Config(str(tmp_path / "config.yaml"))
    assert [d.id for d in cfg2.endpoint_devices("fronius")] == \
        ["fronius-u1", "fronius-u2", "inv3"]


def test_device_crud_refuses_endpoint_materialized_ids(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=ENDPOINT_YAML)
    with pytest.raises(ValueError, match="materialized from endpoint"):
        cfg.upsert_raw_device({"id": "inv3",
                               "connection": {"protocol": "tcp", "host": "x"}})


def test_delete_endpoint_removes_all_units(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=ENDPOINT_YAML)
    removed = cfg.delete_endpoint("fronius")
    assert removed == ["fronius-u1", "fronius-u2", "inv3"]
    assert cfg.endpoint_devices("fronius") == []
    assert cfg.get_raw_endpoint("fronius") is None
    with pytest.raises(ValueError):
        cfg.delete_endpoint("fronius")


def test_endpoint_id_collision_with_device_is_skipped(tmp_path):
    cfg = write_config(tmp_path, extra_yaml="""
devices:
  - id: inv3
    connection: { protocol: tcp, host: 192.0.2.10 }
    enabled: false
endpoints:
  - id: p1
    connection: { protocol: tcp, host: 192.0.2.9 }
    units: [1, {unit_id: 3, id: inv3}]
""")
    # the explicit devices[] entry wins; the colliding unit is skipped
    assert cfg.get_device("inv3").endpoint_id == ""
    assert [d.id for d in cfg.endpoint_devices("p1")] == ["p1-u1"]


# ── API routes ───────────────────────────────────────────────────────────────

def make_app(tmp_path, extra_yaml=""):
    from multibus.api import create_api
    cfg = write_config(tmp_path, extra_yaml=extra_yaml)
    devices = [(d, None) for d in cfg.devices]
    app, _ = create_api(cfg, None, None, None, devices=devices)
    return cfg, TestClient(app, raise_server_exceptions=False)


ENDPOINT_BODY = {
    "id": "sdm-endpoint", "name": "SDM bank",
    "template": "eastron_sdm120",
    "enabled": False,                      # no live clients in tests
    "connection": {"protocol": "tcp", "host": "192.0.2.20", "port": 502},
    "units": [1, 2],
    "influxdb": {"bucket": "sdm_shadow", "device_tag": "sdm_${unit_id}"},
}


@needs_tc
def test_create_endpoint_materializes_seeds_and_lists(tmp_path):
    cfg, client = make_app(tmp_path)
    r = client.post("/api/endpoints", json=ENDPOINT_BODY)
    assert r.status_code == 200, r.text
    out = r.json()
    assert [d["id"] for d in out["devices"]] == ["sdm-endpoint-u1", "sdm-endpoint-u2"]
    # template seeding ran per unit (bundled sdm120 has curated defaults)
    for did in ("sdm-endpoint-u1", "sdm-endpoint-u2"):
        f = tmp_path / "devices" / did / "selected_registers.json"
        assert f.exists()
        assert len(json.loads(f.read_text())["registers"]) > 0
    # list + detail
    endpoints = client.get("/api/endpoints").json()["endpoints"]
    assert len(endpoints) == 1 and endpoints[0]["total_units"] == 2
    got = client.get("/api/endpoints/sdm-endpoint").json()
    assert [u["device_id"] for u in got["units"]] == \
        ["sdm-endpoint-u1", "sdm-endpoint-u2"]
    # materialized units appear in the device list, tagged with their endpoint
    devs = {d["id"]: d for d in client.get("/api/devices").json()["devices"]}
    assert devs["sdm-endpoint-u1"]["endpoint_id"] == "sdm-endpoint"


@needs_tc
def test_endpoint_validation_errors(tmp_path):
    _cfg, client = make_app(tmp_path)
    bad = dict(ENDPOINT_BODY, connection={"protocol": "tcp"})    # no host
    assert client.post("/api/endpoints", json=bad).status_code == 422
    bad = dict(ENDPOINT_BODY, units=[])
    assert client.post("/api/endpoints", json=bad).status_code == 422
    bad = dict(ENDPOINT_BODY, template="no_such_template")
    assert client.post("/api/endpoints", json=bad).status_code == 422
    bad = dict(ENDPOINT_BODY, connection={"protocol": "rtu", "serial_port": "/dev/ttyUSB0"})
    assert client.post("/api/endpoints", json=bad).status_code == 422
    # duplicate id after a successful create
    assert client.post("/api/endpoints", json=ENDPOINT_BODY).status_code == 200
    assert client.post("/api/endpoints", json=ENDPOINT_BODY).status_code == 422


@needs_tc
def test_device_routes_refuse_endpoint_units(tmp_path):
    _cfg, client = make_app(tmp_path)
    assert client.post("/api/endpoints", json=ENDPOINT_BODY).status_code == 200
    r = client.delete("/api/devices/sdm-endpoint-u1")
    assert r.status_code == 422
    assert "endpoint" in r.text
    r = client.put("/api/devices/sdm-endpoint-u1",
                   json={"connection": {"protocol": "tcp", "host": "192.0.2.30"}})
    assert r.status_code == 422
    assert "endpoint" in r.text


@needs_tc
def test_update_endpoint_swaps_units(tmp_path):
    cfg, client = make_app(tmp_path)
    assert client.post("/api/endpoints", json=ENDPOINT_BODY).status_code == 200
    body = dict(ENDPOINT_BODY, units=[2, 3])
    r = client.put("/api/endpoints/sdm-endpoint", json=body)
    assert r.status_code == 200, r.text
    ids = [d.id for d in cfg.endpoint_devices("sdm-endpoint")]
    assert ids == ["sdm-endpoint-u2", "sdm-endpoint-u3"]
    devs = {d["id"] for d in client.get("/api/devices").json()["devices"]}
    assert "sdm-endpoint-u1" not in devs and "sdm-endpoint-u3" in devs


@needs_tc
def test_delete_endpoint_removes_units_keeps_register_files(tmp_path):
    cfg, client = make_app(tmp_path)
    assert client.post("/api/endpoints", json=ENDPOINT_BODY).status_code == 200
    r = client.delete("/api/endpoints/sdm-endpoint")
    assert r.status_code == 200
    assert r.json()["removed_devices"] == ["sdm-endpoint-u1", "sdm-endpoint-u2"]
    assert client.get("/api/endpoints").json()["endpoints"] == []
    devs = {d["id"] for d in client.get("/api/devices").json()["devices"]}
    assert not any(d.startswith("sdm-endpoint") for d in devs)
    # register files stay on disk → a re-add restores the selection
    assert (tmp_path / "devices" / "sdm-endpoint-u1" / "selected_registers.json").exists()
    assert client.delete("/api/endpoints/sdm-endpoint").status_code == 404


# ── boot-time seeding (main.py path) ─────────────────────────────────────────

def test_boot_seeding_for_endpoint_devices(tmp_path):
    """An endpoint written straight into config.yaml (no API involved) seeds its
    units' register selections from the template at boot."""
    from multibus.device_seed import autoselect_template_registers
    from multibus.device_template import TemplateRegistry
    cfg = write_config(tmp_path, extra_yaml="""
endpoints:
  - id: p1
    template: eastron_sdm120
    connection: { protocol: tcp, host: 192.0.2.9 }
    units: [1, 2]
""")
    reg = TemplateRegistry(user_dir=tmp_path / "device_templates")
    for dev in cfg.endpoint_devices("p1"):
        autoselect_template_registers(cfg, reg, dev)
        regs, _g = cfg.load_device_registers(dev)
        assert regs, dev.id
    # idempotent: a second boot keeps the file (no re-seed churn)
    before = (tmp_path / "devices" / "p1-u1" / "selected_registers.json").read_text()
    autoselect_template_registers(cfg, reg, cfg.endpoint_devices("p1")[0])
    assert (tmp_path / "devices" / "p1-u1" /
            "selected_registers.json").read_text() == before


# ── P4: an endpoint is a first-class entity ──────────────────────────────────────

@needs_tc
def test_editing_a_endpoint_keeps_what_the_form_does_not_send(tmp_path):
    """An edit REPLACES the stored entry, so anything the endpoint owns but the
    form omits used to vanish: a locked endpoint unlocked itself and
    `aggregates: false` came back on, one save after the operator set them."""
    cfg, client = make_app(tmp_path)
    assert client.post("/api/endpoints",
                       json=dict(ENDPOINT_BODY, aggregates=False)).status_code == 200
    cfg.set_write_locked("sdm-endpoint-u1", True)
    cfg.set_http_output("sdm-endpoint-u1", True)
    cfg.set_rest_push("sdm-endpoint-u1", {"enabled": True, "url": "http://x/i"})

    # the form sends name/connection/units — and nothing else
    r = client.put("/api/endpoints/sdm-endpoint", json=dict(ENDPOINT_BODY, name="Renamed"))
    assert r.status_code == 200, r.text
    kept = Config(str(tmp_path / "config.yaml")).get_raw_endpoint("sdm-endpoint")
    assert kept["name"] == "Renamed"
    assert kept["write_locked"] is True
    assert kept["aggregates"] is False
    assert kept["http_output"]["enabled"] is True
    assert kept["rest_push"]["url"] == "http://x/i"


@needs_tc
def test_editing_a_endpoint_cannot_reroute_it(tmp_path):
    """Routing identity is fixed after creation, exactly as for a device:
    changing it re-routes every unit and orphans their history + HA entities."""
    cfg, client = make_app(tmp_path)
    assert client.post("/api/endpoints", json=ENDPOINT_BODY).status_code == 200
    moved = dict(ENDPOINT_BODY,
                 mqtt={"topic_prefix": "somewhere/else/${device_id}"},
                 influxdb={"bucket": "other", "device_tag": "x"})
    assert client.put("/api/endpoints/sdm-endpoint", json=moved).status_code == 200
    got = client.get("/api/endpoints/sdm-endpoint").json()
    assert got["influxdb"]["bucket"] == "sdm_shadow"
    assert got["influxdb"]["device_tag"] == "sdm_${unit_id}"
    u1 = next(d for d in Config(str(tmp_path / "config.yaml")).devices
              if d.id == "sdm-endpoint-u1")
    assert u1.influxdb_bucket == "sdm_shadow"


@needs_tc
def test_editing_with_bare_unit_ids_keeps_hand_written_overrides(tmp_path):
    cfg, client = make_app(tmp_path)
    body = dict(ENDPOINT_BODY, units=[1, {"unit_id": 2, "id": "east", "name": "East roof"}])
    assert client.post("/api/endpoints", json=body).status_code == 200
    # a client that re-sends the plain shape must not erase the override
    assert client.put("/api/endpoints/sdm-endpoint",
                      json=dict(ENDPOINT_BODY, units=[1, 2])).status_code == 200
    made = Config(str(tmp_path / "config.yaml")).endpoint_devices("sdm-endpoint")
    assert [d.id for d in made] == ["sdm-endpoint-u1", "east"]
    assert made[1].name == "East roof"


@needs_tc
def test_an_explicit_unit_override_still_wins(tmp_path):
    _cfg, client = make_app(tmp_path)
    assert client.post("/api/endpoints", json=ENDPOINT_BODY).status_code == 200
    assert client.put("/api/endpoints/sdm-endpoint",
                      json=dict(ENDPOINT_BODY,
                                units=[1, {"unit_id": 2, "id": "west"}])).status_code == 200
    got = client.get("/api/endpoints/sdm-endpoint").json()
    assert [u["device_id"] for u in got["units"]] == ["sdm-endpoint-u1", "west"]


def test_endpoint_units_can_carry_the_endpoints_own_sinks(tmp_path):
    """An endpoint is ONE endpoint, so its sinks are declared once and apply to
    every unit — the same rule the write lock follows. These used to raise
    'not a configurable device' because the setters only walked devices[]."""
    cfg = write_config(tmp_path, extra_yaml=ENDPOINT_YAML)
    cfg.set_http_output("fronius-u1", True)
    cfg.set_rest_push("fronius-u2", {"enabled": True, "url": "http://x/ingest"})
    for dev in cfg.endpoint_devices("fronius"):
        assert dev.http_output_enabled is True
        assert dev.rest_push.get("url") == "http://x/ingest"
    # and they persist on the ENDPOINT, never on a materialized unit
    raw = cfg.get_raw_endpoint("fronius")
    assert raw["http_output"]["enabled"] is True
    assert raw["rest_push"]["url"] == "http://x/ingest"
    reloaded = Config(str(tmp_path / "config.yaml"))
    assert reloaded.endpoint_devices("fronius")[0].http_output_enabled is True


@needs_tc
def test_endpoint_test_probes_every_unit(tmp_path):
    _cfg, client = make_app(tmp_path)
    assert client.post("/api/endpoints", json=ENDPOINT_BODY).status_code == 200
    assert client.post("/api/endpoints/nope/test").status_code == 404
    r = client.post("/api/endpoints/sdm-endpoint/test")
    assert r.status_code == 200, r.text
    out = r.json()
    # one answer per unit, keyed to the unit that was probed (192.0.2.x is
    # unroutable, so every probe fails — the SHAPE is what matters here)
    assert [u["unit_id"] for u in out["units"]] == [1, 2]
    assert all("ok" in u and u["device_id"].startswith("sdm-endpoint") for u in out["units"])
    assert out["ok"] is False


@needs_tc
def test_status_reports_endpoints_not_just_their_units(tmp_path):
    """Four healthy unit rows say nothing about an endpoint sitting at 2/4."""
    _cfg, client = make_app(tmp_path)
    assert client.post("/api/endpoints", json=ENDPOINT_BODY).status_code == 200
    st = client.get("/api/status").json()
    assert "endpoints" in st
    p = next(x for x in st["endpoints"] if x["id"] == "sdm-endpoint")
    assert p["units_total"] == 0        # the endpoint is disabled in ENDPOINT_BODY
    assert p["aggregates_enabled"] is True


@needs_tc
def test_endpoint_detail_carries_what_a_endpoint_page_needs(tmp_path):
    _cfg, client = make_app(tmp_path)
    assert client.post("/api/endpoints", json=ENDPOINT_BODY).status_code == 200
    got = client.get("/api/endpoints/sdm-endpoint").json()
    for key in ("status", "aggregates_enabled", "write_locked",
                "http_output_enabled", "rest_push", "online_units"):
        assert key in got, key
    for key in ("health", "last_seen", "staleness_age_s"):
        assert key in got["units"][0], key


def test_influx_reads_resolve_a_endpoint_like_a_device(tmp_path):
    """An endpoint writes its own aggregate series (device=<endpoint id>) into the
    endpoint's bucket, so an endpoint total must chart the way a unit's does."""
    from multibus.routes._shared import device_influx
    cfg = write_config(tmp_path, extra_yaml=ENDPOINT_YAML)
    assert device_influx(cfg, "fronius") == ("fronius_shadow", "fronius")
    assert device_influx(cfg, "fronius-u1") == ("fronius_shadow", "inverter_1")
    assert device_influx(cfg, "") == (None, None)
    assert device_influx(cfg, cfg.primary_device.id) == (None, None)


def test_an_unknown_id_says_so_instead_of_answering_with_the_primary(tmp_path):
    """Returning (None, None) sent the query to the PRIMARY's bucket with no
    filter, so a typo answered with somebody else's data."""
    from multibus.routes._shared import device_influx
    cfg = write_config(tmp_path, extra_yaml=ENDPOINT_YAML)
    with pytest.raises(ValueError, match="unknown device or endpoint"):
        device_influx(cfg, "typo")


@needs_tc
def test_history_picker_lists_a_endpoints_aggregate_series(tmp_path):
    _cfg, client = make_app(tmp_path)
    assert client.post("/api/endpoints", json=ENDPOINT_BODY).status_code == 200
    names = {r["name"] for r in
             client.get("/api/history/registers?device=sdm-endpoint").json()["registers"]}
    # the census always, plus the canonical names the units actually contribute
    assert {"units_online", "units_total"} <= names
    assert any(n.startswith(("power_", "energy_", "voltage_", "current_"))
               for n in names)
    # never a name the aggregator refuses to combine
    assert not any(n.startswith(("operating_state", "serial", "manufacturer"))
                   for n in names)


@needs_tc
def test_editing_a_endpoint_without_a_sink_block_keeps_the_stored_one(tmp_path):
    """Pinning the routing keys must not rebuild the block from them alone —
    ha_discovery would go out with the bathwater."""
    _cfg, client = make_app(tmp_path)
    body = dict(ENDPOINT_BODY, mqtt={"topic_prefix": "mbg/devices/${device_id}",
                                  "ha_discovery": True, "enabled": True})
    assert client.post("/api/endpoints", json=body).status_code == 200
    slim = {k: v for k, v in ENDPOINT_BODY.items() if k not in ("mqtt", "influxdb")}
    assert client.put("/api/endpoints/sdm-endpoint", json=slim).status_code == 200
    got = client.get("/api/endpoints/sdm-endpoint").json()
    assert got["mqtt"]["ha_discovery"] is True
    assert got["mqtt"]["topic_prefix"] == "mbg/devices/${device_id}"
    assert got["influxdb"]["bucket"] == "sdm_shadow"


@needs_tc
def test_a_settings_only_edit_keeps_the_pollers_running(tmp_path):
    """Re-materializing on every save punched a hole in acquisition for a
    rename or a toggle — on a live endpoint that is a gap in the data."""
    from multibus.api import create_api

    class _Client:
        """A unit's client. A restart tears it down — so counting disconnects
        counts restarts."""
        connected = True
        disconnects = 0

        def get_stats(self):
            return {"connected": True, "poll_rate": 1.0, "failed_reads": 0}

        def data_health(self, *a):
            return {"status": "ok", "last_success_ts": 1_700_000_000}

        def disconnect(self):
            _Client.disconnects += 1

    cfg = write_config(tmp_path, extra_yaml=ENDPOINT_YAML)
    app, _ = create_api(cfg, None, None, None,
                        devices=[(d, _Client() if d.endpoint_id else None)
                                 for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    body = {"id": "fronius", "name": "Renamed", "template": "",
            "enabled": True,
            "connection": {"protocol": "tcp", "host": "192.0.2.9", "port": 502},
            "units": [1, 2, {"unit_id": 3, "id": "inv3", "name": "East roof"}]}

    _Client.disconnects = 0
    assert client.put("/api/endpoints/fronius", json=body).status_code == 200
    assert _Client.disconnects == 0                   # nothing was torn down
    assert client.get("/api/endpoints/fronius").json()["name"] == "Renamed"
    # the unit's own config object reflects the rename anyway
    devs = {d["id"]: d for d in client.get("/api/devices").json()["devices"]}
    assert devs["fronius-u1"]["name"].startswith("Renamed")

    # changing the endpoint DOES re-materialize
    moved = dict(body, connection={"protocol": "tcp", "host": "192.0.2.44",
                                   "port": 502})
    assert client.put("/api/endpoints/fronius", json=moved).status_code == 200
    assert _Client.disconnects == 3                   # one per unit
    assert client.get("/api/endpoints/fronius").json()["connection"]["host"] == "192.0.2.44"


def test_endpoint_runtime_signature_ignores_cosmetics(tmp_path):
    """The signature decides restart-or-not, so what it covers is the contract:
    everything a unit is built from, and nothing that only reads differently."""
    from multibus.api import create_api
    cfg = write_config(tmp_path, extra_yaml=ENDPOINT_YAML)
    app, _ = create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    sig = app.state.endpoint_runtime_sig
    base = cfg.get_raw_endpoint("fronius")
    renamed = dict(base, name="Other name")
    assert sig(base) == sig(renamed)
    assert sig(base) == sig(dict(base, aggregates=False))
    # unit display names are cosmetic; their IDS are not
    units_named = [{"unit_id": 1, "id": "fronius-u1", "name": "North"},
                   {"unit_id": 2, "id": "fronius-u2"},
                   {"unit_id": 3, "id": "inv3"}]
    assert sig(base) == sig(dict(base, units=units_named))
    assert sig(base) != sig(dict(base, units=[1, 2]))
    assert sig(base) != sig(dict(base, enabled=False))
    assert sig(base) != sig(dict(base, template="other"))
    assert sig(base) != sig(dict(base, write_locked=True))
    assert sig(base) != sig(dict(base,
                                 connection={"protocol": "tcp", "host": "10.0.0.1"}))


def test_the_old_plants_key_still_loads(tmp_path):
    """`endpoints:` was called `plants:` until 3.51.0. A config written by an
    older version must keep working — and the next save writes the new key."""
    cfg = write_config(tmp_path, extra_yaml="""
plants:
  - id: legacy
    connection: { protocol: tcp, host: 192.0.2.9 }
    units: [1, 2]
""")
    assert [d.id for d in cfg.endpoint_devices("legacy")] == ["legacy-u1", "legacy-u2"]
    cfg.save_yaml_config()
    import yaml as _yaml
    doc = _yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert "plants" not in doc and len(doc["endpoints"]) == 1
    assert Config(str(tmp_path / "config.yaml")).endpoint_devices("legacy")


# ── how many sockets the access point is worth ───────────────────────────────

@needs_tc
def test_the_lane_count_is_bounded_because_sockets_are_scarce(tmp_path):
    """A master serves a handful of clients — the datalogger this replaces
    starts refusing near five. A typo asking for thirty would take the access
    point down for everything else on it, our own other units included."""
    cfg, client = make_app(tmp_path)
    for bad in (0, 9, "two", -1):
        body = dict(ENDPOINT_BODY,
                    connection=dict(ENDPOINT_BODY["connection"],
                                    max_connections=bad))
        r = client.post("/api/endpoints", json=body)
        assert r.status_code == 422, bad
        assert any("max_connections" in e
                   for e in r.json()["detail"]["errors"])


@needs_tc
def test_a_measured_lane_count_is_persisted_and_reaches_every_unit(tmp_path):
    cfg, client = make_app(tmp_path)
    body = dict(ENDPOINT_BODY,
                connection=dict(ENDPOINT_BODY["connection"], max_connections=2))
    assert client.post("/api/endpoints", json=body).status_code == 200
    got = client.get("/api/endpoints/sdm-endpoint").json()
    assert got["connection"]["max_connections"] == 2
    assert all(d.connection.max_connections == 2
               for d in cfg.endpoint_devices("sdm-endpoint"))


@needs_tc
def test_an_endpoint_reports_what_its_wire_costs(tmp_path):
    """Measured, not assumed: the interval an operator may ask for is bounded
    by it, and no configuration file can know it."""
    cfg, client = make_app(tmp_path)
    assert client.post("/api/endpoints", json=ENDPOINT_BODY).status_code == 200
    bus = client.get("/api/endpoints/sdm-endpoint").json()["bus"]
    # nothing has polled (the endpoint is created disabled), so nothing is claimed
    assert bus["lanes"] == 1 and bus["max_connections"] == 1
    assert bus["tx_p50_s"] is None and bus["floor_s"] is None
