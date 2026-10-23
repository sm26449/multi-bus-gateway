# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The device list at scale and the template list's vendors (findability
code review M9, M7): a register file not changed since it was read is not
read, parsed or snapshotted again; a change is seen at once; a template
that names its maker `manufacturer` is listed under that vendor."""
import json
import os
import time

from tests.test_devices_api import make_app, needs_tc


def _make(client, did):
    r = client.post("/api/devices", json={"id": did, "name": did, "template": "eastron_sdm630", "enabled": False,
                                          "connection": {"protocol": "tcp", "host": "127.0.0.1", "port": 15020, "unit_id": 1}})
    assert r.status_code == 200, r.text


@needs_tc
def test_the_device_list_reads_a_register_file_once_per_version(tmp_path):
    cfg, client = make_app(tmp_path)
    _make(client, "em-a")
    path = cfg.device_registers_path("em-a")
    good = path.with_suffix(path.suffix + ".good")
    n0 = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "em-a")["selected_registers"]
    assert n0 > 0 and good.exists()
    stamp = good.stat().st_mtime_ns
    reads = []
    real = cfg._load_json_with_heal
    cfg._load_json_with_heal = lambda *a, **k: (reads.append(a[0]), real(*a, **k))[1]
    time.sleep(0.01)
    for _ in range(3):
        assert client.get("/api/devices").status_code == 200
    assert not [p for p in reads if str(p) == str(path)]      # not read again
    assert good.stat().st_mtime_ns == stamp                     # not snapshotted again


@needs_tc
def test_a_changed_register_file_is_seen_at_once(tmp_path):
    cfg, client = make_app(tmp_path)
    _make(client, "em-b")
    row = lambda: next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "em-b")
    n0 = row()["selected_registers"]
    path = cfg.device_registers_path("em-b")
    data = json.loads(path.read_text())
    data["registers"] = data["registers"][:2]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)                                       # how every save writes it
    assert n0 > 2 and row()["selected_registers"] == 2


@needs_tc
def test_the_list_never_shares_its_registers_with_a_poller(tmp_path):
    cfg, client = make_app(tmp_path)
    _make(client, "em-c")
    dev = cfg.get_device("em-c")
    a = cfg.load_device_registers(dev)[0]
    b = cfg.load_device_registers(dev)[0]
    assert a and a[0] is not b[0]                               # copies for those who may change them
    a[0].name = "changed"
    assert cfg.load_device_registers(dev)[0][0].name != "changed"


@needs_tc
def test_a_template_naming_its_maker_manufacturer_is_listed_under_it(tmp_path):
    cfg, client = make_app(tmp_path)
    tpls = {t["id"]: t for t in client.get("/api/device-templates").json()["templates"]}
    for tid in ("fronius_solar_api_inverter", "fronius_solar_api_inverter_3p", "fronius_solar_api_site"):
        assert tpls[tid]["vendor"] == "Fronius", tid
    got = client.get("/api/device-templates", params={"q": "fronius"}).json()["templates"]
    assert {"fronius_solar_api_inverter", "fronius_solar_api_site"} <= {t["id"] for t in got}
