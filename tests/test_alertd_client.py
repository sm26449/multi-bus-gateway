# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The alertd producer (ADR-0043): key→type catalog, the event/condition split, the
{type,context} body, pending/no-retry, and the AlertManager wiring that fires EVENT-class
alerts to /v1/fire while leaving CONDITION-class ones to alertd rules."""
import io
import json
import urllib.error

import pytest

from multibus.outputs import alerts as A
from multibus.outputs.alertd_client import AlertdClient, alertd_severity, type_for


# ── a fake /v1/fire endpoint (patched onto the shared no-redirect opener) ──────
class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._b = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    def read(self, n=-1):
        return self._b
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _patch_opener(monkeypatch, handler):
    """handler(req) -> _Resp, or raises (e.g. HTTPError) — like urllib's opener.open."""
    monkeypatch.setattr(A._NO_REDIRECT_OPENER, "open",
                        lambda req, timeout=8: handler(req))


# ── 1. key → type catalog + the fire/rule split (ADR-0043 D4) ─────────────────
@pytest.mark.parametrize("key,typ,mode", [
    ("dev:fronius-1:down", "mbg_device", "fire"),
    ("dev:fronius-1:up", "mbg_device", "fire"),
    ("mqtt:down", "mbg_sink", "fire"),
    ("influx:up", "mbg_sink", "fire"),
    ("influx-auth", "mbg_influx_auth", "fire"),       # NOT the influx sink prefix
    ("dropped-schema", "mbg_influx_dropped", "fire"),
    ("dropped-buffer", "mbg_influx_dropped", "fire"),
    ("alarm:hp:err", "mbg_device_alarm", "fire"),
    ("bridge:b1:7001:line", "mbg_bridge", "fire"),
    ("config", "mbg_config", "fire"),
    ("config-downgrade", "mbg_config", "fire"),
    ("role:lease", "mbg_gateway", "fire"),
    ("svc:boiler", "mbg_gateway", "fire"),
    ("rule-fail:r1", "mbg_gateway", "fire"),
    ("test", "mbg_test", "fire"),
    # conditions → rules (NOT fired)
    ("lat:fronius-1", "mbg_latency", "rule"),
    ("buffer", "mbg_influx_buffer", "rule"),
    ("thr:meter:grid_a", "mbg_threshold", "rule"),
    # unknown → fired generic
    ("something-new", "mbg_event", "fire"),
])
def test_type_catalog(key, typ, mode):
    assert type_for(key) == (typ, mode)


def test_severity_map():
    assert [alertd_severity(s) for s in ("info", "warn", "warning", "error", "danger", "critical", "x")] \
        == ["info", "warning", "warning", "critical", "critical", "critical", "warning"]


# ── 2. payload shape (ADR-0043 D5) ────────────────────────────────────────────
def test_build_payload_is_structured_and_maps_severity():
    c = AlertdClient({"enabled": True, "url": "http://alertd/v1/fire", "source": "mbg-test"})
    alert = {"ts": 1.0, "severity": "error", "source": "Fronius", "message": "down",
             "key": "dev:f1:down", "host": "h", "device_id": "f1", "value": 3}
    body = c.build_payload(alert)
    assert body["type"] == "mbg_device"
    assert body["source"] == "mbg-test"
    assert body["context"]["severity"] == "critical"            # error → critical
    assert body["context"]["message"] == "down" and body["context"]["key"] == "dev:f1:down"
    assert body["context"]["device_id"] == "f1" and body["context"]["value"] == 3
    assert "test" not in body
    assert c.build_payload(alert, test=True)["test"] is True


def test_active_needs_enabled_and_http_url():
    assert not AlertdClient({"enabled": True, "url": ""}).active()
    assert not AlertdClient({"enabled": False, "url": "http://a/v1/fire"}).active()
    assert not AlertdClient({"enabled": True, "url": "ftp://a"}).active()
    assert AlertdClient({"enabled": True, "url": "http://a/v1/fire"}).active()


# ── 3. send(): pending/2xx is success, non-2xx is a failure, never retried ─────
def test_send_202_pending_is_success(monkeypatch):
    seen = {}
    def handler(req):
        seen["url"] = req.full_url
        seen["key"] = req.get_header("X-api-key")
        seen["body"] = json.loads(req.data)
        return _Resp(202, {"id": "ntf_1", "status": "pending"})
    _patch_opener(monkeypatch, handler)
    c = AlertdClient({"enabled": True, "url": "http://alertd/v1/fire", "api_key": "alertd_k"})
    res = c.send({"key": "dev:f1:down", "severity": "error", "message": "down"})
    assert res == {"ok": True, "status_code": 202, "id": "ntf_1", "status": "pending", "error": None}
    assert seen["url"] == "http://alertd/v1/fire" and seen["key"] == "alertd_k"
    assert seen["body"]["type"] == "mbg_device"


