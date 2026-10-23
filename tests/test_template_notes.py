# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""A template tells its users what they should know before relying on it — a
model it does not fit, a value the device leaves at 0 — and the gateway shows
it where the template is picked and on every device made from it. And a field
nothing classifies is filed under its own section in InfluxDB, never under
another device's name."""
from types import SimpleNamespace

from multibus.device_template import parse_template, validate_template
from tests.test_devices_api import make_app, needs_tc

REGS = [{"address": 0, "name": "flow", "data_type": "uint16"}]


def _tpl(notes, **blocks):
    return {"device_template": {"schema_version": 1, "id": "notes_t", "name": "Notes",
                                "protocol": {"transports": ["tcp"]},
                                "poll_groups": {"normal": {"interval": 5}},
                                "registers": REGS, "notes": notes, **blocks}}


def test_a_note_is_checked_where_it_is_saved():
    errs = " | ".join(validate_template(_tpl([
        {"level": "danger", "text": "x"},
        {"text": "  "},
        {"text": "ok", "fields": ["flow", "nope"]},
        {"text": "ok", "link": "javascript:alert(1)"},
        "plain text"])))
    for frag in ("notes[0]: level is info or warning", "notes[1]: text is required",
                 "notes[2]: fields not in this template: nope", "notes[3]: link must be an http(s)",
                 "notes[4]: must be an object"):
        assert frag in errs, frag
    assert "too many" not in errs
    assert validate_template(_tpl([{"text": str(i)} for i in range(21)])) == \
        ["notes must be a list of at most 20 notes"]


def test_a_note_may_name_rows_and_commands_and_round_trips():
    data = _tpl([{"level": "warning", "text": "reads 0 on this model", "fields": ["flow", "boost"],
                  "link": "https://example.org/x"}, {"text": "connect once first"}],
                commands={"boost": {"writes": [{"register": "flow", "value": 1}]}})
    REGS[0]["writable"] = True
    try:
        assert validate_template(data) == []
        t = parse_template(data)
        assert t.notes[0]["fields"] == ["flow", "boost"]
        assert parse_template(t.to_dict()).notes == t.notes
        assert t.summary()["notes"] == t.notes
    finally:
        REGS[0].pop("writable")


@needs_tc
def test_the_device_and_the_template_list_carry_the_notes(tmp_path):
    _cfg, client = make_app(tmp_path)
    tpls = {t["id"]: t for t in client.get("/api/device-templates").json()["templates"]}
    mim = tpls["samsung_mim_b19n"]["notes"]
    assert any(n["level"] == "warning" and "quiet" in n.get("fields", []) for n in mim)
    r = client.post("/api/devices", json={"id": "hp", "enabled": False, "template": "samsung_mim_b19n",
                                          "connection": {"protocol": "tcp", "host": "192.168.1.9"}})
    assert r.status_code in (200, 201), r.text
    dev = next(d for d in client.get("/api/devices").json()["devices"] if d["id"] == "hp")
    assert dev["template_notes"] == mim


def test_a_field_nothing_classifies_goes_under_its_own_section():
    from multibus.influxdb_publisher import get_measurement

    def m(unit, category=None, name="x"):
        r = SimpleNamespace(influxdb_measurement="", unit=unit, name=name)
        if category is not None:
            r.category = category
        return get_measurement(r)
    assert m("°C", "outdoor") == "outdoor"
    assert m("kgf/cm²", "Hydro kit") == "hydro_kit"
    assert m("", "") == "other" and m("°C") == "other"      # never another device's name
    assert m("V", "outdoor") == "voltage"                    # a known unit still wins


def test_a_selected_row_keeps_its_section():
    from multibus.config import Config
    rows = Config._parse_selected_payload({"registers": [
        {"address": 1, "name": "t", "label": "T", "unit": "°C", "data_type": "int16",
         "poll_group": "normal", "category": "outdoor"}]})
    assert rows[0].category == "outdoor"
