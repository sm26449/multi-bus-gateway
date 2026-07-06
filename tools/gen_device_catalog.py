#!/usr/bin/env python3
"""Generate docs/device-catalog.md from the bundled device templates.

Single source of truth = janitza/device_templates/*.json. This tool renders a
human-readable reference (register maps + provenance) so we always have the
maps and their sources on hand. Re-run after adding or editing any template:

    python tools/gen_device_catalog.py

The register tables are derived from the JSON; the narrative/provenance comes
from each template's `description` + `source_document`. Huge built-in maps
(e.g. the Janitza primary) are summarised, not dumped row-by-row.
"""
from __future__ import annotations

import json
import os
from glob import glob

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TPL_DIR = os.path.join(ROOT, "janitza", "device_templates")
OUT = os.path.join(ROOT, "docs", "device-catalog.md")

# Above this register count we summarise instead of dumping every row.
DUMP_LIMIT = 120
# Render the primary Janitza built-in first, then the rest alphabetically.
PRIMARY_ID = "janitza_umg512_pro"

FC_BY_TYPE = {"holding": "FC03 (read holding registers)",
              "input": "FC04 (read input registers)"}
ORDER_LABEL = {"big": "big-endian, high word first (ABCD)",
               "little": "little-endian, low word first (CDAB / word-swapped)",
               "badc": "byte-swapped (BADC)",
               "dcba": "full little-endian (DCBA)"}


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)["device_template"]


def _fc_summary(regs):
    kinds = sorted({r.get("register_type", "holding") for r in regs})
    return ", ".join(FC_BY_TYPE.get(k, k) for k in kinds) or FC_BY_TYPE["holding"]


def _reg_table(regs):
    rows = ["| Address (dec / hex) | Name | Description | Type | Scale | Unit | Poll |",
            "|---|---|---|---|---|---|---|"]
    for r in sorted(regs, key=lambda x: (x.get("register_type", "holding"), x["address"])):
        addr = r["address"]
        rows.append("| {dec} / 0x{hx:04X} | `{name}` | {label} | {dt} | {sc} | {unit} | {pg} |".format(
            dec=addr, hx=addr, name=r.get("name", ""), label=r.get("label", ""),
            dt=r.get("data_type", ""), sc=r.get("scale", 1),
            unit=(r.get("unit") or "—"), pg=r.get("poll_group", "")))
    return "\n".join(rows)


def _section(tpl):
    regs = tpl.get("registers", [])
    proto = tpl.get("protocol", {}) or {}
    bo = proto.get("byte_order", "big")
    lines = []
    lines.append(f"## {tpl.get('name', tpl['id'])}")
    lines.append("")
    meta = [f"**id** `{tpl['id']}`", f"**vendor** {tpl.get('vendor', '—')}",
            f"**model** {tpl.get('model', '—')}", f"**version** {tpl.get('version', '—')}",
            f"**registers** {len(regs)}"]
    lines.append(" · ".join(meta))
    lines.append("")
    lines.append(f"- **Transport:** {_fc_summary(regs)} · byte order **{ORDER_LABEL.get(bo, bo)}**")
    if tpl.get("source_document"):
        lines.append(f"- **Source / provenance:** {tpl['source_document']}")
    if tpl.get("description"):
        lines.append("")
        lines.append(f"> {tpl['description']}")
    lines.append("")
    if len(regs) > DUMP_LIMIT:
        cats = {}
        for r in regs:
            cats[r.get("category", "?")] = cats.get(r.get("category", "?"), 0) + 1
        top = ", ".join(f"{k} ({v})" for k, v in sorted(cats.items(), key=lambda kv: -kv[1])[:10])
        lines.append(f"_Large built-in map ({len(regs)} registers) — not dumped here._ "
                     f"Categories: {top}.")
    else:
        lines.append(_reg_table(regs))
    lines.append("")
    return "\n".join(lines)


def main():
    paths = sorted(glob(os.path.join(TPL_DIR, "*.json")))
    tpls = {}
    for p in paths:
        try:
            t = _load(p)
            tpls[t["id"]] = t
        except Exception as e:  # pragma: no cover - defensive
            print(f"skip {p}: {e}")
    ordered = ([tpls[PRIMARY_ID]] if PRIMARY_ID in tpls else []) + \
              [tpls[k] for k in sorted(tpls) if k != PRIMARY_ID]

    out = []
    out.append("# Device catalog — bundled register maps & provenance")
    out.append("")
    out.append("> **Generated** by `tools/gen_device_catalog.py` from "
               "`janitza/device_templates/*.json`. Do not edit by hand — "
               "re-run the generator after changing a template.")
    out.append("")
    out.append("Every built-in device map, with its Modbus transport (function code + "
               "byte/word order), the exact register table, and the **source it was "
               "verified against**. We do not fabricate maps — each entry cites its "
               "provenance. **Confidence varies by entry:** some are *vendor-verified* "
               "(confirmed against the manufacturer manual or a field-tested driver — "
               "e.g. ABB B23 vs the ABB manual, Schneider iEM3000 vs volkszaehler/mbmd, "
               "Carlo Gavazzi EM24 vs Victron), while others are *community-sourced* and "
               "their description says to verify against your specific unit's manual "
               "before billing-grade use (e.g. the Eastron SDM entries). Read each "
               "entry's Source line. `scale` is a divisor — engineering value = raw / scale.")
    out.append("")
    out.append(f"**{len(ordered)} device maps.**")
    out.append("")
    out.append("| Map | Vendor | Model | Registers | Transport |")
    out.append("|---|---|---|---|---|")
    for t in ordered:
        regs = t.get("registers", [])
        bo = (t.get("protocol", {}) or {}).get("byte_order", "big")
        out.append(f"| [{t.get('name', t['id'])}](#{_anchor(t.get('name', t['id']))}) "
                   f"| {t.get('vendor', '—')} | {t.get('model', '—')} | {len(regs)} "
                   f"| {_fc_summary(regs).split(' ')[0]} / {bo} |")
    out.append("")
    out.append("---")
    out.append("")
    for t in ordered:
        out.append(_section(t))
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(out).rstrip() + "\n")
    print(f"wrote {OUT} ({len(ordered)} devices)")


def _anchor(title):
    return "".join(c for c in title.lower().replace(" ", "-") if c.isalnum() or c == "-")


if __name__ == "__main__":
    main()
