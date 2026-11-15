# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The Virtual BMS port registry, source eligibility and save validation (ADR-0045): one BMS
publisher per bus, readers shared; battery packs only; slug/port/pack/limit checks."""
from multibus.outputs.vbms_registry import (available_channels, eligible_sources, port_registry,
                                            validate_instance)

_ITFS = [
    {"name": "can0", "up": True, "listen_only": True, "bitrate": 500000, "state": "ERROR-ACTIVE",
     "devices": ["bms-can"]},                               # a read-only monitor on it
    {"name": "can1", "up": True, "listen_only": False, "bitrate": 500000, "state": "ERROR-ACTIVE",
     "devices": []},                                        # nothing on it
    {"name": "can2", "up": True, "listen_only": False, "bitrate": 250000, "state": "ERROR-ACTIVE",
     "devices": []},
]
_PUBS = {"can2": ["other-vbms"]}                            # another Virtual BMS already on can2


def test_port_registry_classifies_availability():
    reg = {p["name"]: p for p in port_registry(_ITFS, _PUBS)}
    assert reg["can0"]["availability"] == "available_shared" and reg["can0"]["readers"] == ["bms-can"]
    assert reg["can0"]["listen_only"] is True                # surfaced for the warning
    assert reg["can1"]["availability"] == "available" and reg["can1"]["publisher"] is None
    assert reg["can2"]["availability"] == "reserved" and reg["can2"]["publisher"] == "other-vbms"


def test_port_registry_excludes_the_edited_instances_own_claim():
    reg = {p["name"]: p for p in port_registry(_ITFS, {"can1": ["vb"]}, exclude_id="vb")}
    assert reg["can1"]["availability"] == "available"        # its own claim does not read as reserved


def test_available_channels_drops_reserved():
    assert set(available_channels(port_registry(_ITFS, _PUBS))) == {"can0", "can1"}


def test_eligible_sources_battery_only_no_aggregate():
    devs = [
        {"id": "seplos-adr1", "name": "Pack 1", "kind": "battery", "is_aggregate": False},
        {"id": "deye", "name": "Deye", "kind": "inverter", "is_aggregate": False},
        {"id": "bank", "name": "Bank", "kind": "battery", "is_aggregate": True},   # an aggregate
    ]
    got = eligible_sources(devs)
    assert [s["id"] for s in got] == ["seplos-adr1"]


_REG = port_registry(_ITFS, _PUBS)
_SRC = [{"id": "seplos-adr1", "name": "Pack 1"}, {"id": "seplos-adr2", "name": "Pack 2"}]
_EMU = lambda p: p == "pylontech_can_inverter"


def _valid(**over):
    base = {"id": "deye-vbms", "profile": "pylontech_can_inverter", "channel": "can1",
            "packs": ["seplos-adr1", "seplos-adr2"], "bounds": {"charge_current_limit": [0, 80]},
            "floor": {"charge_current_limit": 0}}
    base.update(over)
    return base


def _errs(inst, **kw):
    return validate_instance(inst, existing_ids=kw.get("existing", []), registry=_REG, sources=_SRC,
                             profile_is_emulator=_EMU, editing=kw.get("editing"))


def test_validate_happy_path():
    assert _errs(_valid()) == []


def test_validate_catches_the_core_errors():
    assert any("slug" in e for e in _errs(_valid(id="Deye VBMS")))
    assert any("already exists" in e for e in _errs(_valid(id="dup"), existing=["dup"]))
    assert any("emulation" in e for e in _errs(_valid(profile="seplos_bms_v2_tap")))
    assert any("not a CAN interface" in e for e in _errs(_valid(channel="can9")))
    assert any("already used by" in e for e in _errs(_valid(channel="can2")))      # reserved
    assert any("source pack" in e for e in _errs(_valid(packs=[])))
    assert any("eligible battery pack" in e for e in _errs(_valid(packs=["nope"])))
    assert any("number" in e for e in _errs(_valid(bounds={"charge_current_limit": "lots"})))


def test_validate_edit_allows_own_id_and_port():
    # editing "deye-vbms" which already holds can2 → its own reserved port is allowed
    reg = port_registry(_ITFS, {"can2": ["deye-vbms"]})
    errs = validate_instance(_valid(channel="can2"), existing_ids=["deye-vbms"], registry=reg,
                             sources=_SRC, profile_is_emulator=_EMU, editing="deye-vbms")
    assert errs == []


def test_validate_refuses_circular_self_source():
    errs = _errs(_valid(packs=["seplos-adr1", "deye-vbms"]), editing="deye-vbms")
    assert any("own output" in e for e in errs)


# ── hardening (ADR-0045 §3/§6): bitrate, circular pack-on-bus, external-BMS collision ──
_FACTS = {"bitrate": 500000, "tx_frame_ids": {0x351, 0x355, 0x356, 0x359, 0x35C, 0x35E}}


def _errsf(inst, *, sources=None, reader_frames=None, facts=_FACTS, editing=None, reg=_REG):
    return validate_instance(inst, existing_ids=[], registry=reg,
                             sources=_SRC if sources is None else sources,
                             profile_is_emulator=_EMU, profile_facts=facts,
                             reader_frames=reader_frames or {}, editing=editing)


def test_validate_bitrate_match_is_ok():
    assert _errsf(_valid(channel="can1")) == []            # can1 @ 500k matches the profile


def test_validate_refuses_bitrate_mismatch():
    itfs = [{"name": "can1", "up": True, "listen_only": False, "bitrate": 250000,
             "state": "ERROR-ACTIVE", "devices": []}]
    errs = _errsf(_valid(channel="can1"), reg=port_registry(itfs, {}))
    assert any("250000" in e and "500000" in e for e in errs)


def test_validate_unknown_port_bitrate_is_not_a_mismatch():
    itfs = [{"name": "can1", "up": None, "listen_only": None, "bitrate": None,
             "state": None, "devices": []}]                 # a down port reports no bitrate
    assert _errsf(_valid(channel="can1"), reg=port_registry(itfs, {})) == []


def test_validate_refuses_circular_pack_on_the_output_bus():
    src = [{"id": "seplos-adr1", "name": "Pack 1", "channel": "can1"},
           {"id": "seplos-adr2", "name": "Pack 2", "channel": None}]
    errs = _errsf(_valid(channel="can1"), sources=src)
    assert any("circular" in e and "seplos-adr1" in e for e in errs)


def test_validate_pack_on_another_can_bus_is_fine():
    src = [{"id": "seplos-adr1", "name": "Pack 1", "channel": "can3"},
           {"id": "seplos-adr2", "name": "Pack 2", "channel": "can3"}]
    assert _errsf(_valid(channel="can1"), sources=src) == []


def test_validate_refuses_external_bms_collision():
    errs = _errsf(_valid(channel="can0"), reader_frames={"bms-can": {0x355, 0x359}})
    assert any("collide" in e and "bms-can" in e for e in errs)


def test_validate_reader_on_unrelated_frames_is_not_a_collision():
    assert _errsf(_valid(channel="can0"), reader_frames={"bms-can": {0x100, 0x200}}) == []


def test_validate_collision_skips_a_reader_that_is_a_pack():
    # a pack that reads our bus is reported once, as a circular source, not twice
    src = [{"id": "bms-can", "name": "Pack 1", "channel": "can0"},
           {"id": "seplos-adr2", "name": "Pack 2", "channel": None}]
    errs = _errsf(_valid(channel="can0", packs=["bms-can", "seplos-adr2"]),
                  sources=src, reader_frames={"bms-can": {0x355}})
    assert any("circular" in e for e in errs)
    assert not any("collide" in e for e in errs)
