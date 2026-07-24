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
"""Device Builder routes — the ESPHome-dashboard proxy.

The dashboard itself is faked (no network): from_config() is monkeypatched to
return a FakeDashboard, so these tests pin OUR contract — feature gating,
filename validation, overwrite semantics, error mapping (EsphomeError→502),
settings persistence/redaction, and the WS command stream relay.
"""
import json

import pytest

from multibus.config import Config
from multibus.esphome_client import EsphomeDashboard, EsphomeError
from multibus.routes import builder_routes

from tests.test_devices import write_config
from tests.test_devices_api import make_app, needs_tc


# ---------------------------------------------------------------------------
# esphome_client unit surface (no network)
# ---------------------------------------------------------------------------

def test_client_url_validation():
    c = EsphomeDashboard("https://esphome.lan:6052")
    assert c.base == "https://esphome.lan:6052"
    assert c.ws_base == "wss://esphome.lan:6052"
    for bad in ("", "esphome:6052", "ftp://x", "http://"):
        with pytest.raises(EsphomeError):
            EsphomeDashboard(bad)


# ---------------------------------------------------------------------------
# fakes + fixtures
# ---------------------------------------------------------------------------

class FakeDashboard:
    """In-memory stand-in for one ESPHome dashboard."""

    def __init__(self):
        self.base = "http://fake:6052"
        self.files = {}                 # name -> yaml text
        self.platforms = {}             # name -> target_platform
        self.archived = []
        self.fail = False               # flip to simulate unreachable

    def _check(self):
        if self.fail:
            raise EsphomeError("ESPHome unreachable at http://fake:6052: down")

    def version(self):
        self._check()
        return "2026.5.3"

    def devices(self):
        self._check()
        return {"configured": [
            {"name": n.rsplit(".", 1)[0], "configuration": n,
             "target_platform": self.platforms.get(n, "ESP32"),
             "current_version": "2026.5.3"}
            for n in sorted(self.files)], "importable": []}

    def get_config(self, name):
        self._check()
        if name not in self.files:
            raise EsphomeError(f"{name}: not found on the ESPHome dashboard")
        return self.files[name]

    def save_config(self, name, content):
        self._check()
        self.files[name] = content

    def archive(self, name):
        self._check()
        if name not in self.files:
            raise EsphomeError(f"{name}: not found on the ESPHome dashboard")
        self.files.pop(name)
        self.archived.append(name)

    def downloads(self, name):
        self._check()
        return [{"title": "Factory format", "description": "flash from browser",
                 "file": "firmware.factory.bin", "download": f"{name}.factory.bin"}]

    def download_bin(self, name, file, download=""):
        self._check()
        return b"\xe9BINARY", download or f"{name}-{file}"

    async def stream_command(self, command, configuration, port="OTA", extra=None):
        self._check()
        yield {"event": "line", "data": f"INFO {command} {configuration}\n"}
        yield {"event": "line", "data": "INFO done\n"}
        yield {"event": "exit", "code": 0}


ESPHOME_YAML = "esphome:\n  enabled: true\n  url: http://fake:6052\n"


@pytest.fixture
def fake(monkeypatch):
    dash = FakeDashboard()
    monkeypatch.setattr(builder_routes, "from_config", lambda cfg: dash)
    monkeypatch.setattr(builder_routes, "_clients", {})
    return dash


# ---------------------------------------------------------------------------
# feature gating
# ---------------------------------------------------------------------------

@needs_tc
def test_builder_disabled_by_default(tmp_path):
    _, client = make_app(tmp_path)
    st = client.get("/api/builder/status").json()
    assert st == {"enabled": False, "reachable": False, "version": "", "url": ""}
    assert client.get("/api/builder/nodes").status_code == 503


@needs_tc
def test_builder_status_reachable_and_down(tmp_path, fake):
    _, client = make_app(tmp_path, extra_yaml=ESPHOME_YAML)
    st = client.get("/api/builder/status").json()
    assert st["enabled"] and st["reachable"] and st["version"] == "2026.5.3"

    fake.fail = True
    st = client.get("/api/builder/status").json()
    assert st["enabled"] and not st["reachable"] and "unreachable" in st["error"]
    # EsphomeError on a data route maps to 502 (bad gateway), not a crash
    assert client.get("/api/builder/nodes").status_code == 502


