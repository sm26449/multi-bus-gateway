# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""L3.11b (ADR-0032 §6): one way to edit a device.

- the primary meter is edited in the Edit device modal (PATCH, L3.11a); its
  old page form — "Save & Apply" with a PUT and a separate poll-group POST —
  is gone (FINDINGS L3-F19), and no kind opens a page form instead
  (``EDIT_TARGETS`` has no ``byKind``);
- a unit and a component are edited in the modal too: a unit's name by the
  unit route, a component's name and on/off by its device's route, each
  through ``DeviceStore.devWrite`` (ADR-0030);
- Identity saves the attributes' identity category with the same Save, under
  the attributes' own revision (If-Match);
- the PQ recorder's settings are staged with the rest of Outputs (L3-F21):
  no button of its own in the modal;
- a lock says why, with the way there: the gateway's HA switch is in
  Settings → MQTT;
- the large editors open full screen, one Save in the edit bar, the same
  editor wherever they are opened from: the list's old stand-alone register
  editor is that editor (L3-UX-6).
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
UI = ROOT / "ui" / "js"
HTML = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")


def _js(name):
    return (UI / name).read_text(encoding="utf-8")


def _method(src, name):
    """The body of a prototype method `name(…) {` up to the next one."""
    m = re.search(r"\n    (?:async )?" + re.escape(name) + r"\([^)]*\)\s*\{\n(.*?)\n    \},", src, re.S)
    assert m, name
    return m.group(1)


def _has_method(src, name):
    return re.search(r"\n    (?:async )?" + re.escape(name) + r"\([^)]*\)\s*\{\n", src) is not None


# ── the primary: the modal, never its old page form ─────────────────────────

def test_the_primarys_page_form_is_gone():
    js = _js("app-devices.js")
    assert 'saveDeviceDetail' not in js, "the primary's old Save & Apply (PUT) is still on the page"
    assert 'ddvPollGroups' not in js, "the primary's old interval inputs are still on the page"
    assert not _has_method(js, "_mountDeviceConnection"), "the primary's page connection form is still mounted"
    # the intervals go with the PATCH (polling.poll_groups), never a POST of their own from the page
    assert "/poll-groups`, " not in js and "'POST', `/api/devices/${encodeURIComponent(s.id)}/poll-groups`" not in js


def test_no_kind_opens_a_page_form_instead_of_the_modal():
    vm = _js("device-view-model.js")
    assert "PRIMARY_FORM" not in vm and "byKind" not in vm


def test_the_modal_edits_every_kind():
    js = _js("app-device-edit.js")
    body = _method(js, "_devEditOpenNow")
    # no kind is turned away: the sections come from the shell, for every kind
    assert "if (!sections.length) return false;" not in body or "sectionsFor" in body
    sh = _js("device-shell.js")
    assert "return k === 'device' ? SECTIONS.slice() : [];" not in sh, "sectionsFor still leaves the primary, a unit and a component out"


# ── a unit, a component: their own routes, through the store ────────────────

def test_a_unit_is_renamed_by_its_route_through_the_store():
    body = _method(_js("app-device-edit.js"), "_deSaveUnit")
    assert "DeviceStore.devWrite(" in body and "/units/" in body and "'PATCH'" in body
    assert "method: 'PUT'" not in body and "_endpointPutBody" not in body


def test_a_component_is_changed_by_its_devices_route_through_the_store():
    body = _method(_js("app-device-edit.js"), "_deSaveComponent")
    assert "DeviceStore.devWrite(" in body and "/components/" in body and "'PATCH'" in body


def test_the_unit_page_form_is_gone():
    js = _js("app-devices.js")
    assert "saveUnitName" not in js, "a unit's own Save on the page: its name is edited in the modal"


# ── Identity: the attributes with the same Save, under their own revision ───

def test_the_attributes_are_written_with_their_revision():
    body = _method(_js("app-device-edit.js"), "_deSaveAttributes")
    assert "/api/attributes/devices/" in body and "/identity" in body and "If-Match" in body


def test_the_identity_section_holds_the_attributes():
    js = _js("app-device-edit.js")
    assert "this._deAboutHtml(ed)" in _method(js, "_deIdentityHtml")
    about = _method(js, "_deAboutHtml")
    assert "`about.${k}`" in about and "_DE_ABOUT" in about
    fields = js[js.index("_DE_ABOUT: {"):]
    fields = fields[:fields.index("\n    },\n")]
    for f in ("description", "location", "labels", "manufacturer", "model", "serial_number", "firmware", "installed_on"):
        assert re.search(r"\n\s+" + f + r": \{", fields), f


# ── the PQ recorder: staged with the rest (L3-F21) ──────────────────────────

def test_the_pq_recorder_has_no_button_of_its_own_in_the_modal():
    js = _js("app-device-edit.js")
    out = _method(js, "_deOutputsHtml")
    assert "data-pq-own" not in out and "pqOwn" not in out
    assert "/api/pq/config" in _method(js, "_deSavePq") and "DeviceStore.devWrite(" in _method(js, "_deSavePq")