def test_send_suppressed_unknown_type_is_still_success(monkeypatch):
    _patch_opener(monkeypatch, lambda req: _Resp(202, {"id": "n2", "status": "suppressed",
                                                       "suppressed_reason": "unknown_type"}))
    c = AlertdClient({"enabled": True, "url": "http://alertd/v1/fire"})
    res = c.send({"key": "dev:f1:down", "severity": "info", "message": "x"})
    assert res["ok"] and res["status"] == "suppressed"


def test_send_http_error_is_a_failure_not_a_raise(monkeypatch):
    def boom(req):
        raise urllib.error.HTTPError(req.full_url, 401, "Invalid API key", {}, io.BytesIO(b""))
    _patch_opener(monkeypatch, boom)
    c = AlertdClient({"enabled": True, "url": "http://alertd/v1/fire", "api_key": "bad"})
    res = c.send({"key": "dev:f1:down", "severity": "error", "message": "x"})
    assert res["ok"] is False and res["status_code"] == 401 and res["error"] == "HTTP 401"


def test_send_transport_error_is_swallowed(monkeypatch):
    def boom(req):
        raise OSError("connection refused")
    _patch_opener(monkeypatch, boom)
    c = AlertdClient({"enabled": True, "url": "http://alertd/v1/fire"})
    res = c.send({"key": "x", "severity": "warn", "message": "y"})
    assert res["ok"] is False and res["status_code"] is None and "refused" in res["error"]


# ── 4. AlertManager wiring: events fire, conditions do not (ADR-0043 D1/D4) ────
def _mgr_with_capture(monkeypatch, cfg):
    mgr = A.AlertManager(cfg)
    sent = []
    # capture at the client seam; run the (threaded) send synchronously in the test
    monkeypatch.setattr(mgr.alertd, "send",
                        lambda alert, test=False: sent.append((alert["key"], test)) or
                        {"ok": True, "status_code": 202, "id": "n", "status": "sent", "error": None})
    monkeypatch.setattr(A.threading, "Thread",
                        lambda target, args=(), daemon=None: type("T", (), {"start": lambda s: target(*args)})())
    return mgr, sent


def test_event_key_fires_to_alertd(monkeypatch):
    mgr, sent = _mgr_with_capture(monkeypatch, {
        "enabled": True, "mqtt": False,
        "alertd": {"enabled": True, "url": "http://alertd/v1/fire"}})
    mgr.fire("error", "dev:f1:down", "Fronius", "down")
    assert sent == [("dev:f1:down", False)]
    assert "alertd" in mgr.status()["channels"]


def test_condition_key_is_not_fired_left_to_rules(monkeypatch):
    mgr, sent = _mgr_with_capture(monkeypatch, {
        "enabled": True, "mqtt": False,
        "alertd": {"enabled": True, "url": "http://alertd/v1/fire"}})
    mgr.fire("warn", "thr:meter:grid_a", "Meter", "grid current high", transition=True)
    mgr.fire("warn", "lat:f1", "Fronius", "latency high")
    mgr.fire("warn", "buffer", "InfluxDB", "backlog")
    assert sent == []                      # conditions never fire; MQTT + alertd rules carry them


def test_alertd_off_by_default_no_fire(monkeypatch):
    mgr, sent = _mgr_with_capture(monkeypatch, {"enabled": True, "mqtt": False})
    mgr.fire("error", "dev:f1:down", "Fronius", "down")
    assert sent == [] and "alertd" not in mgr.status()["channels"]


def test_manager_test_probes_alertd(monkeypatch):
    mgr = A.AlertManager({"enabled": True, "mqtt": False,
                          "alertd": {"enabled": True, "url": "http://alertd/v1/fire"}})
    calls = []
    monkeypatch.setattr(mgr.alertd, "send", lambda alert, test=False: calls.append(test) or
                        {"ok": True, "status_code": 202, "id": "n", "status": "pending", "error": None})
    res = mgr.test("hello")
    assert calls == [True]                         # test fire, test:true
    assert "sent" in res["channels"]["alertd"] and "pending" in res["channels"]["alertd"]


def test_last_outcome_recorded_for_status_page(monkeypatch):
    mgr, _sent = _mgr_with_capture(monkeypatch, {
        "enabled": True, "mqtt": False,
        "alertd": {"enabled": True, "url": "http://alertd/v1/fire"}})
    mgr.fire("error", "dev:f1:down", "Fronius", "down")
    st = mgr.alertd_status()
    assert st["active"] and st["last"] and st["last"]["type"] == "mbg_device" and st["last"]["ok"]


