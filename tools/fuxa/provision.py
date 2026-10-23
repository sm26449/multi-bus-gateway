# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Provision FUXA with the gateway's OPC UA server: FUXA's OPC UA plugin
if missing, one OPCUA connection, and one tag per value the gateway serves
(browsed with the same login FUXA will use), each device's _Online and
_Health included.

It sends FUXA the same requests its own editor sends (captured with
Playwright, see docs/fuxa.md), so the result is what a person would make by
hand. Run again after adding devices: existing tags keep their ids, so
screens bound to them survive; --prune drops tags whose nodes are gone.

    MBG_OPCUA_TOKEN=mbgst_... python tools/fuxa/provision.py \\
        --fuxa http://127.0.0.1:1881 --mbg opc.tcp://<gateway-host>:4840/mbg --user fuxa

The password comes only from the environment (MBG_OPCUA_TOKEN) and is sent
to FUXA, which keeps it in its own credential store, never in the project.
"""
import argparse
import asyncio
import json
import os
import secrets
import socket
import sys
import tempfile
import time
import urllib.request

from asyncua import Client, ua

NS_URI = 'urn:multi-bus-gateway'
POLICY = 'http://opcfoundation.org/UA/SecurityPolicy#Basic256Sha256'
SIGN_AND_ENCRYPT = '3'          # FUXA's value for MessageSecurityMode.SignAndEncrypt
# FUXA's tag types for the OPC UA variant types the gateway serves.
TYPES = {ua.VariantType.Double: 'Double', ua.VariantType.Float: 'Float',
         ua.VariantType.Boolean: 'Boolean', ua.VariantType.String: 'String',
         ua.VariantType.Int32: 'Int32', ua.VariantType.Int64: 'Int64',
         ua.VariantType.UInt32: 'UInt32', ua.VariantType.UInt16: 'UInt16',
         ua.VariantType.Int16: 'Int16'}


def fuxa(base, path, body=None):
    req = urllib.request.Request(base + path, method='POST' if body is not None else 'GET',
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def guid(prefix):
    """An id shaped like FUXA's own (t_xxxxxxxx-xxxxxxxx)."""
    return f"{prefix}_{secrets.token_hex(4)}-{secrets.token_hex(4)}"


async def browse(url, user, password, only):
    """[(node id, name, label, FUXA type, description)] for every value
    under MBG/Devices."""
    c = Client(url, timeout=10)
    c.application_uri = 'urn:mbg:fuxa-tools'
    await c.set_security_string(f"Basic256Sha256,SignAndEncrypt,{_client_cert()}")
    c.set_user(user)
    c.set_password(password)
    await c.connect()
    try:
        idx = await c.get_namespace_index(NS_URI)
        out = []
        for dev in await c.get_node(f"ns={idx};s=Devices").get_children():
            d = (await dev.read_browse_name()).Name
            if only and d not in only:
                continue
            for v in await dev.get_children():
                if await v.read_node_class() != ua.NodeClass.Variable:
                    continue                      # the Commands folder: not provisioned
                sid = v.nodeid.Identifier         # Devices/<dev>/<field>
                label = (await v.read_display_name()).Text or sid
                vt = await v.read_data_type_as_variant_type()
                desc = (await v.read_description()).Text or ''      # 'Active power [W]'
                out.append((f"ns={idx};s={sid}", sid.removeprefix('Devices/'),
                            f"{d} · {label}", TYPES.get(vt, 'Double'), desc))
        return out
    finally:
        await c.disconnect()


_CERT = tempfile.mkdtemp(prefix='mbg-opcua-client-')


def _client_cert():
    """asyncua needs a client certificate for SignAndEncrypt. The gateway
    does not pin client certificates (the login is what counts), so a
    throwaway one per run will do."""
    return f"{_CERT}/cert.der,{_CERT}/key.pem"


async def _ensure_client_cert():
    from pathlib import Path

    from asyncua.crypto.cert_gen import setup_self_signed_certificate
    from cryptography.x509.oid import ExtendedKeyUsageOID
    await setup_self_signed_certificate(
        Path(f"{_CERT}/key.pem"), Path(f"{_CERT}/cert.der"), 'urn:mbg:fuxa-tools',
        socket.gethostname(), [ExtendedKeyUsageOID.CLIENT_AUTH], {'commonName': 'mbg-fuxa-tools'})


def tag(tid, name, label, typ, address, description=''):
    return {'id': tid, 'name': name, 'label': label, 'type': typ, 'address': address,
            'description': description,
            'value': '', 'init': '',
            'daq': {'enabled': False, 'interval': 60, 'changed': False, 'restored': False}}


