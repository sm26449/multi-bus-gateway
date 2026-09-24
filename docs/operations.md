# Operating the gateway

Day-2 work on a running Multi-Bus Gateway: moving between versions and
back, what to back up and how to put it back, where the logs and health
signals are, how to remove everything cleanly — and what your consumers see
while the gateway is down. Install paths are in [install.md](install.md);
symptoms and fixes in [troubleshooting.md](troubleshooting.md).

## Upgrading

An upgrade is a new image over the same mounted `./config`; nothing about it
touches your configuration (the loader is additive-forward across 3.x — see
[upgrade-guide.md](upgrade-guide.md)).

```bash
# published image: pin the release in .env, pull it, restart on it
sed -i 's/^#\? *MBG_VERSION=.*/MBG_VERSION=3.83.0/' .env
docker compose pull multi-bus-gateway
docker compose up -d multi-bus-gateway

# from source
git pull && docker compose build multi-bus-gateway && docker compose up -d multi-bus-gateway
```

`MBG_VERSION` accepts a version (`3.83.0`), a minor line (`3.83`) or
`latest`; `docker compose build` ignores it. Naming the service keeps the
bundled broker/InfluxDB untouched. Running the RTU bridge? It carries the
same tag, so pull and restart it in the same breath:
`docker compose --profile rtu-bridge pull && docker compose --profile
rtu-bridge up -d`. (The bridge image carries the same `X.Y.Z`, `X.Y` and
`latest` tags as the gateway, so one `MBG_VERSION` pins both.)

After the restart, `docker compose logs multi-bus-gateway` shows the new
version in `Loaded config from config/config.yaml` and the UI title bar; the
next save stamps `config_version` into `config.yaml`.

## Rolling back

Going **backwards** is the one operation that can lose settings — silently,
on the first save from the older version (it rewrites `config.yaml` with
only the keys it knows). So:

1. Take a snapshot first: **Config → Backup & Restore → Snapshots &
   Rollback → Create** (or rely on the automatic one the last change took).
2. Pin the previous tag and restart:
   ```bash
   MBG_VERSION=3.80.0 docker compose pull multi-bus-gateway
   MBG_VERSION=3.80.0 docker compose up -d multi-bus-gateway
   ```
   The older version logs `config.yaml was written by gateway 3.80.1 but
   3.80.0 is running — settings introduced after 3.80.0 are preserved on
   load but will be DROPPED by the next save from this version`, raises a
   `config-downgrade` alert and flags `/api/status → config.written_by_newer`.
3. If a later save on the old version dropped something, go back to the
   newer tag and **restore the snapshot** from the same page.

## What to back up

Everything the gateway itself owns is in the mounted **`./config`**:

| File | What it holds | Notes |
|---|---|---|
| `config.yaml` | globals, sinks, security, `devices[]` | secrets included; `.yaml.good` beside it is the self-heal copy |
| `selected_registers.json`, `devices/<id>/selected_registers.json` | register selections (primary at the root, other devices per id) | each with its own `.good`/`.bad` pair |
| `devices/<id>/device.json` | tombstone of a deleted device (for *Restore*) | |
| `device_templates/` | your own device register maps | bundled maps live in the image |
| `templates/` | virtual-meter templates (YAML) | the three shipped ones are baked in too |
| `virtual_meters.yaml` | virtual-meter instances | |
| `rules.yaml` | rules | in the bundle since 3.79.0 |
| `calculated_templates.json` | calculated-register presets | |
| `builder_profiles.json` | Device Builder hardware profiles | |
| `passkeys.json` | WebAuthn credentials | `0600`; only in with-secrets backups and snapshots |
| `sessions.json` | login sessions (token hashes) | `0600`; restore keeps everyone logged in — optional |
| `write_leases.json` | active dead-man leases | ephemeral by design; leaves out |
| `snapshots/` | automatic + manual snapshot ZIPs, `lkg.zip`, `index.json` | restore points, not sharing exports |
| `audit.jsonl`, `events.jsonl` | audit trail (1 MB × 5) and event log | not in any bundle — copy them if you need them |
| `influx_buffer.jsonl` | the persisted store-and-forward buffer | replayed on the next start; don't restore a stale one |
| `certs/` | the auto-generated self-signed pair when built-in TLS is on | |

Two ways to capture it, both valid:

- **A backup export** — **Config → Backup & Restore → Export** (or
  `GET /api/config/export`): a ZIP of config, register selections,
  templates, virtual meters, presets, profiles and rules. Secrets are
  stripped by default; `include_secrets=true` (admin or API key, audited)
  keeps them and adds `passkeys.json`. Keep one off the box.
- **A copy of the directory** — `tar czf mbg-config-$(date +%F).tgz config`
  while the gateway runs is fine (every writer is atomic), but stop it first
  if you want `sessions.json` and the Influx buffer consistent too.

