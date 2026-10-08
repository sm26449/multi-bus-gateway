# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Everything in a template is editable in the UI (3.91): the editor's checks
answer without saving, a formula is checked against the map being edited, an
identify block is tried on a real device, and a save names every mistake in
the parts that used to pass unchecked."""
from types import SimpleNamespace

from multibus.device_template import parse_template, validate_template
from tests.test_devices_api import make_app, needs_tc


def _tpl(regs, **blocks):
    return {"device_template": {"schema_version": 1, "id": "adv_t", "name": "Adv",
                                "protocol": {"transports": ["tcp"]},
                                "poll_groups": {"normal": {"interval": 5}},
                                "registers": regs, **blocks}}


REGS = [{"address": 0, "name": "power", "data_type": "int16", "writable": True,
         "write_min": 0, "write_max": 100},
        {"address": 1, "name": "soc", "data_type": "uint16"}]


def test_a_save_names_mistakes_in_row_extras_and_commands():
    errs = validate_template(_tpl([
        {**REGS[0], "aggregates": {"pack_power": "total"}, "nan": "n/a", "monotonic": "yes",
         "state_class": "gauge", "icon": "flash", "suggested_display_precision": 12},
        REGS[1]],
        categories={"basic": {"label": 3}},
        commands={"limit": {"params": {"value": {"min": 10, "max": 5, "default": 50}},
                            "writes": [{"register": "nope", "value": "${missing}"}],
                            "guard": [{"read": "soc", "expect": "${value}"}],
                            "safe": {"other": 1}, "readback_group": "fast"}}))
    text = " | ".join(errs)
    for frag in ("aggregates op 'total'", "nan must be", "monotonic must be true or false",
                 "state_class must be", "icon must look like", "suggested_display_precision",
                 "category 'basic': label must be text", "min 10 > max 5", "default 50 is outside",
                 "'nope' is not a register", "unknown parameter 'missing'", "safe names 'other'",
                 "readback_group 'fast'"):
        assert frag in text, (frag, errs)


def test_a_file_on_disk_with_old_mistakes_still_loads():
    """An upgrade never drops a device: the newer checks only warn on load."""
    data = _tpl([{**REGS[0], "aggregates": {"x": "total"}}, REGS[1]])
    assert parse_template(data, strict=False).id == "adv_t"
    try:
        parse_template(data)
    except ValueError as e:
        assert "aggregates op" in str(e)
    else:
        raise AssertionError("a save must refuse it")


@needs_tc
def test_check_answers_without_saving(tmp_path):
    cfg, client = make_app(tmp_path)
    r = client.post("/api/device-templates/check", json=_tpl(REGS, calculated=[{"name": "x", "expr": "power +"}]))
    assert r.status_code == 200 and r.json()["ok"] is False and "syntax" in " ".join(r.json()["errors"])
    assert client.post("/api/device-templates/check", json=_tpl(REGS)).json() == {"ok": True, "errors": []}
    assert client.get("/api/device-templates/adv_t").status_code == 404          # nothing saved


@needs_tc
def test_a_formula_is_checked_against_the_map(tmp_path):
    cfg, client = make_app(tmp_path)
    tpl = _tpl(REGS, calculated=[{"name": "kw", "expr": "power / 1000"}])
    ask = lambda expr, name="new": client.post("/api/device-templates/check-expression",  # noqa: E731
                                               json={"template": tpl, "expr": expr, "name": name}).json()
    assert ask("power * soc / 100") == {"ok": True, "refs": ["power", "soc"]}
    assert ask("kw * 2")["ok"] is True                                  # another calculated field
    r = ask("powr * 2")
    assert r["ok"] is False and r["unknown"] == ["powr"]
    assert ask("kw + 1", name="kw")["error"] == "'kw' reads itself"
    assert "syntax" in ask("power +")["error"]
    assert "another device" in ask("meter.power + power")["warning"]


@needs_tc
def test_identify_is_tried_on_a_running_device(tmp_path):
    from multibus.modbus_client import ModbusConfig, ModbusConnection, _TRANSPORTS
    from tests.test_shared_transport import _Client
    _TRANSPORTS.clear()
    _Client.ok = True
    conn = ModbusConnection(ModbusConfig(host="192.0.2.90", port=502, unit_id=3, retry_attempts=1,
                                         retry_delay=0), trace_label="umg512")
    conn._new_client = _Client
    conn.connect()
    _Client.read_device_information = lambda self, device_id: SimpleNamespace(information={})
    from fastapi.testclient import TestClient
    from multibus.api import create_api
    from tests.test_devices import write_config
    cfg = write_config(tmp_path)
    fake = SimpleNamespace(publish_callback=None, connection=conn)
    app, _ = create_api(cfg, fake, None, None, devices=[(d, fake) for d in cfg.devices])
    client = TestClient(app, raise_server_exceptions=False)
    yes = _tpl(REGS, identify={"registers": [{"address": 1, "equals": 0}]})       # the fake reads 0
    no = _tpl(REGS, identify={"registers": [{"address": 1, "equals": 731}]})
    r = client.post("/api/device-templates/identify-test", json={"template": yes, "device": "umg512"}).json()
    assert r["ok"] is True and r["unit_id"] == 3, r
    r = client.post("/api/device-templates/identify-test", json={"template": no, "device": "umg512"}).json()
    assert r["ok"] is False and "does not match" in r["message"]
    r = client.post("/api/device-templates/identify-test", json={"template": _tpl(REGS), "device": "umg512"})
    assert r.status_code == 422
    _TRANSPORTS.clear()
