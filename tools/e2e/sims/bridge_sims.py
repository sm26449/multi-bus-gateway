"""RS-485 bridges for bridges_e2e.mjs, with KNOWN values. One process per
simulated box; run them INSIDE the gateway container as the app user (uid
10001) so the gateway reaches them on loopback and on the container's IP:

    docker cp tools/e2e/sims/bridge_sims.py <ctr>:/tmp/
    docker exec -d -u 10001 <ctr> sh -c 'python3 /tmp/bridge_sims.py transparent 14196 > /tmp/br_14196.log 2>&1'
    docker exec -d -u 10001 <ctr> sh -c 'python3 /tmp/bridge_sims.py gateway 15502 > /tmp/br_15502.log 2>&1'

Stop one with `touch /tmp/br_sim_<port>.stop` (or /tmp/br_sims.stop for all).

  transparent PORT   a Waveshare-like converter in "Protocol: None": raw
                     Modbus RTU frames (with CRC) over TCP, two slaves on its
                     bus, ONE client at a time — a new connection takes the
                     line and the older one is closed ("newest wins"). Slaves
                     that are not on the bus stay silent, as on a real wire.
                       unit 1: HR 0 = 1111, HR 1 = 2222
                       unit 2: HR 0 = 3333, HR 1 = 4444
                     Writes /tmp/br_sim_<port>.json every 0.5 s:
                       {"accepts": n, "kicked": n, "frames": {unit: n}, "peer": ip}
  master PORT        a transparent converter on a bus ANOTHER master polls:
                     every 0.5 s it carries that master's questions and the
                     slaves' answers (unit 1 HR 0-1 = 1111, 2222; unit 2 =
                     3333, 4444), torn into 5-byte pieces, to every client.
                     Whatever a client sends is counted, never answered:
                       {"accepts": n, "written": bytes, "cycles": n}
  gateway PORT       a converter in "Modbus TCP to RTU" mode (pymodbus TCP
                     server), two unit ids:
                       unit 1: HR 0 = 5555, HR 1 = 6666
                       unit 2: HR 0 = 7777, HR 1 = 8888
"""
import json
import os
import socket
import struct
import sys
import threading
import time

ALL_STOP = '/tmp/br_sims.stop'

TRANSPARENT = {1: {0: 1111, 1: 2222}, 2: {0: 3333, 1: 4444}}
GATEWAY = {1: [5555, 6666], 2: [7777, 8888]}


def stopped(port):
    return os.path.exists(ALL_STOP) or os.path.exists(f'/tmp/br_sim_{port}.stop')


def crc16(b):
    c = 0xFFFF
    for x in b:
        c ^= x
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c


def adu(body):
    c = crc16(body)
    return body + bytes([c & 0xFF, c >> 8])


# ── transparent: RTU over TCP, one client, newest wins ───────────────────
def transparent(port):
    stats = {'accepts': 0, 'kicked': 0, 'frames': {}, 'peer': ''}
    cur = {'sock': None}
    lock = threading.Lock()

    def answer(frame):
        unit, fc = frame[0], frame[1]
        if unit not in TRANSPARENT:
            return None                      # no such slave on the wire: silence
        addr, count = struct.unpack('>HH', frame[2:6])
        stats['frames'][str(unit)] = stats['frames'].get(str(unit), 0) + 1
        if fc not in (3, 4) or not 1 <= count <= 125:
            return adu(bytes([unit, fc | 0x80, 1]))
        regs = TRANSPARENT[unit]
        data = b''.join(struct.pack('>H', regs.get(addr + i, 0)) for i in range(count))
        return adu(bytes([unit, fc, len(data)]) + data)

    def serve(conn):
        buf = b''
        conn.settimeout(0.5)
        while not stopped(port):
            try:
                chunk = conn.recv(256)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while len(buf) >= 8:             # FC3/FC4 requests are 8 bytes
                frame = buf[:8]
                if crc16(frame[:6]) == frame[6] | (frame[7] << 8):
                    buf = buf[8:]
                    ans = answer(frame)
                    if ans:
                        time.sleep(0.01)     # the bus at 9600 baud is not instant
                        try:
                            conn.sendall(ans)
                        except OSError:
                            return
                else:
                    buf = buf[1:]            # not RTU (e.g. an MBAP header): resync
        try:
            conn.close()
        except OSError:
            pass

    def dump():
        while not stopped(port):
            with open(f'/tmp/br_sim_{port}.json.tmp', 'w') as f:
                json.dump(stats, f)
            os.replace(f'/tmp/br_sim_{port}.json.tmp', f'/tmp/br_sim_{port}.json')
            time.sleep(0.5)

    threading.Thread(target=dump, daemon=True).start()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('0.0.0.0', port))
    srv.listen(4)
    srv.settimeout(0.5)
    print('transparent converter on', port, flush=True)
    while not stopped(port):
        try:
            conn, peer = srv.accept()
        except socket.timeout:
            continue
        with lock:
            stats['accepts'] += 1
            stats['peer'] = peer[0]          # which address the client came from
            old, cur['sock'] = cur['sock'], conn
            if old is not None:              # the line goes to the newest client
                stats['kicked'] += 1
                try:
                    old.shutdown(socket.SHUT_RDWR)
                    old.close()
                except OSError:
                    pass
        threading.Thread(target=serve, args=(conn,), daemon=True).start()


