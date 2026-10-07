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


# ── an endpoint can front an HTTP master ─────────────────────────────────────

def test_the_connection_substitutes_the_unit_id_too(tmp_path):
    """An HTTP master addresses its units by URL, not by a unit id inside a
    frame — a Fronius Solar API endpoint is `…?Scope=Device&DeviceId=${unit_id}`.
    Without substitution in the CONNECTION, an endpoint could not describe one at
    all and four inverters would need four hand-written devices."""
    cfg = write_config(tmp_path, extra_yaml="""
endpoints:
  - id: pvapi
    connection:
      protocol: http
      url: "http://10.0.0.9/solar_api/v1/x.cgi?DeviceId=${unit_id}&p=${endpoint_id}"
    units: [1, 2, 3]
""")
    # an http device carries its address in `http`, not in the Modbus block
    urls = {d.id: (d.http or {}).get('url') for d in cfg.endpoint_devices('pvapi')}
    assert urls['pvapi-u1'].endswith('DeviceId=1&p=pvapi')
    assert urls['pvapi-u2'].endswith('DeviceId=2&p=pvapi')
    assert urls['pvapi-u3'].endswith('DeviceId=3&p=pvapi')


def test_a_connection_without_placeholders_is_left_alone(tmp_path):
    """Substitution must not disturb the Modbus shape, where the unit id travels
    in the frame and every unit shares one host."""
    cfg = write_config(tmp_path, extra_yaml="""
endpoints:
  - id: plain
    connection: { protocol: tcp, host: 192.0.2.50, port: 502 }
    units: [1, 2]
""")
    devs = cfg.endpoint_devices('plain')
    assert {d.connection.host for d in devs} == {'192.0.2.50'}
    assert sorted(d.connection.unit_id for d in devs) == [1, 2]


def test_the_bundled_solar_api_template_reads_one_call_per_inverter():
    """What it can and cannot give decides what stays on Modbus."""
    from multibus.device_template import load_template
    t = load_template('multibus/device_templates/fronius_solar_api_inverter.json',
                      builtin=True)
    names = {r.name for r in t.registers}
    # everything comes from CommonInverterData, so ONE request per inverter
    assert all('Body.Data.' in (r.json_path or '') for r in t.registers)
    assert {'power_active_total', 'frequency', 'voltage_ln_avg',
            'current_total', 'voltage_dc', 'current_dc'} <= names
    # the four things only Modbus has must NOT be claimed here
    assert not (names & {'power_factor_total', 'power_reactive_total',
                         'power_apparent_total', 'event_flags_1'})


# ── groups: a plant holds more than one kind of thing ────────────────────────

PLANT_BODY = {
    "id": "plant", "name": "PV plant", "enabled": False,
    "connection": {"protocol": "tcp", "host": "192.0.2.30", "port": 502},
    "units": [1],
    "groups": [
        {"id": "inverters", "role": "inverter", "template": "eastron_sdm120",
         "connection": {"protocol": "tcp", "host": "192.0.2.30"},
         "units": [1, 2]},
        {"id": "grid", "role": "meter", "template": "eastron_sdm630",
         "connection": {"protocol": "tcp", "host": "192.0.2.30"},
         "units": [{"unit_id": 240, "id": "plant-grid-meter"}]},
    ],
}


@needs_tc
def test_a_plant_materializes_every_group(tmp_path):
    cfg, client = make_app(tmp_path)
    r = client.post("/api/endpoints", json=PLANT_BODY)
    assert r.status_code == 200, r.text
    ids = {d.id for d in cfg.endpoint_devices('plant')}
    assert ids == {'plant-u1', 'plant-u2', 'plant-grid-meter'}
    got = client.get("/api/endpoints/plant").json()
    groups = {g['id']: g for g in got['groups']}
    assert set(groups) == {'inverters', 'grid'}
    assert groups['grid']['role'] == 'meter'
    assert [u['device_id'] for u in groups['grid']['units']] == ['plant-grid-meter']
    assert groups['inverters']['total_units'] == 2


@needs_tc
def test_the_first_group_owns_the_endpoints_headline_topic(tmp_path):
    """Every topic and Influx series that predates groups must stay put."""
    cfg, client = make_app(tmp_path)
    assert client.post("/api/endpoints", json=PLANT_BODY).status_code == 200
    groups = client.get("/api/endpoints/plant").json()['groups']
    assert groups[0]['topic'] == 'mbg/endpoints/plant'
    # a group of ONE unit has no total — it would be that unit republished
    # under another name — so it promises no topic either
    assert groups[1]['total_units'] == 1 and groups[1]['topic'] is None
    assert groups[1]['aggregates'] == {}


