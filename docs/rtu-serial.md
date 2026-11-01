# Modbus RTU & the serial bridge

Multi-Bus Gateway (MBG) talks to Modbus **RTU** slaves (RS-485 / RS-232 over a
USB serial adapter) in two ways. Both are first-class; pick per install.

| | **Direct serial** | **Over network (serial bridge)** |
|---|---|---|
| How | map `/dev/ttyUSBx` into the MBG container | a separate `serial-bridge` container owns the adapters; MBG speaks TCP |
| Add an adapter | edit compose + recreate MBG (drops virtual meters briefly) | plug it in → it appears among the bridge's **Buses** |
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
2. In **Add Device → Connection**: **Port: RS-485 / serial → Where the bus
   is: A port on this host** (pick it from the list, §1.1) → **Role on the
   bus: Master** → **Protocol: Modbus RTU** → **Line** → **Address**. On a
   port that already has devices the line is the bus's, inherited and
   locked; on an empty one the template's line is proposed, else 9600 8N1
   (*a common default — check the device*). The address proposed is the
   template's default unit when it is free, else the next free one.
3. **Test connection** → any protocol-level answer proves the slave is alive.
   The next step, **Check**, reads the template's rows once before anything
   is saved.

### 1.1 The port list

The serial port field lists the ports of this host (`GET /api/serial-ports`,
the same enumeration the serial bridge uses). Each port shows its name, its
kind (USB, RS-485 HAT, board UART), its model and one state:

| State | Meaning | Can be picked |
|---|---|---|
| Free | nothing here uses it | yes |
| Polled | an enabled device polls it (named) | yes to poll another unit there; no to listen |
| Listened to | an enabled device only listens on it (named): another master owns the bus | yes to listen too; no to poll |
| In use | another process in this container has it open (named) | no |
| Not mapped | the host has it, the container has no node with its device number; the `devices:` line and the `--device` flag to add are shown | no |
| Unknown | its node is here, but no process could be read to tell whether it is in use | yes |
| Console | the kernel console or a getty | hidden (`?all=1` shows it) |

A port is matched to the container's `/dev` by its device number
(`major:minor`), not by its name: a udev alias node (`/dev/ttyFRONIUS`) or a
renamed mapping (`/dev/ttyUSB1:/dev/ttyUSB0`) is the port it really is, and
the picker stores the path the container has it under — the
`/dev/serial/by-id` link when the container sees one, else its own node,
else the alias.

Only a by-id link or a udev alias survives a replug: a plain `ttyUSBn` name
can change when the adapter is plugged in again. When the container sees no
by-id link (they live on the host, not in a container's `/dev`), map the
adapter by its by-id link or a udev alias (the tip in step 1); a USB port
that is not mapped says so beside its fix line.

**Refresh** reads the list again (after mapping a port and recreating the
container). A path the list cannot see is typed under **Advanced**. The same
list is in the Edit device dialog's *Connection* section (⋯ → **Edit
connection…**), for Modbus RTU, the listen-only tap and Seplos V2. The
picker fills the same `serial_port` string a hand-written config holds. The device being edited is never its own conflict, but
what the port is (not mapped, in use, a console) still counts.

The list is read from `/sys`, `/dev` and `/proc` only (a `stat`, never an
open): no port is opened. A process in another container is not visible from
here, and the gateway's own process is never counted as a holder.

> Several slaves on one line are several devices on the same port, with
> different unit IDs (§5).

---

## 2. Over network — the serial bridge

> **Bridges on other hosts, several of them, and hardware converters
> (Waveshare, USR, Elfin…)** are covered by
> [rtu-over-network.md](rtu-over-network.md): add them on the Devices page
> as **bridges**. This section is the bridge running next to the gateway.

The `serial-bridge` service wraps [ser2net](https://github.com/cminyard/ser2net):
it exposes each USB serial adapter, and each of the board's own UARTs (RS-485
HAT channels), as a **stable internal TCP endpoint** and reloads itself on
hotplug. A small supervisor enumerates the ports, assigns each a persistent
port, and serves a scan API.

### 2.1 Run the bridge

It ships in `docker-compose.yml` as the opt-in `rtu-bridge` profile (it
needs `/dev` access, so it never starts by accident):

```bash
docker compose --profile rtu-bridge up -d
```

The service (see the compose file for the authoritative definition) runs as
container `mbg-serial-bridge` — the name the gateway's default
`SERIAL_BRIDGE_URL=http://mbg-serial-bridge:7000` resolves. Key points:

- `/dev` is bound **read-only** and only exact device majors are allowed
  (`device_cgroup_rules`): ttyUSB, ttyACM, ttyAMA, plus four `.env` slots for
  other board UARTs. The process runs non-root (dialout).