def ensure_opcua_plugin(base):
    """FUXA ships without its OPC UA client: install node-opcua the way
    Settings → Plugins does, and wait for it (it lands on the _pkg volume)."""
    plugin = next((p for p in fuxa(base, '/api/plugins') if p.get('type') == 'OPCUA'), None)
    if plugin is None:
        sys.exit('this FUXA offers no OPCUA plugin')
    if plugin.get('current'):
        return
    print(f"installing FUXA's OPC UA plugin ({plugin['name']} {plugin['version']}), about a minute…")
    fuxa(base, '/api/plugins', {'params': plugin})
    for _ in range(60):
        time.sleep(3)
        if next(p for p in fuxa(base, '/api/plugins') if p.get('type') == 'OPCUA').get('current'):
            return
    sys.exit('the OPC UA plugin did not install; see Settings → Plugins in FUXA')


def remove(base, name):
    devices = (fuxa(base, '/api/project') or {}).get('devices') or {}
    dev = next((d for d in devices.values() if d.get('name') == name), None)
    if not dev:
        print(f"FUXA has no connection named '{name}'")
        return
    fuxa(base, '/api/projectData', {'cmd': 'del-device', 'data': dev})
    server = devices.get('0')
    if server:
        server['tags'] = {k: t for k, t in server.get('tags', {}).items() if t.get('memaddress') != dev['id']}
        fuxa(base, '/api/projectData', {'cmd': 'set-device', 'data': server})
    print(f"removed '{name}' ({len(dev.get('tags', {}))} tag(s)) from FUXA")


async def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--fuxa', default='http://127.0.0.1:1881')
    ap.add_argument('--mbg', required=True, help='the gateway, e.g. opc.tcp://<gateway-host>:4840/mbg')
    ap.add_argument('--user', default='fuxa', help="the service token's name (or a gateway account)")
    ap.add_argument('--name', default='MBG', help='the connection name in FUXA')
    ap.add_argument('--polling', type=int, default=1000, help='ms; match the gateway\'s update interval')
    ap.add_argument('--device', action='append', default=[], help='only these gateway devices (repeatable)')
    ap.add_argument('--prune', action='store_true', help='drop tags whose nodes the gateway no longer serves')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--remove', action='store_true', help='delete the connection and its status tag from FUXA')
    a = ap.parse_args()
    if a.remove:
        return remove(a.fuxa, a.name)
    password = os.environ.get('MBG_OPCUA_TOKEN')
    if not password:
        sys.exit('MBG_OPCUA_TOKEN is not set (the service token, or the account password)')

    ensure_opcua_plugin(a.fuxa)
    await _ensure_client_cert()
    nodes = await browse(a.mbg, a.user, password, set(a.device))
    print(f"browsed {len(nodes)} value(s) on {a.mbg} as '{a.user}'")

    project = fuxa(a.fuxa, '/api/project') or {}
    devices = project.get('devices') or {}
    dev = next((d for d in devices.values() if d.get('name') == a.name), None)
    dev_id = dev['id'] if dev else guid('d')
    old = {t['address']: t for t in (dev or {}).get('tags', {}).values()}
    tags, added = {}, 0
    for address, name, label, typ, desc in nodes:
        t = old.get(address)
        tid = t['id'] if t else guid('t')
        added += t is None
        tags[tid] = {**(t or {}), **tag(tid, name, label, typ, address, desc)}
    gone = [t for addr, t in old.items() if addr not in {n[0] for n in nodes}]
    if not a.prune:
        tags.update({t['id']: t for t in gone})
    print(f"connection '{a.name}' ({'existing ' + dev_id if dev else 'new'}): "
          f"{added} new tag(s), {len(nodes) - added} kept, {len(gone)} gone"
          f"{' (pruned)' if a.prune else ' (kept; --prune to drop)'}")
    if a.dry_run:
        return

    # The login first, then the connection, as the editor does.
    fuxa(a.fuxa, '/api/device', {'params': {'query': 'security', 'name': dev_id, 'value': {
        'mode': {'endpointUrl': a.mbg, 'securityMode': SIGN_AND_ENCRYPT, 'securityPolicy': POLICY},
        'uid': a.user, 'pwd': password}}})
    fuxa(a.fuxa, '/api/projectData', {'cmd': 'set-device', 'data': {
        'id': dev_id, 'name': a.name, 'type': 'OPCUA', 'enabled': True, 'polling': a.polling,
        'property': {'address': a.mbg}, 'tags': tags}})

    # The editor also gives every connection a status tag on the FUXA Server device.
    server = devices.get('0')
    if server and not any(t.get('memaddress') == dev_id for t in server.get('tags', {}).values()):
        tid = guid('t')
        server.setdefault('tags', {})[tid] = {
            'id': tid, 'name': f"{a.name} Connection Status", 'label': f"{a.name} Connection Status",
            'value': '', 'type': 'number', 'memaddress': dev_id, 'init': '', 'sysType': 1,
            'daq': {'enabled': False, 'interval': 60, 'changed': False, 'restored': False}}
        fuxa(a.fuxa, '/api/projectData', {'cmd': 'set-device', 'data': server})
    print(f"provisioned: FUXA → {a.mbg}, {len(tags)} tag(s)")


if __name__ == '__main__':
    asyncio.run(main())
