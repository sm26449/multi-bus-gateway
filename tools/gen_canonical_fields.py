#!/usr/bin/env python3
"""Regenerate docs/canonical-fields.md from multibus/canonical_fields.py.

Run after editing the canonical dictionary:  python3 tools/gen_canonical_fields.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from multibus.canonical_fields import CANONICAL_FIELDS  # noqa: E402

HEADER = (
    "# Canonical field names\n\n"
    "> **Generated** from `multibus/canonical_fields.py` (the single source of "
    "truth). Do not edit by hand — re-run `tools/gen_canonical_fields.py`.\n\n"
    "The canonical name a register carries becomes its **MQTT topic leaf**, its "
    "**InfluxDB field**, and its `name` tag — so the same physical quantity is "
    "named the same on every device. Convention: `<quantity>_<position>` "
    "(l1/l2/l3, l1_n, l1_l2, ln_avg, ll_avg, total, n). Templates should name "
    "registers from this list; non-canonical names are flagged as a warning at "
    "template validation. Vendor reference maps (e.g. Janitza) predate this and "
    "may differ.\n\n"
)


def main() -> None:
    out = [HEADER]
    by: dict = {}
    order: list = []
    for name, (meas, unit, topic, desc) in CANONICAL_FIELDS.items():
        if meas not in by:
            by[meas] = []
            order.append(meas)
        by[meas].append((name, unit, topic, desc))
    for meas in order:
        out.append(f"## {meas}\n\n| InfluxDB field | MQTT topic | Unit | Description |\n"
                   "|---|---|---|---|\n")
        for name, unit, topic, desc in by[meas]:
            out.append(f"| `{name}` | `{topic}` | {unit or '—'} | {desc} |\n")
        out.append("\n")
    out.append(f"_Total: {len(CANONICAL_FIELDS)} canonical fields across "
               f"{len(order)} measurements._\n")
    path = os.path.join(ROOT, "docs", "canonical-fields.md")
    with open(path, "w") as f:
        f.write("".join(out))
    print(f"wrote {path} — {len(CANONICAL_FIELDS)} fields")


if __name__ == "__main__":
    main()
