# PQ event recorder (Janitza / Jasic)

Janitza UMG-series power analyzers (604/605/508/511/512) run an on-device
**power-quality event recorder**: voltage dips/swells/outages, rapid voltage
changes (RVC) and frequency excursions, each with a bound/min/max/avg record —
plus ~50 s **half-wave-RMS capture windows** around each event. Two problems
make that data hard to use operationally:

1. The event list is a small ring (32 entries on the UMG512). On an agitated
   grid day it wraps within hours — by the time someone investigates an
   outage, the events are gone.
2. The device's **Modbus map only exposes lifetime counters** (UMG512:
   `_EVT_COUNT` @6634, `_FLAG_COUNT` @6636, `_TRANS_COUNT` @6638) — not the
   records. The records are served by the Jasic web firmware over plain,
   unauthenticated HTTP.

The gateway's PQ recorder feature polls those HTTP endpoints and archives
everything through the normal sinks, so PQ history becomes permanent,
queryable and visible next to the rest of the device's data.

## Enabling

Per device, config block `pq_recorder:` (primary = flat section, other
devices inside their `devices[]` entry — see
[config-reference.md](config-reference.md)), or at runtime:

```bash
curl -X POST 'http://gateway:8080/api/pq/config?device=<id>' \
  -H 'Content-Type: application/json' -H 'X-API-Key: <key>' \
  -d '{"enabled": true, "poll_s": 300, "archive_waveforms": true}'
```

Only devices whose template is in the Jasic family accept it
(`multibus/pq_recorder.py: JASIC_PQ_TEMPLATES`).

## Device endpoints used (Jasic web firmware, no auth)

| Endpoint | Content |
|---|---|
| `GET /lib/events/getevt.html` | `{"events": [[start, end, bound, max, min, avg, reason_lo, reason_hi], …]}` — device clock is UTC epoch seconds |
| `GET /json.do?_EVT_COUNT,_FLAG_COUNT,_TRANS_COUNT,` | lifetime counters (the generic `json.do` reads any named variable) |
| `GET /lib/events/hww.html` | half-wave-RMS capture-window index (`[[start, end, trigger, …], …]`, ~50 s windows, retained days on-device) |
| `GET /lib/events/mk_hww.html?_hww_nr=<start>&_val_nr=<ch>` | one channel's RMS trace, 10 ms steps (`{"data": [[ts, value], …]}`) |

### Reason bitmask

64-bit reason = `reason_hi << 32 | reason_lo`; **one nibble per cause, one
bit per channel** inside the nibble (L1..L4 for L/N causes, the three pairs
for L/L causes). Validated against the firmware's own `lib/events/events.js`:

| Nibble (in LO) | Cause | | Nibble (in HI) | Cause |
|---|---|---|---|---|
| `0x0000000F` | Over voltage L/N | | `0x00000F00` | Over frequency |
| `0x000000F0` | Under voltage L/N | | `0x0000F000` | Under frequency |
| `0x00000F00` | Voltage outage L/N | | `0x000F0000` | df/dt event |
| `0x0000F000` | Over current | | `0x00F00000` | Rapid voltage change L/N |
| `0x00F00000` | Over voltage L/L | | `0x0F000000` | Rapid voltage change L/L |
| `0x0F000000` | Under voltage L/L | | `0xF0000000` | RVC multi-phase |
| `0xF0000000` | Voltage outage L/L | | | |

RMS trace channel indexes (`_val_nr`): 0–3 = UL1..UL4, 4–7 = IL1..IL4,
16–18 = UL1-L2 / UL2-L3 / UL3-L1.

## Data model (InfluxDB, the device's bucket)

| Measurement | Tags | Fields | Timestamp |
|---|---|---|---|
| `pq_events` | `device`, `cause`, `channel` | `duration_ms`, `bound`, `vmax`, `vmin`, `vavg`, `reason_lo`, `reason_hi` | event start (ms) |
| `pq_counters` | `device` | `evt_count`, `flag_count`, `trans_count` | poll time |
| `pq_waveforms` | `device`, `event` (event-start ms), `channel` (e.g. `UL2`) | `value` | sample time (ms) |

Point identity = timestamp + tags, so re-reading the same ring every poll is
**idempotent** — and self-healing after an InfluxDB outage (writes go through
the publisher's replay buffer like register data).

Waveforms are archived only for **new** events (detected via a persisted
high-water mark, `config/pq_state_<device>.json`) and only for the channels
the event's causes implicate — a few × ~5000 points per event, so volume
stays proportional to how bad the grid actually is. The very first sync of a
device archives the ring's events but does not announce them or burst-fetch
waveforms.

## Gateway API

| Method | Path | Description | Role |
|---|---|---|---|
| GET | `/api/pq/status` | Recorder support/enabled/health + counters per device | viewer |
| GET | `/api/pq/events?device=&start=-30d&limit=200` | Archived events, newest first (full retained history, not the ring) | viewer |
| GET | `/api/pq/waveform?event=<ms>&channel=UL2&device=` | One archived RMS trace | viewer |
| POST | `/api/pq/config?device=` | Set `{enabled, poll_s, archive_waveforms, base_url}` and (re)start the poller | admin |

## UI

Device workspace → **Power Quality** tab (enabled when the template is a
Jasic family member and the InfluxDB output is on): archived event list with
decoded causes, click-through to the recorded waveform, lifetime counters in
the footer.

Device workspace → **Outputs** tab → "PQ event recorder" card: enable
toggle, poll interval, waveform archiving, base-URL override, live recorder
status — the full configuration without touching config.yaml.

## Alerts

Every NEW event also fires the gateway AlertManager (`alerts:` config block
— MQTT `<prefix>/alert` + webhook): outage causes → `critical`,
voltage/current/frequency excursions → `warning`, bare rapid voltage
changes → `info`. The alert key is `pq_<device>`, so the manager's
`min_interval_s` folds an event burst into one notification.

## EN 50160 note

The meter does **not** expose computed EN 50160 verdict registers — its own
web page derives the indices client-side from FFT/average registers. A
weekly compliance verdict therefore belongs in downstream analytics over
the archived `pq_events`/`pq_waveforms` + the continuously polled
voltage/THD/unbalance series, not in the gateway.

## MQTT

Each new event is published (retained) to `<device topic prefix>/pq/event`:

```json
{"start": 1787808595.696, "end": 1787808595.826, "duration_ms": 130.0,
 "bound": 11.6, "vmax": 19.8, "vmin": 5.6, "vavg": 233.4,
 "causes": [{"cause": "rapid_voltage_change_ln", "channel": "L3"}]}
```

## Operational notes

- **During a mains outage the meter itself is usually dark** (its aux supply
  tends to come from the measured side), so the outage's *onset* event may
  never be written by the device, and polls fail while the grid is down —
  the recorder logs the first failure of a streak at info level and keeps
  retrying. If you need PQ visibility *inside* outages, put the meter's aux
  supply on a UPS.
- Poll floor is 30 s; the default 300 s is comfortable — the ring holds 32
  events, and the high-water mark makes each event announced exactly once.
