# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The REST push rule, one table for both sides (L3.11b second review, R2-6).

tests/fixtures/rest_push_rule_cases.json holds the cases; this file holds the
server to them (``device_patch.rest_push_clean``), and
tools/e2e/device_shell_model_test.mjs holds the page's check before Save
(``DeviceShell.localErrors``) to the same table — so a URL or an interval the
page takes is one the server takes, and the other way round."""
import json
import pathlib

import pytest

from multibus.device_patch import rest_push_clean

CASES = json.loads((pathlib.Path(__file__).parent / "fixtures" / "rest_push_rule_cases.json").read_text())["cases"]


def _flag(payload, key, default):
    return bool(payload.get(key, default))


@pytest.mark.parametrize("case", CASES, ids=[f"{c['enabled']}-{c['url']!r}-{c['interval_s']!r}" for c in CASES])
def test_the_server_gives_the_tables_verdict(case):
    body = {"enabled": case["enabled"], "url": case["url"], "interval_s": case["interval_s"],
            "format": "native", "verify_tls": True, "headers": {}}
    _cfg, errors = rest_push_clean(body, {}, "***", _flag)
    assert (not any(e.startswith("url:") for e in errors)) is case["url_ok"], errors
    assert (not any(e.startswith("interval_s:") for e in errors)) is case["interval_ok"], errors
