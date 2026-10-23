# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Setup writes (3.91.x): a template says what a device must be told before
part of its map exists; the gateway writes it at connect, again when the
device forgets (an illegal-address refusal where a step provides registers,
or a return from an outage), never where writes are not allowed."""
from multibus import device_setup
from multibus.device_setup import SetupRunner, step_values
from multibus.device_template import validate_template

STEPS = [{"name": "outdoor", "address": 6000, "values": ["0x8238", 33284], "provides": {"from": 4, "to": 5}},
         {"name": "indoor", "address": 7000, "values": ["0x42E9"], "provides": {"from": 82, "to": 82}}]


class FakeConn:
    def __init__(self, ok=True):
        self.writes, self.ok, self.events = [], ok, []

    def write(self, address, register_type="holding", values=None, **kw):
        self.writes.append((address, register_type, list(values)))
        return (True, None) if self.ok else (False, "exception 2")

    def record_event(self, *a):
        self.events.append(a)


def test_steps_go_out_once_as_one_frame_each(monkeypatch):
    monkeypatch.setattr(device_setup.time, "sleep", lambda s: None)
    c = FakeConn()
    r = SetupRunner(c, STEPS, "hp")
    assert step_values(STEPS[0]) == [0x8238, 33284]
    assert r.ensure() is True and r.ensure() is True
    assert c.writes == [(6000, "holding", [0x8238, 33284]), (7000, "holding", [0x42E9])]
    assert r.stats()["done"] and r.runs == 1 and c.events


def test_a_refusal_where_a_step_provides_rewrites_it_rate_limited(monkeypatch):
    monkeypatch.setattr(device_setup.time, "sleep", lambda s: None)
    now = [1000.0]
    monkeypatch.setattr(device_setup.time, "monotonic", lambda: now[0])
    c = FakeConn()
    r = SetupRunner(c, STEPS, "hp")
    r.ensure()
    assert r.covers(0, 6) and r.covers(82, 1) and not r.covers(50, 30)
    r.invalidate("refused")                     # too soon after a good run: ignored
    r.ensure()
    assert len(c.writes) == 2
    now[0] += device_setup.REARM_S + 1
    r.invalidate("refused")
    r.ensure()
    assert len(c.writes) == 4 and r.runs == 2


def test_nothing_is_written_where_writes_are_off_and_a_failure_retries_later(monkeypatch):
    monkeypatch.setattr(device_setup.time, "sleep", lambda s: None)
    now = [0.0]
    monkeypatch.setattr(device_setup.time, "monotonic", lambda: now[0])
    c = FakeConn()
    r = SetupRunner(c, STEPS, "hp", gate=lambda: "writes are off")
    assert r.ensure() is False and c.writes == [] and r.last_error == "writes are off"
    bad = FakeConn(ok=False)
    r2 = SetupRunner(bad, STEPS, "hp")
    assert r2.ensure() is False and "exception 2" in r2.last_error
    r2.ensure()                                 # within RETRY_S: no second try
    assert len(bad.writes) == 2
    now[0] += device_setup.RETRY_S + 1
    r2.ensure()
    assert len(bad.writes) == 4


def test_the_template_block_is_validated():
    def tpl(setup):
        return {"device_template": {"id": "s_t", "name": "S", "registers": [
            {"address": 0, "name": "a", "data_type": "uint16"}], "setup": setup}}
    assert validate_template(tpl(STEPS)) == []
    errs = " | ".join(validate_template(tpl([{"name": "x", "address": 70000, "values": ["0x1FFFF", "abc"],
                                              "provides": {"from": 9, "to": 3}}])))
    for frag in ("name invalid", "address must be", "values[0]", "values[1]", "provides must be"):
        assert frag in errs, (frag, errs)
