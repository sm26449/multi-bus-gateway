# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Virtual BMS user-facing management — the shared port registry, source eligibility and
save-time validation (ADR-0045).

Pure functions over injected inputs (CAN interfaces, the configured publishers, a device lookup,
a profile lookup), so the CRUD routes and the UI pickers agree and the whole thing is unit-tested
without a gateway. The model (ADR-0045 §3): a CAN bus has exactly **one BMS publisher**; read-only
consumers (monitors, taps, the inverters) **share** it. Availability is computed over BOTH the
Devices and the Virtual BMS sections.
"""
from __future__ import annotations

import math
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


# ── the id namespace a Virtual BMS shares with the devices (R2D-NAMESPACE-1) ─────────────
# A Virtual BMS publishes under the device topic pattern, so its id and a device's id are one
# MQTT identity. "Device" means everything that is one: a device, each of its components and
# each unit of an installation. These are the ONE check every path runs, in both directions:
# device / component / unit create and restore, Virtual BMS create, and bundle import/restore.

def vbms_ids(virtual_bms: Any) -> Set[str]:
    """The ids of the configured Virtual BMS instances (a malformed entry has none)."""
    return {str(v.get("id")) for v in (virtual_bms if isinstance(virtual_bms, list) else [])
            if isinstance(v, dict) and v.get("id") is not None}


def taken_by_vbms(ids: Iterable[Any], virtual_bms: Any) -> List[str]:
    """The ids among ``ids`` that a Virtual BMS of ``virtual_bms`` already has, sorted."""
    return sorted({str(i) for i in ids if i is not None} & vbms_ids(virtual_bms))


def vbms_id_error(ids: Iterable[Any], virtual_bms: Any, what: str = "id") -> List[str]:
    """The refusal for a device-side id (device, component, unit) that is a Virtual BMS's."""
    return [f"{what}: '{i}' is a Virtual BMS — a device needs its own id"
            for i in taken_by_vbms(ids, virtual_bms)]


def device_namespace_ids(doc: Dict[str, Any]) -> Set[str]:
    """Every device id a raw config document declares: its devices, their components and the
    units of its installations — with the ids the config loader gives an entry that names none."""
    from multibus.core.config import Config
    from multibus.core.config.models import PRIMARY_DEVICE_ID

    def _list(x: Any) -> list:                    # a malformed section declares nothing here
        return x if isinstance(x, list) else []
    # the legacy primary's id is reserved whether or not this config has one (review L1)
    out: Set[str] = {PRIMARY_DEVICE_ID}
    for d in _list(doc.get("devices")):
        if not isinstance(d, dict) or d.get("id") is None:
            continue
        pid = str(d["id"]).strip()
        out.add(pid)
        for c in _list(d.get("components")):
            if isinstance(c, dict) and c.get("type"):
                inst = "" if c.get("instance") is None else str(c.get("instance")).strip()
                out.add(str(c.get("id") or Config.component_id(pid, str(c["type"]).strip(), inst)).strip())
    for ep in _list(doc.get("endpoints")):
        if not isinstance(ep, dict):
            continue
        holders = [g for g in _list(ep.get("groups")) if isinstance(g, dict)] or [ep]
        for h in holders:
            if isinstance(h.get("units"), list):
                out.update(u["id"] for u in Config._endpoint_units(ep, h))
    return out


def config_device_ids(config: Any) -> Set[str]:
    """Every device id of a LOADED config (devices, components, units, the primary) plus the
    reserved primary id — what a Virtual BMS id is judged against at runtime."""
    from multibus.core.config.models import PRIMARY_DEVICE_ID
    return {d.id for d in (getattr(config, "devices", None) or [])} | {PRIMARY_DEVICE_ID}


def namespace_errors(doc: Dict[str, Any]) -> List[str]:
    """A whole config document: each Virtual BMS whose id is also a device's."""
    return [f"virtual_bms {i}: the id is already a device (a device, a component or an "
            f"installation unit) — a Virtual BMS needs its own"
            for i in taken_by_vbms(device_namespace_ids(doc), doc.get("virtual_bms"))]


