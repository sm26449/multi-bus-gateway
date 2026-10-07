"""Fake Seplos pack bus on a PTY: the master asks each unit in TAP_UNITS (default 3)
for PIA (FC4 0x1000 x18), PIB (FC4 0x1100 x26) and PIC (FC1 0x1200 x144) once a
second and the pack answers. /tmp/ttyTAPE2E -> the PTY slave."""
import os
import time
import tty

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


def pib(unit):
    """FC4 0x1100 x26: 16 cells (mV), 4 cell temps + ambient + MOSFET (0.1 K).
    Unit 3's cell 5 is the weak one, so a grid has a clear minimum."""
    cells = [3300 + (i % 4) * 10 for i in range(16)]
    if unit == 3:
        cells[4] = 3250
    temps = [2951, 2953, 2955, 2957]           # ~22 °C
    words = cells + temps + [0, 0, 0, 0] + [2981, 2991]
    req = adu(bytes([unit, 4, 0x11, 0x00, 0, len(words)]))
    resp = adu(bytes([unit, 4, 2 * len(words)]) + b''.join(w.to_bytes(2, 'big') for w in words))
    return req, resp


def pic(unit):
    """FC1 0x1200 x144 coils: both FETs on, cell 3 balancing; unit 3 also has
    one active alarm (cell high voltage, bit 72)."""
    bits = [0] * 144
    bits[48 + 2] = 1                            # balancing mask: cell 3
    bits[120] = bits[121] = 1                   # fet_discharge, fet_charge
    bits[128] = 1                               # balancing active
    if unit == 3:
        bits[72] = 1                            # alarm_cell_high_v
    data = bytes(sum(b << j for j, b in enumerate(bits[i:i + 8])) for i in range(0, 144, 8))
    req = adu(bytes([unit, 1, 0x12, 0x00, 0, 144]))
    resp = adu(bytes([unit, 1, len(data)]) + data)
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
        for req, resp in (exchange(u, w), pib(u), pic(u)):
            os.write(m, req); time.sleep(0.02); os.write(m, resp); time.sleep(0.05)
    time.sleep(1.0)
