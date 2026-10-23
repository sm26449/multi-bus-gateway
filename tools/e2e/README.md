# E2E — Power Quality feature (Playwright)

Validates the full PQ recorder flow in a real browser against a running
gateway: login, capability gating (`pq_supported` from the device template),
event list (day grouping, severity dots, auto-select), waveform rendering
(archived or live read-through from the meter), the Outputs "PQ event
recorder" card, and the `POST /api/pq/config` save round-trip.

```bash
cd tools/e2e
npm install playwright@1.49.1        # last line supporting Node 18
npx playwright install chromium
MBG_URL=http://localhost:8080 MBG_USER=admin MBG_PASS=… node pq_e2e.mjs
```

Notes:
- Run it against a STAGING/ephemeral instance, not production: the config
  save round-trip briefly flips the recorder's poll interval.
- `SHOTS_DIR` (default `./shots`) receives step-by-step screenshots.
- The script dismisses the "Connect your first device" onboarding dialog if
  the instance has no reachable device.
- Exit code 0 = all checks passed; failures are listed on stdout.

## Endpoint page (P4)

`endpoint_page_e2e.mjs` validates the endpoint as a first-class entity: the way in
from the devices list, the header census/status, the aggregate grid, the unit
table (health, hand-written ids and names), the per-unit probe, the
endpoint-totals toggle, a unit rename, and the endpoint-aware unit workspace.

```bash
cd tools/e2e
MBG_URL=http://localhost:18080 ENDPOINT=sunfield node endpoint_page_e2e.mjs
```

It writes (toggles a setting, renames a unit), so point it at an EPHEMERAL
instance. The header of the script carries the throwaway-container line and the
config it expects — an endpoint on a TEST-NET host, so the offline/degraded paths
are the ones under test.

## Device logs

`device_logs_e2e.mjs` validates the device page's Logs tab: the failed batches
listed with the address and size they asked for, the live per-group state
beside them, the counters, the problems-only filter, live refresh, and that
leaving the tab stops the tick.

Point the instance at a host that REFUSES (127.0.0.1:9), so the failure paths
are the ones under test:

```bash
MBG_URL=http://localhost:18081 DEVICE=sunfield-u1 \
  CHROMIUM_PATH=<chrome> node device_logs_e2e.mjs
```

## Plant groups

`plant_groups_e2e.mjs` drives the whole operator path over a plant that holds
more than one kind of thing: a card per group, roles, which group owns the
headline topic, per-group enable/disable, adding a source to ONE group,
reordering precedence with the arrows, removing a source (and the last one being
refused), adding and removing a whole group, and the API refusing two groups
that claim the same unit id.

Config it expects: auth off, one endpoint with an `inverters` group (units 1, 2)
and a `grid` group (unit 240, id `<endpoint>-meter-240`), pointed at a host that
REFUSES so the offline paths are the ones under test.

```bash
MBG_URL=http://localhost:18085 ENDPOINT=sunfield \
  CHROMIUM_PATH=<chrome> node plant_groups_e2e.mjs
```

## Add Installation wizard

`plant_wizard_e2e.mjs` drives the four-step wizard exactly as an operator would
and then checks that what was CREATED matches what the review step promised —
device ids, sources in precedence order, and the topic each group publishes on.

It also pins the refusals that must happen before anything is created: a bad
plant id, a group with no units, and one unit id claimed by two groups.

```bash
MBG_URL=http://localhost:18086 CHROMIUM_PATH=<chrome> node plant_wizard_e2e.mjs
```

Config it expects: auth off and NO endpoints yet. The host never has to answer.

## Per-source measurements

`source_registers_e2e.mjs` drives ticking fields on a unit reached two ways: the
picker appears only when there IS a choice, switching source switches both the
catalog and the selected list, and a save lands in one source's file while the
other is untouched.

