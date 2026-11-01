# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""L3.9 (ADR-0031 §2): the new Add Device is the only one.

- the old wizard is gone: no app-wizard.js, no #deviceWizardModal, no
  devWiz ids or methods left behind, no flag;
- every way in (the Add button, a fresh install's first device, a bridge's
  + Device, a scan row, discovery, Found, Duplicate) goes through
  ``openAddDevice``;
- no probe device anywhere (L3-F3): the connection is judged by
  ``dry_run=true&only=connection``, and the string ``x-dry-run`` is gone;
- the Check step reads ``/api/previews``; the device page's Preview goes
  through the one device write path (ADR-0030);
- every ``adddev.*`` / ``pv.*`` word the flow says exists in en and ro.
"""
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
UI = ROOT / "ui" / "js"
HTML = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")


def _js(name):
    return (UI / name).read_text(encoding="utf-8")


def _all_js():
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(UI.glob("*.js"))}


def test_the_old_wizard_is_gone():
    assert not (UI / "app-wizard.js").exists()
    assert "app-wizard.js" not in HTML
    assert 'id="deviceWizardModal"' not in HTML
    assert not re.search(r'id="devWiz', HTML)
    for name, js in _all_js().items():
        for word in ("_devWiz", "deviceWizardModal", "devWizard", "devWizBody", "_wizSay", "_wizMtrFieldId"):
            assert word not in js, f"{name}: {word}"


def test_no_flag_the_new_add_is_the_add():
    for name, js in _all_js().items():
        assert "_newAddOn" not in js and "mbg.newAdd" not in js and "newadd" not in js, name


def test_every_way_in_opens_the_new_flow():
    add = _js("app-add-device.js")
    m = re.search(r"\n    openDeviceWizard\(([^)]*)\) \{\n(.*?)\n    \},", add, re.S)
    assert m, "openDeviceWizard: the one entry every caller has"
    assert "openAddDevice(" in m.group(2)
    # the Add button and the fresh install's first device
    assert 'data-action="openDeviceWizard"' in HTML
    # a bus's + Device, a scan row, discovery: each hands a prefill to it
    for name in ("app-bridges.js", "app-devices.js", "app-core.js"):
        assert "openDeviceWizard(" in _js(name), name
    # Duplicate is offered (it was held back until the copy list existed)
    assert "duplicate: true" in _js("app-device-view.js")
    assert "case 'duplicate'" in _js("app-device-view.js")


def test_no_probe_device_anywhere():
    for name, js in _all_js().items():
        assert "x-dry-run" not in js, name
    assert "dry_run=true&only=connection" in _js("app-add-device.js")


def test_the_check_and_the_device_preview():
    add = _js("app-add-device.js")
    assert "this._pvStart(body)" in add and "this._pvFollow(" in add
    assert "confirm_open" in add
    pv = _js("app-preview.js")
    assert "fetch('/api/previews'" in pv and "/api/previews/${" in pv
    assert re.search(r"DeviceStore\.devWrite\([^)]*'POST',\s*`/api/devices/\$\{encodeURIComponent\([^)]*\)\}/preview`", pv)


def test_every_word_of_the_flow_exists_in_en_and_ro():
    used = set()
    for name in ("app-add-device.js", "adddev-model.js", "app-preview.js"):
        used |= set(re.findall(r"""\bt[n]?\(\s*'((?:adddev|pv)\.[A-Za-z0-9_.]+)'""", _js(name)))
    assert used
    for lang in ("en", "ro"):
        tr = json.loads((ROOT / "ui" / "languages" / f"{lang}.json").read_text(encoding="utf-8"))
        missing = sorted(k for k in used if k not in tr and f"{k}.other" not in tr)
        assert not missing, f"{lang}: {missing}"


def test_no_style_is_left_for_the_old_wizard():
    css = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "ui" / "css").glob("*.css"))
    assert "deviceWizardModal" not in css and "devWizBody" not in css