# ── master: a bus someone else polls, passed on byte for byte ─────────────
def master(port):
    stats = {'accepts': 0, 'written': 0, 'cycles': 0}
    clients = []
    lock = threading.Lock()

    def traffic():
        out = b''
        for unit, regs in TRANSPARENT.items():
            out += adu(bytes([unit, 3]) + struct.pack('>HH', 0, 2))
            out += adu(bytes([unit, 3, 4]) + struct.pack('>HH', regs[0], regs[1]))
        return out

    def listen(conn):                         # a tap must never speak: count it if it does
        conn.settimeout(0.5)
        while not stopped(port):
            try:
                chunk = conn.recv(256)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            stats['written'] += len(chunk)
        with lock:
            if conn in clients:
                clients.remove(conn)

    def broadcast():
        while not stopped(port):
            data = traffic()
            with lock:
                targets = list(clients)
            for i in range(0, len(data), 5):
                for c in targets:
                    try:
                        c.sendall(data[i:i + 5])
                    except OSError:
                        pass
                time.sleep(0.003)
            stats['cycles'] += 1
            with open(f'/tmp/br_sim_{port}.json.tmp', 'w') as f:
                json.dump(stats, f)
            os.replace(f'/tmp/br_sim_{port}.json.tmp', f'/tmp/br_sim_{port}.json')
            time.sleep(0.5)

    threading.Thread(target=broadcast, daemon=True).start()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('0.0.0.0', port))
    srv.listen(4)
    srv.settimeout(0.5)
    print('bus with a foreign master on', port, flush=True)
    while not stopped(port):
        try:
            conn, _peer = srv.accept()
        except socket.timeout:
            continue
        with lock:
            stats['accepts'] += 1
            clients.append(conn)
        threading.Thread(target=listen, args=(conn,), daemon=True).start()


# ── gateway: Modbus TCP, two unit ids (pymodbus) ─────────────────────────
def gateway(port):
    from pymodbus.datastore import (ModbusDeviceContext, ModbusSequentialDataBlock,
                                    ModbusServerContext)
    from pymodbus.server import StartTcpServer
    devices = {}
    for unit, words in GATEWAY.items():
        hr = words + [0] * 30
        # pymodbus 3.15: a block starting at 1 serves protocol address 0
        devices[unit] = ModbusDeviceContext(hr=ModbusSequentialDataBlock(1, hr),
                                            ir=ModbusSequentialDataBlock(1, [0] * 32))
    threading.Thread(target=lambda: StartTcpServer(
        context=ModbusServerContext(devices=devices, single=False),
        address=('0.0.0.0', port)), daemon=True).start()
    print('modbus tcp gateway on', port, flush=True)
    while not stopped(port):
        time.sleep(0.5)


if __name__ == '__main__':
    kind, port = sys.argv[1], int(sys.argv[2])
    for p in (f'/tmp/br_sim_{port}.stop',):
        if os.path.exists(p):
            os.unlink(p)
    {'transparent': transparent, 'gateway': gateway, 'master': master}[kind](port)
    print('stopped', flush=True)
    os._exit(0)
