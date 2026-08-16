#!/usr/bin/env python3
# Multi-Bus Gateway — load-test harness (seed the test MBG via its OWN API).
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Seed the TEST MBG with N devices (pointing at the sim fleet) and M virtual
meters, using MBG's real REST API — so the config is schema-valid by
construction (no hand-forged YAML that could drift from the schema).

    python seed_via_api.py --base http://127.0.0.1:18080 \
        --sim-host loadtest-sim-devices --sim-port 6502 --sim-units 16 \
        --devices 10 --device-template janitza_umg512 \
        --vmeters 4 --vmeter-template em24_av53 \
        --vmeter-port-start 21502

Run with no --devices/--vmeters to just LIST the available templates first
(so you pick ones whose register map matches the sim, per the plan).

⚠️  Point --base at the TEST instance only.
"""
from __future__ import annotations

import argparse
import json
import urllib.request


def _req(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")
        except Exception:  # noqa: BLE001
            return e.code, {"_error": str(e)}
    except Exception as e:  # noqa: BLE001
        return 0, {"_error": str(e)}


def _list_templates(base: str) -> None:
    _, dev = _req("GET", f"{base}/api/device-templates")
    _, vm = _req("GET", f"{base}/api/virtual-meters")
    print("Device templates available:")
    for t in (dev.get("templates") or dev.get("device_templates") or []):
        tid = t.get("id") if isinstance(t, dict) else t
        print(f"  - {tid}")
    print("Virtual-meter templates: query /api/virtual-meter-templates or the UI; "
          "current vmeter instances:", len(vm.get("instances") or vm.get("meters") or []))


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed test MBG via API")
    ap.add_argument("--base", default="http://127.0.0.1:18080")
    ap.add_argument("--sim-host", default="loadtest-sim-devices")
    ap.add_argument("--sim-port", type=int, default=6502)
    ap.add_argument("--sim-units", type=int, default=16)
    ap.add_argument("--devices", type=int, default=0)
    ap.add_argument("--device-template", default="")
    ap.add_argument("--vmeters", type=int, default=0)
    ap.add_argument("--vmeter-template", default="")
    ap.add_argument("--vmeter-port-start", type=int, default=21502)
    ap.add_argument("--stale-after-s", type=float, default=15.0)
    args = ap.parse_args()

    if not args.devices and not args.vmeters:
        _list_templates(args.base)
        print("\nRe-run with --devices/--device-template and/or --vmeters/--vmeter-template.")
        return

    created_devs = []
    for i in range(args.devices):
        if not args.device_template:
            print("!! --device-template is required to create devices"); break
        unit = (i % args.sim_units) + 1
        dev = {
            "id": f"ltdev{i:03d}",
            "name": f"ltdev{i:03d}",
            "template": args.device_template,
            "protocol": "tcp",
            "connection": {"host": args.sim_host, "port": args.sim_port, "unit_id": unit},
        }
        code, resp = _req("POST", f"{args.base}/api/devices", dev)
        if code in (200, 201):
            did = (resp.get("device") or {}).get("id", dev["name"])
            created_devs.append(did)
            print(f"[dev] {dev['name']} -> unit {unit}  OK ({did})")
        else:
            print(f"[dev] {dev['name']}  FAIL {code}: {resp}")

    # One INSTANCE per template (the product enforces it), so N vmeters need
    # N template copies: fetch the source template once, re-save it under
    # lt_<name>_<j>, then instance each copy — bound to a seeded device in
    # round-robin so the serving load spreads across real source stores.
    tpl_doc = None
    if args.vmeters:
        code, tpl_doc = _req("GET", f"{args.base}/api/virtual-meters/template/{args.vmeter_template}")
        if code != 200:
            print(f"!! cannot fetch vmeter template {args.vmeter_template}: {code} {tpl_doc}")
            tpl_doc = None
    for j in range(args.vmeters if tpl_doc else 0):
        tid = args.vmeter_template if j == 0 else f"lt_{args.vmeter_template}_{j:02d}"
        if j > 0:
            body = dict(tpl_doc.get("template") or tpl_doc)
            body["id"] = tid
            body["name"] = f"{body.get('name', tid)} (LT {j:02d})"
            code, resp = _req("PUT", f"{args.base}/api/virtual-meters/template/{tid}", body)
            if code != 200:
                print(f"[vmeter] template {tid}  FAIL {code}: {resp}"); continue
        port = args.vmeter_port_start + j
        src = created_devs[j % len(created_devs)] if created_devs else ""
        vm = {"template": tid, "port": port, "unit_id": 1,
              "stale_after_s": args.stale_after_s, "device": src,
              "on_stale": "legacy", "enabled": True}
        code, resp = _req("POST", f"{args.base}/api/virtual-meters", vm)
        print(f"[vmeter] port {port} tpl={tid} src={src or 'primary'}  "
              f"{'OK' if code in (200,201) and 'error' not in resp else 'FAIL'} "
              f"{code}: {resp if code not in (200,201) else ''}")

    print(f"\nSeeded {len(created_devs)} devices, requested {args.vmeters} vmeters. "
          f"Verify: {args.base}/api/status and {args.base}/health")


if __name__ == "__main__":
    main()