```bash
MBG_URL=http://localhost:18088 DEVICE=pv-u1 \
  CHROMIUM_PATH=<chrome> node source_registers_e2e.mjs
```

Config it expects: auth off, one endpoint whose group declares TWO sources
(`solar_api` over HTTP, `sunspec` over Modbus) on units 1 and 2.

## Add Installation wizard (against a stand-in datalogger)

`plant_wizard_e2e.mjs` now drives the wizard against `fake_solar_api.mjs`, a
Solar API stand-in on loopback (3 inverters, 1 meter): the segmented choice of
how the datalogger is reached, Test in words, what was found as ticks, the
filtered template list for a group by hand, labelled intervals, the review's
topics, and that what was created matches the review.

```bash
FAKE_PORT=18099 node fake_solar_api.mjs &
MBG_URL=http://localhost:18086 FAKE=127.0.0.1:18099 CHROMIUM_PATH=<chrome> \
  node plant_wizard_e2e.mjs
```

Config it expects: auth off, no endpoints, `security.allow_nonlan_http_devices:
true` (loopback is otherwise refused by the discovery guard).

## Installation safety

`installation_safety_e2e.mjs` guards the three ways the installation page could
lie or destroy: Edit → Save on a grouped installation keeps every group and
source (the dialog shows only the name and the output switches), Test units asks
every source and says its verdict in words, and a unit with nothing selected is
idle — grey, named so, not counted online.

```bash
MBG_URL=http://localhost:18087 ENDPOINT=sunfield CHROMIUM_PATH=<chrome> \
  node installation_safety_e2e.mjs
```

Config it expects (see the script header): auth off; one grouped endpoint with
`inverters` (units 1, 2; an HTTP and a Modbus source), `grid` (unit 240) and
`spare` (unit 7, no template) on hosts that REFUSE. It renames the installation
and puts the name back.

## UI sweep

`ui_sweep.mjs` is not a test: it walks every page, every unit tab, every modal
and both wizards, screenshots each (light, dark, phone width) and reports raw
i18n keys, horizontal overflow and console errors to `/tmp/audit/report.json`.
It is the regression walk behind the installations UI audit (operator notes).

## Unit page

`unit_page_e2e.mjs` drives the page of a unit that belongs to an installation:
the list row naming its sources and rhythms, the three-level breadcrumb, the
Overview stating every source, the **Read via** tab (no connection form, no
Save intervals / Test connection, the name as the one editable thing, a rename
that lands on the installation and keeps every source), Outputs pointing to the
installation, the back arrow returning to it, the footer following the page,
and the Status page naming the sources' protocols.

```bash
MBG_URL=http://localhost:18087 ENDPOINT=sunfield UNIT=sunfield-u1 \
  CHROMIUM_PATH=<chrome> node unit_page_e2e.mjs
```

Uses the installation-safety fixture.

## Measurements tab

`measurements_e2e.mjs` drives the Measurements tab of a unit read several ways:
Selected first, the picker's census (`solar_api · HTTP · every 5 s · 3/7
ticked`), one classification (no "other" for canonical fields), "where" as a
JSON path or a Modbus address with its type, the interval in words, a lock on
template rows, ticking in place saved at once in both views without touching
the other source's map, and the single keyboard-reachable menu (Write hidden
for a JSON source). It puts the selection back at the end.

```bash
MBG_URL=http://localhost:18087 UNIT=sunfield-u1 CHROMIUM_PATH=<chrome> \
  node measurements_e2e.mjs
