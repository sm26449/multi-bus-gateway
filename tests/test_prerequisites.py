# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Guided prerequisites (ADR-0046): a declared precondition is met or not, carries a UI 'fix', and
``require`` raises a 409 with the payload when any is unmet."""
import types

from multibus.core import prerequisites as prereq


def _cfg(writes=False, entities=False):
    return types.SimpleNamespace(
        security=types.SimpleNamespace(allow_writes=writes),
        mqtt=types.SimpleNamespace(allow_write_entities=entities))


def test_allow_writes_met_and_fix():
    off = prereq.allow_writes(_cfg(writes=False))
    assert off.met is False and off.id == "allow_writes"
    assert off.fix == {"method": "POST", "url": "/api/config/security", "body": {"allow_writes": True}}
    assert off.where == "Settings → Security"
    assert prereq.allow_writes(_cfg(writes=True)).met is True


def test_allow_write_entities_met():
    assert prereq.allow_write_entities(_cfg(entities=False)).met is False
    assert prereq.allow_write_entities(_cfg(entities=True)).met is True


def test_unmet_and_detail_build_the_409_payload():
    missing = prereq.unmet([prereq.allow_writes(_cfg(writes=False))])
    assert [p.id for p in missing] == ["allow_writes"]
    body = prereq.detail(missing)
    assert body["prerequisites"][0]["id"] == "allow_writes" and body["prerequisites"][0]["fix"]


def test_unmet_empty_when_all_met():
    assert prereq.unmet([prereq.allow_writes(_cfg(writes=True))]) == []        # the route then proceeds


def test_can_transmit_met_only_when_out_of_listen_only_and_up():
    off = prereq.can_transmit("can0", {"name": "can0", "listen_only": True, "up": True})
    assert off.met is False and off.id == "can_transmit"
    assert off.fix == {"method": "POST", "url": "/api/can-interfaces/can0",
                       "body": {"mode": "transmit", "up": True}}
    assert prereq.can_transmit("can0", {"listen_only": False, "up": True}).met is True
    assert prereq.can_transmit("can0", {"listen_only": False, "up": False}).met is False   # down
    assert prereq.can_transmit("can0", None).met is False                                  # not present
    # listen_only unknown (e.g. vcan) is not a block
    assert prereq.can_transmit("vcan0", {"listen_only": None, "up": True}).met is True


def test_can_bitrate_met_only_when_the_port_matches_the_profile():
    # both known and different → unmet, with a fix that sets the port's bitrate
    off = prereq.can_bitrate("can0", {"bitrate": 250000, "up": True}, 500000)
    assert off.met is False and off.id == "can_bitrate"
    assert off.fix == {"method": "POST", "url": "/api/can-interfaces/can0",
                       "body": {"bitrate": 500000, "up": True}}
    assert off.params == {"channel": "can0", "bitrate": 500000}
    assert prereq.can_bitrate("can0", {"bitrate": 500000}, 500000).met is True      # equal → met
    # nothing to check → met (never a false block): port reports no bitrate, or profile declares none
    assert prereq.can_bitrate("can0", {"bitrate": None}, 500000).met is True         # port down
    assert prereq.can_bitrate("can0", None, 500000).met is True                      # not present
    assert prereq.can_bitrate("can0", {"bitrate": 250000}, None).met is True         # profile silent


def test_detail_only_lists_what_it_is_given():
    d = prereq.detail(prereq.unmet([prereq.allow_writes(_cfg(writes=False)),
                                    prereq.allow_write_entities(_cfg(entities=True))]))
    assert [p["id"] for p in d["prerequisites"]] == ["allow_writes"]          # the met one dropped
