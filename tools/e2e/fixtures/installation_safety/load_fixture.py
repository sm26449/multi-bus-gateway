#!/usr/bin/env python3
# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Write a throwaway gateway's config directory for one e2e fixture variant.

    load_fixture.py VARIANT OUTDIR [--writes] [--login PASSWORD]

VARIANT is safety | plant | drive (a file next to this one), merged over
base.yaml. OUTDIR is emptied and refilled: config.yaml and nothing else, so
every run starts from the same state. --writes turns security.allow_writes
on (arming a rule needs it); --login turns login on for `admin` with that
password, stored hashed exactly as the UI would store it.

Runs inside the gateway image (it hashes with multibus.auth); fixture_up.sh
is the usual way in.
"""
import argparse
import os
import shutil
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
VARIANTS = ('safety', 'plant', 'drive')


def build(variant: str, writes: bool, password: str | None) -> dict:
    with open(os.path.join(HERE, 'base.yaml')) as f:
        cfg = yaml.safe_load(f)
    with open(os.path.join(HERE, f'{variant}.yaml')) as f:
        cfg.update(yaml.safe_load(f))
    cfg['security']['allow_writes'] = writes
    if password:
        sys.path.insert(0, os.path.abspath(os.path.join(HERE, '..', '..', '..', '..')))
        from multibus.auth import hash_password
        cfg['ui']['auth'].update(enabled=True, password=hash_password(password))
    return cfg


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('variant', choices=VARIANTS)
    ap.add_argument('outdir')
    ap.add_argument('--writes', action='store_true')
    ap.add_argument('--login', metavar='PASSWORD')
    a = ap.parse_args()
    cfg = build(a.variant, a.writes, a.login)
    shutil.rmtree(a.outdir, ignore_errors=True)
    os.makedirs(a.outdir)
    with open(os.path.join(a.outdir, 'config.yaml'), 'w') as f:
        yaml.safe_dump(cfg, f, sort_keys=False)


if __name__ == '__main__':
    main()