def test_structured_context_flows_to_the_payload(monkeypatch):
    mgr, _sent = _mgr_with_capture(monkeypatch, {
        "enabled": True, "mqtt": False,
        "alertd": {"enabled": True, "url": "http://alertd/v1/fire"}})
    sent = []
    monkeypatch.setattr(mgr.alertd, "send",
                        lambda alert, test=False: sent.append(alert) or
                        {"ok": True, "status_code": 202, "id": "n", "status": "sent", "error": None})
    mgr.fire("error", "dev:f1:down", "Fronius", "down",
             context={"device_id": "f1", "value": 7, "severity": "SHOULD_NOT_CLOBBER"})
    alert = sent[0]
    assert alert["device_id"] == "f1" and alert["value"] == 7
    assert alert["severity"] == "error"                       # core field not clobbered
    body = mgr.alertd.build_payload(alert)
    assert body["context"]["device_id"] == "f1" and body["context"]["value"] == 7


def test_configure_is_live_and_additive(monkeypatch):
    mgr = A.AlertManager({"enabled": True})
    assert not mgr.alertd.active()
    mgr.configure({"enabled": True, "alertd": {"enabled": True, "url": "http://alertd/v1/fire"}})
    assert mgr.alertd.active() and mgr.alertd.url == "http://alertd/v1/fire"


# ── 5. the config endpoint carries the alertd block (masked key) ──────────────
def test_config_endpoint_masks_key_and_round_trips(tmp_path):
    tc = pytest.importorskip("fastapi.testclient")
    from multibus.api import create_api
    from tests.test_devices import write_config
    cfg = write_config(tmp_path)
    cfg.alerts = {"enabled": True,
                  "alertd": {"enabled": True, "url": "http://klopot:5090/v1/fire",
                             "api_key": "alertd_secret", "source": "mbg"}}
    app, _ = create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    client = tc.TestClient(app, raise_server_exceptions=False)
    g = client.get("/api/config/alerts").json()["alertd"]
    assert g["api_key"] == "••••••" and g["url"].endswith("/v1/fire") and g["enabled"] is True
    # a save from the masked view keeps the key; changes url + source + enable
    r = client.post("/api/config/alerts", json={"alertd": {
        "enabled": False, "url": "http://alertd2/v1/fire", "api_key": "••••••", "source": "edge"}})
    assert r.status_code == 200
    ad = cfg.alerts["alertd"]
    assert ad["api_key"] == "alertd_secret" and ad["url"] == "http://alertd2/v1/fire"
    assert ad["enabled"] is False and ad["source"] == "edge"
    # a real new key replaces it; a bad url is rejected
    assert client.post("/api/config/alerts", json={"alertd": {"api_key": "alertd_new"}}).status_code == 200
    assert cfg.alerts["alertd"]["api_key"] == "alertd_new"
    assert client.post("/api/config/alerts", json={"alertd": {"url": "ftp://x"}}).status_code == 422


# ── 6. the YAML pack (ADR-0043 D6) ────────────────────────────────────────────
def test_pack_covers_every_fired_type():
    from multibus.outputs import alertd_catalog as cat
    # every EVENT type the client can emit must have a template in the pack
    fired = {cat_type for cat_type in cat.types()}
    emitted = set()
    for k in ["dev:x:down", "mqtt:down", "influx-auth", "dropped-schema", "alarm:a:b",
              "bridge:b:1:line", "config", "role:x", "svc:y", "rule-fail:z", "test", "whatever"]:
        typ, mode = type_for(k)
        if mode == "fire":
            emitted.add(typ)
    assert emitted <= fired, emitted - fired


def test_pack_shape_and_routes():
    from multibus.outputs import alertd_catalog as cat
    pack = cat.build_pack(channels=["ui_default", "telegram_oncall"])
    assert pack["version"] == "1.0"
    types_in = {t["type"] for t in pack["templates"]}
    assert types_in == cat.types()
    for t in pack["templates"]:
        assert t["title_template"] and t["severity"] in ("info", "warning", "critical")
        assert t["min_repeat_min"] >= 0
    # every template routed to every requested channel
    assert len(pack["routes"]) == len(pack["templates"]) * 2
    assert {r["channel"] for r in pack["routes"]} == {"ui_default", "telegram_oncall"}


def test_pack_yaml_round_trips():
    import yaml
    from multibus.outputs import alertd_catalog as cat
    doc = yaml.safe_load(cat.to_yaml())
    assert doc["version"] == "1.0" and len(doc["templates"]) == len(cat.types())
    assert all(":" not in t["type"] and t["type"].startswith("mbg_") for t in doc["templates"])


def test_pack_has_only_alertd_accepted_top_level_keys():
    # alertd's config import rejects unknown top-level keys (PACK-EXTRA-KEY-1): the pack
    # must carry ONLY version/templates/routes — provenance lives in the YAML comment.
    from multibus.outputs import alertd_catalog as cat
    assert set(cat.build_pack().keys()) == {"version", "templates", "routes"}
