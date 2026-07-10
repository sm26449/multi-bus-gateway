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
"""MQTT topic browse: collection, retained flag, auth refusal, LAN guard."""
import sys
import types

import pytest

from multibus import discovery


class _Msg:
    def __init__(self, topic, payload, retain=False):
        self.topic, self.payload, self.retain = topic, payload, retain


class _RC:
    def __init__(self, failure=False):
        self.is_failure = failure
    def __str__(self):
        return "Not authorized" if self.is_failure else "Success"


def _fake_paho(messages, connect_rc=None, connect_raises=None):
    """A stand-in for paho.mqtt.client: loop_start fires on_connect, subscribe
    delivers the canned messages."""
    mod = types.ModuleType("paho.mqtt.client")

    class CallbackAPIVersion:
        VERSION2 = 2

    class Client:
        def __init__(self, *a, **k):
            self.on_connect = self.on_message = None
        def username_pw_set(self, u, p):
            pass
        def tls_set(self):
            pass
        def connect(self, broker, port, keepalive=15):
            if connect_raises:
                raise connect_raises
        def loop_start(self):
            self.on_connect(self, None, {}, connect_rc or _RC())
        def subscribe(self, topic, qos=0):
            for m in messages:
                self.on_message(self, None, m)
        def loop_stop(self):
            pass
        def disconnect(self):
            pass

    mod.Client = Client
    mod.CallbackAPIVersion = CallbackAPIVersion
    return mod


@pytest.fixture
def paho(monkeypatch):
    def install(messages, **kw):
        mod = _fake_paho(messages, **kw)
        # ``import paho.mqtt.client as mqtt`` binds via the PARENT package's
        # attribute when it exists (already-imported real paho), falling back
        # to sys.modules — so both must point at the fake.
        import paho.mqtt as pm
        monkeypatch.setattr(pm, "client", mod, raising=False)
        monkeypatch.setitem(sys.modules, "paho.mqtt.client", mod)
    return install


def test_browse_collects_and_sorts(paho):
    paho([
        _Msg("zigbee2mqtt/temp1", b'{"temperature":21.5}', retain=True),
        _Msg("fronius/ac/power", b"12345"),
        _Msg("fronius/ac/power", b"12400"),
    ])
    out = discovery.mqtt_browse("192.168.1.10", duration_s=1.0)
    assert out["ok"] and out["count"] == 2
    assert [t["topic"] for t in out["topics"]] == ["fronius/ac/power", "zigbee2mqtt/temp1"]
    fr = out["topics"][0]
    assert fr["count"] == 2 and fr["payload"] == "12400" and fr["retained"] is False
    assert out["topics"][1]["retained"] is True
    assert out["truncated"] is False


def test_browse_caps_topics(paho):
    paho([_Msg(f"t/{i}", b"x") for i in range(60)])
    out = discovery.mqtt_browse("192.168.1.10", duration_s=1.0, max_topics=10)
    assert out["count"] == 10 and out["truncated"] is True


def test_browse_auth_refused(paho):
    paho([], connect_rc=_RC(failure=True))
    out = discovery.mqtt_browse("192.168.1.10", duration_s=1.0)
    assert out["ok"] is False and "refused" in out["error"]


def test_browse_connect_error(paho):
    paho([], connect_raises=OSError("Connection refused"))
    out = discovery.mqtt_browse("192.168.1.10", duration_s=1.0)
    assert out["ok"] is False and "connect failed" in out["error"]


def test_api_browse_lan_guard(tmp_path):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from multibus.api import create_api
    from tests.test_devices import write_config
    cfg = write_config(tmp_path)
    fake = SimpleNamespace(publish_callback=None)
    app, _ = create_api(cfg, fake, None, None, devices=[(d, fake) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post("/api/discover/mqtt/browse", json={"broker": "8.8.8.8"}).status_code == 422
    assert client.post("/api/discover/mqtt/browse", json={}).status_code == 422


# ── mqtt_sample + /payload-sample (json_path picker) ────────────────────────

def test_sample_grabs_first_message(paho):
    paho([_Msg("seplos/battery_1/state", b'{"soc": 60.3, "cells": [3.25, 3.26]}', retain=True),
          _Msg("seplos/battery_1/state", b'{"soc": 60.4}')])
    out = discovery.mqtt_sample("192.168.1.10", topic="seplos/battery_1/state", timeout_s=1.0)
    assert out["ok"] and out["retained"] is True
    assert '"soc": 60.3' in out["payload"]


def test_sample_requires_topic_and_reports_silence(paho):
    paho([])
    assert discovery.mqtt_sample("192.168.1.10", topic="")["ok"] is False
    out = discovery.mqtt_sample("192.168.1.10", topic="t/quiet", timeout_s=1.0)
    assert out["ok"] is False and "no message" in out["error"]


def _mk_app(tmp_path, client_factory=None):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from multibus.api import create_api
    from tests.test_devices import write_config
    cfg = write_config(tmp_path)
    fake = SimpleNamespace(publish_callback=None)
    app, _ = create_api(cfg, fake, None, None, devices=[(d, fake) for d in cfg.devices])
    return TestClient(app, raise_server_exceptions=False)


def test_payload_sample_route_guards(tmp_path):
    client = _mk_app(tmp_path)
    r = client.post("/api/devices/nope/payload-sample", json={})
    assert r.status_code == 404
    # umg512 e Modbus → 400 (sampling e doar MQTT/HTTP)
    r = client.post("/api/devices/umg512/payload-sample", json={})
    assert r.status_code == 400
