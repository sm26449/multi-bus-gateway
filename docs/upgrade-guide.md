# Upgrading and downgrading

How to move between gateway versions without losing configuration — and the one
trap to know about before going *backwards*.

## Upgrading (3.x → newer 3.x)

An upgrade is just a new image over the same mounted config:

```bash
git pull                       # or: docker pull, if you consume a published image
docker compose build multi-bus-gateway
docker compose up -d multi-bus-gateway
```

Your configuration lives outside the container (`./config` bind mount:
`config.yaml`, per-device register selections, templates, virtual meters,
snapshots), so nothing about an upgrade touches it.

Configuration is **additive-forward across 3.x**: the loader reads only the
keys it knows and fills defaults for anything missing, so an older
`config.yaml` always loads on newer code, and new features arrive **opt-in**
(disabled until you enable them). Unknown keys are ignored at load time — a
config carrying keys from an even newer version still parses.

There is currently **no schema-version stamp** in `config.yaml` itself; the
compatibility contract is exactly the above. (Device *templates* do carry a
`schema_version`, and newer template schemas are rejected with a clear error.)

## Downgrade warning

Going back to an older version is where data can be lost — **silently**:

- Older code *loads* a newer `config.yaml` fine (unknown keys are ignored).
- But the first **save** from older code rewrites `config.yaml` with only the
  keys that version knows. Any key introduced later — a new section, a new
  option you had enabled — is **dropped without a warning**. The same applies
  to newer per-register options in the register files.

So before downgrading:

1. Take a snapshot (**Config → Backup & Snapshots → Create**) or download a
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
in **Config → Backup & Snapshots**.

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
`/api/status` → `config_status.written_by_newer`: **the next save from the
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
