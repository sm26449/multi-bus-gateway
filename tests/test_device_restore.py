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
"""Delete → restore → forget for non-primary devices.

Deleting a device keeps its FULL definition (a tombstone) so the UI can
rebuild the exact device — connection, template AND register selection —
instead of the old behaviour where only a stray registers file survived and
was reusable solely on an exact id-collision."""

from tests.test_devices_api import make_app, needs_tc

SECONDARY = """
devices:
  - id: hall-em24
    name: Hall EM24
    enabled: true
    template: carlo_gavazzi_em24
    connection: {protocol: tcp, host: 192.0.2.9, port: 1502, unit_id: 2}
    mqtt: {topic_prefix: meters/hall}
    influxdb: {bucket: hall}
"""


@needs_tc
def test_delete_keeps_tombstone_and_restore_rebuilds(tmp_path, monkeypatch):
    import multibus.routes.device_templates as dt
    monkeypatch.setattr(dt, 'USER_DIR', tmp_path / 'device_templates', raising=False)
    cfg, client = make_app(tmp_path, extra_yaml=SECONDARY)

    # sanity: device present, not restorable yet
    assert any(d["id"] == "hall-em24" for d in client.get("/api/devices").json()["devices"])
    assert client.get("/api/devices/restorable").json()["devices"] == []

    # give it a custom register selection so we can prove it comes back
    client.post("/api/registers/selected?device=hall-em24", json=[
        {"address": 0, "name": "voltage_l1_n", "label": "L1", "unit": "V",
         "data_type": "float", "poll_group": "realtime"}])

    # delete → tombstone written, appears as restorable with its metadata
    assert client.delete("/api/devices/hall-em24").status_code == 200
    assert not any(d["id"] == "hall-em24" for d in client.get("/api/devices").json()["devices"])
    rest = client.get("/api/devices/restorable").json()["devices"]
    assert len(rest) == 1
    assert rest[0]["id"] == "hall-em24"
    assert rest[0]["template"] == "carlo_gavazzi_em24"
    assert rest[0]["protocol"] == "tcp"
    assert rest[0]["registers"] == 1
    assert rest[0]["deleted_ts"]

    # restore → exact device back, connection + template + registers intact
    rsp = client.post("/api/devices/hall-em24/restore")
    assert rsp.status_code == 200, rsp.text
    dev = next(d for d in client.get("/api/devices").json()["devices"]
               if d["id"] == "hall-em24")
    assert dev["template"] == "carlo_gavazzi_em24"
    assert (dev.get("host") or dev.get("connection", {}).get("host")) == "192.0.2.9"
    regs = client.get("/api/registers/selected?device=hall-em24").json()["registers"]
    assert [r["name"] for r in regs] == ["voltage_l1_n"]
    # no longer restorable once active
    assert client.get("/api/devices/restorable").json()["devices"] == []


@needs_tc
def test_forget_removes_kept_settings(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml=SECONDARY)
    client.delete("/api/devices/hall-em24")
    assert len(client.get("/api/devices/restorable").json()["devices"]) == 1
    assert client.delete("/api/devices/restorable/hall-em24").status_code == 200
    assert client.get("/api/devices/restorable").json()["devices"] == []
    # dir is gone
    assert not (tmp_path / "devices" / "hall-em24").exists()
    # forgetting again → 404
    assert client.delete("/api/devices/restorable/hall-em24").status_code == 404


@needs_tc
def test_forget_refuses_active_device(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml=SECONDARY)
    assert client.delete("/api/devices/restorable/hall-em24").status_code == 422


@needs_tc
def test_restore_conflict_when_id_active(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml=SECONDARY)
    assert client.post("/api/devices/hall-em24/restore").status_code == 409


@needs_tc
def test_stale_registers_reseeded_on_template_mismatch(tmp_path):
    """A kept registers file from a different template must NOT be decoded
    against a new template — it gets re-seeded instead."""
    cfg, client = make_app(tmp_path, extra_yaml=SECONDARY)
    # plant a register selection whose names exist in NO template
    client.post("/api/registers/selected?device=hall-em24", json=[
        {"address": 9, "name": "TOTALLY_ALIEN_REG", "label": "x", "unit": "",
         "data_type": "float", "poll_group": "normal"}])
    client.delete("/api/devices/hall-em24")
    # recreate the SAME id with a DIFFERENT template
    payload = {"id": "hall-em24", "name": "Hall2", "template": "eastron_sdm120",
               "enabled": True,
               "connection": {"protocol": "tcp", "host": "192.0.2.9",
                              "port": 1502, "unit_id": 2}}
    assert client.post("/api/devices", json=payload).status_code == 200
    regs = client.get("/api/registers/selected?device=hall-em24").json()["registers"]
    names = {r["name"] for r in regs}
    assert "TOTALLY_ALIEN_REG" not in names        # stale selection dropped
    assert names                                    # re-seeded from sdm120


# ---------------------------------------------------------------------------
# REGRESSION: path traversal in forget/restore must not escape config/devices
# (a `..` id once wiped the whole config dir — found 2026-07-31)
# ---------------------------------------------------------------------------

def test_forget_rejects_path_traversal_ids(tmp_path):
    from multibus.config import Config
    (tmp_path / "config.yaml").write_text(
        "modbus: {host: 1.2.3.4}\nmqtt: {enabled: false}\ninfluxdb: {enabled: false}\n")
    (tmp_path / "important.txt").write_text("do not delete")
    (tmp_path / "devices").mkdir()
    cfg = Config(str(tmp_path / "config.yaml"))
    import pytest
    for evil in ("..", ".", "../../etc", "a/b", "x\\y", ""):
        with pytest.raises(ValueError):
            cfg.forget_deleted_device(evil)
        with pytest.raises(ValueError):
            cfg.load_deleted_device(evil)
    # nothing outside config/devices/<id> was touched
    assert (tmp_path / "config.yaml").exists()
    assert (tmp_path / "important.txt").exists()


@needs_tc
def test_forget_traversal_via_api_is_422(tmp_path):
    _, client = make_app(tmp_path, extra_yaml=SECONDARY)
    # encoded dot-dot reaches the handler as a literal id → rejected, not a wipe
    assert client.delete("/api/devices/restorable/%2e%2e").status_code in (404, 422)
    assert client.post("/api/devices/%2e%2e/restore").status_code in (404, 422, 409)
