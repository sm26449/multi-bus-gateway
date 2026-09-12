# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Declaration order decides who owns a field.

Two sources reaching one unit both carry `power_active_total`. Left alone they
would publish two values to one topic, alternating, with nothing to say which is
which. The rule: the FIRST source that offers a field owns it, and a lower-ranked
one fills in only after the owner has gone quiet past the owner's window.

"Freshest wins" was rejected deliberately — two sources that disagree by a hair
would hand the field back and forth on every poll.
"""
import time

from multibus.source_arbiter import FieldArbiter, field_of


class _Reg:
    def __init__(self, name): self.name = name


def _item(name, mono=None):
    return {'register': _Reg(name), 'value': 1, 'mono': mono}


# ── ownership ────────────────────────────────────────────────────────────────

def test_the_first_source_to_offer_a_field_owns_it():
    a = FieldArbiter()
    assert a.claim('power_active_total', 0, 'sunspec', 0.0) is True
    assert a.claim('power_active_total', 1, 'solar_api', 15.0) is False
    assert a.owner_of('power_active_total') == 'sunspec'


def test_a_field_nobody_claimed_goes_to_whoever_speaks_first():
    """A partial source is not second-class: it owns everything the complete one
    does not offer at all."""
    a = FieldArbiter()
    assert a.claim('frequency', 1, 'solar_api', 15.0) is True
    assert a.owner_of('frequency') == 'solar_api'


def test_the_owner_keeps_writing_without_fighting_itself():
    a = FieldArbiter()
    for _ in range(5):
        assert a.claim('w', 0, 'sunspec', 0.0) is True
    assert a.handovers == 0


# ── failover ─────────────────────────────────────────────────────────────────

def test_a_lower_source_takes_over_once_the_owner_goes_quiet():
    """The point of the window: when a cached HTTP view freezes or a Modbus
    side dies, the other way of reaching the unit fills in by itself."""
    a = FieldArbiter()
    t = time.monotonic()
    assert a.claim('w', 0, 'sunspec', 2.0, mono=t) is True
    assert a.claim('w', 1, 'solar_api', 15.0, mono=t + 1.0) is False   # still fresh
    assert a.claim('w', 1, 'solar_api', 15.0, mono=t + 2.5) is True    # gone quiet
    assert a.owner_of('w') == 'solar_api'
    assert a.handovers == 1


def test_the_owner_reclaims_the_instant_it_speaks_again():
    a = FieldArbiter()
    t = time.monotonic()
    a.claim('w', 0, 'sunspec', 2.0, mono=t)
    a.claim('w', 1, 'solar_api', 15.0, mono=t + 3)
    assert a.claim('w', 0, 'sunspec', 2.0, mono=t + 4) is True
    assert a.owner_of('w') == 'sunspec'


def test_a_zero_window_never_hands_over():
    """The correct setting for an energy counter: two sources disagreeing by a
    few Wh would make it walk backwards every time ownership changed hands."""
    a = FieldArbiter()
    t = time.monotonic()
    assert a.claim('energy_active_generated', 0, 'sunspec', 0.0, mono=t) is True
    for dt in (10, 100, 10000):
        assert a.claim('energy_active_generated', 1, 'solar_api', 15.0,
                       mono=t + dt) is False


def test_a_departing_source_releases_its_claims_at_once():
    """Otherwise a survivor waits out a window that will never elapse, because
    the owner that would have refreshed it is gone."""
    a = FieldArbiter()
    a.claim('w', 0, 'sunspec', 0.0)
    a.forget('sunspec')
    assert a.owner_of('w') is None
    assert a.claim('w', 1, 'solar_api', 15.0) is True


# ── provenance ───────────────────────────────────────────────────────────────

def test_every_field_can_say_where_it_came_from():
    """A two-source device is undebuggable otherwise: "why does it say 1404 W"
    has to have an answer."""
    a = FieldArbiter()
    a.claim('power_active_total', 0, 'sunspec', 0.0)
    a.claim('frequency', 1, 'solar_api', 15.0)
    snap = a.snapshot()
    assert snap['power_active_total']['source'] == 'sunspec'
    assert snap['power_active_total']['rank'] == 0
    assert snap['frequency']['source'] == 'solar_api'
    assert snap['frequency']['stale_after_s'] == 15.0
    assert snap['frequency']['age_s'] >= 0


def test_ownership_is_decided_by_name_never_by_address():
    """Sources address the same measurement differently — Modbus reads
    power_active_total at 40083, the Solar API from a JSON path at address 1. By
    address they would never collide, and both would write the field unopposed."""
    assert field_of(_item('power_active_total')) == 'power_active_total'
    assert field_of({'value': 1}) == ''


# ── the value gate ───────────────────────────────────────────────────────────

def test_a_losing_source_is_silent_not_merely_overwritten():
    """Suppression happens before the store, MQTT and InfluxDB, so a losing
    source never leaves a shadow value for a later reader to trip over."""
    from multibus.multi_source import MultiSourceClient

    class _Src:
        def __init__(self, sid, stale): self.id, self.stale_after_s = sid, stale
        protocol, template, enabled = 'tcp', 't', True

    class _Drv:
        publish_callback = None
        def get_stats(self): return {}

    hi, lo = _Drv(), _Drv()
    c = MultiSourceClient('u1', [(_Src('sunspec', 0.0), hi),
                                 (_Src('solar_api', 15.0), lo)])
    seen = []
    c.publish_callback = lambda g, d: seen.append((g, {a: i['register'].name
                                                       for a, i in d.items()}))
    hi.publish_callback('normal', {40083: _item('power_active_total')})
    lo.publish_callback('realtime', {1: _item('power_active_total'),
                                     2: _item('frequency')})
    assert seen[0] == ('normal', {40083: 'power_active_total'})
    # the contested field was dropped; the uncontested one got through
    assert seen[1] == ('realtime', {2: 'frequency'})


def test_a_value_carries_the_source_that_produced_it():
    from multibus.multi_source import MultiSourceClient

    class _Src:
        def __init__(self, sid): self.id, self.stale_after_s = sid, 0.0
        protocol, template, enabled = 'tcp', 't', True

    class _Drv:
        publish_callback = None
        def get_stats(self): return {}

    drv = _Drv()
    c = MultiSourceClient('u1', [(_Src('sunspec'), drv)])
    got = {}
    c.publish_callback = lambda g, d: got.update(d)
    drv.publish_callback('normal', {40083: _item('power_active_total')})
    assert got[40083]['source'] == 'sunspec'
