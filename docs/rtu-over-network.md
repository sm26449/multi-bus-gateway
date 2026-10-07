# RS-485 over the network: bridges

An RS-485 bus rarely sits next to the server that runs the gateway. A
**bridge** is the box that puts the bus on the network. The gateway reads
the devices on the bus through it as if the bus were local.

```
Bridge (one box on the LAN)            "Pi garage"   192.168.1.50
 └─ Bus  (an adapter / a converter port → a TCP port)   ttyUSB0 → :7001  9600 8N1
     ├─ Device  unit 1   energy meter      ← each slave is its own device,
     └─ Device  unit 2   inverter            with its own template
 └─ Bus                                     ttyUSB1 → :7002  19200 8E1
     └─ Device  unit 1   BMS
```

Bridges live on the **Devices** page, next to devices and installations:
**Add bridge**, then add the devices on each of its buses.

- [1. Which kind of bridge](#1-which-kind-of-bridge)
- [2. Our serial bridge on a Raspberry Pi or any Linux host](#2-our-serial-bridge-on-a-raspberry-pi-or-any-linux-host)
- [3. A hardware converter: Waveshare, USR, Elfin…](#3-a-hardware-converter-waveshare-usr-elfin)
- [4. Several devices on one bus](#4-several-devices-on-one-bus)
- [5. Security](#5-security)
- [6. Troubleshooting](#6-troubleshooting)

## 1. Which kind of bridge

| | **MBG serial bridge** | **Converter, transparent** | **Converter, Modbus TCP gateway** |
|---|---|---|---|
| What it is | our container (ser2net) on any Linux host: a Pi, a server | a hardware box in "transparent" / "Protocol: None" mode | a hardware box in "Modbus TCP to RTU" mode |
| Buses | one per USB adapter, found by itself | 1–4 ports, you declare them | 1–4 ports, you declare them |
| Serial settings (baud…) | set from the gateway, per bus | set in the box's web page | set in the box's web page |
| What travels | RTU frames over TCP | RTU frames over TCP | Modbus TCP |
| Clients at once | the gateway | usually one (the gateway) | several, if the box has *multi-host* |
| Diagnostics | full (frames, CRC, timing) | full | partial: the box hides wire errors behind exception 0x0B |
| Listen-only, non-Modbus | possible | possible | Modbus only |

**Rules of thumb:**
- **Transparent** is the default. The gateway sees everything, and the
  converter needs little configuration.
- Use **gateway mode** when other systems must read the same bus *directly*
  and the converter accepts several clients.
- Often best of both: transparent for the gateway, and the others read from
  the gateway, over MQTT or its virtual meters (which re-serve the values as
  standard Modbus TCP). The bus is polled once.

You can change your mind: **Check mode** on a bus asks a slave the same
register both ways and tells you how the box is actually set up.

## 2. Our serial bridge on a Raspberry Pi or any Linux host

1. **Devices → Add bridge → MBG serial bridge.** Give it a name and the
   host's IP, then save. The gateway creates a **token** for it.
2. **Set-up** shows the exact command to run on that host. It is a
   `docker run` with the token inside, or you can use the compose file
   [`serial-bridge/docker-compose.bridge.yml`](../serial-bridge/docker-compose.bridge.yml):
   ```bash
   # on the Pi, in the folder with docker-compose.bridge.yml
   echo "BRIDGE_TOKEN=<the token from Set-up>" > .env
   docker compose -f docker-compose.bridge.yml up -d
   ```
   Any Linux with Docker works (arm64 and amd64 images are published).
3. Plug the USB RS-485 adapters in. The bridge turns **online** on the
   Devices page.
4. **Buses** lists one bus per adapter, each with its stable TCP port
   (kept across replug; serial-less CH340 clones are tied to the physical USB
   socket). Set each bus's **baud / parity / stop bits** there. The change
   applies to that bus only, within a second.
5. On a bus, press **+ Device** and choose the template and unit ID.

**Several Pis** are several bridges. Add each one with its own name, IP and
token. **The bridge next to the gateway** (the `rtu-bridge` profile in the
main compose file, [rtu-serial.md §2](rtu-serial.md#2-over-network--the-serial-bridge))
can be added the same way, with host `mbg-serial-bridge`.

Useful settings on the host (in `.env`):

| Variable | Default | Meaning |
|---|---|---|
| `BRIDGE_TOKEN` | — (required on a remote host) | the token the gateway sends |
| `BRIDGE_PORT_LOW` / `BRIDGE_PORT_HIGH` | 7001 / 7016 | the data ports handed out (one per adapter) — and published |
| `BRIDGE_SERIAL_PARAMS` | `9600n81` | default for a bus with no settings of its own |
| `BRIDGE_EXCLUDE` | — | adapters to never touch (owned by another service on that host) |
| `BRIDGE_PUBLISH_IP` | all interfaces | bind the published ports to one interface |

The control API is described in [serial-bridge-api.md](serial-bridge-api.md).

## 3. A hardware converter: Waveshare, USR, Elfin…

1. **In the converter's web page** (each one has its own):
   - *Work mode*: **TCP Server**; note the port (Waveshare 4196, USR 8899,
     Elfin 8899…).
   - *Protocol*:
     - **None / Transparent** for a transparent bridge;
     - **Modbus TCP to RTU** for a gateway. On a Waveshare, also tick
       **Multi-host** if other systems will read it.
   - *Serial*: the bus's baud rate, parity, data and stop bits. All slaves on
     one bus share them.
   - *Packet interval* (transparent): **≥ 10 ms** or "auto". If it is too
     short, the converter cuts a frame in two.
   - *Response timeout* (gateway): above the slowest slave, e.g. 500 ms.
2. **Devices → Add bridge →** *transparent* or *Modbus TCP gateway*: name,
   IP, and one **bus** per converter port (its TCP port). For a gateway, set
   how many **connections it accepts at once**; the gateway opens that many.
   The form repeats what to set on the box.
3. On a bus, **Check mode** with the unit ID of a slave you know:
   - *It speaks the way this bridge is set up* means you are good.
   - *TRANSPARENT mode* / *GATEWAY mode* means the box is set the other way.
     Change the bridge type, or change the box.
4. **+ Device** on the bus.

> A transparent converter usually serves **one** client. That client is the
> gateway: its devices on that bus share one connection (§4). Any other
> program connecting to it would take the line away.

## 4. Several devices on one bus

RS-485 is a shared bus. Up to 247 slaves can sit on it, each with its own
**unit ID**, and they answer one at a time. In the gateway, each slave is a
**separate device** with its own template, topics and history. They are all
added to the same bus:

- **One connection, taking turns.** Devices on the same bus share a single
  connection to the bridge, or a single open serial port when the bus is
  wired directly into the gateway. Their requests go out one after another.
  A converter that accepts one client is never fought over.
- **The unit ID must differ.** Adding a second device with a unit ID already
  used on that bus is refused, with the name of the device that has it.
- **The line settings must agree.** On a directly attached port, every
  device uses the same baud and parity. Through a bridge, they belong to the
  bus anyway.
- **Test connection** for a new slave on a busy bus goes through that same
  connection, in turn, so it does not disturb the devices already reading.
- **Bus time is shared.** At 9600 baud, reading 40 registers takes about
  100 ms. Ten slaves polled every second fill the bus. Slow the poll groups
  that do not need speed, or raise the baud rate on every slave and on the
  bus.

The same holds for a bus attached **directly** to the gateway's host
(`/dev/ttyUSB…`, [rtu-serial.md §1](rtu-serial.md#1-direct-serial)).

## 5. Security

Modbus RTU has **no authentication**. Whoever can reach a bus port controls
the bus.

- **Firewall** the bridge host's data ports *and* control port to the
  gateway's IP only. For example, on the Pi:
  ```bash
  sudo ufw allow from <gateway-ip> to any port 7000:7016 proto tcp
  sudo ufw deny 7000:7016/tcp
  ```
  Or bind them to a dedicated interface with `BRIDGE_PUBLISH_IP`. Never
  forward them from the Internet.
- **The token protects only the control port** (inventory, serial settings).
  It travels as plain HTTP. Across a network that is not yours (another
  building, a remote site), tunnel it, e.g. with WireGuard.
- **Who sees the token:** the Set-up panel shows it only to an admin when
  login or an API key is on.
- Converters have their own web pages and default passwords. Change them.

## 6. Troubleshooting

| On the Devices page | Likely cause | What to do |
|---|---|---|
| Our bridge **offline**, "no answer from …" | not started, wrong IP, firewall, wrong control port | run the Set-up command on the host; `docker logs mbg-serial-bridge` there |
| **Buses** says 401 / wrong token | the host was started with another token | copy the token from Set-up into the host's `.env` and restart the bridge |
| No bus listed | no adapter plugged in, or it is in `BRIDGE_EXCLUDE` | plug it in; the bridge rescans every 10 s |
| A converter **offline** | none of its devices answers | Check mode on its bus — the verdict says which layer |
| Check mode: *no answer either way* | wrong unit ID, slave off, A/B swapped, serial settings differ from the bus | check the slave's address and wiring; match baud/parity |
| Check mode: *bytes came back but not a valid frame* | baud/parity differ, or the packet interval cuts frames | match the serial settings; packet interval ≥ 10 ms |
| Exception 0x0B in gateway mode | the converter got no answer from the slave in time | unit ID, wiring, raise the converter's response timeout |
| "unit N on host:port is already read by …" | two devices for one slave | each slave on a bus has its own unit ID |
| Values right but slow, timeouts as more slaves are added | the bus is saturated | slower poll groups, higher baud, or split slaves across two buses |
