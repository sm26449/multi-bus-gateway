# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""L3.10 (ADR-0032): one action model for a device.

- every reason the ⋯ menu gives for an action it holds back names a refusal
  the server really makes (``DeviceViewModel.REFUSALS`` paired with
  ``multibus/api.py``) — the UI never offers what the server refuses;
- the gateway says what the menu needs to know before a form is typed:
  whether writes are on for it and whether a write can be attributed
  (``/api/status`` → ``writes``), and how many registers a device declares
  writable (``writable_registers``: an operator writes only those);
- a refusal names the menu item, never a tab that left (L3-UX-4);
- no menu inside a tab body (the Measurements "More" menu, L3-UX-11), no dead
  admin button inside a read-only panel (L3-UX-2), every surface draws the
  one builder (L3-UX-3), Disable asks what uses the device wherever it is
  chosen;
- on a phone the ⋯ menu is a bottom sheet (L3-UX-10);
- no text names a place that does not exist (L3-UX-8).
"""
import json
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
UI = ROOT / "ui" / "js"
HTML = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
API = (ROOT / "multibus" / "api.py").read_text(encoding="utf-8")
# the primary's fixed fields are refused in device_patch.py's words (L3.11a)
PATCH_WORDS = (ROOT / "multibus" / "device_patch.py").read_text(encoding="utf-8")
LANGS = {n: json.loads((ROOT / "ui" / "languages" / f"{n}.json").read_text(encoding="utf-8")) for n in ("en", "ro")}

try:
    from fastapi.testclient import TestClient
    _HAS_TC = True
except Exception:  # noqa: BLE001
    _HAS_TC = False
needs_tc = pytest.mark.skipif(not _HAS_TC, reason="TestClient not installed")


def _js(name):
    return (UI / name).read_text(encoding="utf-8")


def _method(src, name):
    """The body of a prototype method `name(…) {` up to the next one."""
    m = re.search(r"\n    (?:async )?" + re.escape(name) + r"\([^)]*\)\s*\{\n(.*?)\n    \},", src, re.S)
    assert m, name
    return m.group(1)


# ── reasons paired with refusals ─────────────────────────────────────────

def _refusals():
    src = _js("device-view-model.js")
    m = re.search(r"const REFUSALS = \{(.*?)\n    \};", src, re.S)
    assert m, "DeviceViewModel.REFUSALS: the server text each reason predicts"
    return dict(re.findall(r"(\w+):\s*'([^']+)'", m.group(1)))


def test_every_reason_code_names_a_refusal_the_server_makes():
    ref = _refusals()
    for code in ("listen_only", "locked", "writes_off", "no_credential", "raw_admin",
                 "primary", "primary_polls", "unit", "component", "meter_only"):
        assert code in ref, code
    for code, text in ref.items():
        assert text in API or text in PATCH_WORDS, f"{code}: {text!r} is not a refusal of multibus/api.py (or device_patch.py)"


def test_a_refusal_names_the_menu_item_not_a_removed_tab():
    # a quoted text (what a caller reads), not a comment
    said = [ln for ln in API.splitlines() if not ln.lstrip().startswith('#')]
    assert not any("Outputs tab" in ln and ('"' in ln or "'" in ln) for ln in said)
    assert "⋯ → Unlock writes…" in API


# ── what the gateway says the menu needs ─────────────────────────────────

def _app(tmp_path, *, allow_writes=True, login=True, api_key=None, monkeypatch=None, extra_devices=()):
    from multibus import auth as _authmod
    from multibus.api import create_api
    from multibus.device_template import parse_template
    from tests.test_devices import write_config
    from tests.test_write_envelope import _wr_tpl
    if monkeypatch is not None:
        monkeypatch.delenv("API_KEY", raising=False)
        monkeypatch.delenv("JANITZA_API_KEY", raising=False)
        if api_key:
            monkeypatch.setenv("API_KEY", api_key)
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = write_config(tmp_path)
    cfg.security.allow_writes = allow_writes
    if login:
        cfg.ui.auth_enabled = True
        cfg.ui.auth_username = "admin"
        cfg.ui.auth_password = _authmod.hash_password("pw")
    cfg.upsert_raw_device({"id": "ctrl", "template": "wr_test", "enabled": True, "write_locked": True,
                           "connection": {"protocol": "tcp", "host": "192.0.2.9"}})
    for raw in extra_devices:
        cfg.upsert_raw_device(dict(raw))
    # no live client: every answer asked here comes before the bus
    app, _ = create_api(cfg, None, None, None, devices=[(d, None) for d in cfg.devices])
    app.state.template_registry._templates["wr_test"] = parse_template(_wr_tpl(), builtin=True)
    client = TestClient(app, raise_server_exceptions=False)
    if login:
        client.post("/api/auth/login", json={"username": "admin", "password": "pw"})
    return client


@needs_tc
def test_status_says_whether_writes_are_on_and_attributable(tmp_path, monkeypatch):
    c = _app(tmp_path, monkeypatch=monkeypatch)
    assert c.get("/api/status").json()["writes"] == {"enabled": True, "credential": True}
    c = _app(tmp_path / "b", allow_writes=False, login=False, monkeypatch=monkeypatch)
    assert c.get("/api/status").json()["writes"] == {"enabled": False, "credential": False}
    c = _app(tmp_path / "c", login=False, api_key="k", monkeypatch=monkeypatch)
    assert c.get("/api/status").json()["writes"] == {"enabled": True, "credential": True}


@needs_tc
def test_a_device_says_how_many_registers_it_declares_writable(tmp_path, monkeypatch):
    c = _app(tmp_path, monkeypatch=monkeypatch)
    dev = next(d for d in c.get("/api/devices").json()["devices"] if d["id"] == "ctrl")
    assert dev["writable_registers"] == 3          # limit, mode, free — not the read-only one


@needs_tc
def test_a_locked_write_names_the_menu_item(tmp_path, monkeypatch):
    c = _app(tmp_path, monkeypatch=monkeypatch)
    r = c.post("/api/devices/ctrl/write", json={"address": 100, "value": 1, "register_type": "holding"})
    assert r.status_code == 403
    said = str(r.json())
    assert "⋯ → Unlock writes…" in said and "Outputs tab" not in said


# ── one menu, every surface ──────────────────────────────────────────────

def test_no_menu_inside_the_measurements_tab():
    for word in ('id="regMenu"', 'id="regMenuBtn"', 'id="writeRegBtn"', 'data-i18n="registers.more"'):
        assert word not in HTML, word
    for name, src in ((p.name, p.read_text(encoding="utf-8")) for p in UI.glob("*.js")):
        assert "toggleRegMenu" not in src and "_regMenuPick" not in src and "_syncWriteMenuItem" not in src, name


def test_the_map_tools_stay_reachable_in_the_measurements_editor():
    """Import CSV, Upload map and Raw JSON are the editor's (admin, editing);
    Download map is in the ⋯ menu's Data group for every role."""
    for word in ("openCsvImport", "uploadDeviceMap", 'id="rawConfigBtn"', "downloadRegisterMap"):
        assert word in HTML, word
    for m in re.finditer(r'<button[^>]*(?:openCsvImport|uploadDeviceMap|id="rawConfigBtn")[^>]*>', HTML):
        assert "data-admin" in m.group(0) and "ddv-edit-only" in m.group(0), m.group(0)
    assert "'download-map'" in _js("device-view-model.js") and "case 'download-map'" in _js("app-device-view.js")


