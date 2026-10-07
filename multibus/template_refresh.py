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
"""Update a seeded unit from its template — metadata only.

A unit's register selection is SEEDED from its template once, then belongs to
the operator. Until now a template improvement (a clearer label, the right
unit, a new way the bank totals combine) never reached a unit that already
existed. This refreshes what the template DESCRIBES on the rows the unit
already has, and nothing the operator decided:

refreshed  label, unit, description, category, aggregates — on registers and
           template-shipped calculated fields matched by name (the unit only
           while the row keeps the template's scale and offset)
kept       which rows are selected, their dashboard/ui flags, MQTT/InfluxDB
           switches and topics, thresholds, and every calculated EXPRESSION
           (a formula is logic; the operator may have tuned it)

The file is rewritten whole and atomically, so its other keys (poll groups,
calculated list, version) survive.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

REGISTER_KEYS = ("label", "unit", "description", "category")
SILENT_FILL = ("description", "category")
CALC_KEYS = ("label", "unit")


def _rescaled(row: Dict[str, Any], t) -> bool:
    """The operator changed how this row is scaled (W read as kW with scale
    0.001): its unit is theirs too, and the template's would lie."""
    for k, default in (("scale", 1), ("offset", 0)):
        mine, theirs = row.get(k), getattr(t, k, None)
        if mine is None or theirs is None:
            continue
        try:
            if float(mine) != float(theirs if theirs is not None else default):
                return True
        except (TypeError, ValueError):
            return True
    return False


def _apply_row(row: Dict[str, Any], t, keys: Tuple[str, ...]) -> None:
    """Bring one row's description in line with its template row (in place)."""
    for k in keys:
        if k == "unit" and _rescaled(row, t):
            continue        # the operator's scale goes with the operator's unit
        v = getattr(t, k, None)
        if v not in (None, ""):
            row[k] = v
    agg = getattr(t, "aggregates", None)
    if agg:
        row["aggregates"] = dict(agg)
    else:
        row.pop("aggregates", None)


def _refresh_rows(rows: List[Dict[str, Any]], tpl_rows: Dict[str, Any],
                  keys: Tuple[str, ...]) -> int:
    changed = 0
    for row in rows:
        t = tpl_rows.get(row.get("name"))
        if t is None:
            continue
        before = json.dumps(row, sort_keys=True, default=str)
        _apply_row(row, t, keys)
        if json.dumps(row, sort_keys=True, default=str) != before:
            changed += 1
    return changed


def _row_changes(rows: List[Dict[str, Any]], tpl_rows: Dict[str, Any],
                 keys: Tuple[str, ...]) -> List[Dict[str, Any]]:
    """What _refresh_rows WOULD change, key by key — computed on a copy with
    the very same rule, so the preview cannot disagree with the apply."""
    out = []
    for row in rows:
        t = tpl_rows.get(row.get("name"))
        if t is None:
            continue
        after = copy.deepcopy(row)
        _apply_row(after, t, keys)
        for k in keys + ("aggregates",):
            # a section or a description the row never had is read from the
            # template anyway: filling it in changes nothing anyone sees, so it
            # is done with the next update but is no reason to announce one
            if k in SILENT_FILL and row.get(k) in (None, ""):
                continue
            if row.get(k) != after.get(k):
                out.append({"field": row.get("name"), "label": after.get("label") or row.get("name"),
                            "key": k, "before": row.get(k), "after": after.get(k)})
    return out


# a preview is asked for on every look at an installation: keep it until a
# register file or the template changes
_PLAN_CACHE: Dict[Any, Dict[str, Any]] = {}


def plan_file(path: Path, tpl, with_new: bool = False) -> Dict[str, Any]:
    """What refreshing this file would do, without doing it: ``changes``
    (one per field and key, with before/after) and, with ``with_new``, the
    template's calculated fields the unit lacks (``new``)."""
    try:
        st = path.stat()
    except OSError:
        return {"changes": [], "new": []}
    key = (str(path), st.st_mtime_ns, st.st_size, id(tpl), getattr(tpl, "version", ""), with_new)
    hit = _PLAN_CACHE.get(key)
    if hit is not None:
        return hit
    data = json.loads(path.read_text(encoding="utf-8"))
    regs = data.get("registers") or []
    calcs = data.get("calculated") or []
    changes = (_row_changes(regs, {r.name: r for r in (tpl.registers or [])}, REGISTER_KEYS)
               + _row_changes(calcs, {c.name: c for c in (tpl.calculated or [])}, CALC_KEYS))
    new = []
    if with_new:
        have = {r.get("name") for r in regs} | {c.get("name") for c in calcs}
        new = [{"field": c.name, "label": c.label or c.name,
                "description": getattr(c, "description", "") or ""}
               for c in (tpl.calculated or []) if c.name not in have]
    res = {"changes": changes, "new": new}
    if len(_PLAN_CACHE) > 512:
        _PLAN_CACHE.clear()
    _PLAN_CACHE[key] = res
    return res


