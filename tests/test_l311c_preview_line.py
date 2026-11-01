# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""L3-UX-12: Add's Check of a device on a bus a running device reads was
refused with a raw line ("connection.timeout: … read through 'em1''s
connection …") over the form's default timeout. The refusal now carries the
holder's values (`line`), so the Check takes them and asks again, and says
it; the words lose their doubled quote."""
from tests.test_preview_review import BRIDGED, _app, _run_holder, _start, _wait, needs_tc, rtu_bridge  # noqa: F401


@needs_tc
def test_a_refused_line_carries_the_holders_values(tmp_path, rtu_bridge):  # noqa: F811
    b = rtu_bridge({1: {}, 4: {}})
    _cfg, client = _app(tmp_path, BRIDGED.format(port=b.port, extra=", baudrate: 19200"))
    _run_holder(client, "m1")
    conn = {"protocol": "rtu-tcp", "host": "127.0.0.1", "port": b.port, "unit_id": 4, "timeout": 3}
    r = client.post("/api/previews", json={"connection": conn, "template": "eastron_sdm120"})
    assert r.status_code == 422, r.text
    det = r.json()["detail"]
    assert det["holder"] == "m1" and det["line"] == {"timeout": 1}, det
    assert "''" not in det["errors"][0] and "m1's connection" in det["errors"][0], det["errors"]
    # taken, the Check runs: through m1's connection, said
    job = _start(client, {"connection": dict(conn, timeout=det["line"]["timeout"]), "template": "eastron_sdm120"})
    ans = _wait(client, job["job"])
    assert any("m1's connection" in w for w in ans["warnings"]), ans["warnings"]