# ---------------------------------------------------------------------------
# node YAML CRUD
# ---------------------------------------------------------------------------

@needs_tc
def test_node_yaml_crud_roundtrip(tmp_path, fake):
    _, client = make_app(tmp_path, extra_yaml=ESPHOME_YAML)

    # create
    rsp = client.put("/api/builder/nodes/node1.yaml/config",
                     json={"content": "esphome:\n  name: node1\n"})
    assert rsp.status_code == 200

    # list sees it
    nodes = client.get("/api/builder/nodes").json()
    assert [n["configuration"] for n in nodes["configured"]] == ["node1.yaml"]

    # read back verbatim
    rsp = client.get("/api/builder/nodes/node1.yaml/config")
    assert rsp.status_code == 200
    assert rsp.text == "esphome:\n  name: node1\n"

    # overwrite refused without the flag, allowed with it
    rsp = client.put("/api/builder/nodes/node1.yaml/config",
                     json={"content": "changed: true\n"})
    assert rsp.status_code == 409
    rsp = client.put("/api/builder/nodes/node1.yaml/config",
                     json={"content": "changed: true\n", "overwrite": True})
    assert rsp.status_code == 200
    assert fake.files["node1.yaml"] == "changed: true\n"

    # archive
    assert client.delete("/api/builder/nodes/node1.yaml").status_code == 200
    assert fake.archived == ["node1.yaml"]
    assert client.get("/api/builder/nodes/node1.yaml/config").status_code == 502


@needs_tc
def test_node_yaml_validation(tmp_path, fake):
    _, client = make_app(tmp_path, extra_yaml=ESPHOME_YAML)
    # path traversal / bad names / bad extension
    for bad in ("../secrets.yaml", "a b.yaml", ".hidden.yaml", "node.txt", "x"):
        assert client.put(f"/api/builder/nodes/{bad}/config",
                          json={"content": "x: 1\n"}).status_code in (404, 422)
    # empty and oversized content
    assert client.put("/api/builder/nodes/n.yaml/config",
                      json={"content": "  "}).status_code == 422
    assert client.put("/api/builder/nodes/n.yaml/config",
                      json={"content": "x" * (512 * 1024 + 1)}).status_code == 422


# ---------------------------------------------------------------------------
# artifacts
# ---------------------------------------------------------------------------

@needs_tc
def test_downloads_and_binary(tmp_path, fake):
    _, client = make_app(tmp_path, extra_yaml=ESPHOME_YAML)
    fake.files["node1.yaml"] = "esphome: {}\n"

    d = client.get("/api/builder/nodes/node1.yaml/downloads").json()["downloads"]
    assert d[0]["file"] == "firmware.factory.bin"

    rsp = client.get("/api/builder/nodes/node1.yaml/download",
                     params={"file": "firmware.factory.bin"})
    assert rsp.status_code == 200
    assert rsp.content == b"\xe9BINARY"
    assert "attachment" in rsp.headers["content-disposition"]


# ---------------------------------------------------------------------------
# settings persistence
# ---------------------------------------------------------------------------

@needs_tc
def test_settings_roundtrip_redaction_and_persistence(tmp_path):
    cfg, client = make_app(tmp_path)

    # defaults: disabled, nothing stored
    s = client.get("/api/builder/settings").json()
    assert s == {"enabled": False, "url": "", "username": "",
                 "password_set": False, "timeout_s": 10.0}

    # invalid: enabled without a proper URL / bad timeout
    assert client.post("/api/builder/settings",
                       json={"enabled": True, "url": "esphome:6052"}).status_code == 422
    assert client.post("/api/builder/settings",
                       json={"enabled": False, "url": "", "timeout_s": 0}).status_code == 422

    # save with password → GET redacts it, YAML on disk has it
    rsp = client.post("/api/builder/settings", json={
        "enabled": True, "url": "http://esphome:6052",
        "username": "u", "password": "secret", "timeout_s": 5})
    assert rsp.status_code == 200
    s = client.get("/api/builder/settings").json()
    assert s["password_set"] is True and "secret" not in json.dumps(s)

    # empty password on a later save keeps the stored secret
    rsp = client.post("/api/builder/settings", json={
        "enabled": True, "url": "http://esphome:6052", "username": "u"})
    assert rsp.status_code == 200
    assert cfg.esphome["password"] == "secret"

    # block survives an unrelated config save + a fresh load (alerts pattern)
    cfg.save_yaml_config()
    cfg2 = Config(str(tmp_path / "config.yaml"))
    assert cfg2.esphome["enabled"] is True
    assert cfg2.esphome["password"] == "secret"


