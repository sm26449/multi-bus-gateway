# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Check that FUXA shows what the gateway serves: for every tag of the
FUXA connection provisioned by provision.py, read the node from the gateway
over OPC UA, read FUXA's value (/api/getTagValue), and compare.

FUXA receives a value one publish after the gateway has it, so a tag
matches when FUXA shows a value the gateway held in the last ~3 s; a tag
that still differs is tried again (--tries) before it fails.

    MBG_OPCUA_TOKEN=mbgst_... python tools/fuxa/compare.py --user fuxa

Exit code 0 when every tag matches.
"""
import argparse
import asyncio
import json
import math
import os
import sys
import urllib.parse
import urllib.request

from asyncua import Client, ua

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from provision import _client_cert, _ensure_client_cert  # noqa: E402


def fuxa_values(base, ids, chunk=100):
    """FUXA takes the ids in the query string: ask in batches, or a large
    project overflows its header limit (HTTP 431)."""
    out = {}
    for i in range(0, len(ids), chunk):
        q = urllib.parse.quote(json.dumps(ids[i:i + chunk]))
        with urllib.request.urlopen(f"{base}/api/getTagValue?ids={q}", timeout=15) as r:
            out.update({v['id']: v.get('value') for v in json.loads(r.read())})
    return out


def same(a, b, tol):
    """FUXA hands values back as JSON: numbers may come as strings, booleans
    as true/false or 1/0."""
    if a is None or b in (None, ''):
        return a is None and b in (None, '')
    if isinstance(a, bool):
        return a == (b in (True, 1, '1', 'true', 'True'))
    if isinstance(a, (int, float)):
        try:
            b = float(b)
        except (TypeError, ValueError):
            return False
        return math.isclose(a, b, rel_tol=tol, abs_tol=tol)
    return str(a) == str(b)


async def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--fuxa', default='http://127.0.0.1:1881')
    ap.add_argument('--mbg', required=True, help='the gateway, e.g. opc.tcp://<gateway-host>:4840/mbg')
    ap.add_argument('--user', default='fuxa')
    ap.add_argument('--name', default='MBG', help='the connection name in FUXA')
    ap.add_argument('--tries', type=int, default=4)
    ap.add_argument('--tol', type=float, default=1e-6)
    a = ap.parse_args()
    password = os.environ.get('MBG_OPCUA_TOKEN')
    if not password:
        sys.exit('MBG_OPCUA_TOKEN is not set')

    with urllib.request.urlopen(a.fuxa + '/api/project', timeout=15) as r:
        devices = (json.loads(r.read()) or {}).get('devices') or {}
    dev = next((d for d in devices.values() if d.get('name') == a.name), None)
    if not dev:
        sys.exit(f"FUXA has no connection named '{a.name}'; run provision.py first")
    tags = {t['id']: t for t in dev.get('tags', {}).values()}

    await _ensure_client_cert()
    c = Client(a.mbg, timeout=10)
    c.application_uri = 'urn:mbg:fuxa-tools'
    await c.set_security_string(f"Basic256Sha256,SignAndEncrypt,{_client_cert()}")
    c.set_user(a.user)
    c.set_password(password)
    await c.connect()
    try:
        nodes = {tid: c.get_node(t['address']) for tid, t in tags.items()}

        async def mbg():
            dvs = await c.read_attributes(list(nodes.values()), ua.AttributeIds.Value)
            return {tid: dv for tid, dv in zip(nodes, dvs)}

        # FUXA gets a value one publish later than the gateway has it: a tag
        # matches when FUXA shows a value the gateway held in the last few
        # seconds (gateway reads on both sides of FUXA's)
        pending, result = set(tags), {}
        for _ in range(a.tries):
            seen = []
            for _i in range(3):
                seen.append(await mbg())
                await asyncio.sleep(0.7)
            got = fuxa_values(a.fuxa, sorted(pending))
            seen.append(await mbg())
            for tid in list(pending):
                held = [s[tid].Value.Value for s in seen]
                ok = any(same(v, got.get(tid), a.tol) for v in held)
                result[tid] = (ok, held[-1], got.get(tid), seen[-1][tid].StatusCode.name)
                if ok:
                    pending.discard(tid)
            if not pending:
                break
            await asyncio.sleep(1.5)
    finally:
        await c.disconnect()

    bad = 0
    for tid, t in sorted(tags.items(), key=lambda kv: kv[1]['name']):
        ok, m, f, status = result.get(tid, (False, '(kept moving)', '', ''))
        bad += not ok
        flag = '' if status in ('Good', '') else f"  [{status}]"
        print(f"{'PASS' if ok else 'FAIL'} {t['name']}: gateway={m!r} fuxa={f!r}{flag}")
    print(f"\n{len(tags) - bad}/{len(tags)} match")
    return bad == 0


if __name__ == '__main__':
    sys.exit(0 if asyncio.run(main()) else 1)
