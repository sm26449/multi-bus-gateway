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
