# Alerts & webhooks

The gateway can raise **infrastructure-health alerts** and deliver them over
**MQTT** and/or an **HTTP webhook**. This is health/plumbing alerting (a device
or sink goes down, latency spikes, the InfluxDB buffer backs up) — **not**
value-based alerting ("grid current > 72 A"); that lives in `alertd`.

## Signals

An alert fires on these conditions (each toggleable):

| Signal | Fires when |
|---|---|
| `device` | a source device goes down / recovers |
| `sink`   | MQTT or InfluxDB disconnects / reconnects |
| `latency`| a source's read latency stays above `latency_ms` |
| `buffer` | the InfluxDB store-and-forward buffer grows past `buffer_points` (or drops points) |

Each alert is **rate-limited per key** (`min_interval_s`) so a flapping signal
can't spam. Every alert is also mirrored into the persisted event log and shown
on the **Status** page.

## Configure (`config.yaml`, applied on restart)

```yaml
alerts:
  enabled: true
  mqtt: true                                   # publish to <mqtt.topic_prefix>/alert
  webhook_url: "http://sms-gateway:5080/send"  # POST target; empty = webhook off
  webhook_headers: { "X-API-Key": "secret" }   # auth / any headers the receiver needs
  webhook_body: { "message": "{severity} {source}: {message}" }
  min_interval_s: 300
  latency_ms: 1000
  buffer_points: 1000
  signals: { device: true, sink: true, latency: true, buffer: true }
```

### The webhook payload

Every alert is the JSON object:

```json
{ "ts": 1751560000.0, "severity": "error", "source": "fronius-solar",
  "message": "device down", "key": "dev:fronius-solar", "host": "janitza-monitor" }
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
  let a durable consumer (Node-RED / alertd) forward it.

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
