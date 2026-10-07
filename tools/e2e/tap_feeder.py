"""Fake Seplos pack bus on a PTY: the master asks each unit in TAP_UNITS (default 3)
for PIA (FC4 0x1000 x18) once a second and the pack answers. /tmp/ttyTAPE2E -> the PTY slave."""
import os, time, tty

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

WORDS = [5313, (-270) & 0xFFFF, 9800, 28000, 0, 769, 1000, 42, 3323, 2981,
         3330, 3315, 2990, 2970, 0, 150, 150, 0]
UNITS = [int(u) for u in os.environ.get('TAP_UNITS', '3').split(',')]


def exchange(unit, words):
    req = adu(bytes([unit, 4, 0x10, 0x00, 0, len(words)]))
    resp = adu(bytes([unit, 4, 2 * len(words)]) + b''.join(w.to_bytes(2, 'big') for w in words))
    return req, resp


m, s = os.openpty()
tty.setraw(s)
link = '/tmp/ttyTAPE2E'
if os.path.lexists(link):
    os.unlink(link)
os.symlink(os.ttyname(s), link)
print('feeding', os.ttyname(s), flush=True)
while not os.path.exists('/tmp/feeder.stop'):
    for u in UNITS:
        w = list(WORDS)
        w[0] += u                      # each pack a slightly different voltage
        req, resp = exchange(u, w)
        os.write(m, req); time.sleep(0.02); os.write(m, resp); time.sleep(0.05)
    time.sleep(1.0)
