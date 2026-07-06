# User Manual — Janitza UMG 512-PRO Monitor

🇬🇧 **English** | [🇷🇴 Română](MANUAL.ro.md)

A step-by-step guide: from a fresh install to monitoring, integrations, and
serving virtual meters. For the architecture deep-dive of the virtual-meter
engine see **[VIRTUAL-METER.md](VIRTUAL-METER.md)**.

> 🇬🇧 English. Localized versions welcome via PR.

## Contents
1. [What you need](#1-what-you-need)
2. [Install (Docker)](#2-install-docker)
3. [First configuration](#3-first-configuration)
4. [The Web UI, tab by tab](#4-the-web-ui-tab-by-tab)
5. [Devices & device templates (multi-device)](#5-devices--device-templates-multi-device)
6. [Virtual Meters — step by step](#6-virtual-meters--step-by-step)
7. [Home Assistant (MQTT)](#7-home-assistant-mqtt)
8. [InfluxDB & Grafana](#8-influxdb--grafana)
9. [Security (optional)](#9-security-optional)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. What you need
- A **Janitza UMG 512-PRO** (or compatible UMG) reachable over the network with
  **Modbus TCP enabled** (default port 502). Note its IP.
- A host with **Docker + Docker Compose**.
- *(Optional)* an MQTT broker (for Home Assistant) and/or InfluxDB (for Grafana).

---

## 2. Install (Docker)

```bash
# 1) Get the code
git clone https://github.com/sm26449/janitza-monitor.git
cd janitza-monitor

# 2) Create your environment file
cp .env.example .env
#    edit .env — at minimum set MODBUS_HOST to your Janitza's IP (see step 3)

# 3) Start it
docker compose up -d

# 4) Open the UI
#    http://<host>:8080
```

That's it for a monitor-only setup. Logs: `docker compose logs -f`.

---

## 3. First configuration

> **Tip:** after the first start you can set all of this from the UI — the meter's
> connection on its **Devices** card, and MQTT/InfluxDB under **Config** —
> it persists to `config/config.yaml` and applies without a restart. `.env` is just a convenient
> way to pre-seed a fresh deploy. An env value takes precedence and locks that field in the UI.

Edit `.env` (or set the same vars in your compose). The essentials:

| Variable | What it is | Example |
|----------|-----------|---------|
| `MODBUS_HOST` | Janitza IP | `192.168.1.100` |
| `MODBUS_PORT` | Modbus TCP port | `502` |
| `MODBUS_UNIT_ID` | Modbus unit | `1` |
| `MQTT_BROKER` / `MQTT_PORT` | broker (optional) | `192.168.1.100` / `1883` |
| `INFLUXDB_URL` / `INFLUXDB_TOKEN` | InfluxDB (optional) | — |
| `UI_PORT` | Web UI port | `8080` |

Restart after editing: `docker compose up -d`. The **Modbus** dot in the UI
top-right turns green when it connects.

You can also configure most of this from the UI → **Config** tab (no restart for
register/poll changes — they hot-reload).

---

## 4. The Web UI, tab by tab

Open `http://<host>:8080`.

The top nav has four areas — everything specific to one meter lives inside that
device's workspace, not in a global menu:

- **Dashboard** — global live KPI cards + the values you pinned across devices.
  Click *Customize* to choose cards; toggle card/table view.
- **Devices** — every source (Modbus TCP, RTU coming, or **HTTP/JSON**) as a card
  with live status; the **Add Device** wizard lives here. Open a device for its
  **tabbed workspace**: *Overview* (read-only summary + data health), *Edit*
  (connection, template, poll intervals, MQTT/InfluxDB output toggles),
  *Registers* (its Available/Selected map), and *Monitor / History / Energy* for
  that device (see step 5).
- **Config** — global settings only: **MQTT** broker, **InfluxDB** connection,
  **Backup**, **Security**. Changes hot-reload.
- **Virtual Meters** — serve the live values as standard meters to other systems
  (see step 6).

The three dots top-right (Modbus / MQTT / InfluxDB) show connection health — click
one for details.

**Per-device Monitor / History / Energy** — from a device's workspace:
*Monitor* (needs polling) drags any value onto a live, zoomable chart; *History*
and *Energy* (need that device's InfluxDB output) read stored data back —
history lines with a min/max band, and monthly import/export/reactive/apparent
energy totals.

---

## 5. Devices & device templates (multi-device)

One install can read **several sources**. Each device pairs a **connection**
(Modbus TCP now, RTU coming, **HTTP/JSON**, or **MQTT**) with a **device
template** — the register map of that equipment type — and its **own data
routing**.

> **Don't know a device's address?** Devices → *Discover devices* → *Modbus TCP
> scan*: it sweeps a private LAN range on the Modbus port for devices that
> answer, and can sweep unit-ids on one host. *Use* prefills the wizard.

**Add a device:** Devices → *Add Device*:

1. **Connection** — protocol, then Modbus (host/IP, port, unit ID, timeout),
   **HTTP/JSON** (a JSON URL; each register extracts its value by a `json_path`,
   e.g. a Fronius meter over the Solar API), or **MQTT** (broker, port, a
   subscribe topic + optional user/pass/TLS; each register reads its value from
   the JSON payload by `json_path`, or takes the bare payload as the number, and
   may use its own topic with `+`/`#` wildcards). Press **Test connection**: for
   Modbus any protocol-level answer confirms a live device; for MQTT it connects
   and waits briefly for a sample message.
2. **Template** — choose from the library (the Janitza UMG 512-PRO map is
   built-in), **upload** a `.json` template (validated row by row; conflicts
   ask before overwriting), or **create** one in the editor. In the editor you
   define metadata (id, vendor, model) and registers (address, name, label,
   unit, data type, category, poll group); errors are marked on the exact row.
   Built-ins are read-only — use *Duplicate to edit*. A template used by a
   device cannot be deleted. *Export* downloads the template for sharing.
3. **Data routing** — the device **id** becomes the routing key: values publish
   under the device's **MQTT topic prefix** (live preview shows a real example
   topic) and land in the device's **InfluxDB bucket** (auto-created, 90-day
   retention) tagged with its device tag.

After creation the device's **Registers** tab shows its catalog from the
template; select what to poll (or use auto-select for the template defaults),
save — only that device's pollers reload. Each device card on the **Devices**
page shows live health (status dot, poll rate, data age) and its routing; the
workspace has edit and delete (delete is blocked while a virtual meter sources
the device).

**Existing installs:** your UMG 512-PRO automatically appears as device #1 —
its topics, bucket, tags and Home Assistant entities are unchanged, and its
routing identity is locked so it stays byte-identical.

**Calculated measurements** (device workspace → *Calculated*): derive a new
value by formula from the device's own measurements — power factor `_P / _S`,
a phase sum, a unit conversion, `(E - prev(E)) / dt * 3600` for average power
from a Wh counter, etc. The builder gives you clickable Measurement-field chips,
a function/operator palette and a live preview; presets fill common formulas and
you can *save your own as a reusable preset*. A calculated value flows to every
output and shows up in Monitor and History like any measurement.

**Outputs** (device workspace → *Outputs*): besides MQTT and InfluxDB, each
device can also **serve** its live values as read-only JSON at
`GET /api/meters/<id>` (Solar-API style, opt-in per device), and **push** them
as JSON to an external URL on an interval (webhook / cloud), with auth headers
stored masked. Both are toggled per device, alongside the MQTT/InfluxDB sinks.

---

## 6. Virtual Meters — step by step

Goal: let another system (Victron ESS, a Fronius inverter, any SunSpec client)
read this one Janitza as the meter *it* expects.

> ⚠️ A virtual meter can feed a control loop. Do steps 6.1→6.3 (validate in
> parallel) before you ever make it a consumer's only meter.

**6.0 — Publish the ports (once).** In `docker-compose.yml` the meter port range
is published (default `1502-1512`, plus `502` for Fronius). Pick instance ports
inside that range. Widen the range + recreate the container if you need more.

**6.1 — Pick or create a template.** Go to **Virtual Meters → Templates**.
- Shipped: `em24_av53` (Carlo Gavazzi EM24 → Victron), `fronius_ts_native`
  (Fronius Smart Meter → DataManager), `fronius_sunspec_meter` (generic SunSpec).
- *New template*: define each register (address, type, scale, source). Source can
  be a live Janitza register, a constant, or a sum of registers.
- *Import*: drop in a `.yaml` someone shared (validated before saving).

**6.2 — Add an instance.** On the **Meters** sub-tab, choose the template, a free
port, unit id → **Add instance**. It starts **disabled**.

**6.3 — Validate in parallel.** Enable the instance (toggle). Point a *test*
consumer — or just open the **Logs** tab — at `host:port`. Watch the live query
log: you see exactly what the consumer reads, when, and what you return. Compare
the served values against your real meter. The **Stats** tab shows request rate,
errors, and which registers are read most.

**6.4 — Cut over.** Once you trust it, point the real consumer at the virtual
meter and remove its dedicated physical meter. The **freshness watchdog** is your
safety net: if the Janitza data goes stale, the meter stops responding so the
consumer's own grid-loss fail-safe engages.

**6.5 — Observe & export.** The **Logs**/**Stats** tabs keep the last 1024
requests + counters in RAM. **Export** a template (YAML) to share it or back it
up; the meter card (accordion) shows active client connections (ip:port).

---


### Data-quality registers for your PLC/SCADA (optional)

If the system READING the virtual meter needs to know whether the data is
trustworthy (fresh vs held vs missing), enable **"In-band quality block"**
on the instance (add/edit form). The meter then also serves a small
read-only block at address **61440** — identical for every virtual meter:

| Address | Type | Meaning |
|--------:|------|---------|
| 61440 | u16 | format version (1) |
| 61441 | u16 | 0 legacy · 1 ok · 2 degraded · 3 stale |
| 61442–61444 | u16 | fresh / stale / missing rows |
| 61445 | u16 | total data rows |
| 61446 | u32 | age of newest fresh value (s); 0xFFFFFFFF = never |

Typical guard in the consumer: *use the data only while 61441 == 1; alarm
on 3*. Pair it with the `fail`/`sentinel`/`hold` policy (with `legacy` the
state reads 0 — quality is not judged per register). Full wire spec:
`docs/virtual-meter-spec.md`.

## 7. Home Assistant (MQTT)

Set `MQTT_BROKER`/`MQTT_PORT` (and credentials) in `.env`, restart. The monitor
publishes **Home Assistant MQTT autodiscovery**, so entities appear automatically
under the device. A Last-Will topic marks the device offline if the monitor stops.
Pick which registers publish (and their topics) in the device's **Registers** tab.

---

## 8. InfluxDB & Grafana

Set `INFLUXDB_URL`, `INFLUXDB_TOKEN`, `INFLUXDB_ORG`, `INFLUXDB_BUCKET` in `.env`.
Writes are batched with automatic retry/backoff and a NaN guard. Point Grafana at
the same bucket. Per-register measurement/tags are configurable in each device's
**Registers** tab. The optional compose profiles can start a local InfluxDB + Grafana
(see README).

**Data guarantees.** Every point is stamped with the Modbus *read* time, not the
flush time. If InfluxDB becomes unreachable, points go to an in-RAM
store-and-forward buffer (default **10 minutes / 50,000 points**, tunable via
`influxdb.buffer_minutes` / `buffer_max_points` in `config.yaml`) and are
replayed with their original timestamps on reconnect — idempotently, since
InfluxDB dedupes on measurement+tags+timestamp, so no duplicates. Batches the
client gives up on after its own ~5 min of retries are recovered into the same
buffer. Outages longer than the buffer window lose the oldest points (RAM only —
a restart clears the buffer); for the voltages the meter's onboard recording can
backfill those via `python -m janitza.backfill`. Watch `buffer_points` /
`replayed_total` / `dropped_total` in **`/api/status`**. MQTT is deliberately
*not* replayed: it is a live bus (consumers act on "now"), and on reconnect the
full current state is republished instead.

---

## 9. Security (optional)

Everything below is **off by default** — this appliance is built for a trusted
LAN. Turn features on from **Config → Security** when it must be reachable from
a wider network.

- **Login** — require a username/password to use the UI/API. One **admin**
  (full access) and an optional **viewer** (read-only: can see everything, can
  change nothing). Passwords are stored hashed (PBKDF2); leave a password field
  blank when saving to keep the current one. Failed logins are rate-limited per
  IP (lockout after N attempts for M minutes, both configurable). A login
  screen appears when enabled; the log-out button sits in the title bar.
- **HTTPS** — serve the UI over TLS. Point it at a certificate + key, or leave
  the paths blank to auto-generate a self-signed pair on the next start
  (**restart the container to apply**). A self-signed cert triggers a browser
  warning; install a real cert for production.
- **MQTT TLS** — encrypt the broker link (port 8883). Upload a CA certificate
  to verify the broker, and optionally a client certificate + key for
  **mutual TLS**. Put the files under `config/` and reference their in-container
  paths. "Skip verification" is for testing only.
- **IP allowlist** — restrict which IPs/subnets may reach the UI/API (one entry
  per line, e.g. `192.168.1.0/24`; empty = open). Loopback is always allowed.
  The card shows *your current IP* so you don't lock yourself out. **Docker
  note:** behind the default bridge network, connections often appear to come
  from the docker gateway IP rather than the real client — check "your current
  IP" and allowlist what you actually see; for true per-client filtering use
  host or macvlan networking. If you do lock yourself out, edit
  `config/config.yaml` (`security.allowlist`) and restart.
- **API key** (pre-existing) — set `API_KEY` in the environment to require an
  `X-API-Key` header on state-changing requests, independent of login.

---

## 10. Troubleshooting

| Symptom | Check |
|---------|-------|
| Modbus dot red | `MODBUS_HOST`/port correct? Janitza Modbus TCP enabled? firewall? |
| UI shows old version after update | hard-refresh the browser (the app bundle is cache-busted, but proxies can cache) |
| Virtual meter "stale / starting" | the Janitza source isn't fresh — check the Modbus connection; the watchdog won't serve stale data by design |
| Consumer can't reach a virtual meter | is the port inside the published compose range? reachable from the consumer's network? check the **Logs** tab for incoming reads |
| MQTT entities missing in HA | broker reachable? autodiscovery enabled? watch `docker compose logs` |
| InfluxDB write-retry warnings | InfluxDB URL/token/bucket correct? The client retries ~5 min, then the batch is recovered into the RAM buffer and replayed on reconnect — check `replayed_total`/`dropped_total` in `/api/status` |

Still stuck? Open an issue — include `docker compose logs` and your (redacted)
config. See **[VIRTUAL-METER.md](VIRTUAL-METER.md)** for the engine internals and
how to add a new meter template.