**The bundled stack's named volumes** (from `docker-compose.yml`) are not in
either bundle: `mosquitto-data` (retained messages), `mqtt-explorer-config`,
`influxdb-data` + `influxdb-config` (**your history**), `grafana-data`
(dashboards), `esphome-config` (**the Device Builder's node YAMLs**),
`serial-bridge-data` (the adapter port map). Back them up with your
infrastructure tooling, e.g.

```bash
docker run --rm -v influxdb-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/influxdb-data-$(date +%F).tgz -C /data .
```

## Restoring

**A snapshot** (same host, same volume): **Config → Backup & Restore →
Snapshots & Rollback**, pick one, *Diff* to see what a rollback undoes,
*Restore*. The restore first takes a `pre-restore` snapshot, so it is itself
reversible, and writes the bundle verbatim through the validated writer.

**A backup ZIP on a fresh host** — the disaster-recovery path of
[MANUAL.md §15](MANUAL.md#15-config-safety-snapshots-rollback-backup): bring
up a fresh gateway, log in with the first-run password, **Config → Backup &
Restore → Import** the ZIP. The import first checks the bundle with the
same rules the settings pages apply (allowlist and proxy entries must
parse, the webhook must be http(s), HTTP sources must be on the LAN, rules
must validate — a bad bundle is refused before anything is written), then
merges over the live config (stripped secrets survive, wherever they sit),
takes a `pre-import` snapshot, and with more than one
device may answer `restart_required` — then
`docker compose restart multi-bus-gateway`. A sanitized export asks you to
re-enter secrets; `passkeys.json` is only honoured from a with-secrets
bundle.

**A manual copy** — when the UI is not reachable at all:

```bash
docker compose stop multi-bus-gateway
tar xzf mbg-config-2026-09-23.tgz          # recreates ./config
docker compose up -d multi-bus-gateway      # the entrypoint chowns it for uid 10001
```

Ownership needs no fixing by hand (the container does it at start); only a
`docker run --user` deployment must match the directory owner itself.

## Logs

```bash
docker compose logs -f multi-bus-gateway          # follow
docker compose logs --since 1h multi-bus-gateway  # recent window
docker compose logs multi-bus-gateway | grep AUDIT
```

- Every service in the compose uses the `json-file` driver capped at
  **10 MB × 3 files** (the `x-logging` anchor), so logs cannot fill an
  appliance disk. Older lines rotate away — anything you need to keep,
  ship elsewhere.
- Line format: `<timestamp> - <module> - <LEVEL> - <message>`, timestamps in
  the container's local time (UTC unless you pass `TZ` to the gateway
  service).
- **`AUDIT` lines** (3.80.0) mirror every entry of `config/audit.jsonl` —
  logins (including failures and `locked out`), passkey events, every
  Modbus write, exports/imports, snapshot restores — with user, IP, action
  and status, secrets redacted. The same trail is on the **Status** page
  (admin) and as CSV at `GET /api/audit/export.csv`.
- Debug level: `python main.py --debug` on bare metal; in Compose, add
  `command: ["python", "main.py", "-c", "config/config.yaml", "--debug"]`
  to the gateway service in a local override.
- The **event log** (`config/events.jsonl`, last 300, on the Status page)
  is the operator-facing subset: read failures, sink connects/disconnects,
  virtual-meter lifecycle, rollbacks, alerts.

## Health and metrics

Both endpoints need no login (a probe cannot log in) but sit behind the IP
allowlist when you set one.

**`GET /health`** — the container probe. The body's `status` is the worst of
virtual-meter health and acquisition health, with a `modbus` block for the
upstream data's freshness and per-meter entries:

| HTTP | `status` | Meaning |
|---|---|---|
| 200 | `ok` | meters serving and fresh, sources fresh |
| 200 | `degraded` | a source is stale or not yet connected, or a meter is serving with a stale source — a correct fail-safe, not a fault |
| **503** | `down` | an **enabled virtual meter is not serving** — something a restart may fix |

A dead upstream meter never fails the probe: restarting the container cannot
fix your wiring, and the freshness watchdog already protects consumers. The
Dockerfile `HEALTHCHECK` (every 30 s, 30 s start period, 3 retries, follows
`UI_PORT`) is what `docker compose ps` reports as `healthy`/`unhealthy`; an
unhealthy container is **not** restarted by `restart: unless-stopped` — that
policy acts on exits.

**`GET /metrics`** — Prometheus text: `gateway_device_up / poll_rate /
reads_total / errors_total / read_latency_ms / staleness_seconds / health`,
`gateway_mqtt_connected / published_total`, `gateway_influx_connected /
written_total / buffer_points / dropped_total`, `gateway_vmeter_up /
requests_total / request_rate / errors_total / connections / quality`.
Counters and health only, never configuration. `GET /api/status` (login
required when auth is on) carries the same plus `config` (`healthy`,
`saves_disabled`, `bad_file`, `written_by`, `written_by_newer`).

## Uninstalling

Removing the gateway completely, in the order that leaves nothing behind:

1. **Let Home Assistant forget the devices first.** Deleting a device in the
   UI clears its retained discovery so HA drops the entities; do that for
   each device and virtual meter while the gateway still runs. Otherwise HA
   keeps orphaned entities you delete by hand under Settings → Devices.
2. **Stop and remove the stack** — profiles included, so the bridge goes too:
   ```bash
   docker compose --profile rtu-bridge down -v      # containers, network, AND the named volumes
   rm -rf ./config                                  # your configuration, snapshots, audit trail
   docker image rm ghcr.io/sm26449/multi-bus-gateway:3.83.0 \
                   ghcr.io/sm26449/multi-bus-gateway-serial-bridge:3.83.0
   ```
   `down -v` deletes `influxdb-data` and `grafana-data` — export what you
   want to keep first. Without `-v` the volumes stay for a later reinstall.
   If you joined an existing network with the overlay, `down` leaves that
   network alone.
3. **Clear retained MQTT topics** — only if the broker survives (your own
   broker, or the bundled one without `-v`). With `retain: true` (the
   default) the last values, availability and discovery configs stay on the
   broker forever. Wipe them with an empty retained publish per topic:
   ```bash
   # inventory first
   mosquitto_sub -h 192.168.1.10 -v --retained-only -t 'multibus/#' -t 'meters/#' -t 'mbg/#' -t 'homeassistant/#'
   # then clear (repeat per topic; a loop over the inventory works)
   mosquitto_pub -h 192.168.1.10 -r -n -t 'multibus/umg512/status'
   ```
   What to look for: `<mqtt.topic_prefix>/#` (default `multibus/umg512` —
   values, `status`, `availability`, `alert`, `vmeter/<id>/state`,
   `pq/event`), each non-primary device's own prefix (default pattern
   `mbg/devices/<id>`), `mbg/#` (endpoint aggregates `mbg/endpoints/<id>/…`),
   and the discovery configs under `mqtt.ha_discovery.prefix` (default
   `homeassistant`): `homeassistant/+/mbg_dev_<device>/+/config` for
   devices, `homeassistant/sensor/multibus/+/config` for the primary's
   sensors, `homeassistant/+/janitza_vmeter/+/config` for virtual meters.
   Add any `mqtt.compat_aliases` prefixes you configured.
