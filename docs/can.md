# CAN bus (`protocol: can`) — listen-only

The gateway reads a CAN bus by **listening**: frames arrive, the template's
signal rows decode them, and the values reach MQTT, InfluxDB, Home Assistant,
the UI and the rules like any other device's. It never transmits — there is
no code that could. The first map is the **Pylontech CAN** link between a
battery's BMS and its inverter (`pylontech_can_inverter`).

This page is the how-to; the manual's short version is
[MANUAL.md §5.5](MANUAL.md#55-can-listen-only).

## 1. What you need

- A CAN interface the gateway **process** can see: `can0` from a USB adapter
  (CANable / candleLight, gs_usb), a HAT (MCP2515, e.g. the Waveshare RS485
  CAN HAT), or `vcan0` for tests. SocketCAN is in the Linux kernel and in
  Python's standard library — nothing to install in the gateway.
- The interface set up **by the host**, listen-only, at the bus's bitrate:

  ```sh
  ip link set can0 down
  ip link set can0 type can bitrate 500000 listen-only on
  ip link set can0 up
  ```

  `listen-only on` makes the controller itself silent (no ACK, no error
  frames): a second guarantee beside the driver having no transmit path.
  It is **set by the operator**, not by the gateway. The driver reads the
  flag from the kernel each time it opens the interface: `listen_only` in
  its status (`true`, `false`, or `null` for an interface without one, such
  as `vcan`), and a warning in the log when it is `false`. Check it before
  connecting to a live link: `ip -details link show can0 | grep LISTEN-ONLY`.
- **Docker:** the container needs host networking to see `can0` (§5). A
  carrier through the serial bridge is listed in the form but not available
  yet.

## 2. The device

```yaml
devices:
  - id: bms-can
    name: Battery (CAN)
    template: pylontech_can_inverter
    connection:
      protocol: can
      carrier: socketcan        # the only carrier in this release
      channel: can0             # the interface
      bitrate: 500000           # what the bus runs at — shown, set by the host (§1)
      stale_after_s: 5          # quiet this long = stale (default 30)
```

Event-driven: there is no polling. A value's time is the moment its frame
arrived; `stale_after_s` is the only freshness notion (Pylontech sends every
second, so 5 s is a quiet bus and 30 s a dead one). Poll groups in the
template are informational. A CAN device cannot be written: writes answer
"no source supports writing", and a standby gateway keeps listening (it only
listens).

The interface may be missing at start, go down or be unplugged: the driver
says so once, keeps trying every 3 s, and resumes on its own.

**Status counters.** The device list shows `successful_reads` and
`failed_reads` — both count **frames** the template knows: a frame any of
whose rows could not be decoded is one failed read. The device's log
(`GET /api/devices/<id>/events` → `counters.error_counts` and `sources[]`)
adds `unknown_frames`, `decode_errors`, `error_frames`. The driver's own
`get_stats()['can']` block has them all:

| Counter | Means |
|---|---|
| `frames` | data frames received |
| `matched_frames` | frames on an id the template describes (`successful_reads` + `failed_reads`) |
| `failed_frames` (`failed_reads`) | frames a row could not be decoded from |
| `unknown_frames`, `unknown_ids` | frames on other ids, per id (`0x123`, `0x18FF50E5x` for 29-bit), first 64 ids |
| `decode_errors` | rows cut by their frame's end (the row starts inside the frame, ends past it) |
| `absent_signals` | rows wholly past a shorter frame's end — the BMS does not send them (a genuine Pylontech's 7-byte 0x359, 2-byte 0x35C); not an error |
| `listen_only` | the interface's own listen-only flag, read at each open |
| `error_frames`, `remote_frames` | controller error reports (bus-off, error counters); remote requests |
| `reopens`, `open_error` | the interface was opened again; why it last failed |

### 2a. Adding it from the UI

**Devices → Add Device** → **Connection** → port **CAN** (or pick a CAN
model first: its port is CAN). The step lists the CAN interfaces of this
host with up/down, bitrate and listen-only. **Listen-only is a hard gate**:
an interface that is down or can transmit cannot be picked; the step shows
the exact command to run on the host (§1), then **Refresh**.

| Field | Default | Note |
|---|---|---|
| Carrier | SocketCAN | the serial-bridge carrier is listed, not selectable yet |
| Interface | `can0` | as the host names it (`can0`, `can1`, `vcan0`) |
| Bitrate | 500 kbit/s | **set by the host, shown only** — the gateway never changes the interface; 10000–1000000 |
| Stale after (s) | 5 | 1–3600 (30 when the key is absent from `config.yaml`) |

