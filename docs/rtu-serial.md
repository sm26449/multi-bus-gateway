# Modbus RTU & the serial bridge

Multi-Bus Gateway (MBG) talks to Modbus **RTU** slaves (RS-485 / RS-232 over a
USB serial adapter) in two ways. Both are first-class; pick per install.

| | **Direct serial** | **Over network (serial bridge)** |
|---|---|---|
| How | map `/dev/ttyUSBx` into the MBG container | a separate `serial-bridge` container owns the adapters; MBG speaks TCP |
| Add an adapter | edit compose + recreate MBG (drops virtual meters briefly) | plug it in → **Scan** in the wizard → it appears |
| MBG needs `/dev` | yes | **no** (stays unprivileged) |
| Addressing | `/dev/ttyUSBn` (renumbers on replug) | a stable TCP endpoint per adapter, kept across replug |
| Best for | one fixed adapter, simplest setup | several adapters, hot-swap, keeping the ESS/virtual meters undisturbed |

The protocol on the wire is identical (Modbus RTU framing). "Over network" just
tunnels those RTU frames across a raw TCP socket (RTU-over-TCP), so the adapter
can live in its own container.

---

## 1. Direct serial

1. Map the adapter into the MBG container in `docker-compose`:
   ```yaml
   multi-bus-gateway:
     devices:
       - "/dev/ttyUSB1:/dev/ttyUSB1"   # your RS-485 adapter — NOT one already
                                       # owned by another service (e.g. a BMS)
   ```
   Recreate the container once (`docker compose up -d multi-bus-gateway`).
   Tip — stable addressing despite replug renumbering: map the
   `/dev/serial/by-id/...` symlink to a fixed container path and configure
   THAT path in MBG:
   ```yaml
     devices:
       - "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_XXXX-if00-port0:/dev/ttyMETER"
   ```
2. In **Add device → Modbus RTU**, choose **Direct serial** and fill serial
   port, baud, parity, unit ID.
3. **Test connection** → any protocol-level answer proves the slave is alive.

> Several slaves on one line are several devices on the same port, with
> different unit IDs (§5).

---

## 2. Over network — the serial bridge

> **Bridges on other hosts, several of them, and hardware converters
> (Waveshare, USR, Elfin…)** are covered by
> [rtu-over-network.md](rtu-over-network.md): add them on the Devices page
> as **bridges**. This section is the bridge running next to the gateway.

