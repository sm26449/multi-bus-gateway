# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""L3.11c, static: Undo in the delete toast, the Restore dialog (ADR-0032 §8)
and the Add Device frictions (L3-UX-12). The behaviour is checked in a
browser by tools/e2e/undo_restore_e2e.mjs and add_device_frictions_e2e.mjs;
these pin the wiring a refactor could lose without a browser noticing."""
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
UI = ROOT / "ui" / "js"
HTML = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
LANGS = {n: json.loads((ROOT / "ui" / "languages" / f"{n}.json").read_text(encoding="utf-8")) for n in ("en", "ro")}


def _js(name):
    return (UI / name).read_text(encoding="utf-8")


def _method(src, name):
    # up to the next method, or the end of the mixin (its last method)
    m = re.search(r"\n    (?:async )?" + re.escape(name) + r"\([^)]*\)\s*\{\n(.*?)\n    \}(?:,|\n\}\);)", src, re.S)
    assert m, name
    return m.group(1)


# ── Delete → a toast with Undo for 10 s ──────────────────────────────────
def test_a_delete_ends_in_a_toast_with_undo():
    body = _method(_js("app-devices.js"), "deleteDevice")
    assert "this._undoToast(" in body
    assert "showToast('success', t('devices.deleted'" not in body, "not the plain toast any more"
    # the used-by flow stays as it was: asked first, refused into the section
    assert "this._ubDelete(ask)" in body and "_ubRecheck(" in body


def test_undo_is_the_restore_for_ten_seconds_keyboard_reachable():
    src = _js("app-restore.js")
    assert re.search(r"UNDO_MS\s*[:=]\s*10000", src), "Undo lasts 10 s"
    toast = _method(src, "_undoToast")
    assert "toast-undo" in toast and ".focus(" in toast, "the Undo button takes the focus the deleted row had"
    assert "'Escape'" in toast, "Esc dismisses it"
    assert "this._restoreNow(" in src
    restore = _method(src, "_restoreNow")
    assert "window.DeviceStore.devWrite(" in restore, "one write path (ADR-0030)"
    # the toast container announces it (polite); a refused Undo interrupts
    assert 'id="toast-container"' in HTML and 'aria-live="polite"' in HTML


# ── Deleted devices: restore… opens a dialog ─────────────────────────────
def test_the_restore_line_opens_the_restore_dialog():
    body = _method(_js("app-device-view.js"), "ddvOpenRestore")
    assert "this.openRestoreDialog()" in body
    assert "scrollIntoView" not in body, "not a scroll to a folded section any more"
    # a modal of the house: openModal gives its box role=dialog, aria-modal
    # and aria-labelledby (its header's heading)
    m = re.search(r'<div class="modal" id="restoreModal">\s*<div class="modal-content[^"]*">\s*<div class="modal-header">\s*<h3 id="restoreModalTitle"', HTML)
    assert m, "the Restore dialog, its title in its header"
    assert HTML.index("/static/js/app-restore.js") < HTML.index("/static/js/app-router.js")


def test_the_dialog_asks_the_gateway_why_one_cannot_come_back():
    src = _js("app-restore.js")
    assert "/api/devices/restorable?check=true" in src
    assert "problems" in _method(src, "_restoreRowHtml")


# ── Add Device frictions (L3-UX-12) ──────────────────────────────────────
def test_the_other_ways_in_come_before_the_model_cards():
    body = _method(_js("app-add-device.js"), "_adWhatHtml")
    assert body.index('class="ad-ways"') < body.index('id="adResults"')
    for lang in ("en", "ro"):
        for k in ("adddev.why.pick", "adddev.none"):
            assert "below" not in LANGS[lang][k] and "mai jos" not in LANGS[lang][k], (lang, k)


def test_the_chip_counts_follow_the_list():
    body = _method(_js("app-add-device.js"), "_adDrawResults")
    assert "M.chipCounts(" in body and "transport" in body


def test_the_result_has_one_done():
    body = _method(_js("app-add-device.js"), "_adFooter")
    assert re.search(r"sh\.el\.next\.hidden\s*=\s*!!ad\.result", body), "the footer's Done is not a second one"


def test_the_first_run_offer_waits_for_a_gateway_without_devices():
    body = _method(_js("app-core.js"), "_maybeFirstRun")
    assert "(devices || []).length" in body


# ── the L3.11c review (2026-11-01) ──────────────────────────────────────
def test_review_m1_m2_undo_sends_the_delete_it_undoes_and_tells_already_back():
    src = _js("app-restore.js")
    assert "deleted_ts=" in _method(src, "_restoreNow"), "the restore names the delete (m1)"
    toast = _method(src, "_undoToast")
    assert "o.restorable === false" in toast, "no Undo when nothing was kept (m2)"
    assert "'exists'" in src, "409 exists: already back, not failed (m2)"
    assert "resumed" in src and "resumes" in src, "what a restore starts again is said (m6)"


def test_review_m4_undo_away_from_the_list_offers_the_page_instead():
    body = _method(_js("app-restore.js"), "_afterRestore")
    assert "_actionToast(" in body, "an Open it link, not a jump"


def test_review_m3_an_adopted_line_is_announced_and_marked():
    body = _method(_js("app-add-device.js"), "_adAdoptLine")
    assert "ad.shell.say(" in body
    assert "data-adopted" in _js("app-add-device.js")


def test_review_m5_only_the_line_refusal_loses_its_field_prefix():
    body = _method(_js("app-preview.js"), "_pvRefusalHtml")
    assert "det.line" in body, "the prefix goes only where the gateway's line refusal names it"


def test_review2_r3_a_restore_from_the_dialog_names_its_row():
    body = _method(_js("app-restore.js"), "_restoreFromDialog")
    assert "deleted_ts" in body and "this._restoreNow(id, " in body


def test_review2_r5_the_marks_observer_is_disconnected_when_the_dialog_closes():
    assert "_adMarkObs.disconnect()" in _js("app-add-device.js")