```

Uses the installation-safety fixture.

## CSP smoke

`csp_smoke.mjs` checks the browser side of the security model (3.82.0): the
login shell and every page under `script-src 'self'`, modal open/close through
`data-action`, `data-on="change|input"` dispatch, `data-with-value`, the
`_navigate` helper, and a console free of CSP violations, unknown actions and
page errors. Any instance will do — a fresh one with the generated password
is enough.

```bash
MBG_URL=http://localhost:18090 MBG_USER=admin MBG_PASS=… node csp_smoke.mjs
```

## Power limit

`power_limit_e2e.mjs` drives the operator's hand on the power-limit action:
the *Limit…* button on the inverter card only, the dialog (limit, reverts
after — 600 s by default —, ramp, scope), the confirmation, and the outcome
in words per unit. Against the safety fixture (writes off) the honest outcome
is a refusal, which must be shown.

```bash
MBG_URL=http://localhost:18087 ENDPOINT=sunfield CHROMIUM_PATH=<chrome> node power_limit_e2e.mjs
```

## Docs screenshots

`capture_docs_screenshots.mjs` regenerates every image the documentation embeds
(`docs/img/guide/01…22*.png` at 1440×950 and `docs/img/vm-logs.png` at 2×) from
a running gateway, always in the same viewport, light theme and language, so
they never drift from the interface. Point it at a throwaway demo instance
seeded with a primary meter (selected registers), a couple of units, an
installation with an inverter group that offers commands, a virtual meter with a
client reading it, a rule and a snapshot — never at production.

```bash
MBG_URL=http://localhost:18090 MBG_USER=admin MBG_PASS=… CHROME_PATH=<chrome> \
  node capture_docs_screenshots.mjs          # SUNSPEC_HOST/PORT for the scan panel