The `serial-bridge` service wraps [ser2net](https://github.com/cminyard/ser2net):
it exposes each USB serial adapter as a **stable internal TCP endpoint** and
reloads itself on hotplug. A small supervisor enumerates adapters, assigns each
a persistent port, and serves a scan API.

### 2.1 Run the bridge

It ships in `docker-compose.yml` as the opt-in `rtu-bridge` profile (it
needs `/dev` access, so it never starts by accident):

```bash
docker compose --profile rtu-bridge up -d
```

The service (see the compose file for the authoritative definition) runs as
container `mbg-serial-bridge` — the name the gateway's default
`SERIAL_BRIDGE_URL=http://mbg-serial-bridge:7000` resolves. Key points:

- `/dev` is bound **read-only** and only ttyUSB/ttyACM device nodes are
  allowed (`device_cgroup_rules`); the process runs non-root (dialout).
- `BRIDGE_EXCLUDE` (in `.env`) lists adapters to NEVER expose — claimed by
  another service. Comma-separated; each token matches a stable id, /dev
  path, or USB port-path (e.g. `1a86:7523@1-1` for a BMS adapter).
- The persistent port map lives in the `serial-bridge-data` volume.
- The control API is published on localhost only (`127.0.0.1:7000`).

- **Data ports (7001–7099) are internal-only.** They are *not* published to the
  LAN — anyone who can reach them speaks raw Modbus to your bus. MBG reaches the
  bridge over the Docker network by name (`mbg-serial-bridge`).
- **Exclusion is mandatory** for any adapter another container already owns.
  Excluded adapters are still *listed* (so you see them) but the bridge never
  opens their serial line. Cross-container "in-use" detection does not work
  (separate PID namespaces), so exclude explicitly.

### 2.2 Add a device over the bridge

1. **Add device → Modbus RTU → Over network (auto-detect)**.
2. Press **Scan**. Available adapters appear as *model (serial) → :port*. Pick
   one (a lone adapter auto-selects). This binds the device to that adapter's
   stable endpoint (`host = mbg-serial-bridge`, `port = <tcp_port>`).
3. Set the unit ID, **Test connection**, pick a template, save.
4. Unplug the adapter and **Scan** again → it disappears. Plug it back → same
   endpoint, the device reconnects automatically.

Baud/parity are set on the bridge (default **9600 8N1**), not per device.

---

## 3. Stable identity ("binds to it for life")

Each adapter gets a stable key, so its TCP port survives replug/restart:

- **Preferred:** the adapter's unique USB serial (FTDI, CP210x, branded) — e.g.
  `B0045K08`.
- **Fallback (no serial):** cheap CH340 clones report no serial number, so the
  bridge keys them by **physical USB port-path** (e.g. `1a86:7523@1-1`). That is
  stable *as long as you don't move the cable to a different USB port*. If you
  must distinguish two identical serial-less adapters, keep each in its own port
  and label the ports.

The map lives in `/data/portmap.json` inside the `serial-bridge-data` volume.

---

## 4. Security

- Bridge data ports bind to the **internal Docker network only**; never publish
  7001–7099 to the LAN. The control API is published on the host's
  `127.0.0.1:7000` only (inside the container it listens on all interfaces of
  the internal network — compose does the host-local binding).
- Direct mode gives MBG `/dev` access; bridge mode does not (MBG stays
  unprivileged) — prefer the bridge on shared/exposed hosts.
- RTU has no authentication of its own — treat network reachability to the bus
  as full control of it.

---

## 5. Several slaves on one bus

Supported since 3.89. Each slave is its own device on the same serial port,
or the same bridge port, with its own unit ID. They share one open line, or
one connection to the bridge, and take turns on it. The unit ID must differ,
and on a direct port the baud and parity must agree. Details and bus-time
budget: [rtu-over-network.md §4](rtu-over-network.md#4-several-devices-on-one-bus).

## 6. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Scan shows "serial bridge unreachable" | bridge container down, or `SERIAL_BRIDGE_URL` wrong. `docker ps` / `docker logs mbg-serial-bridge`. |
| Adapter missing from Scan | not plugged, or it is in `BRIDGE_EXCLUDE` (shown as unavailable), or claimed by another container. |
| Test connection times out | wrong unit ID, wrong baud (bridge is 9600 8N1), A/B wires swapped, or no termination on a long bus. |
| Values decode wrong (freq/scale off) | template register map / byte order mismatch — RTU vs TCP is *not* the cause; the frames are identical. Check the template. |
| Adapter renumbered after replug (direct mode) | expected — that is exactly what the bridge avoids. Move to bridge mode. |

## 7. Listen-only tap (`protocol: rtu_tap`)

A Modbus RTU bus has exactly one master. When the bus you care about already
has one — a BMS master pack polling its slave packs, a vendor datalogger
polling its meters, a PLC owning its drives — a second active poller corrupts
frames for both. The **tap** makes the gateway a silent observer instead: it
opens the serial port, **never transmits a byte**, reassembles the frames the
existing exchange produces (CRC-16 framing, §7.3), pairs requests
with responses, and feeds the decoded values through the exact same
correction/store/publish pipeline a polled device uses.

```yaml
devices:
  - id: tapped-meter
    name: Meter behind someone else's master
    template: eastron_sdm630          # byte order + register map, as usual
    connection:
      protocol: rtu_tap
      serial_port: /dev/serial/by-id/usb-...   # your OWN adapter on the bus
      baudrate: 9600
      unit_id: 2                      # the observed slave's address
      stale_after_s: 60               # "stale" = the master stopped asking
```

Notes:

- **One tap adapter per bus**, wired A/B in parallel like any RS485 node; the
  port cannot be shared with another process. Several devices may observe
  different unit IDs on the same port — they share one reader automatically.
- You only see what the existing master asks for, at the rhythm it asks.
  There are no retries and no polling intervals; `stale` means the master
  went quiet, not that the gateway failed.
- FC 03/04 reads are decoded from request→response pairs. FC 06/16 **writes
  are decoded too** (from the request payload) — watching a master push
  setpoints is half the reason to tap a bus while debugging.
- Exception responses, CRC errors and orphan frames are counted in the
  device's stats; the last ~200 decoded frames (direction, FC, address,
  window) are kept in memory as a debug trace.
- Tap devices are read-only by nature: no write face, no commands.

### 7.1 From the UI

**Devices → Add Device → Modbus RTU → Listen only (tap)**: serial port, baud,
parity and the observed unit ID — there is no *Test connection* in the wizard
(a tap never transmits, so there is nothing to ask). Once saved, **Test** on
the device page reports what the tap has *heard*, naming the layer that is
silent: the port could not be opened (permissions, missing adapter) → the
port is open but no valid frame arrived (master off, A/B swapped, wrong
baud/parity) → the bus is live but nothing for this unit yet → *hearing
unit N — W windows, last X s ago*. A tap is edited like any device; its
protocol is fixed after creation.

For a whole bank behind one master (eight battery packs, a row of meters),
declare an **endpoint** with `connection.protocol: rtu_tap` instead — one
unit per observed slave, one shared reader, and the endpoint's totals
(MANUAL §5b, config-reference `endpoints:`).

### 7.2 Line rules (enforced when saving)

- A serial line is either **polled or tapped, never both**: a device with
  `protocol: rtu` and a tap cannot share a port (the tty opens once, and an
  active master on a tapped bus would collide with the real one).
- Several taps **may** share a port — one reader fans the frames out by unit
  ID — but only with the **same baud and parity**: the reader opens the line
  once, with the first tap's settings.

### 7.3 What the wire taught us (real bus, 2026-10)

- **Framing is CRC-driven, not timing-driven.** The serial layer hands over
  20–50 ms chunks, so every chunk boundary looks like a 3.5-character gap.
  The tap extracts frames by trying the candidate lengths for the head byte
  and accepting the one whose CRC validates; a head that validates at no
  length is junk and the stream slides one byte. The `resync` counter in the
  log (`… frames (crc_err 0, orphans 0, …, resync N B)`) is the bytes skipped
  that way — on a healthy bus it grows steadily (bytes the framer does not
  recognise), what matters is `crc_err` and `orphans` staying at 0.
- **A bus master answers no requests.** On a Seplos bank the master pack
  emits its own blocks unsolicited. Every paired exchange teaches the tap
  which address a response of that shape (function code, byte count) belongs
  to, so an unpaired response is dispatched at the learned address; a shape
  seen at two addresses becomes ambiguous and is never inferred.
- **FC 01 (coils)** are decoded too, in bit-address space: one `uint16` of a
  register map holds 16 coils, LSB first.
- After a failed open (adapter missing, permissions), the next successful
  open clears the error — a tap that recovers reports healthy again.
