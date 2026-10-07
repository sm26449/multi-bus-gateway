"""Field devices for new_user_e2e.mjs, with KNOWN values — one process, four
protocols. Run it INSIDE the gateway container as the app user (uid 10001), so
the gateway can open the PTY and reach the ports on loopback:

    docker cp tools/e2e/sims/new_user_sims.py <ctr>:/tmp/
    docker exec -d -u 10001 <ctr> sh -c 'python3 /tmp/new_user_sims.py > /tmp/nu_sims.log 2>&1'

Stop it with `touch /tmp/nu_sims.stop` (the e2e does both).

  Modbus TCP  0.0.0.0:15020 unit 1 (pymodbus), 32-bit values WORD-SWAPPED (CDAB):
    HR 0-1   float  230.5            voltage
    HR 2-3   int32  123456789        energy counter
    HR 4     int16  12345   (/10 → 1234.5 W)
    HR 5     int16  -1234   (/10 → -123.4 W, proves the sign)
    IR 10    uint16 4321    (FC4; HR 10 is 0, so a wrong FC reads 0)
    coil 8   ON  (coils 0-7 OFF)      discrete input 9 ON (0-8 OFF)
    (a map holds one row per ADDRESS whatever its function code, so the
    bits sit at addresses no register uses)
  Modbus RTU  PTY → /tmp/ttySIM, unit 7, 9600 8N1, ABCD (a minimal responder):
    HR 0-1   float  231.25
    HR 2     uint16 777
    IR 20    uint16 5001    (/100 → 50.01 Hz, FC4)
    coil 30  ON
  HTTP        0.0.0.0:18089  GET /status.json →
              {"Body":{"Data":{"PAC":1500,"UAC":231.2,"Strings":[{"E":42.5}]}}}
  MQTT        publishes every second to $MQTT_HOST (default mbg-e2e-mqtt):
              e2e/meter/state  {"p":800,"soc":55.5}
              e2e/meter/temp   21.5      (a bare number, no JSON)
"""
import json
import os
import struct
import threading
import time
import tty
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STOP = '/tmp/nu_sims.stop'
TCP_PORT = int(os.environ.get('SIM_TCP_PORT', '15020'))
HTTP_PORT = int(os.environ.get('SIM_HTTP_PORT', '18089'))
RTU_LINK = os.environ.get('SIM_RTU_LINK', '/tmp/ttySIM')
RTU_UNIT = 7
MQTT_HOST = os.environ.get('MQTT_HOST', 'mbg-e2e-mqtt')


def words_f32(v, order):
    hi, lo = struct.unpack('>HH', struct.pack('>f', v))
    return [lo, hi] if order == 'cdab' else [hi, lo]


def words_i32(v, order):
    hi, lo = struct.unpack('>HH', struct.pack('>i', v))
    return [lo, hi] if order == 'cdab' else [hi, lo]


# ── Modbus TCP (pymodbus) ────────────────────────────────────────────────
def modbus_tcp():
    from pymodbus.datastore import (ModbusDeviceContext, ModbusSequentialDataBlock,
                                    ModbusServerContext)
    from pymodbus.server import StartTcpServer
    hr = [0] * 64
    hr[0:2] = words_f32(230.5, 'cdab')
    hr[2:4] = words_i32(123456789, 'cdab')
    hr[4] = 12345
    hr[5] = (-1234) & 0xFFFF
    ir = [0] * 64
    ir[10] = 4321
    co = [0] * 16
    co[8] = 1
    di = [0] * 16
    di[9] = 1
    # pymodbus 3.15: a block starting at 1 serves protocol address 0
    dev = ModbusDeviceContext(hr=ModbusSequentialDataBlock(1, hr), ir=ModbusSequentialDataBlock(1, ir),
                              co=ModbusSequentialDataBlock(1, co), di=ModbusSequentialDataBlock(1, di))
    StartTcpServer(context=ModbusServerContext(devices={1: dev}, single=False),
                   address=('0.0.0.0', TCP_PORT))


# ── Modbus RTU on a PTY (minimal responder: FC1-4) ───────────────────────
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


RTU_TABLES = {
    3: dict(enumerate(words_f32(231.25, 'abcd') + [777])),   # holding
    4: {20: 5001},                                           # input
    1: {30: 1},                                              # coils
    2: {},                                                   # discrete
}


def rtu_answer(req):
    unit, fc = req[0], req[1]
    addr, count = struct.unpack('>HH', req[2:6])
    if unit != RTU_UNIT or fc not in RTU_TABLES or not 1 <= count <= 125:
        return None if unit != RTU_UNIT else adu(bytes([unit, fc | 0x80, 1]))
    table = RTU_TABLES[fc]
    if fc in (3, 4):
        data = b''.join(struct.pack('>H', table.get(addr + i, 0)) for i in range(count))
    else:
        bits = [table.get(addr + i, 0) for i in range(count)]
        data = bytes(sum(b << j for j, b in enumerate(bits[i:i + 8])) for i in range(0, count, 8))
    return adu(bytes([unit, fc, len(data)]) + data)


def modbus_rtu():
    m, s = os.openpty()
    tty.setraw(s)
    if os.path.lexists(RTU_LINK):
        os.unlink(RTU_LINK)
    os.symlink(os.ttyname(s), RTU_LINK)
    print('rtu on', os.ttyname(s), '->', RTU_LINK, flush=True)
    buf = b''
    while not os.path.exists(STOP):
        buf += os.read(m, 256)
        while len(buf) >= 8:                 # FC1-4 requests are 8 bytes
            frame = buf[:8]
            if crc16(frame[:6]) == frame[6] | (frame[7] << 8):
                buf = buf[8:]
                ans = rtu_answer(frame)
                if ans:
                    time.sleep(0.005)
                    os.write(m, ans)
            else:
                buf = buf[1:]                # resync on noise


# ── HTTP JSON ────────────────────────────────────────────────────────────
DOC = {"Body": {"Data": {"PAC": 1500, "UAC": 231.2, "Strings": [{"E": 42.5}]}}}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(DOC).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def http_json():
    ThreadingHTTPServer(('0.0.0.0', HTTP_PORT), Handler).serve_forever()


# ── MQTT publisher ───────────────────────────────────────────────────────
def mqtt_pub():
    import paho.mqtt.client as mqtt
    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='nu-e2e-sim')
    except AttributeError:                    # paho < 2
        c = mqtt.Client(client_id='nu-e2e-sim')
    while not os.path.exists(STOP):
        try:
            c.connect(MQTT_HOST, 1883, 30)
            break
        except OSError as e:
            print('mqtt connect:', e, flush=True)
            time.sleep(1)
    c.loop_start()
    while not os.path.exists(STOP):
        c.publish('e2e/meter/state', json.dumps({"p": 800, "soc": 55.5}))
        c.publish('e2e/meter/temp', '21.5')
        time.sleep(1)


if __name__ == '__main__':
    if os.path.exists(STOP):
        os.unlink(STOP)
    for fn in (modbus_tcp, modbus_rtu, http_json, mqtt_pub):
        threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
    print('sims up', flush=True)
    while not os.path.exists(STOP):
        time.sleep(0.5)
    print('sims stopped', flush=True)
    os._exit(0)