4. Bare metal: `systemctl disable --now multi-bus-gateway`, remove the unit,
   the checkout and the `mbg` user.

## When the gateway is down

What each consumer sees while the process is stopped, crashed or being
upgraded — and what to expect when it returns
([MANUAL.md §18c](MANUAL.md#18c-what-consumers-see-when-a-source-is-lost)):

| Consumer | While down | On return |
|---|---|---|
| **Virtual-meter clients** (Victron, Fronius, PLC) | the TCP port is closed — *connection refused*, exactly like an unplugged meter; their own meter-loss fail-safe engages | servers start with the instances, but serve only once the source is fresh (`fail`/`sentinel`/`hold` per policy); consumers reconnect on their own |
| **Home Assistant** | the broker publishes the Last-Will: `<prefix>/status` = `offline`, so every entity whose availability points there goes **unavailable** (device entities also carry `<device prefix>/availability`); retained values stay visible as the last known state | `online` is re-asserted retained, discovery republished, the change cache cleared so the full state is republished |
| **MQTT automations** | value topics stop updating; a retained value carries no freshness stamp — gate on `<prefix>/status` | first publish after reconnect is a full state |
| **InfluxDB / Grafana** | a **gap** in every series (never a flat line) | points buffered before the stop are replayed from `config/influx_buffer.jsonl` with their original timestamps |
| **REST push / `GET /api/meters/<id>`** | no pushes; the feed is unreachable | pushes resume on the interval |
| **Alerts** | none can be sent — the MQTT LWT is the only signal | `device`/`sink` recovery alerts fire only if a down alert had been sent |

A planned restart (`docker compose up -d` on a new tag) is a few seconds of
the above; the shutdown disconnects devices in parallel and flushes the
Influx buffer before exiting, so nothing is lost. Recreating the container
also drops every virtual-meter connection — schedule upgrades when the
consumers can tolerate the blink.

See also: [install.md](install.md) · [troubleshooting.md](troubleshooting.md) ·
[upgrade-guide.md](upgrade-guide.md) · [releasing.md](releasing.md) ·
[reliability.md](reliability.md)

Verified against 3.83.0