def test_esphome_block_preserved_without_api(tmp_path):
    cfg = write_config(tmp_path, extra_yaml=ESPHOME_YAML)
    assert cfg.esphome["enabled"] is True
    cfg.save_yaml_config()
    assert Config(str(tmp_path / "config.yaml")).esphome["url"] == "http://fake:6052"


def test_esphome_env_seeds_fresh_deploy(tmp_path, monkeypatch):
    """Zero-config: ESPHOME_URL alone (no esphome: block yet) enables the
    Builder — this is what the bundled compose service relies on."""
    monkeypatch.setenv("ESPHOME_URL", "http://esphome:6052")
    cfg = write_config(tmp_path)                       # no esphome: block
    assert cfg.esphome == {"url": "http://esphome:6052", "enabled": True}


def test_esphome_env_respects_saved_user_choice(tmp_path, monkeypatch):
    """Once a block exists, the UI's enabled/disabled choice wins over the
    seed URL; only an explicit ESPHOME_ENABLED forces it."""
    monkeypatch.setenv("ESPHOME_URL", "http://esphome:6052")
    off = "esphome:\n  enabled: false\n  url: http://esphome:6052\n"
    cfg = write_config(tmp_path, extra_yaml=off)
    assert cfg.esphome["enabled"] is False             # user choice sticks

    monkeypatch.setenv("ESPHOME_ENABLED", "true")
    cfg = Config(str(tmp_path / "config.yaml"))
    assert cfg.esphome["enabled"] is True              # explicit override


def test_esphome_env_password_never_written_to_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("ESPHOME_URL", "http://esphome:6052")
    monkeypatch.setenv("ESPHOME_PASSWORD", "env-secret")
    cfg = write_config(tmp_path)
    assert cfg.esphome["password"] == "env-secret"     # live client uses it
    cfg.save_yaml_config()
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "env-secret" not in text
    # and a config-file password survives the shadow round-trip
    monkeypatch.delenv("ESPHOME_PASSWORD")
    monkeypatch.delenv("ESPHOME_URL")
    yml = "esphome:\n  enabled: true\n  url: http://x:6052\n  password: cfg-pass\n"
    cfg = write_config(tmp_path, extra_yaml=yml)
    monkeypatch.setenv("ESPHOME_PASSWORD", "env-secret")
    cfg = Config(str(tmp_path / "config.yaml"))
    assert cfg.esphome["password"] == "env-secret"
    cfg.save_yaml_config()
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "cfg-pass" in text and "env-secret" not in text


# ---------------------------------------------------------------------------
# generator + adopt chain
# ---------------------------------------------------------------------------

GEN_PAYLOAD = {
    "template_id": "eastron_sdm630",
    "node": {"name": "hala-sdm630"},
    "uart": {"tx_pin": "GPIO17", "rx_pin": "GPIO16"},
    "modbus": {"unit_id": 2},
    "mqtt": {},
    "registers": ["V_L1", "Import_kWh"],
}


@needs_tc
def test_generate_from_builtin_template(tmp_path, fake):
    _, client = make_app(tmp_path, extra_yaml=ESPHOME_YAML)
    rsp = client.post("/api/builder/generate", json=GEN_PAYLOAD)
    assert rsp.status_code == 200, rsp.text
    out = rsp.json()
    assert out["node_yaml_name"] == "hala-sdm630.yaml"
    # broker inherited from the gateway's own MQTT config (write_config)
    assert 'broker: "mosquitto"' in out["yaml"]
    assert len(out["topics"]) == 2
    # paired artifacts are directly consumable by the existing endpoints
    tpl = out["device_template"]["device_template"]
    assert tpl["id"] == "esphome_hala_sdm630"
    assert tpl["protocol"]["transports"] == ["mqtt"]
    from multibus.device_template import validate_template
    assert validate_template(out["device_template"]) == []
    assert out["device_payload"]["connection"]["topic"] == "esphome/hala-sdm630/#"


