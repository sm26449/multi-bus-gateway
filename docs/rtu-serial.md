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
2. In **Add device → Modbus RTU**, choose **Direct serial** and fill serial
   port, baud, parity, unit ID.
3. **Test connection** → any protocol-level answer proves the slave is alive.

> One physical serial line = one master. MBG refuses a second RTU device on a
> serial port already in use (see §5, multi-slave).

---

## 2. Over network — the serial bridge

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
  7001–7099 to the LAN. The control API binds to `127.0.0.1`.
- Direct mode gives MBG `/dev` access; bridge mode does not (MBG stays
  unprivileged) — prefer the bridge on shared/exposed hosts.
- RTU has no authentication of its own — treat network reachability to the bus
  as full control of it.

---

## 5. Multi-slave on one bus (current limit)

RS-485 is a shared half-duplex bus: one transaction at a time. MBG currently
creates one client per device, so **one master per serial line / bridge
endpoint**. Two RTU devices on the same line collide; the wizard blocks a second
direct-serial device on a port already in use.

Putting several slave IDs on one bus needs a shared per-endpoint bus lock that
serialises all their transactions — planned, not yet shipped. Until then: one
slave per adapter, or one adapter per bus.

---

## 6. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Scan shows "serial bridge unreachable" | bridge container down, or `SERIAL_BRIDGE_URL` wrong. `docker ps` / `docker logs mbg-serial-bridge`. |
| Adapter missing from Scan | not plugged, or it is in `BRIDGE_EXCLUDE` (shown as unavailable), or claimed by another container. |
| Test connection times out | wrong unit ID, wrong baud (bridge is 9600 8N1), A/B wires swapped, or no termination on a long bus. |
| Values decode wrong (freq/scale off) | template register map / byte order mismatch — RTU vs TCP is *not* the cause; the frames are identical. Check the template. |
| Adapter renumbered after replug (direct mode) | expected — that is exactly what the bridge avoids. Move to bridge mode. |