@needs_tc
def test_one_unit_id_cannot_be_claimed_by_two_groups(tmp_path):
    """A unit id is an address on the wire: the same one twice would be two
    devices racing each other on it."""
    cfg, client = make_app(tmp_path)
    bad = dict(PLANT_BODY)
    bad['groups'] = [dict(PLANT_BODY['groups'][0]),
                     dict(PLANT_BODY['groups'][1], units=[2])]
    r = client.post("/api/endpoints", json=bad)
    assert r.status_code == 422
    assert any("already used by another group" in e
               for e in r.json()['detail']['errors'])


@needs_tc
def test_a_group_needs_units_and_a_real_template(tmp_path):
    cfg, client = make_app(tmp_path)
    for bad_group, needle in (
            ({"id": "g", "units": []}, "units"),
            ({"id": "g", "units": [1], "template": "nope"}, "template"),
            ({"id": "BAD ID", "units": [1], "template": "eastron_sdm120"}, "id")):
        body = dict(PLANT_BODY, id="probe", groups=[bad_group])
        r = client.post("/api/endpoints", json=body)
        assert r.status_code == 422, bad_group
        assert any(needle in e for e in r.json()['detail']['errors']), bad_group


@needs_tc
def test_a_group_carries_its_own_sources(tmp_path):
    cfg, client = make_app(tmp_path)
    body = dict(PLANT_BODY, groups=[
        dict(PLANT_BODY['groups'][0], sources=[
            {"id": "fast", "protocol": "http", "url": "http://h/x?d=${unit_id}",
             "template": "eastron_sdm120", "poll_groups": {"realtime": 5}},
            {"id": "full", "protocol": "tcp", "host": "192.0.2.30",
             "template": "eastron_sdm120"},
        ]),
        PLANT_BODY['groups'][1]])
    assert client.post("/api/endpoints", json=body).status_code == 200, 'create'
    groups = {g['id']: g for g in client.get("/api/endpoints/plant").json()['groups']}
    assert [s['id'] for s in groups['inverters']['sources']] == ['fast', 'full']
    assert [s['id'] for s in groups['grid']['sources']] == ['default']
    # the URL resolved per unit — on the SOURCE, which is what polls; the
    # device-level connection stays the group's fallback identity
    d = cfg.get_device('plant-u2')
    fast = next(x for x in d.sources if x.id == 'fast')
    assert fast.http['url'].endswith('d=2')
    assert [x.id for x in d.sources] == ['fast', 'full']


@needs_tc
def test_settings_only_update_preserves_connection_and_aggregate_prefix(tmp_path):
    """The endpoint settings dialog sends only the fields it knows. A save
    like that wiped connection.serial_port out from under 8 running rtu_tap
    units and dropped mqtt.aggregate_prefix (seplos cutover, 2026-10-07) —
    both must survive an edit that does not speak of them."""
    from tests.test_devices import write_config
    cfg = write_config(tmp_path, extra_yaml="""
endpoints:
  - id: bank
    name: Bank
    template: seplos_bms_v3_rtu_tap
    mqtt:
      enabled: true
      topic_prefix: seplos/battery_${unit_id}
      aggregate_prefix: seplos/pack
    influxdb:
      enabled: true
      bucket: seplos
    connection:
      protocol: rtu_tap
      serial_port: /dev/ttyTAP
      baudrate: 19200
    units: [1, 2]
""")
    from multibus.api import create_api
    devices = [(d, None) for d in cfg.devices]
    app, _ = create_api(cfg, None, None, None, devices=devices)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.put("/api/endpoints/bank",
                   json={"name": "Bank renamed", "enabled": True,
                         "mqtt": {"enabled": True, "ha_discovery": False}})
    if r.status_code == 405:
        r = client.patch("/api/endpoints/bank",
                         json={"name": "Bank renamed", "enabled": True,
                               "mqtt": {"enabled": True, "ha_discovery": False}})
    assert r.status_code == 200, r.text
    raw = cfg.get_raw_endpoint("bank")
    assert raw["connection"]["serial_port"] == "/dev/ttyTAP"
    assert raw["connection"]["protocol"] == "rtu_tap"
    assert raw["mqtt"]["aggregate_prefix"] == "seplos/pack"
    assert raw["mqtt"]["topic_prefix"] == "seplos/battery_${unit_id}"
    assert raw["influxdb"]["bucket"] == "seplos"
    assert raw["name"] == "Bank renamed"


TAGGED_BANK_YAML = """
endpoints:
  - id: bank
    name: Bank
    template: seplos_bms_v3_rtu_tap
    connection: { protocol: rtu_tap, serial_port: /dev/ttyTAP, baudrate: 19200 }
    units: [1, 2]
    mqtt: { topic_prefix: "seplos/battery_${unit_id}" }
    influxdb:
      bucket: seplos
      device_tag: "battery_${unit_id}"
      tags: { battery_id: "${unit_id}", bank: "${endpoint_id}" }
"""