def test_no_dead_admin_buttons_in_a_read_only_panel():
    """L3-UX-2: Edit bus line… and a component's Rename / Disable / Remove are
    menu items; a view panel holds none of them."""
    view = _js("app-device-view.js")
    assert 'id="setupBusLine"' not in view
    # (L3.11b: drawn in the Edit device modal's Connection, "set on its device")
    readvia = _method(_js("app-components.js"), "_componentReadViaFactsHtml")
    for word in ("renameComponentUi", "switchComponentUi", "removeComponentUi", "data-admin"):
        assert word not in readvia, word
    vm = _js("device-view-model.js")
    assert "'bus-line'" in vm and "case 'bus-line'" in view


def test_every_surface_draws_the_one_builder():
    devices = _js("app-devices.js")
    assert "this._devRowMenuHtml(d, 'list')" in _method(devices, "_deviceRowHtml")
    assert "_devRowMenuHtml(" in _js("app-find.js"), "the table view (List) has the ⋯ too"
    bridges = _js("app-bridges.js")
    assert "_devRowMenuHtml(full, 'bridge')" in bridges
    assert "!d.endpoint_id ? this._devRowMenuHtml" not in bridges, "a unit on a bridge has its ⋯ too"
    assert "_devRowMenuHtml(" in _method(_js("app-endpoints.js"), "_endpointUnitRowsHtml")
    # the header's operate buttons come from the same builder
    assert "headerActions(" in _js("app-device-view.js")


def test_disable_asks_what_uses_the_device_from_every_surface():
    """L3-UX-3: from a list row or a bridge line too, not only on its page."""
    body = _method(_js("app-devices.js"), "ddvSetEnabled")
    assert "_ubLoad('device', id)" in body


def test_the_restore_line_opens_the_deleted_devices():
    assert "case 'restore'" in _js("app-device-view.js")


def test_the_phone_menu_is_a_bottom_sheet():
    css = (ROOT / "ui" / "css" / "config.css").read_text(encoding="utf-8")
    sheet = re.search(r"\.mbg-menu\s*\{[^}]*position:\s*fixed[^}]*bottom:\s*0[^}]*\}", css)
    assert sheet, "≤600 px: the ⋯ menu sits fixed at the bottom of the screen"
    assert "max-height" in sheet.group(0) and "overflow-y: auto" in sheet.group(0)


