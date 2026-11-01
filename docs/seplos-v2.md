# Seplos V2 BMS (`protocol: seplos_v2`)

Seplos **V2** battery packs (the 10E / 16S generation with the "BMS
communication protocol V2.0") do not speak Modbus on their RS485 port. They
speak an ASCII "telecom" protocol. The gateway reads it with a driver of its
own, `multibus/seplos_v2.py`, and lays every answer out as a **fixed virtual
register map**. A template then reads that map with the normal register
machinery: types, scale, enums, alarms, calculated fields and outputs.

> **Status: not yet verified on hardware.** The decode follows the published
> V2.0 protocol and the community decoders built on it, and the tests use
> frames built from that layout. [What is not verified yet](#7-what-is-not-verified-yet)
> lists what to check against your pack. The master pack's 0x5A summary on
> the inter-pack bus (§8) is decoded from a capture of a real bank and
> checked against that bank's CAN link; 0x42/0x44/0x51 are still unverified.

> **Read-only.** The driver encodes only the read commands 0x42, 0x44, 0x47
> and 0x51. Any other command raises `CommandNotAllowed` before a byte is
> built, so nothing on a pack can be changed from the gateway.

Seplos **V3** packs speak Modbus RTU and use `seplos_bms_v3_rtu_tap` instead
([rtu-serial.md §7](rtu-serial.md)).

---

## 1. The protocol in short

A frame, every byte between `~` and `\r` written as two upper-case ASCII hex
characters:

```
~  VER  ADR  CID1  CID2  LENGTH  INFO…  CHKSUM  \r
   20   00   46    42    E002    00     FD37
```

| Field | Meaning |
|---|---|
| `VER` | protocol version, 0x20 |
| `ADR` | pack address (the DIP switch); the gateway's `unit_id` |
| `CID1` | 0x46, battery |
| `CID2` | in a request: the command. In an answer: the return code (RTN) |
| `LENGTH` | LCHKSUM (4 bits) + LENID (12 bits). LENID = number of ASCII characters of INFO. LCHKSUM = (−(sum of LENID's three nibbles)) mod 16, 0 when LENID is 0 |
| `CHKSUM` | (−(sum of the ASCII bytes between `~` and CHKSUM)) mod 65536, four hex characters |

`~20004642E00200FD37\r` is "telemetry of pack 0"; the encoder reproduces it
byte for byte (test `test_encoder_reproduces_the_known_telemetry_request`).

**Commands the gateway may send (the allow-list):**

| CID2 | Request INFO | Answer | Decoded |
|---|---|---|---|
| 0x42 | pack address (1 byte) | telemetry: cells, temperatures, current, voltage, capacities, SOC, cycles, SOH | yes |
| 0x44 | pack address (1 byte) | telesignalling: alarm and status bytes | yes |
| 0x51 | none | device name, software version, manufacturer | yes |
| 0x47 | pack address (1 byte) | system parameters | **no** (allowed, never sent by the client) |

**Heard only, never sent:** 0x5A, the master pack's summary request to each
slave on the inter-pack bus of a parallel bank. Its question carries no INFO;
its answer **echoes 0x5A in CID2** (no RTN) and carries 32 bytes. It is not
in the allow-list: `encode()` refuses it. See §8.

**Return codes (RTN)** in an answer's CID2: 0x00 ok, 0x01 VER error, 0x02
CHKSUM error, 0x03 LCHKSUM error, 0x04 CID2 invalid, 0x05 command format
error, 0x06 invalid data, 0x90 ADR error, 0x91 internal communication error.
The last two are reported for the telecom family and have not been seen on a
Seplos yet. An RTN other than 0 is counted per pack (`rtn_errors`, `last_rtn`)
and is never retried, because the pack did answer.

### 1.1 What is published and what is assumed

| Point | Source |
|---|---|
| Frame layout, VER/CID1, LENGTH and LCHKSUM, CHKSUM | V2.0 document; matches the known request `~20004642E00200FD37` |
| 0x42 layout: DATAFLAG, pack byte, N, N×cell mV, M, M×temperature (0.1 K, offset 2731), current (int16, 0.01 A), pack voltage (0.01 V), remaining capacity (0.01 Ah), custom count P, then P words: full capacity (0.01 Ah), SOC (0.1 %), rated capacity (0.01 Ah), cycles, SOH (0.1 %), port voltage (0.01 V), reserved | V2.0 document and the ESPHome `seplos_bms` decoder |
| Current sign: + = charging | **assumed** (as the V3 template's `current`) |
| With 6 sensors: 4 cell sensors, then ambient, then power (MOSFET). In general: with M ≥ 3, the last two are ambient and power | **assumed** from the 16S layout |
| 0x44 layout: DATAFLAG, pack byte, N, N cell alarm bytes, M, M temperature alarm bytes, the current/voltage alarm bytes, custom count P (20), then P bytes | V2.0 document. The number of current/voltage bytes is read from the frame (see §3) |
| Cell alarm byte: 0x00 normal, 0x01 below the lower limit, 0x02 above the upper limit, 0xF0 other fault | V2.0 document |
| The bit tables of alarm events 1–8, switch state, balance, system state, disconnection (§2.2) | V2.0 document, from memory of the table; **verify every bit** |
| 0x51 layout: device name 10 bytes, software version 2 bytes, manufacturer 20 bytes | telecom-family layout; **assumed** for Seplos |
| An answer's ADR equals the request's ADR | **assumed** |
| A V2 master pack polls its slaves on the inter-pack bus with the same frames (0x42/0x44) | **refuted** on a real bank: the master asks each slave with **0x5A** only, about once a second, at 19200 Bd; no 0x42/0x44 there (§8) |
| 0x5A layout (§2.5) | **no published source**: read off a capture of a real bank with the units 0x42 uses, cross-checked with the CAN link of the same bank; each word is marked verified or assumed in §8.2 |

---

## 2. The virtual register map (the contract)

Every word is 16 bits in the **input** table (`register_type: input`). A
template row reads it by address, like a Modbus input register. The map does
not move with the number of cells or temperatures, so one template serves a
15-cell and a 16-cell pack.

**Absent is absent.** A cell past the pack's count, a temperature it does not
have, a value it reports as 0xFFFF, or a block not read yet writes **no word**.
A row on such a word publishes nothing, never a zero. When a command's answer
arrives, its whole block is replaced, so a cell that disappears does not keep
its last voltage. The current is the one exception: it is int16, and 0xFFFF
means −0.01 A, which is a real value.

### 2.1 0x0000–0x00FF: telemetry (0x42)

| Word | Content | Type / unit |
|---|---|---|
| 0x0000–0x001F | cell voltages 1–32 | uint16 mV |
| 0x0020 | cell count N | |
| 0x0021 | temperature count M | |
| 0x0022–0x0029 | temperatures 1–8, Kelvin offset removed | int16 0.1 °C |
| 0x002A | custom count P of the 0x42 answer | |
| 0x002B | DATAFLAG | |
| 0x0030 | current (+ charging, see §1.1) | int16 0.01 A |
| 0x0031 | pack voltage | uint16 0.01 V |
| 0x0032 | remaining capacity | 0.01 Ah |
| 0x0033 | full capacity | 0.01 Ah |
| 0x0034 | SOC | 0.1 % |
| 0x0035 | rated capacity | 0.01 Ah |
| 0x0036 | cycles | |
| 0x0037 | SOH | 0.1 % |
| 0x0038 | port voltage | 0.01 V |
| 0x0039–0x003F | reserved words of the answer, as received | |
| 0x0040 | highest cell voltage | mV |
| 0x0041 | lowest cell voltage | mV |
| 0x0042 | average cell voltage (rounded) | mV |
| 0x0043 | cell delta (highest − lowest) | mV |
| 0x0044 / 0x0045 / 0x0046 | highest / lowest / average cell temperature (cell sensors only, §1.1) | int16 0.1 °C |
| 0x0047 | ambient temperature (second-last sensor when M ≥ 3) | int16 0.1 °C |
| 0x0048 | power / MOSFET temperature (last sensor when M ≥ 3) | int16 0.1 °C |
| 0x0049 / 0x004A | number of the cell with the highest / lowest voltage (1-based) | |

Words 0x0040–0x004A are computed from the same answer, so they agree with
the cells by construction.

### 2.2 0x0100–0x01FF: alarms and status (0x44)

| Word | Content |
|---|---|
| 0x0100–0x011F | cell alarm bytes 1–32 (one per word) |
| 0x0120 / 0x0121 | cell count / temperature count, as the 0x44 answer says |
| 0x0122–0x0129 | temperature alarm bytes 1–8 |
| 0x0130 | charge (or charge/discharge) current alarm byte |
| 0x0131 | pack voltage alarm byte |
| 0x0132 | discharge current alarm byte (only in the three-byte layout, §3) |
| 0x0133 | custom count P |
| 0x0134–0x0153 | custom bytes 1–32, one per word, in protocol order (below) |
| 0x0160–0x01DF | custom bytes 1–16 **bit by bit**: word 0x0160 + 8·(byte − 1) + bit holds 0 or 1 |
| 0x01E0 | balancing mask, cells 1–16 (balance 1 \| balance 2 << 8) |
| 0x01E1 | disconnection mask, cells 1–16 |
| 0x01E2 | cells whose alarm byte is 0x01 (below the lower limit), bit n−1 = cell n |
| 0x01E3 | cells whose alarm byte is 0x02 (above the upper limit) |
| 0x01E4 | temperatures with any alarm byte ≠ 0 |
| 0x01E5 | 1 when the current/voltage byte count could not be told from the frame (§3) |

The custom bytes, in protocol order (V2.0 table, **not yet verified on a
pack**), with the bits the template names:

| # | Word | Byte | Bits (0 → 7) |
|---|---|---|---|
| 1 | 0x0134 | alarm event 1 | voltage sensor fault, temperature sensor fault, current sensor fault, key switch fault, cell voltage difference fault, charge switch fault, discharge switch fault, current-limit switch fault |
| 2 | 0x0135 | alarm event 2 | cell high-voltage alarm, cell over-voltage protection, cell low-voltage alarm, cell under-voltage protection, pack high-voltage alarm, pack over-voltage protection, pack low-voltage alarm, pack under-voltage protection |
| 3 | 0x0136 | alarm event 3 | charge high-temperature alarm, charge over-temperature protection, charge low-temperature alarm, charge under-temperature protection, discharge high-temperature alarm, discharge over-temperature protection, discharge low-temperature alarm, discharge under-temperature protection |
| 4 | 0x0137 | alarm event 4 | ambient high-temperature alarm, ambient over-temperature protection, ambient low-temperature alarm, ambient under-temperature protection, power over-temperature protection, power high-temperature alarm, cell low-temperature heating, reserved |
| 5 | 0x0138 | alarm event 5 | charge over-current alarm, charge over-current protection, discharge over-current alarm, discharge over-current protection, transient over-current protection, output short-circuit protection, transient over-current lockout, output short-circuit lockout |
| 6 | 0x0139 | alarm event 6 | charge high-voltage protection, intermittent recharge waiting, remaining-capacity alarm, remaining-capacity protection, cell low-voltage charging forbidden, output reverse-polarity protection, output connection fault, internal |
| 7 | 0x013A | switch state | discharge switch, charge switch, current-limit switch, heating switch, reserved… |
| 8 | 0x013B | balance state 1 | cells 1–8 |
| 9 | 0x013C | balance state 2 | cells 9–16 |
| 10 | 0x013D | system state | discharge, charge, floating charge, reserved, standby, shutdown, reserved, reserved |
| 11 | 0x013E | disconnection state 1 | cells 1–8 |
| 12 | 0x013F | disconnection state 2 | cells 9–16 |
| 13 | 0x0140 | alarm event 7 | not named yet (raw byte in the template) |
| 14 | 0x0141 | alarm event 8 | not named yet (raw byte in the template) |
| 15–20 | 0x0142–0x0147 | reserved | |

### 2.3 0x0200–0x02FF: identity (0x51)

| Word | Content |
|---|---|
| 0x0200–0x0204 | device name, 10 ASCII characters, 2 per word (`string:5`) |
| 0x0205 | software version, first byte << 8 \| second byte |
| 0x0206–0x020F | manufacturer, 20 ASCII characters (`string:10`) |
| 0x0210 | length of the answer's INFO, in bytes (32 expected) |

### 2.4 0x0F00–0x0FFF: the driver's own diagnostics

| Word | Content |
|---|---|
| 0x0F00 | last response time, ms (poll: request → answer; listen: the master's request → the pack's answer) |
| 0x0F01 | checksum errors (CHKSUM and LCHKSUM), mod 65536 |
| 0x0F02 | timeouts |
| 0x0F03 | error answers (RTN ≠ 0) |
| 0x0F04 | last RTN |
| 0x0F05 | good answers, mod 65536 |
| 0x0F06 | answers that did not decode |

0x0300–0x03FF is kept for 0x47 (system parameters), which has no decoder yet.

### 2.5 0x0400–0x04FF: the master's summary (0x5A, listen only)

Filled only by a listener that hears the master pack ask a slave (§8). A
polled device never asks 0x5A: a row here on a polled device stays absent.

| Word | Content | Type / unit |
|---|---|---|
| 0x0400 | summary word 0, raw (0x0001 on the observed bank) | |
| 0x0401 | highest cell voltage | uint16 mV |
| 0x0402 | lowest cell voltage | uint16 mV |
| 0x0403 | cell delta, highest − lowest (computed here) | mV |
| 0x0404 | highest temperature, Kelvin offset removed | int16 0.1 °C |
| 0x0405 | lowest temperature | int16 0.1 °C |
| 0x0406 | current (+ charging) | int16 0.01 A |
| 0x0407 | pack voltage | uint16 0.01 V |
| 0x0408 | remaining capacity | 0.01 Ah |
| 0x0409 | full capacity | 0.01 Ah |
| 0x040A | SOC | 0.1 % |
| 0x040B | port voltage (**assumed**) | 0.01 V |
| 0x0410–0x042F | every INFO word as received (word *i* at 0x0410 + *i*) | raw |
| 0x043F | INFO length in bytes (32 on the observed bank) | |

**Only the observed layout is named.** The words 0x0400–0x040B are written
only from a 32-byte INFO whose first word is 0x0001 and whose values can be:
lowest cell ≤ highest cell, SOC ≤ 100 %, remaining ≤ full capacity, and
temperatures within −40…+100 °C. Any other answer (another length, another
head, a value that cannot be) writes **only** the raw words and the length,
counts a decode error, and its reason shows in the device status: a firmware
with another layout must not publish a plausible SOC read by position. In a named answer, 0xFFFF in a voltage, capacity or SOC word is
absent; a temperature word of 0, 0xFFFF or outside −40…+100 °C is absent; the current is int16 and always a value.

---

## 3. Decoding defensively

- Every count in the frame (N, M, P) is trusted only as far as the bytes go.
  A short 0x42 custom block is decoded up to where it ends; the missing words
  stay absent. A frame that ends inside the fixed part is a decode error
  (`decode_errors`), and nothing is published from it.
- In 0x44, the number of current/voltage alarm bytes (2 or 3) is read from the
  frame: the layout is the one where the custom count equals the bytes left.
  When neither fits, two is assumed and 0x01E5 is set to 1.
- The framer drops bytes before a `~`, a frame cut by a new `~`, and a head
  that never reaches `\r` (`resync_dropped_bytes`). An adapter that echoes our
  own request, and frames from other addresses, are skipped while waiting for
  an answer.

---

## 4. Modes

| | `mode: poll` | `mode: listen` |
|---|---|---|
| Who is master | the gateway | a master pack (or other software) |
| Transmits | read requests only | **never**: the port is opened, read, closed |
| Rhythm | the template's poll groups | the master's |
| Sharing | one transport per serial port (`seplos_v2`) or per host:port (`seplos_v2-tcp`), one lock, every pack on it takes turns | one reader per port; packs register by ADR |
| Missing port | opened again on the next request | reopened every 3 s |

**Poll.** Each poll group asks the commands its rows need: rows at 0x00xx ask
0x42, at 0x01xx ask 0x44, at 0x02xx ask 0x51. The bundled template puts
telemetry on `realtime` (5 s), alarms on `normal` (15 s) and identity on
`slow` (1 h, asked once at start). A request is tried up to `retry_attempts`
times (default 3) on a timeout or a corrupted answer, with
`min(retry_delay, 0.5 s)` between tries. `timeout` (default 3 s) is per request. Statistics per pack:
`timeouts`, `checksum_errors`, `frame_errors`, `rtn_errors`, `decode_errors`,
`io_errors`.

**Listen.** The reader pairs each read request (0x42/0x44/0x51) with the next
answer from the same address within 2 s, and feeds the pack registered at that
address. It pairs the master's 0x5A summary the same way: a 0x5A frame
without INFO is the question, a 0x5A frame with INFO from the same address
within 2 s is the answer (its RTN is taken as 0); a 0x5A answer whose question
was not heard (a collision, a torn frame) is an orphan and is never decoded.
An answer nobody asked for is an orphan. A request with another
command (a master setting something) is counted in `commands_seen` and never
decoded. A corrupted answer is charged to the pack whose answer was awaited.
Values publish on the `tap` group, like `rtu_tap`, so calculated fields run on
every answer.

- **Freshness counts only what the map reads.** An answer to a command no row
  of the device reads (a 0x5A summary heard by a 0x42 map) is not counted as
  an answer (`unused_answers`), and the device's age is that of the
  **stalest** command its map reads among those heard: a master that keeps
  asking 0x5A but stopped asking 0x42 leaves a 0x42 map stale.
- **Said in words.** When answers are heard but none is usable, the status
  says why (`bus.warning`, also in Test and in the health reason): 0x5A
  answers whose question was never heard (`summary_unpaired`; a firmware
  whose question carries INFO, or questions lost on the line), 0x5A answers
  not decoded by name, or answers this map does not read.
- **One device per pack.** A second device on the same port and ADR is
  refused when it starts (down, naming the device that holds the ADR); it
  never displaces the first, and stopping it leaves the first listening.
- **The port never drives the bus.** A listened-to serial port is opened with
  RTS and DTR set low *before* it opens, so an adapter whose driver-enable
  follows RTS stays in receive. Each (re)open starts a new stream:
  half a frame and an unanswered question from before the gap are dropped.
- **A map that is only heard listens.** A device whose template says
  `listen_only` (the inter-pack tap), or whose every row is in the 0x5A page
  and the diagnostics, listens whatever its stored mode says (an import, a
  restore, hand-edited YAML); the log says so once.

**The mode is a choice, never a default.** A new device (UI or API) must say
`mode`: the form pre-selects nothing, and the API refuses a create, a move
onto this protocol, or a Test without it. A device stored before the mode was
asked (no `mode` key) keeps reading as `poll`, so existing configs do not
change. A connection that reaches the driver without passing the API (YAML,
an import, a mirror) **fails closed**: an unknown `mode`, or a `unit_id`
outside 0..255, starts nothing, transmits nothing, logs an error and shows
the device down with the reason.

A poll cycle that raises is logged and counted (`loop_errors`); polling goes
on at the next due time.

On a standby gateway (high availability, available in MBG Pro), a `listen` device keeps running, like `rtu_tap`
(it never transmits); a `poll` device stops, as every polled source does.

---

## 5. Wiring and choosing the mode

- RS485 is two wires, **A (D+)** and **B (D−)**, plus a common ground
  where the adapter has one. A swapped pair shows as silence or as checksum
  errors. Seplos V2 packs talk at **9600 8N1** by default; `baudrate` is
  configurable.
- On a **multi-pack bank** the master pack polls the other packs on
  the **inter-pack** RS485 bus. That bus already has a master: use
  **`mode: listen`** there, one device per pack, each with its own `unit_id`,
  and the template `seplos_bms_v2_tap` (§8). Never poll it, because two
  masters collide: in a test four single reads did, and one slave answered
  RTN 0x02 (checksum error).
- Use **`mode: poll`** only on a bus with no other master: a single pack's
  RS485 port, or a port where nothing else asks.
- Through the network: any transparent TCP ↔ RS485 bridge (the MBG
  `serial-bridge`, an ESP-based serial-to-TCP bridge, a ser2net-style converter) as
  `protocol: seplos_v2-tcp` with its `host` and `port`. A **Modbus TCP
  gateway** does not work: it only forwards Modbus. Bridge records (the
  Devices page's bridges) are not used for this protocol.

```yaml
devices:
  - id: bms-pack-1
    template: seplos_bms_v2
    connection:
      protocol: seplos_v2          # or seplos_v2-tcp with host + port
      serial_port: /dev/ttyBMS     # map it into the container
      baudrate: 9600
      unit_id: 1                   # the pack's ADR (DIP switch)
      mode: listen                 # poll | listen — required for a new device
      timeout: 3                   # poll only
```

Saving checks the bus: one protocol per bus, one mode per bus (polled or
listened to, never both), one device per pack address, the same line settings
for every device on a serial port.

**Test** on an unsaved device sends one 0x42 in poll mode (through the bus's
shared transport) and asks nothing in listen mode. With a listen-only
template (`seplos_bms_v2_tap`), it asks nothing in any mode, over a serial
port or a bridge. It sends nothing — and says
why — when the bus is held by another device that listens there (a master
polls it), by a tap, or by another protocol (a Modbus line); a bridge host off
the LAN is blocked like any probe. A polled pack already on the bus is asked
through that device's transport, in turn.

A bridge's **Scan** and **Check mode** refuse a port a `seplos_v2-tcp` device
reads through that bridge's host and port: they would put Modbus frames on a
Seplos bus. On a saved device it
reports what the running client got: answers, errors, last response time.

---

## 6. Writes

There is no write path. The client has no `write_value`, so a write through
the device is refused like a tap's ("no source supports writing"). The write
API refuses `seplos_v2`, `seplos_v2-tcp` and `rtu_tap` devices up front with
"read-only". The encoder refuses any CID2 outside the allow-list.

---

## 7. What is not verified yet

Check these against your own pack before you rely on a value, and report
what you find:

1. The pack answers `~20004642E00200FD37\r` (and the ADR-1 form
   `~20014642E00201FD35\r`), and the answer's ADR is the pack's.
2. 0x42: the cell and temperature counts, the sensor order (cells, ambient,
   power), the current's sign, the custom count and the order of the words
   after it; SOC and capacities against the pack's display or BatteryMonitor.
3. 0x44: two or three current/voltage bytes (0x01E5 says which the frame
   gave), the custom count, and **every named bit** of alarm events 1–6,
   switch state, balance and system state.
4. 0x51: the layout and lengths of name, version and manufacturer.
5. RTN codes seen in practice, and which commands a pack refuses (0x04).
6. Listen mode on an inter-pack bus is observed (§8): the master asks each
   slave with 0x5A once a second at 19200 Bd, and its own data never appears
   there. Not observed yet: the current's sign while charging, an alarm, and
   what the master asks when a slave is switched off.
7. Timing: response time at 9600, and whether a pack needs a pause between
   requests (several packs polled back to back).
8. 0x47: its answer is not decoded yet.

---

## 8. A parallel bank: the inter-pack tap

### 8.1 What is on the bus

A bank of Seplos V2 packs in parallel has one **master** pack (set by its DIP
switch). The master talks to the inverter (CAN or RS485) and polls the
**slaves** over the inter-pack RS485 bus. On the observed bank (three packs,
an inverter on the CAN side) that bus runs at **19200 Bd** and carries only
this, about once a second per slave:

```
~2001465A0000FD9D                                    master → ADR 1, no INFO
~2001465AC04000010CCA0CC50BA00B98FFD11474249B6D60014E147900000000000003028010F00E
                                                     ADR 1's answer, 32 bytes
~2002465A0000FD9C                                    master → ADR 2
~2002465AC040…                                       ADR 2's answer
```

The master's own values never appear on this bus. They are on its CAN link
to the inverter (Pylontech protocol, `pylontech_can_inverter`), as **bank**
values: SOC, voltage, current, limits, the bank's cell and temperature
extremes (0x370), the module count (0x359). Whether the master's second
RS485 port answers 0x42 for its own pack is **not verified**.

The capture the tests replay is in `tests/fixtures/seplos_v2/` (240 frames
over 60 s; every frame passes LENGTH, LCHKSUM and CHKSUM).

### 8.2 The 0x5A answer, word by word

No published document or community decoder covers 0x5A (the V2.0 document
stops at 0x51). The layout below is read off the
capture with the units 0x42 uses. Each word is checked against physics and
against the CAN link of the same bank. Values are ADR 1 / ADR 2 in the first
second of the capture.

| Word | ADR 1 | ADR 2 | Meaning | Status |
|---|---|---|---|---|
| 0 | 0x0001 | 0x0001 | DATAFLAG and a group byte, as 0x42 starts? | assumed (raw) |
| 1 | 3274 mV | 3278 mV | highest cell | verified: inside the 3.23–3.29 V of CAN 0x370; ≥ word 2 on all 120 answers |
| 2 | 3269 mV | 3241 mV | lowest cell | verified (as word 1); 16 × lowest ≤ pack voltage ≤ 16 × highest on every answer |
| 3 | 2976 → 24.5 °C | 2969 → 23.8 °C | highest temperature, 0.1 K | verified: 24.5 °C is exactly the bank maximum in CAN 0x370. Which sensors it covers is assumed |
| 4 | 2968 → 23.7 °C | 2958 → 22.7 °C | lowest temperature, 0.1 K | verified: 22.7 °C is exactly the bank minimum in CAN 0x370 |
| 5 | −47 → −0.47 A | −45 → −0.45 A | current, int16 0.01 A | magnitude verified; the sign (+ charging, as 0x42) is assumed: discharging at the time, like the bank on CAN |
| 6 | 5236 → 52.36 V | 5231 → 52.31 V | pack voltage, 0.01 V | verified (CAN: 52.24 V for the bank) |
| 7 | raw → raw ÷ 100 Ah | raw → raw ÷ 100 Ah | remaining capacity, 0.01 Ah | verified through word 9 |
| 8 | raw → raw ÷ 100 Ah | raw → raw ÷ 100 Ah | full capacity, 0.01 Ah | verified: the packs' rated capacity; their sum is the bank total on CAN 0x359 |
| 9 | raw → raw ÷ 10 % | raw → raw ÷ 10 % | SOC, 0.1 % | verified: exactly ⌊1000 × remaining ÷ full⌋ on all 120 answers (the pack truncates) |
| 10 | 5241 → 52.41 V | 5237 → 52.37 V | port voltage, 0.01 V? | **assumed**: 0.06 V above the pack voltage while discharging, not explained |
| 11–13 | 0 | 0 | alarms or status? | unknown (raw) |
| 14 | 0x0302 | 0x0302 | ? | unknown (raw) |
| 15 | 0x8010 | 0x8001 | ? (differs per pack) | unknown (raw) |

Not in the summary: per-cell voltages, cycles, SOH, alarms (unless words
11–15 carry them). The slaves' SOC bracketed the bank SOC on CAN. The
master's own SOC is not in any 0x5A answer: read the bank from CAN.

### 8.3 Assembling the bank

1. **Two tap devices**, one per slave: template `seplos_bms_v2_tap`,
   `protocol: seplos_v2` on the port that sits on the inter-pack bus,
   19200 Bd, `mode: listen`, `unit_id` 1 and 2, `stale_after_s: 10`:

   ```yaml
   devices:
     - id: seplos-adr1
       template: seplos_bms_v2_tap
       connection: {protocol: seplos_v2, serial_port: /dev/ttySC0, baudrate: 19200,
                    unit_id: 1, mode: listen, stale_after_s: 10}
     - id: seplos-adr2
       template: seplos_bms_v2_tap
       connection: {protocol: seplos_v2, serial_port: /dev/ttySC0, baudrate: 19200,
                    unit_id: 2, mode: listen, stale_after_s: 10}
   ```

   Both share one reader on the port, which never writes. Saving refuses
   `mode: poll` for this template.
2. **The master** comes from the CAN link: a `pylontech_can_inverter` device
   on the BMS ↔ inverter CAN bus ([can.md](can.md)). It gives the **bank**
   (SOC, voltage, current, limits, the bank's cell and temperature extremes),
   not the master pack alone.
3. **Bank figures** across the three devices: an installation (`endpoints:`)
   does not apply, because it groups the units of one connection and
   `seplos_v2` is not an endpoint protocol. Use calculated fields on one
   device (Devices → the device → Calculated), which may read other devices
   ([templates-advanced.md](templates-advanced.md)). For example, on the CAN
   device: `seplos-adr1.remaining_capacity + seplos-adr2.remaining_capacity`
   for the slaves' stored charge, or
   `min(seplos-adr1.min_cell_voltage, seplos-adr2.min_cell_voltage)` for
   their lowest cell. The CAN device is itself a bank aggregate, not one of
   its packs.
