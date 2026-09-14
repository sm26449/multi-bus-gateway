# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Migration — derived measurements for units read through SOURCES.

A template's ``calculated`` entries (``status/text``, ``status/alarm``,
``status/active`` on the Fronius inverter) are copied to a device when it is
seeded. Units created under the sources model (P8) keep each source's
registers in ``devices/<id>/sources/<source>/selected_registers.json`` and
were never seeded at the device level — so ``config.load_calculated(id)``,
which reads ``devices/<id>/selected_registers.json``, finds nothing, and the
unit publishes no decoded status. Node-RED's inverter monitor and pv-stack-ui
read exactly those leaves.

This script gives every sources unit the calculated entries of the template
of its Modbus source, by name, in the device-level file (registers stay in the
source files — the loader reads a source file first and falls back to the
device file only when the source file is missing). Entries already present
by name are left alone; a hand-written formula is never touched.

Dry run by default; ``--apply`` writes, backing an existing file up first.

  docker run --rm --entrypoint python -v /docker-storage/pv-stack/multi-bus-gateway/config:/app/config \\
      multi-bus-gateway:latest scripts/migrate_sources_calculated.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from multibus.config import Config  # noqa: E402
from multibus.device_template import TemplateRegistry  # noqa: E402

MODBUS = ('tcp', 'rtu', 'rtu-tcp')


def plan(config: Config, registry: TemplateRegistry):
    """(device id, path, new file content or None, template id, missing names)."""
    out = []
    for dev in config.devices:
        if dev.primary or not (dev.sources or []):
            continue
        src = next((s for s in dev.sources if str(s.protocol or 'tcp').lower() in MODBUS and s.template), None)
        if src is None:
            continue
        tpl = registry.get(src.template)
        if tpl is None or not getattr(tpl, 'calculated', None):
            continue
        path = Path(config.device_registers_path(dev.id))
        data = {"version": "1.0", "registers": [], "poll_groups": {}}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
            except ValueError:
                out.append((dev.id, path, None, tpl.id, ['<unreadable file — left alone>']))
                continue
        have = {c.get('name') for c in (data.get('calculated') or []) if isinstance(c, dict)}
        missing = [c.to_dict() if hasattr(c, 'to_dict') else dict(c) for c in tpl.calculated
                   if (c.name if hasattr(c, 'name') else c.get('name')) not in have]
        if not missing:
            out.append((dev.id, path, None, tpl.id, []))
            continue
        data['calculated'] = list(data.get('calculated') or []) + missing
        out.append((dev.id, path, data, tpl.id, [m['name'] for m in missing]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    ap.add_argument("--owner", default="", help="uid:gid for written files (e.g. 10001:10001)")
    args = ap.parse_args()
    config = Config(args.config)
    registry = TemplateRegistry()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    touched = 0
    for dev_id, path, data, tpl_id, missing in plan(config, registry):
        if not missing:
            print(f"  {dev_id} ({tpl_id}): already current")
            continue
        if data is None:
            print(f"  {dev_id}: {missing[0]}")
            continue
        touched += 1
        print(f"  {dev_id} ({tpl_id}): + {', '.join(missing)}  → {path}")
        if args.apply:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                shutil.copy2(path, path.with_suffix(path.suffix + f".bak-calc-{stamp}"))
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
            tmp.replace(path)
            if args.owner:
                uid, gid = (int(x) for x in args.owner.split(':'))
                os.chown(path, uid, gid)
                os.chown(path.parent, uid, gid)
            print("    written")
    print(f"\n{touched} unit(s) {'updated' if args.apply else 'to update'}.")
    if touched and not args.apply:
        print("dry run — re-run with --apply to write; restart the gateway afterwards.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