def port_registry(interfaces: List[Dict[str, Any]], publishers: Dict[str, List[str]], *,
                  exclude_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """The CAN ports for the picker (ADR-0045 §3/§3a).

    ``interfaces`` is ``can_interfaces.interfaces()`` output (``{name, up, listen_only, bitrate,
    state, devices}``). ``publishers`` maps a channel → the Virtual BMS ids that publish on it.
    ``exclude_id`` is the instance being edited, so its own claim does not read as "reserved".

    Each port gets an ``availability``: ``reserved`` (another Virtual BMS publishes there, not
    selectable), ``available_shared`` (free to publish, a read-only consumer is already on the
    bus) or ``available`` (nothing on it)."""
    out: List[Dict[str, Any]] = []
    for itf in interfaces:
        name = itf.get("name")
        pubs = [p for p in publishers.get(name, []) if p != exclude_id]
        readers = list(itf.get("devices") or [])
        if pubs:
            availability, publisher = "reserved", pubs[0]
        elif readers:
            availability, publisher = "available_shared", None
        else:
            availability, publisher = "available", None
        out.append({
            "name": name, "up": itf.get("up"), "listen_only": itf.get("listen_only"),
            "bitrate": itf.get("bitrate"), "state": itf.get("state"),
            "readers": readers, "publisher": publisher, "availability": availability,
        })
    return out


def available_channels(registry: List[Dict[str, Any]]) -> List[str]:
    """Just the channel names a new/edited instance may publish on."""
    return [p["name"] for p in registry if p["availability"] != "reserved"]


def eligible_sources(devices: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The battery devices that may be aggregated as packs (ADR-0045 §5). A pack is a battery
    device that is **not** itself a bank aggregate (aggregating an aggregate would hide N and the
    weakest pack — design §3.1). ``devices`` are dicts ``{id, name, kind, is_aggregate}``."""
    out = []
    for d in devices:
        if str(d.get("kind") or "").lower() != "battery":
            continue
        if d.get("is_aggregate"):
            continue
        out.append({"id": d.get("id"), "name": d.get("name") or d.get("id")})
    return out


_ANALOG = ("charge_current_limit", "discharge_current_limit",
           "charge_voltage_limit", "discharge_voltage_limit")
_ENABLES = ("charge_enable", "discharge_enable")
_CONTROL = _ANALOG + _ENABLES
PERIOD_S = (0.05, 60.0)            # a frame set every 50 ms at most, every minute at least
HOLD_S = (0.0, 60.0)
COMMAND_MAX_AGE_S = (1.0, 3600.0)
# the return policy (R2D-VBMS-3, design §4.1/§4.9). The floors keep a return slow and a
# ramp finite: there is no "return at once" and no "jump to the limit" setting.
RETURN_AFTER_S = (5.0, 3600.0)
FLAP_DROPS = (1, 20)
FLAP_WINDOW_S = (60.0, 86400.0)
SLEW_A_PER_S = (0.1, 100.0)
PACK_LIMIT_A = (0.0, 10000.0)
SHARE_FACTOR = (0.5, 1.0)


def _num(x: Any) -> bool:
    """A real, finite number (never a bool, never NaN/inf)."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _control_errors(inst: Dict[str, Any]) -> List[str]:
    """``bounds`` and ``floor`` (WK7-SAFE-3, WK7-CFG-1): every bound is a finite, ordered,
    non-negative ``[lo, hi]`` pair on a real control field; every floor is a finite value
    inside its field's bounds. A scalar bound, a NaN, or a floor above its ceiling is refused
    here — not stored and then discovered when the instance is built."""
    errors: List[str] = []
    bounds, floor = inst.get("bounds"), inst.get("floor")
    good: Dict[str, Any] = {}
    if bounds is not None and not isinstance(bounds, dict):
        errors.append("bounds must be a mapping of field → [lo, hi]")
        bounds = {}
    for field, pair in (bounds or {}).items():
        if field not in _CONTROL:
            errors.append(f"bounds.{field}: not a control field ({', '.join(_CONTROL)})")
        elif not (isinstance(pair, (list, tuple)) and len(pair) == 2 and all(_num(x) for x in pair)):
            errors.append(f"bounds.{field} must be [lo, hi] — two finite numbers")
        elif pair[0] > pair[1]:
            errors.append(f"bounds.{field}: lo {pair[0]} is above hi {pair[1]}")
        elif pair[0] < 0:
            errors.append(f"bounds.{field}: lo must be 0 or more")
        elif field in _ENABLES and (pair[0] not in (0, 1) or pair[1] not in (0, 1)):
            errors.append(f"bounds.{field}: an enable is 0 or 1")
        else:
            good[field] = pair
    if floor is not None and not isinstance(floor, dict):
        errors.append("floor must be a mapping of field → value")
        floor = {}
    for field, val in (floor or {}).items():
        if field not in _CONTROL:
            errors.append(f"floor.{field}: not a control field ({', '.join(_CONTROL)})")
        elif not _num(val):
            errors.append(f"floor.{field} must be a finite number")
        elif val < 0:
            errors.append(f"floor.{field} must be 0 or more")
        elif field in _ENABLES and val not in (0, 1):
            errors.append(f"floor.{field}: an enable is 0 or 1")
        elif field in good and not good[field][0] <= val <= good[field][1]:
            errors.append(f"floor.{field} = {val} is outside its bounds "
                          f"[{good[field][0]}, {good[field][1]}]")
    return errors


def _range_error(inst: Dict[str, Any], key: str, lo: float, hi: float, unit: str = "s") -> List[str]:
    v = inst.get(key)
    if v is None:
        return []
    if not _num(v) or not lo <= v <= hi:
        return [f"{key} must be a number between {lo:g} and {hi:g} {unit}".rstrip()]
    return []


def _recovery_errors(inst: Dict[str, Any]) -> List[str]:
    """``recovery`` (how a pack returns, the flapping hold, the ramp) and ``pack_limits`` (the
    per-pack current capability) — R2D-VBMS-3. Both optional; absent keys take the design's
    defaults (``return_after_s`` 60, 3 drops in 600 s, 2 A/s; no per-pack limit = the bank
    limit scaled by admitted / configured packs)."""
    errors: List[str] = []
    rec = inst.get("recovery")
    if rec is not None:
        if not isinstance(rec, dict):
            errors.append("recovery must be a mapping")
        else:
            unknown = sorted(set(rec) - {"return_after_s", "flap_drops", "flap_window_s",
                                         "slew_a_per_s"})
            errors += [f"recovery.{k} must not be empty" for k in sorted(rec) if rec[k] is None]
            errors += [f"recovery.{k}: not a recovery option" for k in unknown]
            errors += [f"recovery.{e}" for e in _range_error(rec, "return_after_s", *RETURN_AFTER_S)]
            errors += [f"recovery.{e}" for e in _range_error(rec, "flap_window_s", *FLAP_WINDOW_S)]
            errors += [f"recovery.{e}" for e in _range_error(rec, "slew_a_per_s", *SLEW_A_PER_S, "A/s")]
            fd = rec.get("flap_drops")
            if fd is not None and (isinstance(fd, bool) or not isinstance(fd, int)
                                   or not FLAP_DROPS[0] <= fd <= FLAP_DROPS[1]):
                errors.append(f"recovery.flap_drops must be a whole number between "
                              f"{FLAP_DROPS[0]} and {FLAP_DROPS[1]}")
    pl = inst.get("pack_limits")
    if pl is not None:
        if not isinstance(pl, dict):
            errors.append("pack_limits must be a mapping")
        else:
            unknown = sorted(set(pl) - {"charge_current_limit", "discharge_current_limit",
                                        "share_factor"})
            errors += [f"pack_limits.{k} must not be empty" for k in sorted(pl) if pl[k] is None]
            errors += [f"pack_limits.{k}: not a per-pack limit" for k in unknown]
            for key in ("charge_current_limit", "discharge_current_limit"):
                errors += [f"pack_limits.{e}" for e in _range_error(pl, key, *PACK_LIMIT_A, "A")]
            errors += [f"pack_limits.{e}" for e in _range_error(pl, "share_factor", *SHARE_FACTOR, "")]
    return errors


def _runtime_errors(inst: Dict[str, Any], n_packs: int) -> List[str]:
    """The cadence and failure-policy options (WK7-CFG-1): a period of 0 is a transmit loop
    with no pause, a negative one raises in the executor thread."""
    errors = _range_error(inst, "period_s", *PERIOD_S)
    errors += _range_error(inst, "command_max_age_s", *COMMAND_MAX_AGE_S)
    mp = inst.get("min_packs")
    if mp is not None and (isinstance(mp, bool) or not isinstance(mp, int)
                           or not 1 <= mp <= max(1, n_packs)):
        errors.append(f"min_packs must be a whole number between 1 and {max(1, n_packs)}")
    fb = inst.get("fallback")
    if fb is not None:
        if not isinstance(fb, dict):
            errors.append("fallback must be a mapping")
        else:
            errors += [f"fallback.{e}" for e in _range_error(fb, "hold_s", *HOLD_S)]
    fm = inst.get("field_map")
    if fm is not None and not (isinstance(fm, dict) and fm and all(
            isinstance(k, str) and isinstance(v, str) and k and v for k, v in fm.items())):
        errors.append("field_map must be a non-empty mapping of source field → bank field")
    fields = inst.get("fields")
    if fields is not None and not (isinstance(fields, dict) and all(
            isinstance(v, bool) for v in fields.values())):
        errors.append("fields must be a mapping of option → true/false")
    errors += _recovery_errors(inst)
    for key in ("name", "manufacturer", "inverter"):
        if inst.get(key) is not None and not isinstance(inst.get(key), str):
            errors.append(f"{key} must be text")
    return errors


def structural_errors(inst: Any) -> List[str]:
    """What is wrong with one stored instance BY ITSELF — its shape, its envelope, its cadence
    and its packs list — without asking the host or the other devices. The CRUD routes run it
    (inside ``validate_instance``), bundle validation runs it on every imported instance, and
    the constructor refuses to build what fails it: the schema is one, whichever way a config
    arrives (R2D-VBMS-2: an import used to bring in ``period_s: 0`` and a 999 V floor)."""
    if not isinstance(inst, dict):
        return ["a Virtual BMS instance must be a mapping"]
    errors: List[str] = []
    iid = inst.get("id")
    if not isinstance(iid, str) or not _SLUG.match(iid):
        errors.append("the id must be a slug: lowercase letters, digits and dashes")
    if not isinstance(inst.get("profile"), str) or not inst.get("profile"):
        errors.append("a protocol profile is required")
    packs = inst.get("packs") or []
    if not isinstance(packs, list) or not all(isinstance(p, str) for p in packs):
        errors.append("packs must be a list of device ids")
        packs = []
    dup = sorted({p for p in packs if packs.count(p) > 1})
    if dup:
        errors.append("a source pack is listed more than once: " + ", ".join(dup))
    if isinstance(iid, str) and iid in packs:
        errors.append("a Virtual BMS cannot read back its own output as a pack")
    if inst.get("channel") is not None and not isinstance(inst.get("channel"), str):
        errors.append("channel must be text")
    errors += _control_errors(inst)
    errors += _runtime_errors(inst, len(packs))
    return errors


def _hex_ids(ids) -> str:
    """CAN frame IDs as ``0x351, 0x355`` for an operator-facing message."""
    return ", ".join(f"0x{i:X}" for i in sorted(ids))


def validate_instance(inst: Dict[str, Any], *, existing_ids: List[str],
                      registry: List[Dict[str, Any]], sources: List[Dict[str, Any]],
                      profile_is_emulator: Callable[[str], bool],
                      profile_facts: Optional[Dict[str, Any]] = None,
                      reader_frames: Optional[Dict[str, Any]] = None,
                      editing: Optional[str] = None,
                      device_ids: Optional[List[str]] = None) -> List[str]:
    """Return a list of human-readable errors ([] = ok) for a proposed Virtual BMS instance
    (ADR-0045 §6). ``editing`` is the id when this is an edit (its own id/port do not clash).

    ``profile_facts`` (``{bitrate, tx_frame_ids}``, from the emulation template) and
    ``reader_frames`` (device id → the CAN frame IDs it decodes) drive the physical-safety checks
    (ADR-0045 §3/§6): the port's bitrate matches the profile, no pack reads the bus we transmit on
    (a circular source), and no other device on the bus already reads a BMS on our frame IDs (two
    BMS masters would collide). Each is skipped when its inputs are absent (nothing to check)."""
    errors: List[str] = []
    iid = str(inst.get("id") or "").strip()
    if not iid:
        errors.append("an id is required")
    elif not _SLUG.match(iid):
        errors.append("the id must be a slug: lowercase letters, digits and dashes")
    elif iid in [e for e in existing_ids if e != editing]:
        errors.append(f"a Virtual BMS with id {iid!r} already exists")

    profile = str(inst.get("profile") or "").strip()
    if not profile:
        errors.append("a protocol profile is required")
    elif not profile_is_emulator(profile):
        errors.append(f"profile {profile!r} is not a Virtual BMS (emulation) profile")

    channel = str(inst.get("channel") or "").strip()
    by_name = {p["name"]: p for p in registry}
    if not channel:
        errors.append("a CAN port is required")
    elif channel not in by_name:
        errors.append(f"{channel!r} is not a CAN interface on this host")
    elif by_name[channel]["availability"] == "reserved" and by_name[channel]["publisher"] != editing:
        errors.append(f"{channel} is already used by Virtual BMS "
                      f"{by_name[channel]['publisher']!r}")

    packs = inst.get("packs") or []
    src_by_id = {s["id"]: s for s in sources}
    src_ids = set(src_by_id)
    if not isinstance(packs, list) or not all(isinstance(p, str) for p in packs):
        errors.append("packs must be a list of device ids")
        packs = []
    elif not packs:
        errors.append("at least one source pack is required")
    for p in packs:
        if p not in src_ids:
            errors.append(f"{p!r} is not an eligible battery pack")
    dup = sorted({p for p in packs if packs.count(p) > 1})
    if dup:                                        # a pack listed twice is counted twice
        errors.append("a source pack is listed more than once: " + ", ".join(dup))
    if iid and iid in packs:
        errors.append("a Virtual BMS cannot read back its own output as a pack")
    if iid and taken_by_vbms(device_ids or (), [{"id": iid}]):     # the id as judged above
        # it publishes under the device topic pattern: the same id is the same MQTT identity
        # (``device_ids``: every device, component and installation unit — R2D-NAMESPACE-1)
        errors.append(f"the id {iid!r} is already a device — a Virtual BMS needs its own")

    # physical-safety checks on the chosen bus (ADR-0045 §3/§6) — each skipped without its inputs
    facts = profile_facts or {}
    port = by_name.get(channel)
    prof_bitrate = facts.get("bitrate")
    if prof_bitrate and port and port.get("bitrate") and port["bitrate"] != prof_bitrate:
        errors.append(f"{channel} runs at {port['bitrate']} bit/s but the profile needs "
                      f"{prof_bitrate} bit/s — set the port's bitrate to match")

    for p in packs:                                            # circular: a pack on our output bus
        p_channel = src_by_id.get(p, {}).get("channel")
        if p_channel and p_channel == channel:
            errors.append(f"pack {p!r} reads CAN {channel}, the bus this Virtual BMS transmits on "
                          f"— a circular source")

    tx_ids = set(facts.get("tx_frame_ids") or ())             # external-BMS collision on the bus
    if tx_ids and port:
        rframes = reader_frames or {}
        for reader in port.get("readers", []):
            if reader in packs:
                continue                                       # a pack is flagged circular above
            clash = tx_ids & set(rframes.get(reader) or ())
            if clash:
                errors.append(f"{channel} is already read by {reader!r} on the emulation's frame "
                              f"IDs ({_hex_ids(clash)}) — a second BMS master would collide")

    errors += _control_errors(inst)
    errors += _runtime_errors(inst, len(packs) if isinstance(packs, list) else 0)
    return errors