- **RS-485 HAT ports** (`ttySC*` on SC16IS752 HATs, `ttyAMA*`, real `ttyS*`,
  `ttymxc*`, `ttyO*`) are listed next to the USB adapters, keyed by the
  device behind them (`spi0.0:0`, `fe201000.serial`). An SC16IS7xx HAT has a
  dynamic device major, so at first it shows as **no-access** with the
  command that fixes it: `sh scripts/serial-access.sh`, then
  `docker compose --profile rtu-bridge up -d serial-bridge`
  ([install.md §D](install.md#d-the-rtu-serial-bridge)). The kernel console
  and the Bluetooth UART are listed as **reserved** and never served. The
  SoC's own UARTs (`ttyAMA*`, real `ttyS*`…) are listed as **not enabled**
  until you enable them (bridge → Buses → Enable, or `BRIDGE_INCLUDE`): the
  bridge cannot see a program that uses one without locking it.
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

1. Add the bridge once on the Devices page: **Add bridge → MBG serial
   bridge**, host `mbg-serial-bridge`
   ([rtu-over-network.md §2](rtu-over-network.md#2-our-serial-bridge-on-a-raspberry-pi-or-any-linux-host)).
   Its **Buses** list shows one bus per port, as *USB · model (serial) →
   :port* or *Board UART · SC16IS752 ch0 (spi0.0) → :port*. Ports the bridge
   cannot serve are listed with why; a `no-access` port has its fix command,
   with a Copy button.
2. On a bus, **+ Device**: Add Device opens on **Connection** with that bus
   and the next free unit filled in. (From **Add Device** itself: **Port:
   RS-485 / serial → Where the bus is: Through a bridge or converter**, then
   the bus.) This binds the device to that port's stable endpoint.
3. **Test connection**, **Check**, **Name & save**.
4. Unplug the adapter → the bus disappears from the list. Plug it back →
   same endpoint, the device reconnects automatically.

Baud/parity are set on the bridge's bus (default **9600 8N1**), not per
device; **Edit bus line…** changes the bridge port and its devices together
([rtu-over-network.md §4](rtu-over-network.md#4-several-devices-on-one-bus)).

---

## 3. Stable identity ("binds to it for life")

Each port gets a stable key, so its TCP port survives replug/restart:

- **Preferred:** the adapter's unique USB serial (FTDI, CP210x, branded) — e.g.
  `B0045K08`.
- **Fallback (no serial):** cheap CH340 clones report no serial number, so the
  bridge keys them by **physical USB port-path** (e.g. `1a86:7523@1-1`). That is
  stable *as long as you don't move the cable to a different USB port*. If you
  must distinguish two identical serial-less adapters, keep each in its own port
  and label the ports.
- **Board UARTs:** the device behind the port (`/sys/class/tty/<name>/device`),
  plus the channel when the device carries several: `spi0.0:0`, `spi0.0:1` for
  an SC16IS752 HAT, `fe201000.serial` for a SoC UART. Never the tty name.

The map lives in `/data/portmap.json` inside the `serial-bridge-data` volume.

**What keeps a port, and what does not:**

| Event | Result |
|---|---|
| Restart, reboot, image upgrade (state from 3.91 or later) | every port kept |
| `ttyUSB` renumbered, HAT channels renamed by the kernel | ports kept; ser2net follows the new node |
| Adapter unplugged and plugged back into the same socket | same port |
| Adapter **with** a USB serial number into another socket | same port (the id is the adapter) |
| Adapter **without** a serial number (CH340) into another socket | a **new** port: it is a new adapter to the bridge. Its old port stays reserved for the old socket, never handed to another adapter |
| HAT taken out and put back | its ports back; adapters plugged meanwhile take free ports, never the HAT's |
| Port range narrowed | assignments outside the range are dropped, loudly in the log; they never take a port another adapter holds |
| Port map lost or unreadable (volume deleted, restore) | logged loudly; ports may be handed out afresh — the gateway's bus guard (below) stops reading |

### 3.1 The bus guard (the gateway side)

A device reached through our serial bridge records **which adapter** its bus
is: the adapter's stable id, picked in Scan, or learned on the first
successful connect for older configs. It is stored on the bridge's bus entry,
or on the device's own `host:port` connection.

Before every fresh connection to the bridge, the gateway asks the bridge's
`/adapters` which adapter serves that port now:

- **The same one:** it connects.
- **Another one, or none:** the device is **not read** — nor written — and
  the Devices page says so: *"its adapter B0045K08 is now on :7005"* or *"…
  is missing"*. **Follow the adapters** re-points every moved bus of that
  bridge in one go (two swapped buses swap), and the devices restart on the
  right bus.
- **A bus that cannot be checked** (the bridge's control API does not answer
  — a token or firewall problem on the control port fails independently of the
  data ports): it **keeps reading** on its recorded binding. It is shown as a
  warning, *"not verified, still reading"*, logged once per episode, and checked
  again on the next connect. Only positive evidence from a bridge that
  answered (*moved*, *missing*) stops a bus.

A device whose bus was never recorded keeps working as before, and records
the adapter on its first successful check.

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
and on a direct port the baud and parity must agree: a new device on a
port with devices inherits the bus's line, and **Edit bus line…** changes it
for all of them at once (admin; they restart). Several at once: Add Device →
**Many of the same on one bus** scans the port and adds the units chosen.
Details and bus-time budget: [rtu-over-network.md §4](rtu-over-network.md#4-several-devices-on-one-bus).

### 5.1 Modbus ASCII

Some older PLCs and meters speak **Modbus ASCII** instead of RTU: each frame
is readable hex between a `:` and a line end, checked by an LRC instead of a
CRC. The manual says so explicitly ("Modbus ASCII", "7E1", "LRC").

- **Direct serial:** Add Device → **Connection**: a port on this host →
  **Protocol: Modbus ASCII** (`protocol: ascii`). ASCII lines are often 7 data bits with even
  parity: set them as the manual says.
- **Through a bridge:** a kind of bridge whose file declares
  `"framing": "ascii"` gives its devices `ascii-tcp`. Copy
  `bridge_types/rtu_transparent.json`, change `id`, `name` and `framing`.
  Devices on it then speak ASCII over the converter's TCP port.
- All slaves on one line speak the **same** framing. An RTU device on a port
  already read as ASCII, or the reverse, is refused.
- The bus monitor shows ASCII frames as the text they are, and checks the
  LRC.
- Listening (`rtu_tap`) decodes RTU only.

## 6. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Scan shows "serial bridge unreachable" | bridge container down, or `SERIAL_BRIDGE_URL` wrong. `docker ps` / `docker logs mbg-serial-bridge`. |
| Adapter missing from Scan | not plugged, or it is in `BRIDGE_EXCLUDE` (shown as unavailable), or claimed by another container. |
| HAT port shown as *no access* | the container has no device rule for its major (an SC16IS7xx HAT's is dynamic). Run `sh scripts/serial-access.sh`, then the `docker compose … up -d` line it prints. Again after a kernel update. |
| Port shown as *reserved* | it is the kernel console or the Bluetooth UART: never served. Free the console in the boot configuration first if you really mean to use it. |
| Port shown as *in use* | another program holds it exclusively (often a direct mapping into the gateway). One owner per line. A program that does not lock the port is not seen at all. |
| Port shown as *not enabled* | a UART of the SoC itself: enable it in the bridge's Buses dialog (or `BRIDGE_INCLUDE`) once you know nothing else uses it. |
| Bridge shown as *degraded*: "device rule … out of date" | a kernel update gave the RS-485 HAT another device number. Run `sh scripts/serial-access.sh` again and recreate the bridge. |
| HAT port served, but slaves never answer | the HAT's RS-485 chip may need the kernel to drive its direction: tick **RS-485** for that port in the bridge's Buses dialog (`rs485` in the API). |
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

A Seplos **V2** bank is not Modbus: its packs speak an ASCII protocol, which
the `seplos_v2` driver reads (and listens to, with `mode: listen`) — see
[seplos-v2.md](seplos-v2.md).

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
- **The bus can be elsewhere.** A tap can listen through a transparent
  bridge or our serial bridge on another host:
  `connection: {protocol: rtu_tap, bridge: pi-garage, bridge_port: 7001, unit_id: 2}`.
  See [rtu-over-network.md §5.1](rtu-over-network.md#51-listening-to-a-bus-through-a-bridge).

### 7.1 From the UI

**Devices → Add Device → Connection**: **Port: RS-485 / serial** → *where
the bus is* (a serial port on this host, or a bus of a transparent bridge) →
**Role on the bus: Listen-only** → the line (for a local port) and the
observed unit ID. A template that only listens pre-fills Listen-only. There
is no *Test connection* (a tap never transmits, so there is nothing to ask);
the **Check** step joins the bus's listener instead and says *heard n of m
groups*. Once saved, **Test** on
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
  active master on a tapped bus would collide with the real one). A tty, its
  `/dev/serial/by-id` link and a udev alias are the same line.
- While an enabled device listens on a bus, nothing that would transmit
  there runs: Test, the unit sweeps, the SunSpec walk, a bridge's Scan and
  Check mode answer 409 with the listening devices named, and send nothing.
  Disable the listener first.
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
