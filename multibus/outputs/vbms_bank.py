# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Virtual BMS — live bank aggregation (ADR-0037 inc.2, design §4).

Combine the canonical fields of the real packs MBG reads (the Seplos packs on the bench) into
one bank state — the ``values`` the Virtual BMS reports to the inverter, so it is **live**, not
static. Pure: packs in → values out. The rules are conservative (design §4): the inverter must
never be told the bank can take/give more than the weakest-link pack allows.

Parallel packs on one bank:
* **voltage** — mean (they share a rail);
* **current** — **sum** (they split it);
* **SOC** — capacity-weighted mean; **SOH** — min (conservative);
* **temperatures** — max of maxes, min of mins;
* **cell voltages** — max of maxes, min of mins (the real extremes drive protection);
* **capacity / modules** — sum;
* **alarm/protection bits** — OR (any pack's alarm is the bank's).

Eligibility/freshness (design §4.4) is only roughed in here (a pack with no SOC/voltage is
dropped); per-field freshness, min-packs and the recovery-CCL path arrive with later increments.
The *control* fields (CVL/CCL/DCL/DVL/enables) are NOT here — they are the EMS/MBG control plane
(ADR-0037 D4); this module produces only the MEASURED fields.
"""
from __future__ import annotations

import statistics
from typing import Any, Dict, List

# pack canonical field  →  how it aggregates  →  which profile value it feeds
_MEAN = "mean"
_SUM = "sum"
_MAX = "max"
_MIN = "min"


# aggregation key (what a pack contributes) → the profile field(s) it is SENT as. The preview
# needs this to put a pack's column next to the right sent value (WK7-PREVIEW-1).
SENT_AS = {
    "soc": "soc", "soh": "soh", "voltage": "pack_voltage", "current": "battery_current",
    "cell_v_max": "max_cell_voltage", "cell_v_min": "min_cell_voltage",
    "temp_max": "max_cell_temp", "temp_min": "min_cell_temp",
    "capacity_total": "total_capacity",
    "protection_flags": "protection_flags", "alarm_flags": "alarm_flags",
}


def _vals(packs: List[Dict[str, Any]], field: str) -> List[float]:
    return [p[field] for p in packs if p.get(field) is not None]


def eligible(packs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Packs that can be reported: a dict carrying at least a SOC and a voltage. (Full
    eligibility — per-field freshness, min-packs, implausibility — is design §4.4, later.)"""
    return [p for p in (packs or []) if p and p.get("soc") is not None and p.get("voltage") is not None]


def aggregate_bank(packs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate the eligible packs into the Virtual BMS **measured** values, keyed by the
    Pylontech profile's canonical names. Empty dict when no pack is eligible (caller stops /
    safe-defaults). Rounding matches each field's wire resolution."""
    live = eligible(packs)
    if not live:
        return {}
    out: Dict[str, Any] = {}

    v = _vals(live, "voltage")
    if v:
        out["pack_voltage"] = round(statistics.mean(v), 2)
    i = _vals(live, "current")
    if i:
        out["battery_current"] = round(sum(i), 1)

    # capacity-weighted SOC (fall back to a plain mean when capacities are absent/equal)
    socw = [(p["soc"], p.get("capacity_total") or 1.0) for p in live if p.get("soc") is not None]
    if socw:
        out["soc"] = round(sum(s * c for s, c in socw) / sum(c for _s, c in socw))
    soh = _vals(live, "soh")
    if soh:
        out["soh"] = round(min(soh))                       # conservative

    tmax = _vals(live, "temp_max")
    if tmax:
        out["max_cell_temp"] = round(max(tmax), 1)
        out["battery_temperature"] = round(max(tmax), 1)   # the profile's single temp = the hottest
    tmin = _vals(live, "temp_min")
    if tmin:
        out["min_cell_temp"] = round(min(tmin), 1)

    cmax = _vals(live, "cell_v_max")
    if cmax:
        out["max_cell_voltage"] = round(max(cmax), 3)
    cmin = _vals(live, "cell_v_min")
    if cmin:
        out["min_cell_voltage"] = round(min(cmin), 3)

    cap = _vals(live, "capacity_total")
    if cap:
        out["total_capacity"] = round(sum(cap))
    out["module_count"] = sum(int(p.get("modules") or 1) for p in live)

    # worst-case alarm/protection bits: OR across packs
    for field in ("protection_flags", "alarm_flags"):
        bits = _vals(live, field)
        if bits:
            acc = 0
            for b in bits:
                acc |= int(b)
            out[field] = acc

    out["packs_live"] = len(live)
    return out
