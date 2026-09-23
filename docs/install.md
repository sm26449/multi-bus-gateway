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

# 2) Environment — change the change-me passwords, optionally pin a release
cp .env.example .env
echo 'MBG_VERSION=3.80.1' >> .env      # a version, a minor line (3.80) or latest

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

**The bundled services** (none of them uses a Compose profile):

| Service | Container | Purpose | Data |
|---|---|---|---|
| `mosquitto` | `mbg-mosquitto` | MQTT broker — anonymous on the LAN by default; credentials via the two-line recipe in `mosquitto/config/mosquitto.conf` | `mosquitto-data` |
| `mqtt-explorer` | `mbg-mqtt-explorer` | web view of every topic, `:4000` | `mqtt-explorer-config` |
| `esphome` | `esphome` | build engine for the Device Builder; no published port on purpose | `esphome-config` |
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
  esphome:       { profiles: ["extras"] }
```

They then start only with `docker compose --profile extras up -d`. Deleting
the service blocks from your copy of `docker-compose.yml` works too. If you
turn ESPHome off, set `ESPHOME_ENABLED=false` in `.env` (or just turn the
Builder off in the UI — that sticks).

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
  ghcr.io/sm26449/multi-bus-gateway:3.80.1
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

For Modbus RTU over a USB adapter the recommended path is the bundled
`serial-bridge` service (ser2net + a supervisor): it owns the adapters,
exposes each as a stable TCP endpoint on the internal Docker network, and
the gateway stays unprivileged. It is an opt-in profile because it needs
`/dev`:

```bash
docker compose --profile rtu-bridge up -d
```

- **Permissions.** `/dev` is mounted read-only and `device_cgroup_rules`
  allow only ttyUSB (major 188) and ttyACM (major 166) nodes; the process runs
  as uid 10002 in `dialout`. No udev rule is required on the host for a
  standard USB adapter; if you add your own rules, keep the nodes group
  `dialout`-readable.
- **Exclusions.** An adapter another container already owns (a BMS, say)
  must be listed in `BRIDGE_EXCLUDE` in `.env` (`1a86:7523@1-1`, a stable id,
  or a `/dev` path) — cross-container in-use detection cannot work.
- **`SERIAL_BRIDGE_URL`.** The gateway reaches the bridge's control API at
  `http://mbg-serial-bridge:7000` by default; set the variable only if you
  rename the container. The control port is published on `127.0.0.1` only;
  data ports 7001–7099 are never published.
- Then **Add device → Modbus RTU → Over network → Scan** and pick the
  adapter. Baud/parity are set on the bridge (9600 8N1), not per device.

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
- Keep sub-second polling for the one register that drives a control loop;
  put counters and diagnostics in the `normal`/`slow` groups.

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
2. **WebSockets.** Two paths upgrade: `/ws` (live values for the UI) and
   `/api/builder/stream/<command>` (Device Builder build/flash streams).
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
- **First device**: **Devices → Add device** (or **Discover devices** for a
  CIDR scan / SunSpec walk / MQTT browse), **Test connection**, pick a
  template, save — the device polls immediately and its card turns green.
  With the bundled stack, MQTT Explorer on `:4000` shows the topics as they
  flow.
- **Virtual meter**: only after the source is fresh — add an instance on a
  port inside the published range, enable it, watch its **Logs** tab for the
  consumer's reads ([MANUAL.md §11](MANUAL.md#11-virtual-meters--step-by-step)).

See also: [operations.md](operations.md) · [troubleshooting.md](troubleshooting.md) ·
[upgrade-guide.md](upgrade-guide.md) · [rtu-serial.md](rtu-serial.md) ·
[MANUAL.md](MANUAL.md)

Verified against 3.80.1