# ── a lock says why, with the way there ─────────────────────────────────────

def test_the_ha_lock_leads_to_settings_mqtt():
    body = _method(_js("app-device-edit.js"), "_deLockWay")
    assert re.search(r"'outputs\.ha_discovery'\s*:\s*'mqtt'", body), body


# ── the large editors: full screen, one Save, the same editor everywhere ────

def test_the_large_editors_open_full_screen_with_one_save():
    dv = _js("app-device-view.js")
    assert "tg.full" in _method(dv, "ddvEdit")
    bar = _method(dv, "_ddvEditBarShow")
    assert "ddvEditSave" in bar
    assert 'id="ddvEditSave"' in _js("app-devices.js")
    css = (ROOT / "ui" / "css").glob("*.css")
    assert any(".ddv-full" in p.read_text(encoding="utf-8") for p in css)


def test_the_old_stand_alone_register_editor_is_the_page_editor():
    body = _method(_js("app-registers.js"), "jumpToDeviceRegisters")
    assert "openDeviceDetail" in body and "ddvEdit('measurements')" in body
    assert "deviceRegistersTitle" not in body, "the stand-alone overlay is still drawn"


# ── the review of L3.11b (2026-11-01) ───────────────────────────────────────

def test_review_m1_one_leave_guard_everywhere_a_page_is_left():
    """M1: staged edits (the modal's, a full editor's) are asked about before
    Back, a hash change, a nav tab, another device or Back to the list."""
    dv = _js("app-device-view.js")
    guard = _method(dv, "_ddvLeave")
    assert "_deLeave" in guard and "_ddvFullDirty" in guard and "_confirmDiscard" in guard
    assert "this._ddvLeave()" in _method(_js("app-router.js"), "_routeApply")
    assert "_ddvFullDirty" in _method(_js("app-core.js"), "navigateTo")
    devs = _js("app-devices.js")
    # (R2-1: through _ddvBeforeOpen — the modal's guard, else _ddvLeave)
    assert "this._ddvBeforeOpen()" in _method(devs, "openDeviceDetail")
    assert "this._ddvLeave()" in _method(devs, "closeDeviceDetail")


def test_review_m1_the_full_editor_holds_the_focus_and_esc_asks():
    dv = _js("app-device-view.js")
    assert ".inert" in _method(dv, "_ddvFullTrap")
    keys = _method(dv, "_ddvWireKeys")
    assert "'Escape'" in keys and "ddvEditDone" in keys


def test_review_m2_the_banner_names_its_real_scope():
    import json
    for lang in ("en", "ro"):
        words = json.loads((ROOT / "ui" / "languages" / f"{lang}.json").read_text(encoding="utf-8"))
        assert "{group}" in words["devedit.outputsShared"], lang


def test_review_n2_a_staged_tick_is_said_on_or_off():
    body = _method(_js("app-cmdbinding.js"), "_cmdSaveStaged")
    assert "ddv.full.cmdEnabled" in body and "ddv.full.cmdDisabled" in body


def test_review2_a_link_out_of_the_edit_modal_asks_first():
    """R2-1: a link inside the Edit modal (a component's device, a unit's
    installation) opens a page; opening it asks about the modal's edits."""
    assert "_ddvBeforeOpen" in _method(_js("app-devices.js"), "openDeviceDetail")
    assert "_ddvBeforeOpen" in _method(_js("app-endpoints.js"), "openEndpointDetail")
    guard = _method(_js("app-device-view.js"), "_ddvBeforeOpen")
    assert "_deLeave" in guard and "_ddvLeave" in guard


def test_review2_navigate_asks_about_the_modals_edits():
    assert "_deDirty" in _method(_js("app-core.js"), "navigateTo")


def test_review2_a_discard_resets_the_typed_binding_forms():
    """R2-3: one question for one move — Discard leaves no typed form dirty."""
    body = _method(_js("app-device-view.js"), "_ddvFullDrop")
    assert ".cb-form" in body and "defaultValue" in body


def test_review2_a_part_button_keeps_its_icon_and_label_on_one_line():
    css = (ROOT / "ui" / "css" / "config.css").read_text(encoding="utf-8")
    m = re.search(r"\.ddv-editparts \.seg-btn \{([^}]*)\}", css)
    assert m and "flex-wrap: nowrap" in m.group(1)


def test_review_n3_the_template_notes_stay_under_the_bar_and_the_part_shows_pressed():
    assert "tpl-notes-fold" in _method(_js("app-device-view.js"), "_ddvFullTrap")
    css = (ROOT / "ui" / "css" / "config.css").read_text(encoding="utf-8")
    assert re.search(r'\.ddv-editparts \.seg-btn\[aria-pressed="true"\]', css)