# ── texts name real places (L3-UX-8) ─────────────────────────────────────

DEAD = (
    "+ Add above", "on its Commands tab", "on its Outputs tab", "Outputs tab", "device's Commands tab",
    "(or the E key)", "(or E)", "Edit in its installation",
    # Romanian, the same places
    "+ Adaugă de mai sus", "în fila Comenzi", "fila Ieșiri", "în fila Ieşiri",
)


def test_no_text_names_a_place_that_does_not_exist():
    for lang, d in LANGS.items():
        bad = {k: v for k, v in d.items() if isinstance(v, str) and any(p in v for p in DEAD)}
        assert bad == {}, (lang, bad)
    for p in UI.glob("*.js"):
        # what the page can say (a comment is not said)
        said = "\n".join(ln for ln in p.read_text(encoding="utf-8").splitlines()
                         if not ln.lstrip().startswith(("//", "*", "/*")))
        for phrase in DEAD:
            assert phrase not in said, (p.name, phrase)


# keys whose words send the reader to an admin's menu item: shown only to an
# admin, or through _roleHint (an admin reads the way; anyone else reads who
# does it)
ADMIN_WAY = re.compile(r"⋯ →|the E key|\(or E\)")


# flows only an admin opens (Add Device, Add many): every word there is an admin's
ADMIN_ONLY_FILES = {"app-add-device.js", "adddev-model.js", "app-add-many.js"}


def test_a_hint_that_names_an_admin_item_is_said_to_an_admin_only():
    keys = sorted(k for k, v in LANGS["en"].items() if isinstance(v, str) and ADMIN_WAY.search(v))
    assert keys, "the scan sees the dictionary"
    bad = []
    for p in UI.glob("*.js"):
        if p.name in ADMIN_ONLY_FILES:
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            for k in keys:
                if f"'{k}'" in line and "_roleHint(" not in line and "data-admin" not in line and "// admin:" not in line:
                    bad.append(f"{p.name}:{n} {k}")
    assert bad == [], bad


# ── the L3.10 review (2026-11-01) ────────────────────────────────────────

@needs_tc
def test_review_m2_the_server_says_listen_only_before_the_lock(tmp_path, monkeypatch):
    """The menu's first reason is the server's first refusal: a locked tap
    is refused as read-only (it can never be written), not as locked."""
    c = _app(tmp_path, monkeypatch=monkeypatch, extra_devices=[
        {"id": "tap9", "enabled": True, "write_locked": True,
         "connection": {"protocol": "rtu_tap", "host": "192.0.2.9", "port": 4001, "unit_id": 2}}])
    r = c.post("/api/devices/tap9/write", json={"address": 100, "value": 1, "register_type": "holding"})
    assert r.status_code == 400 and "read-only" in str(r.json()), r.text


@needs_tc
def test_review_m1_writable_registers_leaves_out_what_a_command_owns(tmp_path):
    """A register a command writes (or one scaled by a live factor) is
    refused by /write: it is not counted as an operator's to write."""
    from tests.test_commands import _Inverter
    from tests.test_commands_api import _app as _cmd_app
    _cfg, _app_, client = _cmd_app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    dev = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "pv-u1")
    assert dev["writable_registers"] == 0, dev["writable_registers"]


@needs_tc
def test_review_m4_a_row_says_whether_its_template_declares_components(tmp_path, monkeypatch):
    c = _app(tmp_path, monkeypatch=monkeypatch, extra_devices=[
        {"id": "gx9", "enabled": False, "template": "victron_gx_mqtt",
         "connection": {"protocol": "mqtt", "broker": "192.0.2.10", "topic": "N/abc/#", "vars": {"portal": "abc"}}}])
    by = {d["id"]: d for d in c.get("/api/devices").json()["devices"]}
    assert by["gx9"]["components_declared"] is True
    assert by["ctrl"]["components_declared"] is False