```

## New user, every protocol

`new_user_e2e.mjs` walks the path of someone who has a register spec and a
device and nothing else: maps written by CSV import (word order and "read over"
from the form, an `fc` column for FC1/FC2/FC4), YAML import (refusing a map
that puts two rows on one address), and the visual editor (a coil row, HTTP and
MQTT maps with json_path / topic, a poll interval); one device per protocol
through the Add Device wizard (Modbus TCP, Modbus RTU on a serial line, HTTP —
loopback refused, LAN accepted —, MQTT); EXACT decoded values from simulators
that hold known numbers; the wrong word order found in Diagnostics → Probe and
fixed there with no restart; the dashboard; a virtual meter re-serving two
values read back with pymodbus, its export and re-import; and the rules
import/export smoke.

```bash
cd tools/e2e
MBG_URL=http://127.0.0.1:8099 CTR=mbg-ui-sandbox CHROMIUM_PATH=<chrome> node new_user_e2e.mjs
```

Config it expects: auth off, a container it may `docker exec` into (app user
uid 10001, pymodbus + paho-mqtt installed) on `pv-stack-network`, the vmeter
port range starting at a free port. The script starts its own throwaway
anonymous broker (`mbg-e2e-mqtt`) and `sims/new_user_sims.py` inside the
container, and removes every `e2e_nu_*` / `e2e-nu-*` thing it made, at the start
and at the end. Lines starting `NOTE` are UX observations, not failures.

## Bridges (RS-485 over the network)

`bridges_e2e.mjs` drives the Devices page's bridges against two simulated boxes
(`sims/bridge_sims.py`, started inside the container): a TRANSPARENT converter
(raw RTU over TCP, slaves 1 and 2, one client at a time — newest wins, and it
counts its accepts) and a Modbus TCP GATEWAY (pymodbus, units 1 and 2). It adds
a transparent bridge in the modal, two slaves on one bus with the wizard (main
button and the bus's "+ Device"), checks their exact values and that they ride
ONE connection, the duplicate-unit refusal, the wizard's Test through the
shared bus while they poll, Check mode (right type, and both wrong types), a
device behind the gateway (resolves to Modbus TCP), editing the bridge's bus
port and host (devices follow; the sim sees the new client address), the MBG
serial bridge's Set-up command with its token and its offline reason, delete
refused while in use, and the Devices page at 390 px.

```bash
cd tools/e2e
MBG_URL=http://127.0.0.1:8099 CTR=mbg-ui-sandbox CHROMIUM_PATH=<chrome> node bridges_e2e.mjs
```

Config it expects: auth off, a container it may `docker exec` into (app user
uid 10001, pymodbus) on `pv-stack-network`, ports 14196-14198 and 15502 free in
it. It removes every `e2e_br_*` / `e2e-br-*` thing it made at the start and the
end. `BUG` lines are product bugs known when it was written: the check states
the right behaviour, does not fail the run unless `STRICT=1`, and prints
`FIXED` once the product is right.

## Dialogs in a test (`lib/dialogs.mjs`)

The UI asks through its own dialog (`ui/js/app-dialogs.js`), never the
browser's `confirm()`/`prompt()`/`alert()`. A script answers it with
`watchDialogs(page)` (called before `page.goto`): `dlg.answer` is the default
answer, `dlg.answers.push(…)` queues the next ones (`true`/`false` for a
confirm; an object of field values, a bare value for the first field, or
`null` for a dialog with fields), `dlg.seen` records each dialog's title and
message, and `dlg.native` counts any native dialog that still fired.
`dialogs_e2e.mjs` drives the dialog itself by keyboard and mouse.

## Throwaway fixtures (`fixture_up.sh`)

The rules, commands and installation scripts run against a throwaway gateway
built from `fixtures/installation_safety/`: `base.yaml` plus one variant, with
every address on the container's own loopback where nothing listens — every
read and every write is refused, nothing real is reached. `fixture_up.sh`
rebuilds its config from scratch on each start and runs this checkout's `ui/`
and `multibus/` (image `MBG_IMAGE`, default `multi-bus-gateway:latest`):

    tools/e2e/fixture_up.sh safety 18087                    # mbg-fx-safety
    tools/e2e/fixture_up.sh plant  18085                    # mbg-fx-plant
    tools/e2e/fixture_up.sh drive  18090                    # mbg-fx-drive, auth off, writes off
    tools/e2e/fixture_up.sh drive  18090 --writes --login <pass>   # login on, writes on

| Variant | What it holds | Scripts |
|---|---|---|
| `safety` | installation `sunfield`: `inverters` (units 1, 2; sources `solar_api` http + `sunspec` tcp; power_limit/restore), `grid` (unit 240 as `sunfield-meter-240`), `spare` (unit 7, no template → idle) | `installation_safety_e2e`, `rules_e2e`, `commands_e2e`, `attributes_e2e` (`ATTR_DEVICE=sunfield-meter-240`), `attributes_use_e2e`, `endpoint_outputs_e2e`, `new_user_e2e` |
| `plant` | `sunfield` with `inverters` (1, 2; `sunspec`) and `grid` (240) only | `plant_groups_e2e` |
| `drive` | `vfd1` (delta_vfd_el, `run_at` enabled) | `rules_trim_e2e`, `rules_schedule_e2e`, `rules_timers_e2e`; with `--writes --login`: `rules_layers_e2e`, `sequences_e2e` (`MBG_USER=admin MBG_PASS=<pass>`) |

Run the safety scripts on a fresh start (`rules_e2e` and `commands_e2e`
expect writes off; `installation_safety_e2e` puts the name back itself).
Extra setup some scripts need on `mbg-fx-safety`:

- `attributes_use_e2e` — a location and a grid connection, then
  `E2E_TARGET=sunfield-u1/power_limit`:

      curl -XPUT -H 'Content-Type: application/json' localhost:18087/api/attributes/site/location \
        -d '{"latitude_deg":48.86,"longitude_deg":2.35}'
      curl -XPUT -H 'Content-Type: application/json' localhost:18087/api/attributes/devices/umg512/grid_connection \
        -d '{"role":"connection_point","max_export_kw":45,"max_import_kw":60}'

- `endpoint_outputs_e2e` — the fake pack bus:
  `docker cp tools/e2e/tap_feeder.py mbg-fx-safety:/tmp/ && docker exec -d -u 10001 -e TAP_UNITS=2,3 mbg-fx-safety python /tmp/tap_feeder.py`
- `new_user_e2e` — a private network for its throwaway broker:
  `docker network create mbg-fx-net; docker network connect mbg-fx-net mbg-fx-safety`,
  then `CTR=mbg-fx-safety DOCKER_NET=mbg-fx-net`.

Stop with `docker rm -f mbg-fx-<variant>`.

## Finding at scale (`findability_e2e.mjs`)

Search, filter chips, tree / list and the address (`#/devices?…`) on the
Devices and Templates pages run against the `unchanged_save` sandbox. With
`SCALE_URL` the script also measures the pages at 200 devices / 100
templates — a second safety gateway filled by the scale loader:

    MBG_FX_NAME=mbg-fx-scale tools/e2e/fixture_up.sh safety 18096 --writes
    python3 tools/e2e/fixtures/scale/load_scale.py http://127.0.0.1:18096 200 100
    SCALE_URL=http://127.0.0.1:18096 MBG_URL=http://127.0.0.1:8097 node tools/e2e/findability_e2e.mjs