@needs_tc
def test_generate_validation_and_gating(tmp_path, fake):
    _, client = make_app(tmp_path, extra_yaml=ESPHOME_YAML)
    # unknown template
    rsp = client.post("/api/builder/generate",
                      json={**GEN_PAYLOAD, "template_id": "nope"})
    assert rsp.status_code == 404
    # MQTT template refused (nothing to transpile)
    rsp = client.post("/api/builder/generate",
                      json={**GEN_PAYLOAD, "template_id": "zigbee2mqtt_sensor"})
    assert rsp.status_code == 422
    # generator ValueError → 422 with message
    rsp = client.post("/api/builder/generate",
                      json={**GEN_PAYLOAD, "node": {"name": "Bad Name"}})
    assert rsp.status_code == 422
    assert "mDNS-safe" in rsp.json()["detail"]["errors"][0]


@needs_tc
def test_adopt_chain_creates_paired_device(tmp_path, fake, monkeypatch):
    """The full Adopt flow the UI drives: generate → save YAML on the
    dashboard → upload paired template → create the mqtt-in device."""
    import multibus.device_template as dt
    monkeypatch.setattr(dt, 'USER_DIR', tmp_path / 'device_templates')
    _, client = make_app(tmp_path, extra_yaml=ESPHOME_YAML)

    out = client.post("/api/builder/generate", json=GEN_PAYLOAD).json()
    assert client.put(f"/api/builder/nodes/{out['node_yaml_name']}/config",
                      json={"content": out["yaml"]}).status_code == 200
    assert client.post("/api/device-templates/upload",
                       json={"template": out["device_template"]}).status_code == 200
    rsp = client.post("/api/devices", json=out["device_payload"])
    assert rsp.status_code == 200, rsp.text

    # the device exists, uses the paired template, and auto-seeded registers
    # carry the firmware's exact topics with no json_path and scale 1
    devs = {d["id"]: d for d in client.get("/api/devices").json()["devices"]}
    assert "hala-sdm630" in devs
    assert devs["hala-sdm630"]["template"] == "esphome_hala_sdm630"
    regs = client.get("/api/registers/selected",
                      params={"device": "hala-sdm630"}).json()["registers"]
    by_name = {r["name"]: r for r in regs}
    assert set(by_name) == {"V_L1", "Import_kWh"}
    assert by_name["V_L1"]["topic"] == "esphome/hala-sdm630/V_L1/state"
    assert not by_name["V_L1"].get("json_path")
    assert float(by_name["V_L1"].get("scale", 1) or 1) == 1.0


@needs_tc
def test_secrets_ensure_appends_missing_only(tmp_path, fake):
    _, client = make_app(tmp_path, extra_yaml=ESPHOME_YAML)
    fake.files["secrets.yaml"] = 'wifi_ssid: "MyNet"\n'
    rsp = client.post("/api/builder/secrets/ensure", json={"keys": {
        "wifi_ssid": None, "wifi_password": None, "mqtt_password": "s3"}})
    assert rsp.status_code == 200
    assert rsp.json()["added"] == ["mqtt_password", "wifi_password"]
    body = fake.files["secrets.yaml"]
    assert 'wifi_ssid: "MyNet"' in body and 'mqtt_password: "s3"' in body
    # idempotent second call
    rsp = client.post("/api/builder/secrets/ensure",
                      json={"keys": {"wifi_password": None}})
    assert rsp.json()["added"] == []
    # bad key names refused
    assert client.post("/api/builder/secrets/ensure",
                       json={"keys": {"bad key!": "x"}}).status_code == 422


# ---------------------------------------------------------------------------
# web-flasher manifest + hardware profiles + fleet update (phase 3)
# ---------------------------------------------------------------------------

