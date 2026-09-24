# Troubleshooting

Symptom → check → fix, one table per area, with the exact log lines the
gateway writes so you can `grep` for them. Start every investigation with
`docker compose logs --since 1h multi-bus-gateway` and the **Status** page;
`/health` and `/metrics` work without a login. Installation is covered in
[install.md](install.md), routine operations in [operations.md](operations.md).

## Cannot log in

| Symptom | Check | Fix |
|---|---|---|
| Fresh install, no password known | The banner `FIRST RUN — generated admin credentials (shown ONLY once):` is printed to stdout only when no `config.yaml` existed at boot | `docker compose logs multi-bus-gateway \| grep -A3 'FIRST RUN'` — the log is rotated at 10 MB × 3, so grab it early |
| The banner is gone from the logs | A `config.yaml` exists, so it will not print again | Stop the gateway, set `ui.auth.enabled: false` in `config/config.yaml`, start, set a new admin password under **Config → Security** and re-enable login there. (Deleting `config.yaml` would also regenerate a password — and lose your configuration.) |
| Login answers **429** `locked_out` with `retry_after_s` | Per-IP lockout after `lockout_threshold` failures (default 5) for `lockout_minutes` (default 5); `AUDIT … "action": "login", "status": "locked out"` in the log | Wait it out, or restart the container — the lockout counter is in memory, sessions survive in `config/sessions.json` |
| Every client is locked out at once | Behind a reverse proxy without `ui.trusted_proxies`, all clients share the proxy's IP | Set `ui.trusted_proxies` to the proxy address ([install.md §G](install.md#g-behind-a-reverse-proxy-traefik--nginx--caddy)) |
| **403** `forbidden (IP not in allowlist)` | `security.allowlist` does not contain your address — behind Docker's bridge you often appear as the docker gateway IP | Edit `security.allowlist` in `config/config.yaml`, restart; loopback is always allowed |
| The UI asks for an API key | `API_KEY` (or `JANITZA_API_KEY`) is set — every POST/PUT/PATCH/DELETE needs `X-API-Key`; the UI prompts once and remembers it | Enter the key; scripts send the header. Since 3.80.0 the login POSTs are outside the gate, so key + login work together |
| "Login refuses to enable" | A blank or default `admin` password is refused at every entry point (UI, hand edit, import, restore) | Set a real password first |
| Passkey enrollment/login fails | WebAuthn needs a secure context: `localhost` or a hostname over HTTPS; a raw IP is rejected | Use the hostname; `ui.canonical_url` steers browsers there |
| Logged out after an upgrade | Rotating any password revokes every session; a hash with < 100 000 PBKDF2 iterations is refused (3.80.0): `SECURITY: stored password hash declares … iterations` | Log in again; set a new password if the hash was refused |

## Device shows down / degraded / stale

The verdict comes from `data_health()`: the age of the last successful read
against a threshold of `max(stale_after_s, fastest_interval × 3 + 2)` s.

| State | Meaning |
|---|---|
| `ok` | a read succeeded within half the threshold and the link is up |
| `degraded` (amber) | age past half the threshold, **or** not connected yet (cold start, a reconnect in progress), **or** one poll group failing while others succeed (`stale_groups`) |
| `down` (red) | age past the threshold, or reads have been failing since boot |
| `idle` (grey) | the device is disabled / nothing is configured to poll |

