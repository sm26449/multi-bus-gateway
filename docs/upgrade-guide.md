# Upgrading and downgrading

How to move between gateway versions without losing configuration — and the one
trap to know about before going *backwards*.

## Upgrading (3.x → newer 3.x)

An upgrade is just a new image over the same mounted config:

```bash
# published image (default): pick the release, pull it, restart on it
MBG_VERSION=3.79.0 docker compose pull multi-bus-gateway
MBG_VERSION=3.79.0 docker compose up -d multi-bus-gateway

# from source: same, with a local build
git pull && docker compose build multi-bus-gateway && docker compose up -d multi-bus-gateway
```

Put `MBG_VERSION=3.79.0` in `.env` to pin the tag for every later
`docker compose up`; `latest` follows the newest release. **Rolling back** is
the same command with the previous tag (`MBG_VERSION=3.78.2 …`) — read the
downgrade warning below first, then restore the matching config snapshot
(§Snapshots) if the newer version had migrated your config.

Your configuration lives outside the container (`./config` bind mount:
`config.yaml`, per-device register selections, templates, virtual meters,
snapshots), so nothing about an upgrade touches it.

Configuration is **additive-forward across 3.x**: the loader reads only the
keys it knows and fills defaults for anything missing, so an older
`config.yaml` always loads on newer code, and new features arrive **opt-in**
(disabled until you enable them). Unknown keys are ignored at load time — a
config carrying keys from an even newer version still parses.

Since 3.24.2 every save stamps `config_version` (the writing gateway's
version) into `config.yaml` — see the downgrade section below. The stamp is
informational: loading never depends on it, so the compatibility contract is
exactly the above. (Device *templates* additionally carry a `schema_version`,
and newer template schemas are rejected with a clear error.)

## 3.81.0 — the bundled stack requires credentials (breaking)

Only installs that run the **bundled** `docker-compose.yml` stack are
affected; a gateway pointed at your own broker/InfluxDB is not.

What changed:

- Mosquitto no longer accepts anonymous clients. `MQTT_USERNAME` and
  `MQTT_PASSWORD` in `.env` are required — `docker compose up` refuses to
  start the broker without them — and the password file is regenerated from
  them at every start (on the `mosquitto-data` volume).
- `DOCKER_INFLUXDB_INIT_PASSWORD`, `DOCKER_INFLUXDB_INIT_ADMIN_TOKEN` and
  `GF_SECURITY_ADMIN_PASSWORD` have no built-in default any more; they must
  be in `.env`.
- MQTT Explorer (no login of its own) starts only with `--profile debug`
  and listens on `127.0.0.1` when it does. Its image is tag-pinned.
- `STACK_BIND` (default `0.0.0.0`) prefixes the broker, InfluxDB and Grafana
  port mappings; `STACK_BIND=127.0.0.1` keeps them host-local.

Migration, in order:

1. Add to `.env` (copy the lines from `.env.example`):

   ```
   MQTT_USERNAME=mbg
   MQTT_PASSWORD=<a long random secret>
   ```

   and make sure the three Influx/Grafana values are present (they were
   already there if you copied `.env.example` when you installed).
2. `docker compose up -d` — the gateway reads `MQTT_USERNAME`/`MQTT_PASSWORD`
   itself (they are passed through as config overrides), so it reconnects
   with credentials on the same start. Check `docker compose logs mosquitto`
   for `mosquitto version … running` and the gateway's MQTT dot in the UI.
3. Give the same credentials to **every other client** of that broker: Home
   Assistant's MQTT integration, Node-RED broker nodes, Telegraf, scripts.
   Until you do, they are refused with `not authorised` and their retained
   topics simply stop updating — nothing is deleted.
4. If a dashboard host reached MQTT Explorer on `:4000`, start it with
   `docker compose --profile debug up -d` and reach it through an SSH
   tunnel or on the host itself; it never had a login.
5. Optional: extra broker users for third-party clients —
   `docker compose exec mosquitto mosquitto_passwd -b /mosquitto/data/passwd <user> <pass>`
   then `docker compose kill -s HUP mosquitto`. The first user is rewritten
   from `.env` at each start; the others persist on the volume.

