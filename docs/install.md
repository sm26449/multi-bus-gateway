# Installing

Every way to get a Multi-Bus Gateway running, end to end — from the one-file
Compose stack most people want, through `docker run`, the RTU serial bridge,
a Raspberry Pi, bare metal with systemd, a reverse proxy and an existing
Docker network — and how to prove it works before you add the first device.
Day-2 work (upgrades, backups, logs, uninstall) is in
[operations.md](operations.md); when something misbehaves, see
[troubleshooting.md](troubleshooting.md).

## Prerequisites

- **Docker + Docker Compose v2** (`docker compose …`, not `docker-compose`),
  amd64 or arm64. Paths F (bare metal) needs Python 3.11 instead.
- **Ports on the host** (all remappable from `.env`):

  | Port | What | Variable |
  |---|---|---|
  | `8080` | Web UI + API (`/health`, `/metrics`, `/ws`) | `UI_PORT` |
  | `1502–1512` | virtual-meter range — each instance takes a port inside it | `VMETER_PORT_START` / `VMETER_PORT_END` |
  | `502` | standard Modbus, for consumers that insist on it (e.g. a Fronius DataManager) | `MODBUS_502_HOST_PORT` — or remove the line |
  | `1883` / `9001` | bundled broker (MQTT / WebSockets) | `MQTT_BROKER_PORT` / `MQTT_WS_PORT` |
  | `4000`, `8086`, `3000` | MQTT Explorer, InfluxDB, Grafana | `MQTT_EXPLORER_PORT`, `INFLUXDB_HOST_PORT`, `GRAFANA_HOST_PORT` |

- **An MQTT broker.** The stack bundles one (`mosquitto`, the gateway's
  default broker host); with plain `docker run` you point the gateway at a
  broker you already have.