@needs_tc
def test_flash_manifest_maps_chip_family(tmp_path, fake):
    _, client = make_app(tmp_path, extra_yaml=ESPHOME_YAML)
    fake.files["n1.yaml"] = "esphome: {}\n"
    fake.platforms["n1.yaml"] = "ESP32-S3"      # dashboards report variants

    m = client.get("/api/builder/nodes/n1.yaml/manifest").json()
    assert m["builds"][0]["chipFamily"] == "ESP32-S3"
    # relative part path → resolves under this node's own endpoint
    assert m["builds"][0]["parts"][0] == {
        "path": "download?file=firmware.factory.bin", "offset": 0}
    assert m["new_install_prompt_erase"] is True

    assert client.get("/api/builder/nodes/nope.yaml/manifest").status_code == 404
    fake.platforms["n1.yaml"] = "RTL87XX"       # non-ESP → clear 422
    assert client.get("/api/builder/nodes/n1.yaml/manifest").status_code == 422


@needs_tc
def test_hardware_profiles_crud(tmp_path, fake):
    _, client = make_app(tmp_path, extra_yaml=ESPHOME_YAML)
    ps = client.get("/api/builder/profiles").json()["profiles"]
    builtin_ids = {p["id"] for p in ps if p["builtin"]}
    assert "esp32-generic-uart2" in builtin_ids

    rsp = client.post("/api/builder/profiles", json={
        "name": "LilyGO T-CAN485", "platform": "esp32", "board": "esp32dev",
        "tx_pin": "GPIO22", "rx_pin": "GPIO21", "flow_control_pin": "GPIO17",
        "baud_rate": 19200})
    assert rsp.status_code == 200
    pid = rsp.json()["profile"]["id"]
    assert pid == "lilygo-t-can485"

    ps = client.get("/api/builder/profiles").json()["profiles"]
    mine = next(p for p in ps if p["id"] == pid)
    assert mine["tx_pin"] == "GPIO22" and mine["builtin"] is False
    # persisted on disk next to config.yaml
    assert (tmp_path / "builder_profiles.json").exists()

    # built-ins cannot be shadowed or deleted
    assert client.post("/api/builder/profiles",
                       json={"name": "esp32 generic uart2"}).status_code == 422
    assert client.delete("/api/builder/profiles/esp32-generic-uart2").status_code == 404
    assert client.delete(f"/api/builder/profiles/{pid}").status_code == 200
    assert client.delete(f"/api/builder/profiles/{pid}").status_code == 404


@needs_tc
def test_update_all_stream(tmp_path, fake):
    _, client = make_app(tmp_path, extra_yaml=ESPHOME_YAML)
    with client.websocket_connect("/api/builder/stream/update-all") as ws:
        msgs = [ws.receive_json() for _ in range(3)]
    assert msgs[-1] == {"event": "exit", "code": 0}


def test_generated_yaml_includes_improv_serial():
    from tests.test_esphome_generator import gen
    assert "improv_serial:" in gen()["yaml"]


# ---------------------------------------------------------------------------
# command stream (WS proxy)
# ---------------------------------------------------------------------------

@needs_tc
def test_stream_relays_lines_and_exit(tmp_path, fake):
    _, client = make_app(tmp_path, extra_yaml=ESPHOME_YAML)
    fake.files["node1.yaml"] = "esphome: {}\n"

    with client.websocket_connect(
            "/api/builder/stream/compile?configuration=node1.yaml") as ws:
        msgs = [ws.receive_json() for _ in range(3)]
    assert msgs[0]["event"] == "line" and "compile node1.yaml" in msgs[0]["data"]
    assert msgs[-1] == {"event": "exit", "code": 0}


@needs_tc
def test_stream_rejects_bad_command_and_name(tmp_path, fake):
    from starlette.websockets import WebSocketDisconnect
    _, client = make_app(tmp_path, extra_yaml=ESPHOME_YAML)
    for path in ("/api/builder/stream/rm-rf?configuration=node1.yaml",
                 "/api/builder/stream/compile?configuration=../x.yaml",
                 "/api/builder/stream/compile?configuration="):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(path) as ws:
                ws.receive_json()


@needs_tc
def test_stream_disabled_feature_closes(tmp_path, fake):
    from starlette.websockets import WebSocketDisconnect
    _, client = make_app(tmp_path)          # esphome: not configured
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
                "/api/builder/stream/compile?configuration=n.yaml") as ws:
            ws.receive_json()
