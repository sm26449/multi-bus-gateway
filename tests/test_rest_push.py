# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Generic REST push sink: payload builder, config persistence, API + masking."""
from multibus import rest_push as rp
from multibus.config import Config

from tests.test_devices_api import make_app, needs_tc

_MASK = "••••••"


def test_build_payload_snapshot_under_concurrent_mutation():
    # A poller thread inserts/removes addresses while a consumer iterates the store.
    # Without the list() snapshot this raises "dictionary changed size during
    # iteration" intermittently (the systemic value-store race).
    import threading
    store = {i: {"name": f"r{i}", "value": float(i), "unit": "", "timestamp": "t"}
             for i in range(300)}
    stop = threading.Event()

    def churn():
        n = 100000
        while not stop.is_set():
            store[n] = {"name": f"r{n}", "value": 1.0, "timestamp": "t"}
            store.pop(n, None)
            n += 1

    t = threading.Thread(target=churn, daemon=True)
    t.start()
    try:
        for _ in range(2000):
            rp.build_payload("d", "D", store, "native")   # must never raise
    finally:
        stop.set()
        t.join(timeout=1)


def test_build_payload_native_and_flat():
    store = {
        100: {"value": 231.2, "name": "_ULN1", "unit": "V", "timestamp": "2026-07-05T10:00:00"},
        102: {"value": 49.98, "name": "_FREQ", "unit": "Hz", "timestamp": "2026-07-05T10:00:01"},
        104: {"value": 5.0, "name": "", "unit": "", "timestamp": "x"},   # no name → skipped
    }
    native = rp.build_payload("umg512", "Janitza", store, "native")
    assert native["device"] == "umg512" and native["ts"] == "2026-07-05T10:00:01"
    assert native["values"]["_ULN1"] == {"value": 231.2, "unit": "V", "ts": "2026-07-05T10:00:00"}
    assert "" not in native["values"]
    flat = rp.build_payload("umg512", "Janitza", store, "flat")
    assert flat["values"] == {"_ULN1": 231.2, "_FREQ": 49.98}


@needs_tc
def test_rest_push_validation_and_persist(tmp_path):
    _cfg, client = make_app(tmp_path)
    # enabled without a url → 422
    assert client.post("/api/devices/umg512/rest-push",
                       json={"enabled": True}).status_code == 422
    # too-small interval → 422
    assert client.post("/api/devices/umg512/rest-push",
                       json={"enabled": True, "url": "http://x/y", "interval_s": 1}).status_code == 422
    # valid config with a secret header
    r = client.post("/api/devices/umg512/rest-push", json={
        "enabled": True, "url": "http://192.0.2.9/ingest", "interval_s": 20,
        "format": "flat", "headers": {"Authorization": "Bearer SECRET"}})
    assert r.status_code == 200
    rpub = r.json()["rest_push"]
    assert rpub["url"] == "http://192.0.2.9/ingest" and rpub["format"] == "flat"
    assert rpub["headers"]["Authorization"] == _MASK          # masked on echo
    # persisted (primary → flat rest_push section) with the REAL secret
    reloaded = Config(str(tmp_path / "config.yaml"))
    saved = reloaded.get_device("umg512").rest_push
    assert saved["enabled"] and saved["interval_s"] == 20
    assert saved["headers"]["Authorization"] == "Bearer SECRET"


@needs_tc
def test_rest_push_masked_header_preserved_on_resave(tmp_path):
    _cfg, client = make_app(tmp_path)
    client.post("/api/devices/umg512/rest-push", json={
        "enabled": True, "url": "http://192.0.2.9/i",
        "headers": {"Authorization": "Bearer SECRET"}})
    # re-save sending back the MASK → the real secret must survive
    client.post("/api/devices/umg512/rest-push", json={
        "enabled": True, "url": "http://192.0.2.9/i", "interval_s": 15,
        "headers": {"Authorization": _MASK}})
    saved = Config(str(tmp_path / "config.yaml")).get_device("umg512").rest_push
    assert saved["headers"]["Authorization"] == "Bearer SECRET"


@needs_tc
def test_import_rejects_zip_bomb(tmp_path):
    import io
    import json
    import zipfile
    _cfg, client = make_app(tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("config.yaml", b"\x00" * (60 * 1024 * 1024))   # 60 MB → tiny compressed
    body = buf.getvalue()
    assert len(body) < 25 * 1024 * 1024                            # passes the body cap
    r = client.post("/api/config/import", content=body,
                    headers={"Content-Type": "application/zip"})
    assert r.status_code == 422 and "expands too large" in json.dumps(r.json())


@needs_tc
def test_export_redacts_device_secrets(tmp_path):
    import io
    import zipfile
    import yaml
    _cfg, client = make_app(tmp_path)
    # primary REST-push with a bearer header
    client.post("/api/devices/umg512/rest-push", json={
        "enabled": True, "url": "http://192.0.2.9/i",
        "headers": {"Authorization": "Bearer SECRET"}})
    # a non-primary MQTT-input device with a broker password
    client.post("/api/devices", json={"id": "mq1", "enabled": False,
                "template": "mqtt_json_generic",
                "connection": {"protocol": "mqtt", "broker": "b", "topic": "t",
                               "password": "BROKERPW"}})
    r = client.get("/api/config/export")           # default include_secrets=false
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.content))
    conf = yaml.safe_load(z.read("config.yaml"))
    blob = yaml.dump(conf)
    assert "SECRET" not in blob and "BROKERPW" not in blob   # neither leaks
    assert "headers" not in (conf.get("rest_push") or {})    # primary rest_push headers gone
    dev = next(d for d in conf.get("devices", []) if d.get("id") == "mq1")
    assert "password" not in (dev.get("connection") or {})   # broker password gone


