# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Virtual BMS executor increment 1 (ADR-0037): build the CAN frame set from canonical battery
values through a profile, behind an arm gate. The frames are checked against the ones the Deye
accepted on the 2026-11-09 bench."""
import json
import pathlib

from multibus.codec.can_signal import decode_signal, parse_can_id
from multibus.outputs.vbms import VirtualBms, VbmsRuntime, encode_value

_TPL = pathlib.Path(__file__).resolve().parent.parent / "multibus" / "device_templates" / "pylontech_can_inverter.json"
ROWS = json.loads(_TPL.read_text())["device_template"]["registers"]

# canonical battery state — what a live aggregation would hand the executor
VALUES = {
    "charge_voltage_limit": 57.6, "charge_current_limit": 80,
    "discharge_current_limit": 180, "discharge_voltage_limit": 46.4,
    "soc": 86, "soh": 100,
    "pack_voltage": 52.50, "battery_current": 4.5, "battery_temperature": 23.1,
    "manufacturer": "SHEnergy", "module_count": 3,
    "charge_enable": 1, "discharge_enable": 1,
}


class FakeTx:
    def __init__(self):
        self.sent, self.opened, self.closed = [], False, False
    def open(self):
        self.opened = True
    def send(self, can_id, data):
        self.sent.append((can_id, data))
    def close(self):
        self.closed = True


class FakeStop:
    """threading.Event-like: fires after ``n`` waits."""
    def __init__(self, n):
        self.n, self.waits = n, 0
    def is_set(self):
        return self.waits >= self.n
    def wait(self, _t):
        self.waits += 1


def _rows(cid_name):
    cid = int(cid_name, 16)
    return [r for r in ROWS if parse_can_id(r.get("can_id")) == cid]


def test_build_351_matches_the_bench_frame():
    # CVL 57.6 / CCL 80 / DCL 180 / DVL 46.4 is exactly the limit frame the Deye honoured
    vb = VirtualBms(ROWS)
    frames = vb.build_frames(VALUES)
    assert frames[0x351].hex().upper() == "400220030807D001"


def test_built_frames_round_trip_to_the_values():
    vb = VirtualBms(ROWS)
    frames = vb.build_frames(VALUES)
    # the scalar fields decode back (through scale) to what we put in
    def val(cid, name):
        r = next(x for x in _rows(cid) if x["name"] == name)
        raw = decode_signal(frames[int(cid, 16)], r)
        sc = r.get("scale")
        return raw / sc if sc else raw
    assert val("0x351", "charge_voltage_limit") == 57.6
    assert val("0x351", "charge_current_limit") == 80
    assert val("0x351", "discharge_current_limit") == 180
    assert val("0x355", "soc") == 86 and val("0x355", "soh") == 100
    assert abs(val("0x356", "pack_voltage") - 52.50) < 1e-6
    assert val("0x356", "battery_temperature") == 23.1
    assert val("0x359", "module_count") == 3


def test_manufacturer_string_encodes():
    vb = VirtualBms(ROWS)
    frames = vb.build_frames(VALUES)
    man = next(r for r in _rows("0x35E") if r["name"] == "manufacturer")
    assert decode_signal(frames[0x35E], man) == "SHEnergy"
    assert frames[0x35E] == bytes.fromhex("5348456E65726779")


def test_charge_enable_bit():
    vb = VirtualBms(ROWS)
    on = vb.build_frames(VALUES)
    off = vb.build_frames({**VALUES, "charge_enable": 0, "discharge_enable": 0})
    ce = next(r for r in _rows("0x35C") if r["name"] == "charge_enable")
    assert decode_signal(on[0x35C], ce) == 1 and decode_signal(off[0x35C], ce) == 0


def test_encode_value_scale_and_offset():
    row = {"start_bit": 0, "bit_length": 16, "byte_order": "little", "data_type": "uint16", "scale": 10}
    buf = bytearray(8)
    encode_value(buf, row, 57.6)
    assert int.from_bytes(buf[0:2], "little") == 576


# ── the arm gate (ADR-0037 D5) ────────────────────────────────────────────────
def test_disarmed_sends_nothing():
    vb = VirtualBms(ROWS)
    tx = FakeTx()
    assert vb.armed is False
    n = vb.transmit_once(tx, VALUES)
    assert n == 0 and tx.sent == []


def test_armed_transmits_the_set():
    vb = VirtualBms(ROWS)
    vb.arm()
    tx = FakeTx()
    n = vb.transmit_once(tx, VALUES)
    assert n == len(vb.can_ids()) and len(tx.sent) == n
    sent = dict(tx.sent)
    assert sent[0x351].hex().upper() == "400220030807D001"
    vb.disarm()
    assert vb.transmit_once(tx, VALUES) == 0          # back to silent


def test_rx_keepalive_is_never_transmitted_raw_or_live_loaded():
    """0x305 is the inverter→BMS keepalive (direction: rx) — the Virtual BMS must RECEIVE it, never
    transmit it. Regression (full-suite finding): direction was dropped when a template loaded as
    TemplateRegister objects (the live path), so the keepalive would have been sent."""
    from multibus.catalog.device_template import parse_template

    vb_raw = VirtualBms(ROWS); vb_raw.arm()
    assert 0x305 not in vb_raw.can_ids()                     # raw-dict rows (what the tests load)

    tpl = parse_template(json.loads(_TPL.read_text()))       # the LIVE path: TemplateRegister objects
    vb_live = VirtualBms(list(tpl.registers)); vb_live.arm()
    assert 0x305 not in vb_live.can_ids()                    # still dropped
    ka = next(r for r in tpl.registers if parse_can_id(r.can_id) == 0x305)
    assert ka.direction == "rx"                              # direction survived parse


# ── optional detail frames (ADR-0045 L2) ──────────────────────────────────────
def test_send_optional_false_drops_the_detail_frames():
    """With send_optional off, the mandatory frames (limits/SOC/VIT/enables) are kept but the
    optional detail frames (alarms 0x359, manufacturer 0x35E, cell extremes 0x370/0x371) are not
    built or sent — a BMS must never drop the fields the inverter needs to run."""
    full = set(VirtualBms(ROWS).can_ids())
    core = set(VirtualBms(ROWS, send_optional=False).can_ids())
    assert {0x351, 0x355, 0x356, 0x35C} <= core                 # mandatory kept
    assert not ({0x359, 0x35E, 0x370, 0x371} & core)            # optional dropped
    assert {0x359, 0x35E, 0x370, 0x371} <= full                 # full profile keeps them
    vb = VirtualBms(ROWS, send_optional=False); vb.arm()
    tx = FakeTx(); vb.transmit_once(tx, VALUES)
    assert set(dict(tx.sent)) == core                           # only the core set goes on the wire


# ── the runtime loop (ADR-0037 inc.3) ─────────────────────────────────────────
def test_runtime_survives_a_transmitter_that_will_not_open():
    """A listen-only or absent CAN interface makes SocketCanTx.open() raise. The runtime must log
    and exit (armed-but-not-running), not escape the thread as an unhandled traceback."""
    class RefusingTx:
        def __init__(self): self.opened = self.closed = False
        def open(self): raise OSError("can0 is listen-only")
        def send(self, *a): raise AssertionError("must not transmit when open failed")
        def close(self): self.closed = True
    vb = VirtualBms(ROWS); vb.arm()
    tx = RefusingTx()
    rt = VbmsRuntime(vb, tx, lambda: VALUES, period_s=0)
    assert rt.run(FakeStop(3)) == 0        # no frame-set sent, no exception
    assert tx.closed is False              # nothing was opened, so nothing to close


def test_runtime_ticks_the_frame_set_while_armed():
    vb = VirtualBms(ROWS); vb.arm()
    tx = FakeTx()
    rt = VbmsRuntime(vb, tx, lambda: VALUES, period_s=0)
    sets = rt.run(FakeStop(3))
    assert sets == 3 and tx.opened and tx.closed
    assert len(tx.sent) == 3 * len(vb.can_ids())
    assert dict(tx.sent)[0x351].hex().upper() == "400220030807D001"


def test_runtime_disarmed_opens_and_closes_but_sends_nothing():
    vb = VirtualBms(ROWS)                          # not armed
    tx = FakeTx()
    sets = VbmsRuntime(vb, tx, lambda: VALUES, period_s=0).run(FakeStop(2))
    assert sets == 0 and tx.sent == [] and tx.opened and tx.closed


def test_runtime_closes_tx_even_if_a_tick_raises():
    """A send that raises costs its cycle, not the executor (R2D-VBMS-1: it used to propagate
    and kill the thread while the instance stayed "armed"). Each cycle is retried; the
    transmitter is still closed when the loop ends."""
    vb = VirtualBms(ROWS); vb.arm()
    class Boom(FakeTx):
        def send(self, *a):
            raise RuntimeError("bus-off")
    tx = Boom()
    rt = VbmsRuntime(vb, tx, lambda: VALUES, period_s=0)
    assert rt.run(FakeStop(5)) == 0                # nothing got out …
    assert rt.failures >= 1 and "bus-off" in rt.refused   # … and it says why
    assert tx.closed                               # safe-default: the inverter sees silence


def test_arm_gate_is_the_only_transmit_path():
    # the executor module may transmit (it is the seam), but only via transmit_once's gate:
    # no module-level send, and VirtualBms holds no socket.
    import multibus.outputs.vbms as m
    assert not hasattr(VirtualBms, "_socket") and not hasattr(VirtualBms, "send")
    src = pathlib.Path(m.__file__).read_text()
    # no actual transmit here — the real CanTx (socket) is injected, not imported/used
    for forbidden in ("import socket", "socket.socket", "AF_CAN", "cansend", ".bind("):
        assert forbidden not in src, f"vbms.py must not transmit directly — found {forbidden!r}"