def test_endpoint_influx_tags_substitute_per_unit(tmp_path):
    """`influxdb.tags` is a series identity per unit: the Seplos history is
    keyed on battery_id=N, so the bank declares it once with ${unit_id}."""
    cfg = write_config(tmp_path, extra_yaml=TAGGED_BANK_YAML)
    u1, u2 = cfg.endpoint_devices("bank")
    assert u1.influxdb_tags == {"battery_id": "1", "bank": "bank"}
    assert u2.influxdb_tags == {"battery_id": "2", "bank": "bank"}
    assert cfg.get_raw_endpoint("bank")["influxdb"]["tags"]["battery_id"] == "${unit_id}"


def test_malformed_influx_tags_are_ignored_not_fatal(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=TAGGED_BANK_YAML.replace(
        'tags: { battery_id: "${unit_id}", bank: "${endpoint_id}" }', 'tags: oops'))
    assert [d.influxdb_tags for d in cfg.endpoint_devices("bank")] == [{}, {}]


@needs_tc
def test_endpoint_edit_keeps_its_influx_tags(tmp_path):
    """Tags are pinned like bucket/device_tag: the settings dialog sends an
    influxdb block without them, and a save must not split the series."""
    from multibus.api import create_api
    cfg = write_config(tmp_path, extra_yaml=TAGGED_BANK_YAML)
    app, _ = create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    r = client.put("/api/endpoints/bank", json={"name": "Bank", "enabled": True,
                                                "influxdb": {"enabled": True}})
    assert r.status_code == 200, r.text
    assert cfg.get_raw_endpoint("bank")["influxdb"]["tags"] == {
        "battery_id": "${unit_id}", "bank": "${endpoint_id}"}
    assert cfg.get_device("bank-u2").influxdb_tags["battery_id"] == "2"


def test_build_point_device_tags_never_displace_identity_or_register_tags():
    from types import SimpleNamespace
    from multibus.influxdb_publisher import build_point
    reg = SimpleNamespace(address=4096, name="pack_voltage", influxdb_tags={"bank": "own"},
                          influxdb_measurement="seplos_battery", measurement="seplos_battery",
                          influxdb_field="", unit="V")
    line = build_point(reg, 52.6, 1_700_000_000.0, poll_group="tap", device_tag="battery_3",
                       extra_tags={"battery_id": "3", "bank": "x", "device": "evil",
                                   "poll_group": "evil"}).to_line_protocol()
    assert "battery_id=3" in line and "device=battery_3" in line
    assert "bank=own" in line and "evil" not in line


@needs_tc
def test_endpoint_outputs_and_tags_are_validated_and_editable(tmp_path):
    """Outputs and tags are the operator's: sent from the page they replace
    the stored ones, omitted by the settings dialog they survive, malformed
    they are refused with the field named."""
    from multibus.api import create_api
    cfg = write_config(tmp_path, extra_yaml=TAGGED_BANK_YAML)
    app, _ = create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    out = {"id": "legacy-pack", "measurement": "seplos_pack",
           "fields": [{"name": "total_power", "source": "pack_total_power"},
                      {"name": "energy_remaining", "source": "pack_energy_remaining",
                       "scale": 0.001}]}
    body = {"name": "Bank", "enabled": True,
            "influxdb": {"enabled": True, "tags": {"battery_id": "${unit_id}"},
                         "outputs": [out]}}
    r = client.put("/api/endpoints/bank", json=body)
    assert r.status_code == 200, r.text
    ix = cfg.get_raw_endpoint("bank")["influxdb"]
    assert ix["tags"] == {"battery_id": "${unit_id}"}          # edited: 'bank' tag gone
    assert ix["outputs"][0]["measurement"] == "seplos_pack"
    assert cfg.get_device("bank-u1").influxdb_tags == {"battery_id": "1"}
    # the settings dialog omits both → both kept
    r = client.put("/api/endpoints/bank", json={"name": "Bank 2", "enabled": True,
                                                "influxdb": {"enabled": True}})
    assert r.status_code == 200, r.text
    ix = cfg.get_raw_endpoint("bank")["influxdb"]
    assert ix["outputs"][0]["id"] == "legacy-pack" and ix["tags"]["battery_id"] == "${unit_id}"
    # malformed: every problem named
    bad = {"id": "Bad Id", "measurement": "_time", "mode": "sometimes",
           "fields": [{"name": "a", "source": ""}, {"name": "a", "source": "x", "scale": 0}]}
    r = client.put("/api/endpoints/bank", json={"name": "Bank", "enabled": True,
                   "influxdb": {"tags": {"_field": "x"}, "outputs": [bad]}})
    assert r.status_code == 422
    msg = json.dumps(r.json())
    for frag in ("outputs[0].id", "outputs[0].measurement", "outputs[0].mode",
                 "fields[0].source", "fields[1].name", "fields[1].scale", "tags._field"):
        assert frag in msg, frag


