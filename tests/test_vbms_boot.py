# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Virtual BMS lifecycle at boot (ADR-0037): a configured ``virtual_bms`` instance reaches the
live ``VbmsManager`` and its EMS command topics are subscribed on the publisher — disarmed, so
nothing transmits. This is the full create_api wiring, not the units."""
import textwrap

import pytest

try:
    from fastapi.testclient import TestClient  # noqa: F401
    _HAS_TC = True
except Exception:  # noqa: BLE001
    _HAS_TC = False

from multibus.config import Config


class _Pub:
    """Minimal MQTT publisher surface for create_api; records the command topics it subscribes."""
    connected = True
    connects = 1

    def __init__(self):
        self.sent, self.topics, self.discovery_hooks = [], {}, []
        self.published = {}
        self.client = self

    def _publish(self, *a, **k):
        return True

    def publish_if_changed(self, topic, value, retain=True, heartbeat=None):
        changed = self.published.get(topic) != value
        self.published[topic] = value
        return changed

    def register_command(self, topic, fn):
        self.topics[topic] = fn

    def unregister_command(self, topic):
        self.topics.pop(topic, None)

    def set_command_write_handler(self, fn):
        self.handler = fn

    def __getattr__(self, name):
        return lambda *a, **k: None


def _boot(tmp_path):
    (tmp_path / "config.yaml").write_text(textwrap.dedent("""
        mqtt:
          enabled: false
          topic_prefix: mbg
        virtual_bms:
          - id: vb
            profile: pylontech_can_inverter
            packs: [seplos-adr1]
            bounds:
              charge_current_limit: [0, 80]
            floor:
              charge_current_limit: 20
    """))
    from multibus.api import create_api
    cfg = Config(str(tmp_path / "config.yaml"))
    pub = _Pub()
    app, _ = create_api(cfg, None, pub, None, devices=[(d, None) for d in cfg.devices])
    return app, pub


@pytest.mark.skipif(not _HAS_TC, reason="fastapi TestClient not installed")
def test_configured_instance_boots_disarmed_with_command_topics(tmp_path):
    app, pub = _boot(tmp_path)
    mgr = app.state.vbms_manager
    assert list(mgr.instances) == ["vb"]                        # the config block reached the manager
    assert mgr.status()["vb"]["armed"] is False                 # disarmed at boot — nothing transmits
    # the EMS command topics were subscribed on our broker at boot
    assert "mbg/devices/vb/cmd/charge_current_limit" in pub.topics
    assert len([t for t in pub.topics if t.startswith("mbg/devices/vb/cmd/")]) == 6


@pytest.mark.skipif(not _HAS_TC, reason="fastapi TestClient not installed")
def test_command_over_the_subscribed_topic_clamps(tmp_path):
    app, pub = _boot(tmp_path)
    pub.topics["mbg/devices/vb/cmd/charge_current_limit"]("200")  # an EMS publish, routed as at runtime
    mgr = app.state.vbms_manager
    # clamped, not 200 — read on the control plane: this fixture has no live pack, and a bank
    # that cannot be vouched for is SENT a limit of 0 whatever was commanded (WK7-SAFE-2)
    assert mgr.instances["vb"].control.resolve()["charge_current_limit"] == 80
    assert mgr.values(mgr.instances["vb"])["charge_current_limit"] == 0


@pytest.mark.skipif(not _HAS_TC, reason="fastapi TestClient not installed")
def test_boot_state_publisher_publishes_telemetry(tmp_path):
    app, pub = _boot(tmp_path)
    assert app.state.vbms_state_publisher is not None
    app.state.vbms_state_publisher.publish_once()                 # deterministic (no thread timing)
    # no live pack in this fixture: the telemetry says what would be sent — 0, not the floor
    # of 20 — and why (WK7-SAFE-2)
    assert pub.published["mbg/devices/vb/charge_current_limit"] == 0
    assert pub.published["mbg/devices/vb/status"]["data_state"] in ("hold", "silent")
    assert pub.published["mbg/devices/vb/armed"] == 0                  # disarmed at boot
    assert "mbg/devices/vb/status" in pub.published


@pytest.mark.skipif(not _HAS_TC, reason="fastapi TestClient not installed")
def test_state_publisher_thread_starts_and_stops_with_lifespan(tmp_path):
    from fastapi.testclient import TestClient
    app, _ = _boot(tmp_path)
    sp = app.state.vbms_state_publisher
    with TestClient(app):
        assert sp._thread is not None and sp._thread.is_alive()   # started by the lifespan
    assert sp._thread is None                                     # stopped on shutdown


@pytest.mark.skipif(not _HAS_TC, reason="fastapi TestClient not installed")
def test_arm_is_gated_on_allow_writes_disarm_is_not(tmp_path, monkeypatch):
    """Arming transmits to the inverter — a hardware write — so it needs security.allow_writes.
    Disarming is the fail-safe and must always be allowed (silence the BMS even with writes off)."""
    from fastapi.testclient import TestClient
    from multibus.transports.can import can_interfaces
    # a transmit-capable can0 so the only unmet prerequisite is allow_writes (ADR-0046)
    monkeypatch.setattr(can_interfaces, "interfaces", lambda **k: [
        {"name": "can0", "up": True, "listen_only": False, "bitrate": 500000, "state": "error-active", "devices": []}])
    app, _ = _boot(tmp_path)
    with TestClient(app) as client:
        # the default config has writes off: arm answers 409 with a guided prerequisite (ADR-0046)
        r = client.post("/api/vbms/vb/arm")
        assert r.status_code == 409
        prereqs = r.json()["detail"]["prerequisites"]
        assert any(p["id"] == "allow_writes" and p["met"] is False and p["fix"] for p in prereqs)
        assert app.state.vbms_manager.status()["vb"]["armed"] is False
        assert client.post("/api/vbms/vb/disarm").status_code == 200       # the safe direction, always
        # flip the master switch on → arming is allowed
        app.state.ctx.config.security.allow_writes = True
        assert client.post("/api/vbms/vb/arm").status_code == 200
        assert app.state.vbms_manager.status()["vb"]["armed"] is True
        assert client.post("/api/vbms/vb/disarm").status_code == 200


@pytest.mark.skipif(not _HAS_TC, reason="fastapi TestClient not installed")
def test_a_gateway_scoped_topic_pattern_names_the_gateway(tmp_path):
    """VBMS-TOPIC-1: on a `gateway`-scope install the default pattern is
    mbg/{gateway}/devices/{device}; the Virtual BMS topics were built with
    str.format(device=…) and raised KeyError on {gateway} — no command topic,
    no telemetry. They are filled like every device's topics."""
    (tmp_path / "config.yaml").write_text(textwrap.dedent("""
        identity_scope: gateway
        gateway:
          id: site-a
        mqtt:
          enabled: false
        virtual_bms:
          - id: vb
            profile: pylontech_can_inverter
            packs: [seplos-adr1]
            bounds:
              charge_current_limit: [0, 80]
            floor:
              charge_current_limit: 20
    """))
    from multibus.api import create_api
    cfg = Config(str(tmp_path / "config.yaml"))
    assert "{gateway}" in cfg.mqtt.default_topic_pattern
    pub = _Pub()
    app, _ = create_api(cfg, None, pub, None, devices=[(d, None) for d in cfg.devices])
    assert "mbg/site-a/devices/vb/cmd/charge_current_limit" in pub.topics, sorted(pub.topics)
    app.state.vbms_state_publisher.publish_once()
    assert any(t.startswith("mbg/site-a/devices/vb/") for t in pub.published), sorted(pub.published)