def refresh_file(path: Path, tpl, lock=None, add_new: bool = False) -> Dict[str, int]:
    """Refresh one selected-registers file in place. Returns how many register
    and calculated rows changed (and, with ``add_new``, how many of the
    template's calculated fields the unit did not have were added); a missing
    file changes nothing."""
    if not path.exists():
        return {"registers": 0, "calculated": 0, "added": 0}
    data = json.loads(path.read_text(encoding="utf-8"))
    regs = data.get("registers") or []
    calcs = data.get("calculated") or []
    n_reg = _refresh_rows(regs, {r.name: r for r in (tpl.registers or [])}, REGISTER_KEYS)
    n_calc = _refresh_rows(calcs, {c.name: c for c in (tpl.calculated or [])}, CALC_KEYS)
    n_add = 0
    if add_new:
        # a field the template gained since this unit was seeded (asked for:
        # a calculated field the operator deleted would come back too)
        have = {r.get("name") for r in regs} | {c.get("name") for c in calcs}
        for c in (tpl.calculated or []):
            if c.name not in have:
                calcs.append(c.to_dict())
                n_add += 1
        if n_add:
            data["calculated"] = calcs
    if n_reg or n_calc or n_add:
        tmp = path.with_suffix(path.suffix + ".tmp")

        def _write():
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        if lock is not None:
            with lock:
                _write()
        else:
            _write()
    return {"registers": n_reg, "calculated": n_calc, "added": n_add}


def _jobs(config, dev_cfg) -> List[Tuple[Path, str, bool]]:
    """Every register file of a device with the template it is read against,
    and whether it is the unit's own file (where derived measurements live)."""
    jobs = []
    dev_tid = getattr(dev_cfg, "template", "") or ""
    for src in getattr(dev_cfg, "sources", None) or []:
        p = config.source_registers_path(dev_cfg.id, src.id)
        if p.exists():
            jobs.append((p, getattr(src, "template", "") or dev_tid))
    root = config.device_registers_path(dev_cfg.id)
    if root.exists():
        jobs.append((root, dev_tid or next(
            (getattr(sx, "template", "") for sx in (getattr(dev_cfg, "sources", None) or [])
             if getattr(sx, "template", "")), "")))
    return [(p, t, p == root) for p, t in jobs]


def refresh_device(config, template_registry, dev_cfg, add_new: bool = False) -> Dict[str, Any]:
    """Refresh every register file of one device: its own, and each source's
    against that source's template. ``add_new`` also adds the template's
    calculated fields the unit lacks — to the unit's own file, where derived
    measurements live."""
    out = {"device": dev_cfg.id, "registers": 0, "calculated": 0, "added": 0, "files": 0}
    for path, tid, own in _jobs(config, dev_cfg):
        tpl = template_registry.get(tid) if tid else None
        if tpl is None:
            continue
        res = refresh_file(path, tpl, getattr(config, "_file_lock", None),
                           add_new=add_new and own)
        out["registers"] += res["registers"]
        out["calculated"] += res["calculated"]
        out["added"] += res["added"]
        out["files"] += 1
    return out


def _same(v) -> str:
    return json.dumps(v, sort_keys=True, default=str)


def plan_devices(config, template_registry, devs) -> Dict[str, Any]:
    """What "Update from template" would do to these units, merged: the same
    change on eight packs is one line that says ``units: 8``. ``pending`` is
    whether there is anything to apply at all — the button exists only then.
    ``templates`` names the template(s) and versions it would bring in."""
    devs = list(devs)
    changes: Dict[Any, Dict[str, Any]] = {}
    new: Dict[str, Dict[str, Any]] = {}
    templates: Dict[str, Dict[str, Any]] = {}
    affected = set()
    for dev in devs:
        for path, tid, own in _jobs(config, dev):
            tpl = template_registry.get(tid) if tid else None
            if tpl is None:
                continue
            templates[tid] = {"id": tid, "name": getattr(tpl, "name", "") or tid,
                              "version": getattr(tpl, "version", "") or ""}
            plan = plan_file(path, tpl, with_new=own)
            for c in plan["changes"]:
                k = (c["field"], c["key"], _same(c["before"]), _same(c["after"]))
                entry = changes.setdefault(k, {**c, "units": 0})
                entry["units"] += 1
                affected.add(dev.id)
            for n in plan["new"]:
                entry = new.setdefault(n["field"], {**n, "units": 0})
                entry["units"] += 1
                affected.add(dev.id)
    return {"pending": bool(changes or new), "units_total": len(devs),
            "units_affected": len(affected), "templates": list(templates.values()),
            "changes": sorted(changes.values(), key=lambda c: (c["key"], c["field"])),
            "new": list(new.values())}
