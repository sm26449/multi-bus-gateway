# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""MonotonicFilter: a cumulative energy counter only grows, so a downward glitch
is dropped (held) — but a genuine, sustained reset is eventually accepted."""
from multibus.counter_filter import MonotonicFilter


def test_normal_growth_passes_through():
    f = MonotonicFilter()
    assert f.feed(100.0) == 100.0     # first read seeds the baseline
    assert f.feed(100.0) == 100.0     # flat is fine
    assert f.feed(101.5) == 101.5     # grows
    assert f.feed(101.5) == 101.5


def test_single_downward_glitch_is_dropped_and_held():
    f = MonotonicFilter()
    f.feed(1000.0)
    assert f.feed(3.0) is None        # implausible drop → glitch → held
    assert f.feed(1000.5) == 1000.5   # real value returns → accepted, no phantom reset
    assert f.feed(1001.0) == 1001.0   # baseline never slipped below 1000


def test_glitch_does_not_lower_baseline():
    f = MonotonicFilter()
    f.feed(500.0)
    assert f.feed(0.0) is None        # a 0 read (classic bad frame) is dropped
    assert f.feed(499.0) is None      # still below the held 500 baseline → dropped
    assert f.feed(501.0) == 501.0     # only a value at/above baseline resumes


def test_sustained_reset_is_eventually_accepted():
    f = MonotonicFilter(reset_confirm=3)
    f.feed(9000.0)
    assert f.feed(2.0) is None        # 1st low read — unconfirmed
    assert f.feed(3.0) is None        # 2nd low read — still unconfirmed
    assert f.feed(4.0) == 4.0         # 3rd consecutive low → real reset, adopted
    assert f.feed(5.0) == 5.0         # grows from the new baseline
    assert f.feed(4.5) is None        # a dip below the new baseline is a glitch again


def test_recovery_between_glitches_resets_the_reset_counter():
    f = MonotonicFilter(reset_confirm=3)
    f.feed(9000.0)
    assert f.feed(2.0) is None        # low
    assert f.feed(9001.0) == 9001.0   # recovered → not a reset; counter cleared
    assert f.feed(2.0) is None        # low again, but only the 1st in a fresh run
    assert f.feed(3.0) is None        # 2nd
    assert f.feed(9002.0) == 9002.0   # recovered before confirm → never reset


def test_reset_confirm_one_accepts_immediately():
    f = MonotonicFilter(reset_confirm=1)
    f.feed(100.0)
    assert f.feed(5.0) == 5.0         # trust every downward step


def test_noise_tolerance_absorbs_float_jitter():
    f = MonotonicFilter(noise=0.5)
    f.feed(230.0)
    assert f.feed(229.7) == 229.7     # within noise → accepted, not a regression
    assert f.feed(230.2) == 230.2
    assert f.feed(228.0) is None      # beyond noise → treated as a real drop


def test_non_numeric_passes_through_untouched():
    f = MonotonicFilter()
    assert f.feed("online") == "online"
    assert f.feed(None) is None
    # bool must not be treated as an int counter
    assert f.feed(True) is True


# ── the flag actually takes effect on the poll path ──────────────────────────

def _u32(v):
    """Split a uint32 into two big-endian 16-bit registers."""
    return [(v >> 16) & 0xFFFF, v & 0xFFFF]


def test_poller_holds_a_downward_counter_glitch():
    from unittest.mock import MagicMock
    from multibus.config import SelectedRegister
    from multibus.modbus_client import RegisterPoller
    from multibus.register_parser import RegisterParser

    reg = SelectedRegister(address=100, name="energy_active_import", label="Import",
                           unit="Wh", data_type="uint32", poll_group="slow",
                           monotonic=True)
    conn = MagicMock()
    poller = RegisterPoller("slow", 60, [reg], conn, RegisterParser(),
                            publish_callback=lambda *a: None)

    conn.read_registers.return_value = _u32(1_000_000)     # baseline
    assert poller._poll_registers()[100]["value"] == 1_000_000

    conn.read_registers.return_value = _u32(7)             # glitch → dropped
    assert 100 not in poller._poll_registers()             # register absent this cycle

    conn.read_registers.return_value = _u32(1_000_060)     # real value resumes
    assert poller._poll_registers()[100]["value"] == 1_000_060


def test_poller_untouched_when_flag_absent():
    from unittest.mock import MagicMock
    from multibus.config import SelectedRegister
    from multibus.modbus_client import RegisterPoller
    from multibus.register_parser import RegisterParser

    reg = SelectedRegister(address=100, name="energy_active_import", label="Import",
                           unit="Wh", data_type="uint32", poll_group="slow")  # no flag
    conn = MagicMock()
    poller = RegisterPoller("slow", 60, [reg], conn, RegisterParser(),
                            publish_callback=lambda *a: None)
    conn.read_registers.return_value = _u32(1_000_000)
    assert poller._poll_registers()[100]["value"] == 1_000_000
    conn.read_registers.return_value = _u32(7)             # no filter → passes straight through
    assert poller._poll_registers()[100]["value"] == 7


def test_monotonic_round_trips_through_template():
    from multibus.device_template import parse_template, validate_template
    tpl = {"device_template": {
        "id": "x_mono", "name": "X", "protocol": {},
        "registers": [
            {"address": 0, "name": "energy_active_import", "unit": "Wh",
             "data_type": "uint32", "monotonic": True},
        ]}}
    assert validate_template(tpl) == []
    t = parse_template(tpl)
    assert t.registers[0].monotonic is True
    assert t.registers[0].to_dict()["monotonic"] is True


def test_monotonic_absent_by_default_stays_lean():
    from multibus.device_template import parse_template
    t = parse_template({"device_template": {
        "id": "x_plain", "name": "X", "protocol": {},
        "registers": [{"address": 0, "name": "voltage_l1_n", "unit": "V",
                       "data_type": "uint16"}]}})
    assert t.registers[0].monotonic is False
    assert "monotonic" not in t.registers[0].to_dict()   # not serialized when off


def test_the_daily_counter_holds_the_days_maximum_and_adopts_midnight():
    """Solar API Site.E_Day sums the inverters still awake: it drops at
    sunset. The filter serves the day's maximum through the evening and
    adopts the reset at midnight."""
    from multibus.counter_filter import DailyCounterFilter
    f = DailyCounterFilter()
    assert f.feed(1000) == 1000 and f.feed(272810) == 272810        # a growing day
    assert f.feed(199020) is None and f.feed(137670) is None and f.feed(125150) is None   # sunset: held
    assert not f.just_reset
    assert f.feed(272900) == 272900                                # a late uptick is real
    assert f.feed(0) == 0 and f.just_reset                          # midnight: adopt
    assert f.feed(517) == 517 and f.feed(20000) == 20000            # the new day grows
    assert f.feed(1999) is None and f.feed(4000) is None             # dips (10 %, 20 %) are sleeping units, not a reset
    assert f.feed(300) == 300 and f.just_reset                      # below 2 % of the held 20000: a reset
    assert f.feed('n/a') == 'n/a' and f.feed(True) is True          # non-numerics pass through