@needs_tc
def test_endpoint_totals_are_shown_validated_and_kept(tmp_path):
    from multibus.api import create_api
    cfg = write_config(tmp_path, extra_yaml=TAGGED_BANK_YAML)
    app, _ = create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    tot = {"pack_max_temp": {"op": "max", "from": ["max_cell_temp", "ambient_temp"]}}
    r = client.put("/api/endpoints/bank", json={"name": "Bank", "enabled": True, "totals": tot})
    assert r.status_code == 200, r.text
    ep = client.get("/api/endpoints/bank").json()
    ep = ep.get("endpoint", ep)
    assert ep["totals"] == tot
    assert "totals_declared" in ep and "unit_fields" in ep
    # omitted → kept; {} → cleared
    client.put("/api/endpoints/bank", json={"name": "Bank", "enabled": True})
    assert cfg.get_raw_endpoint("bank")["totals"] == tot
    client.put("/api/endpoints/bank", json={"name": "Bank", "enabled": True, "totals": {}})
    assert "totals" not in cfg.get_raw_endpoint("bank")
    r = client.put("/api/endpoints/bank", json={"name": "Bank", "enabled": True,
                   "totals": {"t": {"op": "median", "from": []}}})
    assert r.status_code == 422
    assert "totals.t.op" in json.dumps(r.json()) and "totals.t.from" in json.dumps(r.json())


def test_template_refresh_updates_metadata_and_keeps_the_operators(tmp_path):
    """A seeded file gets the template's labels/units/aggregates; selection,
    dashboard flags and calculated expressions stay as the operator left them."""
    import json as _json
    from types import SimpleNamespace as NS
    from multibus.template_refresh import refresh_file
    f = tmp_path / "selected_registers.json"
    f.write_text(_json.dumps({"version": "1.0", "poll_groups": {"tap": {"interval": 1}},
        "registers": [
            {"address": 1, "name": "maxdiscurt", "label": "MaxDisCurt", "unit": "A",
             "ui": {"show_on_dashboard": True}, "aggregates": {"old": "sum"}},
            {"address": 2, "name": "operator_only", "label": "mine"}],
        "calculated": [{"name": "power", "expr": "my * own", "label": "Power",
                        "aggregates": {"pack_total_power": "sum", "power_active_total": "sum"}}]}))
    tpl = NS(registers=[NS(name="maxdiscurt", label="Max discharge current (BMS limit)",
                           unit="A", description="", category="battery",
                           aggregates={"pack_max_discharge_current": "sum"})],
             calculated=[NS(name="power", label="Power (− charging)", unit="W",
                            aggregates={"pack_total_power": "sum"})])
    assert refresh_file(f, tpl) == {"registers": 1, "calculated": 1}
    d = _json.loads(f.read_text())
    r0, r1, c0 = d["registers"][0], d["registers"][1], d["calculated"][0]
    assert r0["label"] == "Max discharge current (BMS limit)" and r0["category"] == "battery"
    assert r0["aggregates"] == {"pack_max_discharge_current": "sum"}
    assert r0["ui"] == {"show_on_dashboard": True}           # operator's flag kept
    assert r1 == {"address": 2, "name": "operator_only", "label": "mine"}
    assert c0["expr"] == "my * own" and c0["label"] == "Power (− charging)"
    assert c0["aggregates"] == {"pack_total_power": "sum"}   # the duplicate total is gone
    assert d["poll_groups"] == {"tap": {"interval": 1}}      # other keys survive
    assert refresh_file(f, tpl) == {"registers": 0, "calculated": 0}   # idempotent


@needs_tc
def test_an_endpoint_created_at_runtime_runs_its_template_formulas(tmp_path):
    """The calc engine loaded formulas at boot only: a bank created from the UI
    had its template's derived fields (a pack's power) saved but never run
    until the next restart."""
    from multibus.api import create_api
    cfg = write_config(tmp_path)
    app, _ = create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/endpoints", json={
        "id": "bank", "name": "Bank", "template": "seplos_bms_v3_rtu_tap", "enabled": True,
        "connection": {"protocol": "rtu_tap", "serial_port": "/dev/null-tap-rt", "baudrate": 9600},
        "units": [1, 2], "influxdb": {"enabled": False}})
    assert r.status_code == 200, r.text
    names = {e["_reg"].name for e in (app.state.calc_engine.store.get("bank-u1") or [])}
    assert {"power", "cell_delta", "alarm_count"} <= names, names