**Test connection** works before saving, and reports:

- the interface **missing** — `no CAN interface 'can0' here …` (the answer
  in a container without host networking);
- **down** — with the `ip link` line that brings it up;
- **up**, its **listen-only** flag (on / NOT listen-only, with the fix /
  unknown for `vcan`), and the **frames heard in 2 s** on how many ids —
  on the carrier's own receive-only socket, never a transmit. A saved
  device's Test adds what its running driver has counted.

The model step offers the templates whose transport is `can`, and only
those; **Check** and **Name & save** are as for any device. Later, ⋯ →
**Edit connection…** opens the same form in the Edit device dialog (a saved
CAN device stays CAN; its Model list shows CAN maps only), and Save sends
only what changed.

The device list and the Overview show the CAN counters beside the last
value — frames, unknown frames (and on how many ids), decode errors, and a
*listen-only* / *not listen-only* badge; the Status page's device table
shows them under its read fails. They are `can` in `GET /api/devices` and
`GET /api/status` (a summary of the driver's `get_stats()['can']`).

## 3. Template rows for CAN

A CAN row is an ordinary row with five more fields — DBC's words:

| Field | Meaning |
|---|---|
| `can_id` | the frame: `849` or `"0x351"`; several rows per id is normal |
| `can_id_extended` | `true` for a 29-bit id (required for ids ≥ 0x800) |
| `start_bit` | DBC start bit: Intel (`little`) the signal's LSB; Motorola (`big`) its MSB in DBC's numbering |
| `bit_length` | 1..64 |
| `byte_order` | `little` (DBC Intel, `@1`) or `big` (DBC Motorola, `@0`); required over 8 bits |

The rest is what every row has: `data_type` gives signedness and container
(`uint16` holds up to 16 bits, `int16`/`int32`/`int64` are sign-extended at
`bit_length`, `float32`/`double` need exactly 32/64 bits, `string:N` is N×2
ASCII bytes in wire order — **strings are little-endian only**, `start_bit`
names their first byte × 8); `scale` divides (DBC's `factor` is
`1/scale`), `offset` is added after; `enum`, `bits`, `mask` decode as usual.
`address` stays synthetic (as on MQTT rows) and keys the value store.

The template declares `protocol.transports: [can]` (and may give
`protocol.bitrate`). Saving checks: the bits fit in 8 bytes, an id over
0x7FF says it is extended, the type can hold the length, every row of a CAN
template names its frame, no CAN fields on a Modbus/MQTT template. Two rows
on the same bits of one frame are a **warning** (template load warnings),
not an error — some protocols overlap on purpose.

Not in this release: CAN FD (over 8 bytes), multiplexed signals, signals
across two frames, the CAN columns in the template editor (edit the JSON or
import), a DBC importer.

## 4. `pylontech_can_inverter`

Sources: Pylontech "PYLON CANBUS Protocol" V1.2 (2018); Seplos "CANBUS
Protocol V1.0" (2020); LuxPower "Battery communication CAN Protocol" V04
(2024); cross-checked, no code taken, against esphome-yambms (Sleeper85,
GPLv3, `packages/yambms/yambms_canbus.yaml`); verified on a 2-minute capture
of a Deye SG05LP1 ↔ Seplos V2 bank (`tests/fixtures/can/`). Rows from one
vendor's extension say **UNVERIFIED** in their description; three of them
(`total_capacity`, `online_packs`, `cycles`), which a genuine Pylontech
fills otherwise or not at all, are **not selected** on a new device — add
them from the catalog on a BMS that sends them.

The capture, decoded (first frame set; the whole file has 0 unknown ids and
0 decode errors):

| Frame | Row | Bytes → value | Plausible? |
|---|---|---|---|
| 0x305 | `inverter_keepalive` | 8 × 00 → 0 | yes — the Deye's 1 s reply |
| 0x351 | `charge_voltage_limit` | 0x0240 → 57.6 V | yes (16S LFP, 3.6 V/cell) |
| 0x351 | `charge_current_limit` | 0x0BB8 → 300.0 A | yes for 3 packs × 100 A |
| 0x351 | `discharge_current_limit` | 0x16DA → 585.0 A | high but consistent: 3 × 195 A |
| 0x351 | `discharge_voltage_limit` | 0x01D0 → 46.4 V | yes (2.9 V/cell) |
| 0x355 | `soc` / `soh` | 29 % / 100 % | yes |
| 0x356 | `pack_voltage` | 0x1482 → 52.50 V (52.49 in some sets) | yes: 16 × ~3.28 V |
| 0x356 | `battery_current` | 0x002D → +4.5 A (4.2–4.5 over 2 min) | yes, charging |
| 0x356 | `battery_temperature` | 0x00F5 → 24.5 °C | yes; equals 0x370's max |
| 0x359 | `protection_flags`, `alarm_flags` | 0 → none | yes |
| 0x359 | `module_count` | 3 | yes |
| 0x359 | `total_capacity` (LuxPower) | the bank's total, in Ah | yes: the sum of the packs' rated capacity |
| 0x359 | `online_packs` (Seplos) | 0x07 → packs 0, 1, 2 | yes |
| 0x35C | `charge_enable` / `discharge_enable` | bits 7, 6 → 1 / 1 | yes |
| 0x35C | force charge I/II, full charge | 0 / 0 / 0 | yes |
| 0x35C | `cycles` (LuxPower) | 0x0136 → 310 | plausible |
| 0x35E | `manufacturer` | "SHEnergy" | yes — Seplos's id (Deye recognises it) |
| 0x370 | `max_cell_temp` / `min_cell_temp` | 24.5 / 22.7 °C | yes |
| 0x370 | `max_cell_voltage` / `min_cell_voltage` | 3.289 (3.288) / 3.252 V | yes; 37 mV spread |
| 0x371 | cell ids max T / min T / max V / min V | 2 / 3 / 3 / 3 | odd: one cell both max and min V? see below |

What does not match a document:

- **0x355 bytes 4–7** carry the max/min cell voltage (D90C B40C = 3289 /
  3252 mV, the same as 0x370), as LuxPower defines them. Victron's protocol
  puts a 0.01 % SOC there and the Pylontech/Seplos documents leave them empty
  — a "high-resolution SOC" row would read 32.89 % beside a 29 % SOC. No row
  for them; the cell values come from 0x370.
- **0x356 bytes 6–7** = 22.7 °C, the min cell temperature (LuxPower); empty
  in Pylontech. No row (0x370 has it).
- **0x359 bytes 5–6** are 0x48 0x03 (capacity, LuxPower), not Pylontech's
  "PN" (0x50 0x4E). On a real Pylontech `total_capacity` would read 20048.
- **0x370 voltages**: the Seplos document says "unit 0.1 °C" for the cell
  voltages — a copy error; the frames are millivolts.
- **0x371**: the max-voltage and min-voltage cell ids are both 3 (and the
  ids may be "pack·cell" or 0-based); verify on your bank against the
  inter-pack map before relying on them.
- **0x35C bits 4/5**: Pylontech and Seplos say bit 5 = force charge I, bit 4
  = II; LuxPower swaps the names. The template follows Pylontech.
- esphome-yambms writes charge over-current at byte 1 bit 7 of 0x359
  (and the warning at byte 3 bit 7); Pylontech, Seplos and LuxPower all say
  bit 0. The template follows the documents.

## 5. Running it where the bus is

Bare metal: run the gateway on the host that has `can0` (a reading socket
needs no privilege).

In Docker, a container sees the host's CAN interfaces only with host
networking (Compose: `network_mode: host`; `docker run --network host`). No
capability is needed to read. Host networking means the container's ports are
the host's ports: published `ports:` are ignored, and the UI, the virtual
meters, :502 and OPC UA bind directly on the host — check for port clashes.
The container stays on the host network for everything else too. Listen-only
is set on the host (`ip link … listen-only on`).

## 6. Tests

- `tests/test_can_signal.py` — the decoder on captured bytes and on
  cantools-decoded vectors (Intel and Motorola), the template checks.
- `tests/test_can_client.py` — the driver with a fake carrier: the whole
  capture through `pylontech_can_inverter`, counters, freshness, reconnects,
  no transmit path, config → client.
- `tools/e2e/can_vcan/run.sh` — the same capture replayed on a real `vcan0`
  in a throwaway privileged container (`--network none`), through the real
  `socketcan` carrier; it also counts the frames on the bus to prove the
  gateway put none there, and takes the interface down and up mid-replay.