Every scale device is created off, at a loopback port where nothing listens.
The timings are printed; they are targets, not gates.

## The shared pickers (`pickers_e2e.mjs`)

A rule made with the keyboard alone (target found by typing, signal
completed `device.` then `.field`, a misspelt field said with the nearest
one), a calculated formula completing a device's own names, a group's
template list (its kind first, the others folded, vendor chips), the device
wizard's template panel, and the list on screen at phone width in both
themes. On the safety fixture; the one rule it saves is deleted at the end:

    tools/e2e/fixture_up.sh safety 18087
    CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18087 OUT=<dir> node tools/e2e/pickers_e2e.mjs

## The device page's tabs (`lib/device-page.mjs`)

The device page shows its tabs in three groups (Live, Configure, Diagnose),
one strip at a time: a tab of another group is out of sight until its group
is picked. A script opens a tab as a user does — `openDeviceTab(page, 'meters')`
clicks the group, then the tab (`edit` is the earlier name of `connection`) —
and `deviceAction(page, '#ddvDeleteBtn')` opens the header's ⋯ menu and
clicks an item (a held-back item still answers, it says why). The next
reorganisation then changes this file only. `device_page_e2e.mjs` covers the
groups, the keyboard, links, the Meters tab and a meter-only device on the
`safety` fixture with `--writes`.

## The connection form (`connform_e2e.mjs`, `connform_model_test.mjs`)

One form says how a device is reached, in Add Device and on a device's
Configure → Connection tab (`ui/js/app-connform.js`); its model and what it
sends are `ui/js/connform-model.js`. Three checks, chained:

- `tests/test_connform_payloads.py` serves every way a device is reached
  (`tests/fixtures/connform_shapes.yaml` plus the primary) and keeps the rows
  the form opens from in `tests/fixtures/connform_rows.json`;
- `node tools/e2e/connform_model_test.mjs` runs the model over those rows
  (an untouched form sends nothing; one edited field sends that field) and
  keeps what it would send in `tests/fixtures/connform_payloads.json`;
- the pytest then sends those bodies to the server as the model built them
  (`pagePayload`: the page's own fields beside the connection): unchanged on
  the first save, the running client kept; an edit changes that field only.

After a deliberate change: `CONNFORM_WRITE=1` on the pytest, then the node
test with `--write`. `connform_e2e.mjs` drives the form in a browser on the
`unchanged_save` fixture (it edits the devices: restart the sandbox after),
with screenshots per protocol in both themes and on a phone under `OUT`.
`connform_review_e2e.mjs` (same fixture, a fresh sandbox) drives the paths
around a save: off a gateway's bus, Test after leaving a bridge and on an
edited bridged device, arrow keys on the segments, Back in Add Device with a
typed address, the components lock, a local tap, a meter-only and an HTTP
device saved untouched.
