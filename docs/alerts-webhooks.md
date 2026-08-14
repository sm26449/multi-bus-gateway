# Alerts & webhooks

The gateway raises **two families of alerts** and delivers both over **MQTT**
and/or an **HTTP webhook**:

- **Infrastructure health** — a device or sink goes down, read latency stays
  high, the InfluxDB buffer backs up.
- **Value thresholds** — a register crosses its configured warning/danger
  limits ("grid current > 72 A"), evaluated by the built-in per-register
  **threshold engine** (off by default, see below).

The gateway is the *detector*: it emits compact JSON alert events,
rate-limited per key. Deduplication, routing, escalation and the actual
Telegram/SMS/e-mail fan-out are the job of **your webhook receiver /
notification system** (ntfy, Node-RED, Home Assistant, an SMS gateway, …) —
the gateway deliberately does not reimplement a notification pipeline.

## Signals

An alert fires on these conditions (each toggleable):

| Signal | Default | Fires when |
|---|---|---|
| `device` | on | a source device goes down / recovers |
| `sink`   | on | MQTT or InfluxDB disconnects / reconnects |
| `latency`| on | a source's read latency stays above `latency_ms` |
| `buffer` | on | the InfluxDB store-and-forward buffer grows past `buffer_points` (or drops points) |
| `threshold` | **off** | a register value crosses one of its warning/danger thresholds (see next section) |

Each alert is **rate-limited per key** (`min_interval_s`) so a flapping signal
can't spam. Every alert is also mirrored into the persisted event log and shown
on the **Status** page.

## Value thresholds (the threshold engine)

Every selected register can carry **thresholds**
(`dangerLow` / `warningLow` / `warningHigh` / `dangerHigh`) — the same limits
that color the dashboard and gauges. With `alerts.signals.threshold: true`,
those limits also become alert **events**:

- **Five bands, one band per value** — `danger_low`, `warning_low`, `normal`,
  `warning_high`, `danger_high`. A value sits in exactly one band, so a higher
  severity inherently suppresses the lower one (no double-firing).
- **Fires only on band transitions** — steady state is silent; a return to
  `normal` fires one "back to normal" info event. `AlertManager`'s per-key
  rate limit still applies as a backstop.
- **Fast to alarm, slow to clear (hysteresis)** — escalation fires on the raw
  limit; clearing requires the value to retreat past the boundary by
  `threshold_deadband_pct` (default 2 % of the boundary value). A value
  hovering exactly at a limit cannot flap.
- **Startup** — `threshold_alert_on_start` (default `true`): a value that is
  *already* in an alarm band on the very first evaluation fires once, so a
  condition that was true across a restart is still surfaced.
- **Stale suppression** — a register the device has stopped refreshing
  (down/frozen link) is **not** evaluated: no alarm can fire off frozen data.
  The bound is roughly 2.5× the register's poll interval (minimum 15 s); the
  band holds and evaluation resumes cleanly on reconnect. Band state is pruned
  when a register or device is removed, so a deleted limit can't leave a stuck
  alarm.
- **Off the hot path** — evaluation runs in the ~5 s health harvester over
  each device's live value store; the poll loop is untouched.

A threshold event has `key` = `thr:<device-id>:<address>`, `severity` =
`error` (danger band), `warn` (warning band) or `info` (back to normal), and a
message like `Grid current L1 = 74.2 A — above danger limit 72 A` /
`… — back to normal`.

## Configure (`config.yaml`, applied on restart; live-tunable from Config → Alerts)

```yaml
alerts:
  enabled: true
  mqtt: true                                   # publish to <mqtt.topic_prefix>/alert
  webhook_url: "https://ntfy.example/gateway"  # POST target; empty = webhook off
  webhook_headers: { "X-API-Key": "secret" }   # auth / any headers the receiver needs
  webhook_body: { "message": "{severity} {source}: {message}" }
  min_interval_s: 300
  latency_ms: 1000
  buffer_points: 1000
  signals: { device: true, sink: true, latency: true, buffer: true,
             threshold: true }                 # threshold defaults to false
  threshold_deadband_pct: 2.0                  # clear-side hysteresis, % of the boundary
  threshold_alert_on_start: true               # fire once if already in alarm at start
```

### The webhook payload

Every alert is the JSON object:

```json
{ "ts": 1751560000.0, "severity": "error", "source": "fronius-solar",
  "message": "device down", "key": "dev:fronius-solar", "host": "multi-bus-monitor" }
```

- **Without `webhook_body`** that raw object is POSTed as-is.
- **With `webhook_body`** (a dict), each string value is rendered with the alert
  fields as `{placeholders}` and the resulting dict is POSTed. Available fields:
  `{ts}` `{severity}` `{source}` `{message}` `{key}` `{host}`.

Example — mapping onto an SMS gateway that expects `{"message": "..."}`:

```yaml
  webhook_body: { "message": "[{severity}] {source}: {message}" }
```

MQTT subscribers get the raw alert object on `<topic_prefix>/alert` regardless of
`webhook_body` (the body template only shapes the webhook POST).

### Security notes

- The webhook has **no LAN/SSRF restriction** (unlike HTTP *device* fetches) —
  it is meant to reach external notifiers (ntfy, Telegram, an SMS gateway).
  Authenticate to the receiver with `webhook_headers` (e.g. an API key).
- Delivery is **best-effort** (fire-and-forget, no retry). A failed POST is
  logged and dropped. For guaranteed delivery, subscribe to the MQTT topic and
  let a durable consumer (Node-RED / your notification system) forward it.

## Test the wiring

You don't have to wait for a real alert. On the **Status** page, next to the
Alerts line, click **Test** — it fires a synthetic alert over the configured
channels and reports per-channel `sent` / `failed`. It bypasses `enabled` and the
rate limit, so you can verify config before going live.

A test-fire drives **real** outbound traffic (webhook → SMS, MQTT), so it is
guarded like a Modbus write:

- **Credentialed** — refused (`403`) unless login (`ui.auth`) is enabled or an
  `API_KEY` is set. On a default LAN-open deployment it can't be spammed anonymously.
- **Throttled** — a short cooldown between fires (`429` if you retry too soon).
- **No redirects** — the webhook POST carries your `webhook_headers` (e.g.
  `X-API-Key`); a `3xx` from the target is refused rather than followed, so those
  credentials can't be replayed to an unintended host.

API equivalent (send the API key / an authenticated session):

```bash
curl -X POST http://<gateway>:8080/api/alerts/test \
     -H 'Content-Type: application/json' -H 'X-API-Key: <key>' -d '{"message":"hello"}'
# -> { "delivered": true, "channels": { "mqtt": "sent", "webhook": "sent (HTTP 200)" },
#      "sent_body": { "message": "[info] test: hello" } }
```

`GET /api/alerts` returns the current config/channels + recently fired alerts.
