# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
#
"""A page left open across a deploy runs the old code and saves in the old
shape (FINDINGS PROD-5/6). The page carries its build; /api/status says the
build a page loaded now would carry, and the page compares the two."""
import re

from multibus.api import ui_build, _render_index_html
from tests.test_devices_api import make_app, needs_tc


@needs_tc
def test_the_page_and_the_status_name_the_same_build(tmp_path):
    cfg, client = make_app(tmp_path)
    html = client.get("/").text
    m = re.search(r'<meta name="mbg-build" content="([0-9a-f]{12})">', html)
    assert m, html[:300]
    assert client.get("/api/status").json()["ui_build"] == m.group(1)


def test_another_asset_is_another_build(tmp_path):
    a, b = tmp_path / "a.html", tmp_path / "b.html"
    a.write_text('<html><head></head><script src="/static/js/app-core.js?v=0"></script></html>')
    b.write_text('<html><head></head><script src="/static/js/app-status.js?v=0"></script></html>')
    _render_index_html(str(a))
    first = ui_build(str(a))
    _render_index_html(str(b))
    assert ui_build(str(b)) != first and len(first) == 12
    # the build stays put while nothing changes
    assert ui_build(str(b)) == ui_build(str(b))
