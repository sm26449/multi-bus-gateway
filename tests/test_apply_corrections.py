# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""The shared wire→value correction pipeline (backlog §I unification).

apply_corrections() is the ONE place nan/enum/bits/scale/offset/monotonic
happen, on every transport. These tests pin the contract:

- stage order matches the historical Modbus path (nan on RAW → decode →
  scale+offset → monotonic),
- every stage engages only when declared (undeclared = pass-through, so
  wiring the helper into HTTP/MQTT changed nothing for existing configs),
- the ``info['stage']`` diagnostic distinguishes the three None causes,
- the HTTP poller / MQTT input actually route through it (feature tests).
"""
from types import SimpleNamespace

from multibus.config import SelectedRegister
from multibus.counter_filter import MonotonicFilter
from multibus.value_decode import apply_corrections, is_sentinel_value


def _reg(**kw):
    base = dict(address=1, name="r", label="r", unit="", data_type="float",
                poll_group="normal")
    base.update(kw)
    return SelectedRegister(**base)


# ── unit: stages engage only when declared ───────────────────────────────────

def test_undeclared_register_passes_through():
    assert apply_corrections(123.5, _reg()) == 123.5
    assert apply_corrections(0, _reg()) == 0
    assert apply_corrections("text", _reg()) == "text"   # non-numeric untouched


def test_none_in_none_out():
    info = {}
    assert apply_corrections(None, _reg(), info=info) is None
    assert "stage" not in info          # nothing engaged — value was absent


def test_scale_and_offset():
    # engineering = raw/scale + offset, same convention on every transport
    assert apply_corrections(2305, _reg(scale=10.0)) == 230.5
    assert apply_corrections(50, _reg(offset=-40.0)) == 10.0
    assert apply_corrections(1000, _reg(scale=10.0, offset=5.0)) == 105.0
    # scale=0 / None must not divide-by-zero — treated as 1.0
    assert apply_corrections(7, _reg(scale=0)) == 7


def test_nan_sentinel_on_raw_value_before_scaling():
    info = {}
    # numeric sentinel is compared to the RAW value, not the scaled one
    r = _reg(nan=65535, scale=10.0)
    assert apply_corrections(65535, r, info=info) is None
    assert info["stage"] == "sentinel"
    assert apply_corrections(65534, r) == 6553.4          # non-sentinel scales


def test_nan_sentinel_list_and_type_standard():
    r = _reg(nan=[65535, 32767])
    assert apply_corrections(32767, r) is None
    assert apply_corrections(1, r) == 1
    # nan=True → the type-standard sentinel (uint16 → 0xFFFF)
    r2 = _reg(nan=True, data_type="uint16")
    assert apply_corrections(0xFFFF, r2) is None
    assert apply_corrections(0xFFFE, r2) == 0xFFFE


def test_enum_decode_skips_numeric_stages():
    # scale/offset are numeric-only: a status register with an (accidental)
    # scale must still decode to its label, never to a scaled number
    r = _reg(enum={4: "MPPT", 7: "Fault"}, scale=10.0, offset=5.0)
    assert apply_corrections(4, r) == "MPPT"
    assert apply_corrections(3, r) == "unknown (3)"


def test_bits_decode():
    r = _reg(bits={0: "overvolt", 2: "overtemp"})
    assert apply_corrections(0b101, r) == "overvolt, overtemp"
    assert apply_corrections(0, r) == ""                 # healthy/idle state


def test_decode_failure_reports_stage():
    info = {}
    r = _reg(enum={1: "on"})
    assert apply_corrections("garbage", r, info=info) is None
    assert info["stage"] == "decode_failed"


def test_monotonic_filter_only_when_passed():
    r = _reg(monotonic=True)
    # diagnostic callers pass no filter → monotonic is a no-op
    assert apply_corrections(5.0, r) == 5.0
    # polling callers own the stateful filter
    f = MonotonicFilter()
    info = {}
    assert apply_corrections(1000.0, r, counter_filter=f) == 1000.0
    assert apply_corrections(3.0, r, counter_filter=f, info=info) is None
    assert info["stage"] == "filter_drop"


def test_monotonic_applies_after_scaling():
    # the filter must see ENGINEERING values, else a scale change would look
    # like a giant counter reset
    r = _reg(monotonic=True, scale=1000.0)   # Wh on the wire → kWh
    f = MonotonicFilter()
    assert apply_corrections(5_000_000, r, counter_filter=f) == 5000.0
    assert apply_corrections(5_001_000, r, counter_filter=f) == 5001.0


def test_dict_like_register_supported():
    # diagnostic views hand in plain dicts — same contract
    assert apply_corrections(2305, {"scale": 10.0}) == 230.5
    assert apply_corrections(4, {"enum": {4: "MPPT"}}) == "MPPT"


def test_is_sentinel_value_semantics():
    assert is_sentinel_value(0xFFFF, "uint16", True)
    assert not is_sentinel_value(0xFFFE, "uint16", True)
    assert is_sentinel_value(9999, "float", 9999)
    assert is_sentinel_value(2, "float", [1, 2, 3])
    assert not is_sentinel_value(4, "float", [1, 2, 3])
    assert not is_sentinel_value(1, "float", False)      # False = not declared


# ── feature: HTTP poller routes through the pipeline ─────────────────────────

def _http_poller(regs):
    from multibus.http_client import _JsonPoller
    owner = SimpleNamespace(_note_success=lambda n: None,
                            _note_failure=lambda: None)
    return _JsonPoller("g", 1.0, regs, lambda: None, None, owner)


def _http_extract(poller, doc):
    """Run one extraction pass exactly as _JsonPoller.run() does."""
    from multibus.http_client import _coerce_numeric, resolve_json_path
    from multibus.value_decode import apply_corrections as ac
    data = {}
    for reg in poller.registers:
        val = _coerce_numeric(resolve_json_path(doc, reg.json_path))
        if val is None:
            continue
        cf = None
        if reg.monotonic:
            cf = poller._counter_filters.setdefault(reg.address, MonotonicFilter())
        val = ac(val, reg, counter_filter=cf)
        if val is None:
            continue
        data[reg.address] = val
    return data


def test_http_poller_has_per_register_filters():
    p = _http_poller([_reg(address=1, json_path="r", monotonic=True),
                      _reg(address=2, name="e2", label="e2", json_path="e2",
                           monotonic=True)])
    assert p._counter_filters == {}
    _http_extract(p, {"r": 100.0, "e2": 200.0})
    # one independent filter per monotonic register, keyed by address
    assert set(p._counter_filters) == {1, 2}
    assert p._counter_filters[1] is not p._counter_filters[2]


def test_http_monotonic_drop_holds_last_good():
    p = _http_poller([_reg(address=1, name="energy", label="energy",
                           json_path="energy", monotonic=True)])
    assert _http_extract(p, {"energy": 5000.0}) == {1: 5000.0}
    # firmware glitch: counter dips → dropped, register absent (hold last-good)
    assert _http_extract(p, {"energy": 3.0}) == {}
    assert _http_extract(p, {"energy": 5001.0}) == {1: 5001.0}


def test_http_nan_sentinel_dropped():
    p = _http_poller([_reg(address=1, name="t", label="t", json_path="t",
                           nan=[-999])])
    assert _http_extract(p, {"t": 21.5}) == {1: 21.5}
    assert _http_extract(p, {"t": -999}) == {}


def test_http_enum_decodes_to_text():
    p = _http_poller([_reg(address=1, name="state", label="state",
                           json_path="state", enum={0: "idle", 2: "charging"})])
    assert _http_extract(p, {"state": 2}) == {1: "charging"}


# ── feature: MQTT input routes through the pipeline ──────────────────────────

def _mqtt_client(regs):
    from multibus import mqtt_input as mi
    return mi.MqttInputClient({"topic": "s/x"}, regs)


def test_mqtt_monotonic_and_sentinel():
    regs = [_reg(address=1, name="energy", label="energy", json_path="energy",
                 topic="s/x", monotonic=True),
            _reg(address=2, name="temp", label="temp", json_path="temp",
                 topic="s/x", nan=[-127])]
    cli = _mqtt_client(regs)
    got = {}
    cli.publish_callback = lambda pg, data: got.update(data)

    cli._on_message(None, None, SimpleNamespace(
        topic="s/x", payload=b'{"energy": 1000.0, "temp": 21.0}'))
    assert got[1]["value"] == 1000.0 and got[2]["value"] == 21.0

    got.clear()
    # counter dip + sensor-error sentinel in one message → neither published
    cli._on_message(None, None, SimpleNamespace(
        topic="s/x", payload=b'{"energy": 2.0, "temp": -127}'))
    assert got == {}

    cli._on_message(None, None, SimpleNamespace(
        topic="s/x", payload=b'{"energy": 1001.0, "temp": 22.0}'))
    assert got[1]["value"] == 1001.0 and got[2]["value"] == 22.0


def test_mqtt_enum_text_published():
    cli = _mqtt_client([_reg(address=1, name="st", label="st", json_path="st",
                             topic="s/x", enum={1: "on", 0: "off"})])
    got = {}
    cli.publish_callback = lambda pg, data: got.update(data)
    cli._on_message(None, None, SimpleNamespace(topic="s/x", payload=b'{"st": 1}'))
    assert got[1]["value"] == "on"


def test_mqtt_scale_offset_still_applied():
    # regression: the refactor must preserve the old scale/offset behavior
    cli = _mqtt_client([_reg(address=1, name="v", label="v", json_path="v",
                             topic="s/x", scale=10.0, offset=1.0)])
    got = {}
    cli.publish_callback = lambda pg, data: got.update(data)
    cli._on_message(None, None, SimpleNamespace(topic="s/x", payload=b'{"v": 2305}'))
    assert got[1]["value"] == 231.5


# ── feature: 'Query now' shows the corrected value too ───────────────────────

def _query_app(regs):
    import pytest
    try:
        from fastapi.testclient import TestClient
    except Exception:
        pytest.skip("TestClient not available")
    from multibus.api import create_api
    from multibus.config import Config
    fake = SimpleNamespace(
        registers=regs,
        read_register=lambda addr, dt, rt: {1: 65535, 2: 2305, 3: 4}.get(addr),
        read_registers_batch=lambda rs: {r["address"]: {1: 65535, 2: 2305, 3: 4}.get(r["address"]) for r in rs},
    )
    # temp-anchored Config: create_api derives runtime paths (audit/events/…)
    # from config_path.parent — a bare Config() would write into ./config
    import os as _o
    import tempfile as _tf
    app, _ = create_api(Config(_o.path.join(_tf.mkdtemp(prefix="mbg-ac-"), "config.yaml")),
                        fake, None, None)
    return TestClient(app, raise_server_exceptions=False)


def test_query_register_corrected_field_additive():
    regs = [_reg(address=1, name="t", label="t", nan=[65535]),
            _reg(address=2, name="v", label="v", scale=10.0, offset=1.0),
            _reg(address=3, name="st", label="st", enum={4: "MPPT"})]
    c = _query_app(regs)
    # raw 'value' contract unchanged; 'corrected' added for selected registers
    r = c.post("/api/query/register", json={"address": 2, "data_type": "uint16"}).json()
    assert r["value"] == 2305 and r["corrected"] == 231.5
    r = c.post("/api/query/register", json={"address": 1, "data_type": "uint16"}).json()
    assert r["value"] == 65535 and r["corrected"] is None      # declared sentinel
    r = c.post("/api/query/register", json={"address": 3, "data_type": "uint16"}).json()
    assert r["corrected"] == "MPPT"
    # an address that is NOT a selected register gets no corrected key
    fake_extra = c.post("/api/query/register",
                        json={"address": 2, "data_type": "uint16",
                              "register_type": "input"}).json()
    assert "corrected" not in fake_extra


def test_query_batch_corrected_map():
    regs = [_reg(address=2, name="v", label="v", scale=10.0)]
    c = _query_app(regs)
    r = c.post("/api/query/batch", json={"registers": [
        {"address": 2, "data_type": "uint16"},
        {"address": 3, "data_type": "uint16"},
    ]}).json()
    assert r["values"]["2"] == 2305 and r["values"]["3"] == 4
    assert r["corrected"] == {"2": 230.5}      # only the selected register
