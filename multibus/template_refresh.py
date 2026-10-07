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
           template-shipped calculated fields matched by name
kept       which rows are selected, their dashboard/ui flags, MQTT/InfluxDB
           switches and topics, thresholds, and every calculated EXPRESSION
           (a formula is logic; the operator may have tuned it)

The file is rewritten whole and atomically, so its other keys (poll groups,
calculated list, version) survive.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

REGISTER_KEYS = ("label", "unit", "description", "category")
CALC_KEYS = ("label", "unit")


def _refresh_rows(rows: List[Dict[str, Any]], tpl_rows: Dict[str, Any],
                  keys: Tuple[str, ...]) -> int:
    changed = 0
    for row in rows:
        t = tpl_rows.get(row.get("name"))
        if t is None:
            continue
        before = json.dumps(row, sort_keys=True, default=str)
        for k in keys:
            v = getattr(t, k, None)
            if v not in (None, ""):
                row[k] = v
        agg = getattr(t, "aggregates", None)
        if agg:
            row["aggregates"] = dict(agg)
        else:
            row.pop("aggregates", None)
        if json.dumps(row, sort_keys=True, default=str) != before:
            changed += 1
    return changed


def refresh_file(path: Path, tpl, lock=None) -> Dict[str, int]:
    """Refresh one selected-registers file in place. Returns how many register
    and calculated rows changed; a missing file changes nothing."""
    if not path.exists():
        return {"registers": 0, "calculated": 0}
    data = json.loads(path.read_text(encoding="utf-8"))
    regs = data.get("registers") or []
    calcs = data.get("calculated") or []
    n_reg = _refresh_rows(regs, {r.name: r for r in (tpl.registers or [])}, REGISTER_KEYS)
    n_calc = _refresh_rows(calcs, {c.name: c for c in (tpl.calculated or [])}, CALC_KEYS)
    if n_reg or n_calc:
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
    return {"registers": n_reg, "calculated": n_calc}


def refresh_device(config, template_registry, dev_cfg) -> Dict[str, Any]:
    """Refresh every register file of one device: its own, and each source's
    against that source's template."""
    out = {"device": dev_cfg.id, "registers": 0, "calculated": 0, "files": 0}
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
    for path, tid in jobs:
        tpl = template_registry.get(tid) if tid else None
        if tpl is None:
            continue
        res = refresh_file(path, tpl, getattr(config, "_file_lock", None))
        out["registers"] += res["registers"]
        out["calculated"] += res["calculated"]
        out["files"] += 1
    return out