- At least one southbound source to read (Modbus TCP/RTU, HTTP/JSON or
  MQTT) — see [MANUAL.md §1](MANUAL.md#1-what-you-need).

---

## A. Compose with the published image (default)

The compose file names the published multi-arch image
(`ghcr.io/sm26449/multi-bus-gateway:${MBG_VERSION:-latest}`), so nothing is
built locally.

```bash
# 1) Get the files — a clone, or just the two files you need
git clone https://github.com/sm26449/multi-bus-gateway.git && cd multi-bus-gateway
#    (minimal: docker-compose.yml + .env.example in an empty directory;
#     add mosquitto/config/mosquitto.conf, which the bundled broker mounts)

# 2) Environment — uncomment and set the four REQUIRED secrets (the broker refuses a placeholder), optionally pin a release
cp .env.example .env
echo 'MBG_VERSION=3.92' >> .env        # a version (3.92.1), a minor line (3.92) or latest

# 3) Pull and start the COMPLETE stack
docker compose pull
docker compose up -d

# 4) The admin password — generated on the first boot, printed ONCE
docker compose logs multi-bus-gateway | grep -A3 'FIRST RUN'
```

The banner in the log reads `FIRST RUN — generated admin credentials (shown
ONLY once):` followed by `username: admin` and the password. It appears only
when no `config.yaml` exists yet — the gateway writes one with login enabled
and the password stored hashed. Open `http://<host>:8080`, log in, and change
the password under **Config → Security**.

**Where things live.** `./config` on the host is bind-mounted to
`/app/config` in the container: `config.yaml` (globals + devices),
`selected_registers.json` and `devices/<id>/selected_registers.json`
(register selections), `device_templates/` (your own device maps),
`templates/` (virtual-meter templates), `virtual_meters.yaml`, `snapshots/`,
`audit.jsonl`, `events.jsonl`, `sessions.json`, `passkeys.json`. The
entrypoint starts as root only to `chown` this directory to the app user
(uid 10001) and then drops privileges — no manual ownership fix-up.

**The bundled services** (only MQTT Explorer sits behind a profile):

| Service | Container | Purpose | Data |
|---|---|---|---|
| `mosquitto` | `mbg-mosquitto` | MQTT broker — requires `MQTT_USERNAME`/`MQTT_PASSWORD` from `.env` (3.81.0); every LAN consumer logs in with that pair | `mosquitto-data` (retained messages + the generated password file) |
| `mqtt-explorer` | `mbg-mqtt-explorer` | web view of every topic, `127.0.0.1:4000`, only with `--profile debug` (it has no login of its own) | `mqtt-explorer-config` |
| `influxdb` | `mbg-influxdb` | history/energy store; self-configures on first boot (org/bucket `multibus`) | `influxdb-data`, `influxdb-config` |
| `grafana` | `mbg-grafana` | dashboards, `:3000`, login `admin` / `GF_SECURITY_ADMIN_PASSWORD` | `grafana-data` |

Two more things the compose does for the gateway: the per-container sysctl
`net.ipv4.ip_unprivileged_port_start=0` (so the non-root process can bind
`:502`), and `cap_drop: ALL` plus the five capabilities the entrypoint's root
phase needs, with `no-new-privileges` (3.80.0).

**Running less.** Name the services you want:

```bash
docker compose up -d multi-bus-gateway mosquitto     # the core
docker compose up -d multi-bus-gateway               # gateway only — you have a broker
```

`up -d multi-bus-gateway` starts exactly the gateway (there is no
`depends_on`; the boot network-probe waits for the broker). To make the
choice stick across a plain `docker compose up -d`, put the extras behind a
profile in a local `docker-compose.override.yml` (Compose merges it
automatically):

```yaml
services:
  grafana:       { profiles: ["extras"] }
  influxdb:      { profiles: ["extras"] }
  mqtt-explorer: { profiles: ["extras"] }
```

They then start only with `docker compose --profile extras up -d`. Deleting
the service blocks from your copy of `docker-compose.yml` works too.

**ESP boards** (flashing from the browser, the node list) are available in
**MBG Pro**, which brings its own ESPHome build engine. Run it behind the same host name (e.g. under `/nodes`, with the
proxy asking `/api/auth/forward` so the gateway's login covers it) and set
`ui.nodes_url` (or a `modules:` entry) to list it in Settings → Modules.

**Connection settings** (Modbus, MQTT, InfluxDB) are made in the UI and
persisted to `config/config.yaml`, applied without a restart. The `.env`
variables for them are optional pre-seeds: an env value **overrides** the UI
on every start and shows as *locked by environment* — and the matching
`environment:` lines in `docker-compose.yml` ship commented out, so a `.env`
value reaches the container only after you uncomment them.

---

## B. Compose building from source

Same files, same layout — only the image comes from your checkout:

```bash
git clone https://github.com/sm26449/multi-bus-gateway.git && cd multi-bus-gateway
cp .env.example .env
docker compose build            # honours the build: key; MBG_VERSION is ignored
docker compose up -d
docker compose logs multi-bus-gateway | grep -A3 'FIRST RUN'
```

The image installs from `requirements.lock` with `--require-hashes` and bakes
`config/`, `ui/`, `multibus/` and `docs/modbus_data.json`. For live frontend
editing without rebuilds, copy `docker-compose.override.yml.example` to
`docker-compose.override.yml` (it mounts `./ui` read-only over `/app/ui`;
Python changes still need a rebuild).

---

## C. Plain `docker run` (gateway only)

```bash
mkdir -p config
docker run -d --name multi-bus-gateway --restart unless-stopped \
  -p 8080:8080 -p 1502-1512:1502-1512 -p 502:502 \
  --sysctl net.ipv4.ip_unprivileged_port_start=0 \
  --env-file .env -v "$PWD/config:/app/config" \
  ghcr.io/sm26449/multi-bus-gateway:3.92
docker logs multi-bus-gateway | grep -A3 'FIRST RUN'
```

This starts **only the gateway** — no broker, no InfluxDB. Point the MQTT
sink at an existing broker from **Config → MQTT** (or pre-seed `MQTT_BROKER`
in the `.env` you pass; with `--env-file` every variable reaches the
container directly). Don't need `:502`? Drop `-p 502:502` and the `--sysctl`
together. Starting with `--user` skips the entrypoint's `chown`, so the
`config` directory must then already belong to that user.

---

## D. The RTU serial bridge

For Modbus RTU over a USB adapter or an RS-485 HAT the recommended path is
the bundled `serial-bridge` service (ser2net + a supervisor): it owns the
serial ports, exposes each as a stable TCP endpoint on the internal Docker
network, and the gateway stays unprivileged. It is an opt-in profile because it needs
`/dev`:

```bash
docker compose --profile rtu-bridge up -d
```

- **Permissions.** `/dev` is mounted read-only and `device_cgroup_rules`
  allow exact device majors only: ttyUSB (188), ttyACM (166), the Pi's own
  UARTs ttyAMA (204), plus four slots for a board's other UARTs that
  `scripts/serial-access.sh` fills (below). The process runs as uid 10002 in
  `dialout`. No udev rule is required on the host for a standard USB adapter;
  if you add your own rules, keep the nodes group `dialout`-readable.
- **RS-485 HAT ports appear in Scan.** The bridge lists the board's own
  UARTs next to the USB adapters: `ttySC0`/`ttySC1` on SC16IS752 HATs such as
  the Waveshare RS485 CAN HAT (B), `ttyAMA*`, real `ttyS*`, `ttymxc*`,
  `ttyO*`, each keyed by the device behind it (`spi0.0:0`), so the key
  survives a reboot. Every port has a status:
  - `ok`: it can be picked;
  - `no-access`: the container may not open it yet. An SC16IS7xx HAT gets
    a *dynamic* device major at boot (509 on a Pi 5), which compose cannot
    know. Run, in the directory of `docker-compose.yml`:

    ```bash
    sh scripts/serial-access.sh        # writes BRIDGE_TTY_1..4 into .env
    docker compose --profile rtu-bridge up -d serial-bridge
    ```

    The script allows exactly the majors of the UARTs present (a real
    `ttyS` by its exact minor, since major 4 also holds the virtual
    consoles), skipping consoles and Bluetooth UARTs. It refuses anything
    that is not a tty and never writes a wildcard. It also records which
    driver each slot is for, so after a kernel update that moves the HAT to
    another major the bridge warns that the slot is stale (shown on the
    bridge in the gateway). Run the script again then, or after a new HAT;
    the Scan shows the command next to the port, with a Copy button.

    With a plain `docker run` instead of compose, the fix shown is the exact
    rule to recreate the container with, e.g.
    `--device-cgroup-rule='c 509:* rmw'` (a real `ttyS`: `'c 4:64 rmw'`).
  - `not-enabled`: a UART of the SoC itself (`ttyAMA*`, a real `ttyS*`,
    `ttymxc*`, `ttyO*`). It may already be in use by a GPS, a login console
    or a modem, which the bridge cannot see, so it is served only once you
    enable it: **Enable** in the bridge's Buses dialog, or `BRIDGE_INCLUDE`
    in `.env`. RS-485 HAT channels (`ttySC*`) need no opt-in;
  - `reserved`: the kernel console or the Bluetooth UART, never served;
  - `busy`: another program holds it exclusively.

  **`BRIDGE_TTY_1..4` are trusted operator input.** The compose files grant
  whatever they contain. The script only ever writes exact tty majors; a
  hand-written wildcard (`*:*`) there removes the container's device
  isolation.

  The gateway's UI shows the ports it cannot use, with the reason, but does
  not let you pick them. Details: [serial-bridge-api.md](serial-bridge-api.md#get-adapters).
- **One owner per line.** A port mapped straight into the gateway (direct
  serial, [rtu-serial.md §1](rtu-serial.md#1-direct-serial)) cannot also be
  served by the bridge. Put it in `BRIDGE_EXCLUDE`, or move the device to the
  bridge. The bridge sees only a program that holds a port **exclusively**
  (as `busy`); one that opened it plainly — pyserial's default, gpsd, a
  getty — is invisible to it.
- **Exclusions.** An adapter another container already owns (a BMS, say)
  must be listed in `BRIDGE_EXCLUDE` in `.env` (`1a86:7523@1-1`, a stable id,
  or a `/dev` path). Cross-container in-use detection only works when the
  other program locks the port.
- **`SERIAL_BRIDGE_URL`.** The gateway reaches the bridge's control API at
  `http://mbg-serial-bridge:7000` by default; set the variable only if you
  rename the container. The control port is published on `127.0.0.1` only;
  data ports 7001–7099 are never published.
- Then add the bridge on the Devices page (**Add bridge → MBG serial
  bridge**, host `mbg-serial-bridge`), and on one of its buses press
  **+ Device**: Add Device opens on its Connection step with that bus filled
  in ([rtu-serial.md §2.2](rtu-serial.md#22-add-a-device-over-the-bridge)).
  Baud/parity are set on the bridge's bus (9600 8N1), not per device. A HAT
  whose RS-485 chip does not switch direction by itself needs the port's
  **RS-485** box ticked in the bridge's Buses dialog.

**Direct serial instead.** Map the adapter into the gateway service with a
`devices:` line in a compose override, preferably by its
`/dev/serial/by-id/…` link or a udev alias, since a `ttyUSBn` name can
change on a replug. Add Device lists the ports of the host: a port the host
has but the container does not shows **Not mapped**, with the `devices:`
line (and, for `docker run`, the `--device` flag) to add; recreate the
container, then **Refresh** ([rtu-serial.md §1.1](rtu-serial.md#11-the-port-list)).

The bridge image is `ghcr.io/sm26449/multi-bus-gateway-serial-bridge` and
follows the same `MBG_VERSION` tag. Direct passthrough (`devices:` in a
compose override) is the alternative — full comparison in
[rtu-serial.md](rtu-serial.md).

---

## E. Raspberry Pi / arm64 notes

- The release workflow publishes **linux/amd64 + linux/arm64**; `docker
  compose pull` picks the right one. Use a **64-bit OS** — there is no
  32-bit image; on a 32-bit OS take path F.
- Path A applies unchanged; a Pi 3 boots the full stack, but InfluxDB and
  Grafana are the heavy neighbours — start with
  `docker compose up -d multi-bus-gateway mosquitto` on a Pi 3 and add
  the rest if the box has headroom.
- **Envelope** ([MANUAL.md §18b](MANUAL.md#18b-running-on-constrained-hardware-raspberry-pi)):
  ~65–80 MB RSS for one device, 90–110 MB for 5 devices + 3 virtual meters;
  CPU scales with polls per second — on a Pi 3, 3–5 % at a 1 s realtime
  group for one device, 15–20 % for 5 devices + 3 meters, and a 250 ms
  cadence on a big map saturates a core. Rule of thumb: ~4–5 devices + ~3
  virtual meters with realtime ≥ 1 s; a Pi 4/5 has 2–3× the headroom.
- **Memory cgroup.** Raspberry Pi OS boots with the kernel's memory controller
  off: `docker stats` shows 0 B and container memory limits are not enforced.
  Add `cgroup_enable=memory` to the single line in `/boot/firmware/cmdline.txt`
  and reboot when you want limits or memory figures (capacity tests need it).
- Keep sub-second polling for the one register that drives a control loop;
  put counters and diagnostics in the `normal`/`slow` groups.
- **CAN (listen-only).** A CAN interface (a USB adapter, an MCP2515 HAT such
  as the Waveshare RS485 CAN HAT) is set up **on the host**, listen-only, at
  the bus's bitrate — the gateway never changes it and never transmits:

  ```sh
  ip link set can0 down
  ip link set can0 type can bitrate 500000 listen-only on
  ip link set can0 up
  ip -details link show can0 | grep LISTEN-ONLY     # check
  ```

  In Docker, a container sees the host's CAN interfaces only with host
  networking (Compose: `network_mode: host`; `docker run --network host`). No
  capability is needed to read. Host networking means the container's ports are
  the host's ports: published `ports:` are ignored, and the UI, the virtual
  meters, :502 and OPC UA bind directly on the host — check for port clashes.
  The container stays on the host network for everything else too. Listen-only
  is set on the host (`ip link … listen-only on`). On bare metal (path F) a reading socket needs
  no privilege either. See [can.md](can.md).
- **Enclosure:** [docs/enclosures](enclosures/) has a 3D-printable DIN-rail
  case for a Pi 5 with an NVMe HAT, the Waveshare RS485 CAN HAT (B) and a
  40 mm fan.

---

## F. Bare metal / systemd

The process is a single Python 3.11 program; the container adds nothing
else.

```bash
sudo useradd --system --home /opt/multi-bus-gateway --shell /usr/sbin/nologin mbg
sudo usermod -aG dialout mbg                # only for direct-serial RTU adapters
sudo git clone https://github.com/sm26449/multi-bus-gateway.git /opt/multi-bus-gateway
cd /opt/multi-bus-gateway
sudo -u mbg python3.11 -m venv venv
sudo -u mbg venv/bin/pip install --require-hashes -r requirements.lock
sudo chown -R mbg:mbg /opt/multi-bus-gateway/config
```

The CLI (`main.py`): `-c/--config` (default `config/config.yaml`, relative to
the working directory), `--host` and `--port` (override the UI bind), and
`--debug`. The bare-metal default bind is **loopback** (`ui.host:
127.0.0.1`); set `UI_HOST=0.0.0.0` (or `--host`) to serve the LAN, and
`UI_PORT` to move the port. The first start with no `config.yaml` prints the
`FIRST RUN` banner to stdout — with systemd it lands in the journal.

```ini
# /etc/systemd/system/multi-bus-gateway.service
[Unit]
Description=Multi-Bus Gateway
After=network-online.target
Wants=network-online.target

[Service]
User=mbg
Group=mbg
WorkingDirectory=/opt/multi-bus-gateway
Environment=UI_HOST=0.0.0.0
Environment=UI_PORT=8080
ExecStart=/opt/multi-bus-gateway/venv/bin/python main.py -c config/config.yaml
Restart=always
RestartSec=5
# Only if a virtual meter must bind :502 (a privileged port) as this user:
# AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now multi-bus-gateway
journalctl -u multi-bus-gateway | grep -A3 'FIRST RUN'
```

Virtual meters bind whatever port each instance declares — no port range to
publish, but the firewall must allow it. Ports below 1024 need the
capability line above (the container uses a sysctl instead). There is no
bundled broker on bare metal: install one or point the sink at an existing
broker.

---

## G. Behind a reverse proxy (Traefik / nginx / Caddy)

Terminate TLS in the proxy and route `https://gateway.example.lan` to the
gateway's `:8080`. Four things make it work properly:

1. **Trust the proxy.** Set `ui.trusted_proxies` in `config.yaml` to the
   proxy's address (the Traefik container's IP, or `127.0.0.1` for a proxy on
   the same host). Only then does uvicorn honour `X-Forwarded-For` /
   `X-Forwarded-Proto` — with the list empty (default) proxy headers are
   ignored entirely, so the login lockout, the IP allowlist and the audit
   trail would all see the proxy instead of the real client.
   ```yaml
   ui:
     trusted_proxies: ["172.20.0.5"]
     canonical_url: "https://gateway.example.lan"
   ```
   (`ui.tls` stays off — the proxy owns the certificate.) Restart after the
   edit.
2. **WebSockets.** `/ws` (live values for the UI) upgrades.
   Pass the `Upgrade`/`Connection` headers through and forward `Host` and
   `Origin` unchanged — the gateway checks `Origin` / `Sec-Fetch-Site` on
   `/ws` and on every state-changing request, so a proxy that rewrites them
   turns every save into a 403.
3. **Secure cookies.** The session cookie is marked `Secure` when the UI is
   served over HTTPS — by the built-in TLS, or when a *trusted* proxy sets
   `X-Forwarded-Proto: https`. Without step 1 the cookie is issued without
   `Secure` even though the browser talks HTTPS.
4. **Canonical URL.** `ui.canonical_url` steers a visitor who opened the box
   by raw IP or plain HTTP onto the one hostname (a client-side redirect, so
   cookies, passkeys and HSTS bind to a single origin). Append `?local` to
   stay on the IP when DNS or the proxy is down. Passkeys (WebAuthn) need
   exactly this: a hostname over HTTPS, never a raw IP.

Leave `/health` and `/metrics` reachable by your monitoring — they need no
login, but they are behind the IP allowlist if you set one.

---

## H. Joining an existing Docker network

If a broker, InfluxDB or the rest of a stack already run on a shared network,
don't create a second broker — join theirs. The overlay repoints the compose
file's `monitoring` alias at an **external** network:

```bash
docker network create pv-stack-network        # only if it does not exist yet
docker compose -f docker-compose.yml -f docker-compose.external-network.yml \
  up -d multi-bus-gateway
```

The network name defaults to `pv-stack-network`; override it with
`PV_STACK_NETWORK=my-net` in `.env`. Every service in the base file keeps its
alias, so `up -d` without a service name would also start the bundled extras
on that network — name the services you want. The gateway then reaches the
other containers by name (**Config → MQTT**, broker `mosquitto` or whatever
yours is called).

---

## Verification checklist

```bash
docker compose ps                                   # gateway "healthy" after ~30 s
curl -s http://gateway.example.lan:8080/health      # {"status":"ok",...} — HTTP 200
curl -s http://gateway.example.lan:8080/metrics | grep gateway_device_up
docker compose logs multi-bus-gateway | tail -50    # "Multi-Bus Gateway starting..." … "Starting web server on 0.0.0.0:8080"
```

- **`docker compose ps`** shows the container's own health probe (every 30 s,
  30 s start period, 3 retries; it follows `UI_PORT`). `/health` answers
  **503 only when an enabled virtual meter is down**; a stale upstream source
  degrades the JSON `status` but stays 200.
- **First login** works with the generated password; **Config → Security**
  to change it.
- **First device**: a fresh install starts with **no device** — none is
  assumed (until 3.95 a Janitza UMG 512-PRO was created by default; an
  upgraded install that has one keeps it, see `primary_device` in the
  [configuration reference](config-reference.md)). After the first login the
  gateway offers to add one: **Devices → Add Device** (or **Discover devices**
  for a CIDR scan / SunSpec walk / MQTT browse) — *What is it?*,
  *Connection* (with **Test connection**), *Check*, *Name & save* — and the
  device polls immediately and its card turns green. It is created
  **write-locked**: nothing is written to it until an admin chooses ⋯ →
  **Unlock writes…**.
  Until then the gateway publishes only its own topics (`mbg/gateway/status`,
  `mbg/gateway/site/meta`).
  With the bundled stack, `docker compose --profile debug up -d` puts MQTT
  Explorer on `127.0.0.1:4000` to watch the topics flow.
- **Virtual meter**: only after the source is fresh — add an instance on a
  port inside the published range, enable it, watch its **Logs** tab for the
  consumer's reads ([MANUAL.md §11](MANUAL.md#11-virtual-meters--step-by-step)).

See also: [operations.md](operations.md) · [troubleshooting.md](troubleshooting.md) ·
[upgrade-guide.md](upgrade-guide.md) · [rtu-serial.md](rtu-serial.md) · [seplos-v2.md](seplos-v2.md) · [can.md](can.md) ·
[rtu-over-network.md](rtu-over-network.md) · [MANUAL.md](MANUAL.md)

Verified against 3.83.0
