"""Fake Seplos pack bus on a PTY: the master asks unit 3 for PIA (FC4 0x1000 x18)
once a second and the pack answers. /tmp/ttyTAPE2E -> the PTY slave."""
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
REQ = adu(bytes([3, 4, 0x10, 0x00, 0, len(WORDS)]))
RESP = adu(bytes([3, 4, 2 * len(WORDS)]) + b''.join(w.to_bytes(2, 'big') for w in WORDS))

m, s = os.openpty()
tty.setraw(s)
link = '/tmp/ttyTAPE2E'
if os.path.lexists(link):
    os.unlink(link)
os.symlink(os.ttyname(s), link)
print('feeding', os.ttyname(s), flush=True)
while not os.path.exists('/tmp/feeder.stop'):
    os.write(m, REQ); time.sleep(0.02); os.write(m, RESP)
    time.sleep(1.0)