Rolling back to 3.80.x restores anonymous access to the broker as before —
remove the credentials from the other clients only if you go back.

## Downgrade warning

Going back to an older version is where data can be lost — **silently**:

- Older code *loads* a newer `config.yaml` fine (unknown keys are ignored).
- But the first **save** from older code rewrites `config.yaml` with only the
  keys that version knows. Any key introduced later — a new section, a new
  option you had enabled — is **dropped without a warning**. The same applies
  to newer per-register options in the register files.

So before downgrading:

1. Take a snapshot (**Config → Backup & Restore → Snapshots & Rollback → Create**) or download a
   backup export.
2. Downgrade and run.
3. If something newer went missing after a save on the old version, restore the
   snapshot once you're back on the newer version.

## Snapshots and the boot seatbelt

You rarely need to prepare for an upgrade manually, because the gateway
snapshots itself:

- **Automatic snapshots** — after every successful config mutation (debounced,
  so a burst of edits produces one snapshot), a verbatim ZIP of the whole
  config bundle (config.yaml, per-device registers, templates, virtual meters)
  is written to `config/snapshots/` (kept: 50, file mode `0600` — snapshots
  include secrets; they are restore points, not sharing exports).
- **Last known good (LKG)** — a separate snapshot stamped only after the app
  has booted **and stayed healthy**.
- **Boot seatbelt** — at startup, a `config.yaml` that no longer parses is
  restored from the LKG automatically and boot retries once, so a bad manual
  edit or a torn write can't brick an unattended box. Additionally, a corrupt
  file detected at load falls back to the in-process `.yaml.good` copy, the
  broken file is preserved as `config.yaml.bad`, and **saves are disabled**
  until it's repaired (surfaced in `/api/status` → `config_status`).

Restore, download, diff ("what changed since this snapshot") and delete are all
in **Config → Backup & Restore → Snapshots & Rollback**.

## Non-root containers (3.24.1+)

The gateway process runs as a dedicated non-root user (uid 10001; the
serial-bridge as 10002 in `dialout`). Two things follow:

- **Config volume ownership is automatic.** The entrypoint starts as root
  only to `chown` the mounted `/app/config` to the app user, then drops
  privileges (`setpriv`) — fresh installs and upgrades need no manual
  `chown`. Only if you override the container user (`docker run --user`)
  must the host directory ownership match that user.
- **Port `:502` needs one sysctl.** It is a privileged port, and the app no
  longer runs as root — add `net.ipv4.ip_unprivileged_port_start=0` as a
  *per-container* sysctl (already present in the shipped `docker-compose.yml`
  and the README `docker run` example). It applies inside the container's own
  network namespace only; no capabilities, no host-wide change. Ports ≥1024
  (the default 1502–1512 range) need nothing. Symptom of a missing sysctl: a
  virtual meter on 502 logs a bind `PermissionError` while meters on 1502+
  serve fine.

## Downgrades and the config version stamp (3.24.2+)

Every save writes a `config_version` stamp (the writing gateway's version) as
the first key of `config.yaml`. If you **downgrade** and the file carries a
newer stamp, the gateway loads it fine — unknown keys are simply not read —
but logs a warning, raises a `config-downgrade` alert, and flags it in
`/api/status` → `config.written_by_newer`: **the next save from the
older version silently drops every setting the newer version introduced.**
Either upgrade back before saving, or accept the loss knowingly. (There is no
migration framework; the stamp exists precisely so a downgrade+save can no
longer lose settings *silently*.)

## Field-naming note (3.6+)

Since 3.6.0 the bundled vendor templates use **canonical field names**
([canonical-fields.md](canonical-fields.md)) — the same physical quantity is
named the same on every device (`voltage_l1_n` everywhere), and the name drives
the MQTT topic and the InfluxDB field.

Upgrading the app does **not** rename anything on a configured device: register
selections are per-device copies, and template updates are not retroactive. But
if you **re-assign** an updated template to a device (or import a canonical
map), its MQTT topics and InfluxDB field names change — update any dashboards,
automations or queries keyed on the old names at the same time.

See also: [config-reference.md](config-reference.md) · [MANUAL.md](MANUAL.md) ·
[canonical-fields.md](canonical-fields.md) · [API.md](API.md)
