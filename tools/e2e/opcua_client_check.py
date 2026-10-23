# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""A real OPC UA client against a running gateway (after opcua_e2e.mjs):
browse MBG/Devices, read every value with its status and source time, and
check that a wrong password and an anonymous client are refused.

    python tools/e2e/opcua_client_check.py opc.tcp://127.0.0.1:14840/mbg <user> <password>
"""
import asyncio
import sys

from asyncua import Client


async def connect(url, user=None, pw=None):
    c = Client(url, timeout=5)
    if user:
        c.set_user(user)
        c.set_password(pw)
    await c.connect()
    return c


async def main(url, user, pw):
    ok = True
    c = await connect(url, user, pw)
    try:
        idx = await c.get_namespace_index('urn:multi-bus-gateway')
        devs = c.get_node(f"ns={idx};s=Devices")
        devices = await devs.get_children()
        print(f"PASS browsed {len(devices)} device(s) under MBG/Devices")
        for d in devices:
            name = (await d.read_browse_name()).Name
            from asyncua import ua
            for v in await d.get_children():
                vn = (await v.read_browse_name()).Name
                if await v.read_node_class() != ua.NodeClass.Variable:
                    cmds = [(await x.read_browse_name()).Name for x in await v.get_children()]
                    print(f"  {name}/{vn}/: {', '.join(cmds)}")
                    continue
                dv = await v.read_data_value(raise_on_bad_status=False)
                print(f"  {name}/{vn} = {dv.Value.Value!r} [{dv.StatusCode.name}] read {dv.SourceTimestamp}")
            online = await c.get_node(f"ns={idx};s=Devices/{name}/_Online").read_value()
            print(f"PASS {name}/_Online = {online}")
    finally:
        await c.disconnect()
    for label, u, p in (('a wrong password', user, 'definitely-wrong'), ('an anonymous client', None, None)):
        try:
            c = await connect(url, u, p)
            await c.disconnect()
            print(f"FAIL {label} was let in")
            ok = False
        except Exception as e:  # noqa: BLE001
            print(f"PASS {label} is refused ({type(e).__name__})")
    return ok


if __name__ == '__main__':
    sys.exit(0 if asyncio.run(main(*sys.argv[1:4])) else 1)