| Symptom | Check | Fix |
|---|---|---|
| `Modbus device unreachable (192.168.1.100:502): <reason>` | The error taxonomy on the Status page: `timeout` = no answer, `exception_N` = the device answered with Modbus exception N (link fine, request wrong), `connection` = TCP/serial level | Host/port/unit id; Modbus TCP enabled on the device; firewall. **Diagnostics → probe** — an exception answer still proves a live device |
| `Modbus[<id>] device host:port not reachable after 30s` at boot | Boot waits up to 30 s for the TCP port before the first connect; pollers keep retrying regardless (`connection failed - pollers will keep retrying`) | Nothing if the device is simply slow; otherwise the wiring/address |
| Alert `down — not responding`, then `recovered — connected` minutes later | A device flips down only after a 45 s grace (`DOWN_GRACE_S`); a Fronius DataManager stalls for a few seconds several times a day and is filtered by it | Expected; a real outage stays down. The transition is in the event log at once either way |
| Alert `read latency 1200 ms exceeds 1000 ms` | `alerts.latency_ms` threshold; `gateway_device_read_latency_ms` in `/metrics` | A slow RTU line, a busy datalogger, or a Solar API device polled too fast — lengthen the interval, raise the threshold, or disable `alerts.signals.latency` |
| HTTP/JSON (Solar API) device degrades at night or mid-day | `HTTP device: initial fetch failed (…) — pollers will retry`; a datalogger answers slowly or not at all while it sleeps or is polled by another client | Longer poll interval; `drop_all_zero: true` for an inverter that answers zeros while asleep; `stale_after_s` above the device's quiet gaps |
| Values freeze but the device is `ok` | `stale_groups` on Status: one poll group's batch keeps failing (`batch_failures`) while the fastest group drives the timestamp | A register the slave rejects — add it to `illegal_registers`, or set `max_gap: 0` for a strict slave |
| Values look like garbage | Data type / word order | **Diagnostics → Register probe** shows the type × order matrix |
| RTU device flaps | Two masters on one line (a second device on the same bridge endpoint or `/dev` node) | One master per serial line / bridge endpoint — [rtu-serial.md §5](rtu-serial.md#5-multi-slave-on-one-bus-current-limit) |

## No data in Home Assistant

| Symptom | Check | Fix |
|---|---|---|
| No entities at all | `MQTT connected to mosquitto:1883` and `Published N HA discovery configs` in the log; `mqtt.ha_discovery.enabled: true`; `mqtt.ha_discovery.prefix` (default `homeassistant`) equals HA's discovery prefix | Enable discovery under **Config → MQTT**; both sides on the **same broker** (with the bundled stack HA must point at `<host>:1883`) |
| `MQTT connection failed: <reason code>` / `MQTT: all 10 connection attempts failed. Will retry in background.` | Broker host/port/credentials; TLS (`MQTT TLS setup FAILED … refusing to connect`) | Fix under **Config → MQTT** — applied live |
| `MQTT broker mosquitto:1883 not reachable after 30s` at boot | The bundled broker is not running, or you started only the gateway | `docker compose up -d mosquitto`, or point the sink at your broker |
| Entities exist but stay **unavailable** | Availability: `<prefix>/status` (retained, LWT `offline`) and each device's `<device prefix>/availability`; a broker restarted without persistence lost the retained `online` | The gateway re-asserts availability on every reconnect; `mosquitto_sub -v -t '<prefix>/status'` shows the current retained value |
| Entities never update | `publish_mode: changed` publishes only changes — a steady reading looks frozen | Set `mqtt.heartbeat_interval` (seconds) so unchanged values are republished with a fresh timestamp |
| Entities missing after renaming a device / changing its topic prefix | The routing identity is fixed after creation; discovery is keyed `mbg_dev_<device>_<addr>_<name>` | Delete the device (this clears its retained discovery) and recreate it, or use `mqtt.compat_aliases` during a topic migration |
| Old entities linger after a device was deleted | Retained discovery configs outlive a gateway that was stopped before the delete | Clear `homeassistant/+/mbg_dev_<id>/+/config` with `mosquitto_pub -r -n` ([operations.md](operations.md#uninstalling)) |
| No `number`/`select` entities for writable registers | Controls exist only for non-primary devices and only with `mqtt.allow_write_entities` **and** `security.allow_writes` | See *MQTT writes ignored* below |

## Nothing in InfluxDB

| Symptom | Check | Fix |
|---|---|---|
| The sink is off | `influxdb.enabled` (**Config → InfluxDB**) **and** the device's InfluxDB output toggle (device → *Edit* / *Outputs*) | Enable both; the per-device bucket is auto-created (`Bucket auto-create failed for '…' (non-fatal)` if the token cannot) |
| `InfluxDB rejected writes with HTTP 401 — token revoked/rotated or bucket missing. Points are buffered until this is fixed.` | Token, org, bucket; with the bundled stack the token is `DOCKER_INFLUXDB_INIT_ADMIN_TOKEN` from `.env`, org/bucket `multibus` | Paste the right token; buffered points replay on their own — `influx_auth_failed` clears |
| `InfluxDB write retry: …` then `InfluxDB batch failed permanently (…)` | URL reachable from the container? (`http://influxdb:8086` inside the stack, not `localhost`) | Fix the URL; the failed batch is recovered into the buffer and replayed |
| `InfluxDB connection failed: …` at boot | Same — plus the bundled InfluxDB still initialising | Wait for the monitor thread (`InfluxDB reconnection failed, monitor thread will keep trying`) or fix the URL |
| Buffer grows (`gateway_influx_buffer_points`, alert `buffer`) | Outage longer than `influxdb.buffer_minutes` / `buffer_max_points` drops the oldest points (`InfluxDB replay dropped N points`) | Restore connectivity; the buffer persists across restarts in `config/influx_buffer.jsonl` |
| `connected: true` but no points land | `writes_confirmed` / `last_confirm_age_s` in `/api/status → influxdb` | A rejected batch shows there first; check the log lines above |
| History tab empty for a new device | The bucket was created with 90-day retention at device creation; History reads the device's own bucket | Wait for the first write interval (`influxdb.write_interval`, default 5 s) |

## Virtual meter not seen by the consumer

| Symptom | Check | Fix |
|---|---|---|
| Connection refused, meter never listened | The instance starts **disabled**; `virtual meter '<id>' started (port=1502)` in the log | Enable it on **Virtual Meters → Meters** |
| Refused from the LAN, works from the host | The instance port is outside the published `1502–1512` range | Pick a port inside it, or widen `VMETER_PORT_START/END` and recreate the container |
| `:502` instance fails with a bind `PermissionError`, `1502+` serve | The non-root process needs the per-container sysctl `net.ipv4.ip_unprivileged_port_start=0` (shipped in the compose; `--sysctl` with `docker run`) | Add the sysctl; on bare metal grant `CAP_NET_BIND_SERVICE` |
| `virtual meter <id> STOPPED responding (stale/shutdown)` | The staleness watchdog: all sources stale → the server goes silent so the consumer's meter-loss fail-safe engages | Fix the source device first; the meter resumes by itself once data is fresh |
| Consumer reads a Modbus exception | `on_stale: fail` — a read touching a stale row is refused (no partial truth); `hold` does the same after `max_hold_s` | Intended; check the source's freshness and the per-row `stale_after_s` |
| `vmeter <id>: source device '…' not found — serving nothing (fail-safe)` | The source device id changed or was deleted | Edit the instance's source device |
| `virtual meter instance <template> failed to start: …` | Port already in use (another instance, or the host), or a template error | One port per instance; the **Logs**/**Decode** tabs show the map |
| `virtual meter <id> server thread died — restarting` / `alive but not serving — force-restarting` | The supervisor recovering a wedged server; `/health` returns 503 while a meter is `down` | Look at the lines before it; a repeat is worth an issue |
| Consumer connects but reads zeros / wrong scale | Template row (address, type, word order, source name) | **Logs** shows every request and answer; **Decode** maps an address range back to the source variables |

## MQTT writes ignored

A command arriving over MQTT (an HA `number`/`select`, a controller's command
topic) is executed only when the whole gate chain passes — the log names
the first gate that failed:

| Log line | Gate | Fix |
|---|---|---|
| `MQTT command <name> ignored: writes over MQTT are off (security.allow_writes + mqtt.allow_write_entities)` | Both switches must be on | **Config → Security** `allow_writes`, **Config → MQTT** `allow_write_entities` — and know that this turns broker-publish access into hardware-write access |
| `MQTT command <name> ignored: mqtt.allow_write_entities is on but the broker connection is anonymous — set mqtt.username/password (broker ACLs are the only authentication a publish carries)` | **3.80.0:** the broker session must be authenticated | The bundled broker already requires credentials (3.81.0, `MQTT_USERNAME`/`MQTT_PASSWORD` in `.env`); set the same pair as `mqtt.username`/`password` in the gateway (Config → MQTT) |
| `MQTT command on <topic> ignored: retained message (stale write replay)` | Retained messages on command topics are never obeyed (they would re-actuate hardware on every reconnect) | Publish commands without `retain`; the gateway clears the retained copy at subscribe |
| `MQTT command for <device> addr=<n> failed: …` | The write handler refused: register not declared writable in the template, value outside `write_min`/`write_max`, the primary device (always read-only), an HTTP/JSON device, or the per-IP rate limit | Fix the template row or the value; use a non-primary device |
| `MQTT command on <topic> dropped: write queue full` | Commands are executed one at a time off paho's thread; a flood backs up | Slow the sender |
| API write returns 403 `writes require authentication — enable login (ui.auth) or set an API_KEY` | Anonymous writes are refused even with the gate open | Log in, or send `X-API-Key` |

Every attempt, accepted or refused, is an `AUDIT` line.

## RTU bridge unreachable

| Symptom | Check | Fix |
|---|---|---|
| Scan says `serial bridge unreachable at http://mbg-serial-bridge:7000: …` | `docker ps` — the bridge runs only with `--profile rtu-bridge`; the gateway resolves the container **name** on the Docker network | `docker compose --profile rtu-bridge up -d` |
| Same, but the bridge is running | The container is not named `mbg-serial-bridge` (it was `pv-stack-serial-bridge` before 3.79.0), or it is on another network | Set `SERIAL_BRIDGE_URL` on the gateway to the real name/port, or rename the container |
| Adapter missing from Scan | Not plugged in, listed in `BRIDGE_EXCLUDE` (shown as unavailable), or a `/dev` node that is not ttyUSB/ttyACM (the cgroup rules allow only majors 188/166) | `docker logs mbg-serial-bridge`; adjust `BRIDGE_EXCLUDE` |
| `[supervisor] PORTMAP UNREADABLE (…) — starting empty` | The port map in the `serial-bridge-data` volume is corrupt; endpoints get reassigned | Re-scan and re-bind the affected devices |
| Adapter renumbers after a replug | Direct-serial mode (`/dev/ttyUSBn`) — exactly what the bridge avoids | Move the device to *Over network* |
| Test connection times out | Unit id, baud (the bridge is 9600 8N1), A/B swapped, missing termination | The frames are identical over TCP and serial — the template and wiring are the suspects |

## Container restarts or will not start

| Symptom | Check | Fix |
|---|---|---|
| `Error loading config: … — broken file copied to config/config.yaml.bad; config saves are DISABLED until it is repaired` then `SELF-HEAL: loading last known-good snapshot config/config.yaml.good` | A bad manual edit or a torn write; the gateway keeps running on the `.good` copy and refuses saves (`/api/status → config.saves_disabled`) | Repair `config.yaml` (diff it against `.good`), restart; or restore a snapshot |
| `config failed to load (…) — restoring last known good` then `config restored from last known good — review recent changes` | The boot seatbelt restored `snapshots/lkg.zip` (stamped only after ~5 minutes of healthy uptime with at least one successful read) | Review what the LKG undid: **Config → Backup & Restore → Snapshots & Rollback → Diff** |
| `LKG restore failed` and the process exits | No LKG yet (a fresh install that never reached a healthy boot) and the file is unparsable | Fix `config.yaml` by hand, or move it away to trigger a first-run |
| `config.yaml lost devices relative to config/config.yaml.good — NOT refreshing the last-known-good snapshot` | A truncated-but-parsable file; the plausibility gate protects the `.good` copy | Restore the missing `devices[]`, or a snapshot |
| `config.yaml was written by gateway 3.80.1 but 3.80.0 is running — … DROPPED by the next save` | You downgraded | Read [upgrade-guide.md](upgrade-guide.md) before saving anything |
| `docker compose ps` says **unhealthy** but the container runs | The probe hits `http://localhost:$UI_PORT/health`; 503 means an enabled virtual meter is `down` | `curl /health` and the virtual-meter table above; an unhealthy status does not restart the container |
| Restart loop | `docker compose logs --tail 200 multi-bus-gateway` — the traceback before `Shutting down...` | Typical: a port in use (`UI_PORT`, a meter port), or a config error not caught by the seatbelt |
| Cannot write `snapshots/`, `sessions.json` | You started with `docker run --user` — the entrypoint's `chown` is skipped | Match the directory owner to that user |
| `still disconnecting — proceeding with shutdown` at stop | A wedged device disconnect; shutdown is bounded (8 s per device, in parallel) so the Influx buffer still flushes | Harmless; investigate the device if it repeats |

## High CPU

| Symptom | Check | Fix |
|---|---|---|
| Steady high CPU on a Pi | **Status → resources** (CPU %, RSS, threads); total CPU scales with **polls per second** under one GIL ([MANUAL.md §18b](MANUAL.md#18b-running-on-constrained-hardware-raspberry-pi)) | Realtime group at 1 s; reserve 250 ms for the one control-loop register; counters and diagnostics in `normal`/`slow` |
| CPU jumps while a browser tab is open | The bus monitor (Diagnostics) captures every frame into a RAM ring while enabled | Stop the capture; it never persists and boots disabled |
| Log volume explodes | `--debug` on the command line — DEBUG output at a sub-second cadence is expensive | Run at the default `INFO` level |
| Spikes every few seconds with many devices | Poll groups firing in lock-step, notably over a shared RTU bridge | `polling.startup_jitter_s` (global) or `startup_jitter_s` per device |
| High CPU + `MQTT publish error` / `InfluxDB write error` bursts | A sink retry loop | Fix the sink first; the buffer is bounded either way |

## Time and timezone

| Symptom | Check | Fix |
|---|---|---|
| Log timestamps are off by hours | The gateway logs in the container's local time; the compose passes `TZ` only to the ESPHome service | Add `TZ=${TZ}` to the gateway's `environment:` in a local override if you want local-time logs; InfluxDB points are UTC regardless |
| Monthly energy totals split on the wrong day | `ui.timezone` (**Config → General**, IANA name, default `Europe/Bucharest`) drives the calendar boundaries — not `TZ` | Set it; applied live |
| Points land in the past / future | InfluxDB timestamps are the **read** time from the host clock | Run NTP on the host; buffered points keep their original stamps by design |
| Staleness verdicts flap after a clock step | They don't: staleness uses the monotonic clock (`staleness_age_s` is NTP-step-proof) | If the device still reads stale, the cause is the link, not the clock |
| Logged out after a month of daily use | 7-day sliding with a 30-day absolute cap (3.80.0) | Expected at the cap; log in again |

Still stuck? Open an issue with `docker compose logs` and a redacted config
(the export strips secrets by default).

See also: [MANUAL.md §19](MANUAL.md#19-troubleshooting) ·
[rtu-serial.md §6](rtu-serial.md#6-troubleshooting) ·
[reliability.md](reliability.md) · [operations.md](operations.md)

Verified against 3.80.1