@needs_tc
def test_review_m4_a_device_on_a_bus_an_installation_reads_is_told_so(tmp_path):
    """Edit bus line… on such a bus is refused after the click (busLineOpen):
    the device's own locks say it first, so the menu holds the item."""
    from tests.test_devices_api import make_app
    tty = tmp_path / "ttySC5"
    tty.write_text("")
    _cfg, client = make_app(tmp_path, extra_yaml=f"""
endpoints:
  - id: pv
    name: PV
    template: eastron_sdm630
    enabled: false
    connection: {{protocol: rtu, serial_port: {tty}, baudrate: 9600, parity: N}}
    units: [{{unit_id: 1}}]
devices:
  - id: solo
    template: eastron_sdm120
    enabled: false
    connection: {{protocol: rtu, serial_port: {tty}, unit_id: 5, baudrate: 9600, parity: N}}
""")
    solo = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "solo")
    # its own code: line_installation is the unit's own lock (conn-step-model
    # takes it as "this device is a unit"); a bus-mate's is line_bus_installation
    own = [lk for lk in solo["locks"] if lk["field"] == "connection.line" and lk["code"] == "line_bus_installation"]
    assert own and own[0]["installation"] == "pv", solo["locks"]
    assert not any(lk["code"] == "line_installation" for lk in solo["locks"]), solo["locks"]
    unit = next(d for d in client.get("/api/devices").json()["devices"] if d.get("endpoint_id") == "pv")
    assert any(lk["code"] == "line_installation" for lk in unit["locks"])      # the unit's own, unchanged


def test_review2_a_bus_a_component_is_on_is_set_by_its_device():
    from types import SimpleNamespace as NS
    from multibus.device_summary import _bus_owner_locks
    cfg = NS(devices=[NS(id='solo', endpoint_id='', component_of=''),
                      NS(id='bank-u1', endpoint_id='', component_of='bank')])
    out = _bus_owner_locks(cfg, {'port': 'serial'}, {'bank-u1'})
    assert [(lk['field'], lk['code'], lk.get('parent')) for lk in out] == [('connection.line', 'line_bus_component', 'bank')]
    assert _bus_owner_locks(cfg, {'port': 'serial'}, {'solo'}) == []
    assert _bus_owner_locks(cfg, {'port': 'ethernet'}, {'bank-u1'}) == []


@needs_tc
def test_review_m4_a_bridge_lock_says_whether_the_line_is_set_from_here(tmp_path):
    from tests.test_device_summary import _fixture
    from tests.test_devices_api import make_app
    _t, yml = _fixture(tmp_path)
    _cfg, client = make_app(tmp_path, extra_yaml=yml)
    by = {d["id"]: d for d in client.get("/api/devices").json()["devices"]}
    lk = next(x for x in by["brpoll"]["locks"] if x["code"] == "line_bridge")
    assert lk["settable"] is False          # a transparent converter keeps its own line


@needs_tc
def test_review2_our_serial_bridge_takes_its_line_from_here(tmp_path):
    from tests.test_devices_api import make_app
    _cfg, client = make_app(tmp_path, extra_yaml='''
bridges:
  - {id: pi, name: Pi, type: mbg_serial_bridge, host: 192.0.2.61, ports: [{port: 4196}]}
devices:
  - id: brpi
    enabled: false
    connection: {protocol: rtu, bridge: pi, bridge_port: 4196, unit_id: 9}
''')
    dev = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "brpi")
    lk = next(x for x in dev["locks"] if x["code"] == "line_bridge")
    assert lk["settable"] is True


def test_review2_the_credential_way_opens_a_tab_that_exists():
    tabs = re.search(r"const TABS = \[([^\]]*)\]", _method(_js("app-settings.js"), "securityTab")).group(1)
    tabs = re.findall(r"'(\w+)'", tabs)
    body = _method(_js("app-device-view.js"), "ddvWhy")
    asked = re.findall(r"'([\w-]+)'", body[body.index("securityTab"):body.index("securityTab") + 160])
    named = [x for x in asked if x not in ("security", "security-login")]
    assert named and all(x in tabs for x in named), (named, tabs)
    assert "accounts" in named


def test_review_nit_a_reason_is_said_once():
    view = _js("app-device-view.js")
    body = _method(view, "_devMenuItemHtml")
    assert 'class="mbg-menu-why" aria-hidden="true"' in body
    assert "ddvDeleteWhy" not in view and "ddvDeleteWhy" not in _js("app-devices.js")


def test_review_m3_facts_are_asked_again_and_a_failure_is_not_free():
    view = _js("app-device-view.js")
    assert "own.usedBy = 0" not in view and "own.usedBy = null" in view
    assert "this._ddvFacts = {}" in _js("app-devices.js")     # a list drawn again asks again


def test_review_m5_download_map_leaves_the_register_context_alone():
    body = _method(_js("app-device-view.js"), "ddvDownloadMap")
    assert not re.search(r"_regDevice\s*=(?!=)", body) and "downloadRegisterMap(id)" in body


def test_review_m6_the_write_dialog_names_its_device():
    assert 'id="writeModalDevice"' in HTML
    assert "writeModalDevice" in _method(_js("app-registers.js"), "openWriteModal")


def test_review_b1_component_actions_need_no_open_page():
    """Rename…, Disable… / Enable and Remove… of a component work from a list
    row, a table row or a bridge line: none waits for its page."""
    src = _js("app-components.js")
    for name in ("renameComponentUi", "switchComponentUi", "removeComponentUi"):
        assert "if (!s) return" not in _method(src, name), name