@needs_tc
def test_rest_push_sink_and_test_endpoint(tmp_path):
    _cfg, client = make_app(tmp_path)
    # test before configuring → 422 (no url)
    assert client.post("/api/devices/umg512/rest-push/test").status_code == 422
    client.post("/api/devices/umg512/rest-push",
                json={"enabled": True, "url": "http://127.0.0.1:9/none"})
    # sink surfaced in /api/devices
    dev = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "umg512")
    assert dev["sinks"]["rest"]["enabled"] is True
    assert dev["sinks"]["rest"]["url"] == "http://127.0.0.1:9/none"
    # test push attempts and reports a result (connection refused → ok False, no crash)
    tr = client.post("/api/devices/umg512/rest-push/test").json()
    assert tr["status"] == "ok" and tr["ok"] is False


@needs_tc
def test_export_redacts_nested_source_secrets_and_import_restores_them(tmp_path):
    """F-05 (3.83.0): a "sanitized" export any viewer can download stripped
    only connection.* and rest_push.*; http.headers, mqtt_in.password and
    the credentials of installation sources went out in clear. The merge-
    import must bring the live values back for a backup taken this way."""
    import io
    import zipfile
    import yaml
    _cfg, client = make_app(tmp_path)
    cfg_path = tmp_path / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text()) or {}
    data.setdefault("devices", []).append({
        "id": "h1", "enabled": False, "template": "mqtt_json_generic",
        "http": {"url": "http://192.0.2.7/api?token=HTTPTOKEN", "headers": {"Authorization": "Bearer HTTPSECRET"}},
        "mqtt_in": {"broker": "b", "topic": "t", "password": "MQTTINPW"},
        "sources": [{"id": "s1", "protocol": "http", "url": "http://user:SRCPW@192.0.2.8/x",
                     "headers": {"X-Key": "SRCHEADER"}}]})
    data["endpoints"] = [{"id": "ep1", "enabled": False, "groups": [
        {"id": "g", "role": "inverter", "units": [1], "sources": [
            {"id": "solar", "protocol": "http", "url": "http://192.0.2.9/solar?api_key=EPTOKEN",
             "headers": {"Authorization": "Bearer EPSECRET"}}]}]}]
    cfg_path.write_text(yaml.safe_dump(data, sort_keys=False))
    r = client.get("/api/config/export")
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.content))
    conf = yaml.safe_load(z.read("config.yaml"))
    blob = yaml.dump(conf)
    for s in ("HTTPTOKEN", "HTTPSECRET", "MQTTINPW", "SRCPW", "SRCHEADER", "EPTOKEN", "EPSECRET"):
        assert s not in blob, s
    # import the sanitized backup back over the live config: secrets return
    from multibus.snapshots import write_bundle_files
    zin = zipfile.ZipFile(io.BytesIO(r.content))
    write_bundle_files(zin, cfg_dir=tmp_path, user_tpl_dir=tmp_path / "ut",
                       registers_path_for=lambda d: tmp_path / "devices" / d / "selected_registers.json",
                       replace_config=False)
    after = yaml.safe_load(cfg_path.read_text())
    h1 = next(d for d in after["devices"] if d["id"] == "h1")
    assert h1["http"]["headers"] == {"Authorization": "Bearer HTTPSECRET"}
    assert h1["http"]["url"] == "http://192.0.2.7/api?token=HTTPTOKEN"
    assert h1["mqtt_in"]["password"] == "MQTTINPW"
    assert h1["sources"][0]["url"] == "http://user:SRCPW@192.0.2.8/x"
    assert h1["sources"][0]["headers"] == {"X-Key": "SRCHEADER"}
    src = after["endpoints"][0]["groups"][0]["sources"][0]
    assert src["url"] == "http://192.0.2.9/solar?api_key=EPTOKEN"
    assert src["headers"] == {"Authorization": "Bearer EPSECRET"}


@needs_tc
def test_import_meets_the_live_validators_before_writing(tmp_path):
    """F-18 (3.83.0): a backup with an allowlist entry that does not parse,
    a non-http webhook, a non-boolean write flag or an off-LAN HTTP source is
    refused before a byte is written; a clean one still imports."""
    import io
    import zipfile
    import yaml
    _cfg, client = make_app(tmp_path)
    before = (tmp_path / "config.yaml").read_text()

    def bundle(cfg):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("config.yaml", yaml.safe_dump(cfg))
            z.writestr("manifest.json", '{"backup_version": 1}')
        return buf.getvalue()
    bad = [
        {"security": {"allowlist": ["not-a-network"]}},
        {"ui": {"trusted_proxies": ["10.0.0.0/33"]}},
        {"alerts": {"webhook_url": "ftp://x/y"}},
        {"security": {"allow_writes": "yes"}},
        {"devices": [{"id": "h", "http": {"url": "http://8.8.8.8/x"}}]},
    ]
    for cfg in bad:
        r = client.post("/api/config/import", content=bundle(cfg), headers={"Content-Type": "application/zip"})
        assert r.status_code == 422, (cfg, r.text)
    assert (tmp_path / "config.yaml").read_text() == before        # nothing written
    r = client.post("/api/config/import", content=bundle({"security": {"allowlist": ["192.168.1.0/24"]}}),
                    headers={"Content-Type": "application/zip"})
    assert r.status_code == 200, r.text
