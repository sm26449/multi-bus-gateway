# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""L3.10-F16 (review 2 N3): a lock's text is the server's English. The page
says it in its own language: `ui/js/lock-text.js` maps the lock's `code`
(and, for the primary's locks, its field) to a dictionary key, filled with
the lock's `params`; the server's text stays the fallback (an unknown code,
a key whose words need a value the lock does not carry).

- every code the server can emit has a key, in English and in Romanian —
  the codes read from the server's own code (multibus/device_summary.py),
  never a hand-kept list;
- the two languages ask for the same values;
- every value a key asks for is in the lock's `params`, for every lock the
  devices of a fixture carry (each code met at least once);
- the setup's "Fixed, and why" goes through the helper."""
import ast
import json
import re
from pathlib import Path

import pytest

from tests.test_devices_api import make_app, needs_tc

ROOT = Path(__file__).resolve().parent.parent
SUMMARY = ROOT / "multibus" / "device_summary.py"
LANG = {k: json.loads((ROOT / "ui" / "languages" / f"{k}.json").read_text(encoding="utf-8")) for k in ("en", "ro")}
PH = re.compile(r"\{(\w+)\}")


def _server_codes(src=None):
    """Every lock code device_summary.py can emit: the code of each `_lock`
    call — positional (`_lock(field, 'code', …)`) or by keyword (`code=…`) —
    and each `code = '…'` the class lock picks from. A `_lock` call whose code
    the scan cannot read fails the scan, never passes it silently (review n2)."""
    tree = ast.parse(src if src is not None else SUMMARY.read_text(encoding="utf-8"))
    codes = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_lock":
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            a = node.args[1] if len(node.args) >= 2 else kw.get("code")
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                codes.add(a.value)
            elif isinstance(a, ast.Name) and a.id == "code":
                pass                                       # the assignments below
            else:
                raise AssertionError(f"a lock code the scan cannot read: line {node.lineno}")
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "code" for t in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                codes.add(node.value.value)
    return codes


def test_the_scan_reads_a_code_given_by_keyword_and_refuses_one_it_cannot_read():
    assert _server_codes("_lock('f', code='by_kw', text='t')\n_lock('f', 'pos', 't')") == {"by_kw", "pos"}
    import pytest as _pt
    with _pt.raises(AssertionError):
        _server_codes("_lock('f', text='no code')")
    with _pt.raises(AssertionError):
        _server_codes("_lock('f', code=some_call(), text='t')")


def test_romanian_components_lock_reads_right_for_any_count():
    """Review n3: "are 1 componente" / "are 20 componente" — the words do not
    put a count before the noun (lock texts are not plural-aware)."""
    assert "{n} componente" not in LANG["ro"]["lock.components"]


def _primary_fields():
    from multibus.device_patch import PRIMARY_FIXED
    return set(PRIMARY_FIXED)


def _keys_of(code):
    if code == "primary":
        return [f"lock.primary.{f}" for f in sorted(_primary_fields() | {"connection.protocol"})]
    return [f"lock.{code}"]


def test_the_scan_sees_the_server_code():
    codes = _server_codes()
    # a broken scan would pass everything below
    assert {"line_shared", "role_bus_polled", "installation_unit", "primary", "protocol_fixed",
            "line_bus_component", "can_listen"} <= codes, codes
    assert len(codes) >= 15, codes


@pytest.mark.parametrize("lang", ["en", "ro"])
def test_every_lock_code_the_server_can_emit_has_words(lang):
    missing = [k for c in sorted(_server_codes()) for k in _keys_of(c) if not LANG[lang].get(k)]
    assert missing == [], missing


def test_romanian_says_it_in_romanian_with_the_same_values():
    for c in sorted(_server_codes()):
        for k in _keys_of(c):
            en, ro = LANG["en"][k], LANG["ro"][k]
            assert en != ro, k
            assert set(PH.findall(en)) == set(PH.findall(ro)), (k, en, ro)


def _fixture_locks(tmp_path):
    from tests.test_device_summary import _UNIT_AND_COMPONENT, _fixture
    out = []
    (tmp_path / "a").mkdir()
    _t, yml = _fixture(tmp_path / "a")
    _cfg, client = make_app(tmp_path / "a", extra_yaml=yml)
    out += [lk for d in client.get("/api/devices").json()["devices"] for lk in d["locks"]]
    (tmp_path / "b").mkdir()
    _cfg, client = make_app(tmp_path / "b", extra_yaml=_UNIT_AND_COMPONENT)
    out += [lk for d in client.get("/api/devices").json()["devices"] for lk in d["locks"]]
    (tmp_path / "c").mkdir()
    tty = tmp_path / "c" / "ttyS9"
    tty.write_text("")
    _cfg, client = make_app(tmp_path / "c", extra_yaml=f"""
devices:
  - {{id: a, connection: {{protocol: rtu, serial_port: {tty}, unit_id: 1}}}}
  - {{id: b, connection: {{protocol: rtu, serial_port: {tty}, unit_id: 2, baudrate: 19200}}}}
  - {{id: t1, connection: {{protocol: rtu_tap, host: 192.0.2.9, port: 4001, unit_id: 1}}}}
  - {{id: t2, connection: {{protocol: rtu_tap, host: 192.0.2.9, port: 4001, unit_id: 2}}}}
""")
    out += [lk for d in client.get("/api/devices").json()["devices"] for lk in d["locks"]]
    return out


@needs_tc
def test_every_value_a_lock_text_asks_for_is_in_its_params(tmp_path):
    locks = _fixture_locks(tmp_path)
    seen = set()
    for lk in locks:
        assert isinstance(lk.get("params"), dict), lk
        key = f"lock.primary.{lk['field']}" if lk["code"] == "primary" else f"lock.{lk['code']}"
        if lk["code"] == "line_shared" and lk["params"].get("disagree"):
            key = "lock.line_shared_disagree"
        for lang in ("en", "ro"):
            want = set(PH.findall(LANG[lang].get(key, "")))
            assert want <= set(lk["params"]), (lang, key, want, lk)
        seen.add(lk["code"])
    # the fixtures meet most codes (the rest are pinned by the key test above)
    assert {"line_shared", "line_bridge", "line_remote", "role_bus_polled", "role_bus_listened",
            "template_listens", "can_listen", "protocol_fixed", "meter_only", "primary",
            "installation_unit", "component", "line_installation"} <= seen, seen


def test_the_setup_list_goes_through_the_helper():
    view = (ROOT / "ui" / "js" / "app-device-view.js").read_text(encoding="utf-8")
    m = re.search(r"Fixed, and why'\)\)\}</h4>\s*<ul>\$\{sec\.locks\.map\(l => [^\n]*", view)
    assert m and "this._lockText(l)" in m.group(0), "Setup's lock list says the page's words"
    shell = (ROOT / "ui" / "templates" / "index.html").read_text(encoding="utf-8")
    assert shell.index("/static/js/lock-text.js") < shell.index("/static/js/app-device-view.js")


def test_every_combination_the_matrix_refuses_has_words():
    """The connection step's held roles say the matrix's reason (`INVALID`):
    in the page's words too (the Ethernet role pre-fill says it, L3-UX-12)."""
    from multibus.connection_matrix import INVALID
    codes = {c["code"] for c in INVALID["device"]}
    assert codes
    for lang in ("en", "ro"):
        missing = [c for c in sorted(codes) if not LANG[lang].get(f"cs.inv.{c}")]
        assert missing == [], (lang, missing)
    model = (ROOT / "ui" / "js" / "conn-step-model.js").read_text(encoding="utf-8")
    assert "`cs.inv.${inv.code}`" in model
