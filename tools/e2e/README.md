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
tools/e2e/fixture_up.sh endpoint 18089
CHROMIUM_PATH=<chrome> MBG_URL=http://127.0.0.1:18089 ENDPOINT=sunfield node tools/e2e/endpoint_page_e2e.mjs
docker rm -f mbg-fx-endpoint
```

It writes (toggles a setting, renames a unit): start the fixture fresh. The
`endpoint` variant reads over Modbus TCP at a port that refuses (127.0.0.1:9
inside the container), so the offline/degraded paths are the ones under test.

## Fronius installations (F1)

`fronius_endpoint_e2e.mjs` checks the two ways a Fronius plant is read, side by
side: inverters through the datalogger's Solar API (answered by
`fake_solar_api.mjs`, 4200 W each) and the meter directly over SunSpec Modbus
(a refusing port). It covers the installations in the devices list with their
units nested, the unit's Read via and Outputs (locked to the installation), the
installation and group dialogs naming each template, and the delete refusal.

```bash
tools/e2e/fixture_up.sh fronius 18094
CHROMIUM_PATH=<chrome> MBG_URL=http://127.0.0.1:18094 node tools/e2e/fronius_endpoint_e2e.mjs
docker rm -f mbg-fx-fronius mbg-fx-fronius-solar
```

`fixture_up.sh fronius` starts the stand-in in a companion container
(`<name>-solar`, image `node:24-slim` or `NODE_IMAGE`) inside the gateway's
network namespace: it answers on the gateway's 127.0.0.1:18099 and nowhere else.

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

`qa_397_sweep_e2e.mjs` is the release sweep (3.97): every page and its tabs,
every device's tabs and sub-tabs, every installation, the ⋯ menu, every dialog
an "open/add/new/import/view" control opens, Add Device (every protocol, the
RTU modes, steps 1–3) and Add Installation — in en and ro, 1440 and 390 px,
light and dark. Per screen it reports console errors, failed requests, page
scroll at phone width and what sticks out, clipped text, raw i18n keys,
English left on a ro page, controls without a name (or named only by a
placeholder), text under AA contrast and empty cards; per dialog, that Tab
stays inside, Esc closes it and focus returns. Not pass/fail: a report.

```bash
MBG_FX_NAME=qa-types tools/e2e/fixture_up.sh types 18540
CHROMIUM_PATH=<chrome> MBG_URL=http://127.0.0.1:18540 OUT=/tmp/qa SHOTS=1 \
  node tools/e2e/qa_397_sweep_e2e.mjs        # → /tmp/qa/report.json (+ shots of problem screens)
```

The full product with dialogs takes hours on a busy host: `DIALOGS=0` walks
the screens only; `COMBOS=en/1440/light,ro/390/dark` picks combinations;
`PAGES`, `DEVS`, `PARTS`, `ONLY` narrow the walk.

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
| `endpoint` | `sunfield` over Modbus TCP at a refusing port: units 1, 2 and `sunfield-east` "East roof", `max_connections: 2`; no primary meter | `endpoint_page_e2e`, `device_logs_e2e` (`DEVICE=sunfield-u1`, after ~20 s) |
| `fronius` | `fronius-inverters` (Solar API, units 1, 2, answered by `fake_solar_api.mjs`) and `fronius-meter` (SunSpec, unit 240, refusing); no primary meter | `fronius_endpoint_e2e` |
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

The device page (L3.3, ADR-0031 §3) shows its tabs in four groups (Live,
Data, Setup, Diagnose), one strip at a time: a tab of another group is out
of sight until its group is picked; a group of one tab (Setup) shows no
strip. Nothing in the page body edits: editing opens from the ⋯ menu (or E),
until the Edit modal (L3.4) today's editors in place, under an "Editing" bar
with Done. A script opens a tab as a user does — `openDeviceTab(page,
'measurements')` clicks the group, then the tab — or an editor —
`openDeviceEditor(page, 'commands')` opens ⋯ and its item; `openDeviceTab`
with a tab that left (`connection`, `edit`, `outputs`, `attributes`,
`meters`) opens its editor the same way — and `deviceAction(page,
'#ddvDeleteBtn')` clicks any ⋯ item (a held-back item still answers, it says
why). The next reorganisation then changes this file only.
`device_page_e2e.mjs` covers the groups, the keyboard, links, the meters
editor and a meter-only device on the `safety` fixture with `--writes`.

`device_view_e2e.mjs` covers the view itself on the `qa` fixture with login
(`MBG_FX_NAME=mbg-fx-view tools/e2e/fixture_up.sh qa 18331 --login e2e-pass`,
then `MBG_PASS=e2e-pass`): the one-line summary per kind (the same on the list
row and the bridge card), the ⋯ menu per role and by keyboard, no Save in the
page body, editing from ⋯ and E, Setup read-only for a viewer, links to the
tabs that left, the dirty guard on Esc and ✕, a 390 px phone and Romanian.
`node tools/e2e/device_view_model_test.mjs` checks the summary renderer and
the menu builder without a browser.

## The connection form (`connform_e2e.mjs`, `connform_model_test.mjs`)

One form says how a device is reached, in Add Device and on a device's
connection editor, ⋯ → Edit connection… (`ui/js/app-connform.js`); its model and what it
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

## The guide's UI gaps (UI-G1..G4)

`node tools/e2e/ui_g_findings_test.mjs` runs the page's own scripts in a
sandbox, no browser, against both dictionaries: a fleet row of a device
measured by meters (G1), the line under Add Device's connection choice (G2),
what the History chart draws at desktop and phone sizes (G3), and the
Monitor's group headings (G4).

`ui_g_findings_e2e.mjs` checks the same in a browser on the demo gateway
(`tools/demo/demo_up.sh`), read-only: the UPS's row against `/api/fleet`,
its page's `/api/values` and its input meter; every connection kind in en and
ro; History at 1440 and 390 px; the Monitor's headings and their keyboard
use. Screenshots go to `SHOTS_DIR`.

```bash
cd tools/e2e && MBG_URL=http://127.0.0.1:18100 node ui_g_findings_e2e.mjs
```

## Board UARTs in the pickers (ADR-0016)

`bridge_ports_model_test.mjs` checks `ui/js/bridge-ports.js` with no browser:
which ports a picker may offer (only `ok` with a data port), how `no-access`,
`reserved`, `busy` and `excluded` are told apart, and that a bridge older than
board UARTs (no `status`, no `kind`) still reads right.

```bash
node tools/e2e/bridge_ports_model_test.mjs
```

`bridge_ports_e2e.mjs` drives Add Device's Scan and a bridge's Buses dialog
with the serial bridge's answers faked in the browser (no bridge, no
hardware): the served USB adapter and HAT channel can be picked; the
no-access channel and the console are listed disabled, each with why, the
no-access one with its command and a Copy button; RS-485 only on the board
port, sent by Save; Romanian words; a phone width with no sideways scroll.
It saves nothing on the gateway.

```bash
tools/e2e/fixture_up.sh safety 18087
CHROMIUM_PATH=<chrome> MBG_URL=http://127.0.0.1:18087 node tools/e2e/bridge_ports_e2e.mjs
```

## Line health of a bridged bus (ADR-0017)

`bridge_diag_model_test.mjs` checks `ui/js/bridge-diag.js` with no browser:
the chip's level per diagnosis, line counters with `null` kept `null` (a
CH340 is never shown as a clean 0), the activity of a bus.

```bash
node tools/e2e/bridge_diag_model_test.mjs
```

`bridge_diag_e2e.mjs` fakes what the gateway knows about one bridge (an FTDI
bus with frame errors and timeouts, a CH340 bus) and checks the card's health
chips, the Health dialog (diagnosis, counters with their rate, "not
counted", client, the gateway's own counts, events), the Buses dialog's
column, Config → Alerts (the bus-health signal and its limits, sent by Save),
Romanian, and a phone width. It saves nothing on the gateway.

```bash
tools/e2e/fixture_up.sh safety 18087
CHROMIUM_PATH=<chrome> MBG_URL=http://127.0.0.1:18087 node tools/e2e/bridge_diag_e2e.mjs
```

## Gateway identity (`identity_e2e.mjs`, ADR-0026)

Settings → General's "Gateway identity" card (labels, hints, keyboard, a
refused id said at its field, Romanian), the default patterns on the MQTT
and InfluxDB tabs (empty = the identity scope's default), a device added
afterwards getting the scoped topic and bucket in Add Device step 3, a topic
prefix another device holds refused at its field in the wizard, and the
device page still saving (200, never 412). It writes; on a fresh safety
fixture, removed after:

    MBG_FX_NAME=mbg-fx-l1 tools/e2e/fixture_up.sh safety 18120
    CHROMIUM_PATH=… MBG_URL=http://127.0.0.1:18120 OUT=<dir> node tools/e2e/identity_e2e.mjs
    docker rm -f mbg-fx-l1

## The serial port picker (ADR-0028)

`serial_ports_model_test.mjs` checks `ui/js/serial-ports.js` with no browser:
poll or listen per mode, which port may be picked and why not, what is stored
(a USB adapter's by-id link), the device being edited not its own conflict.

```bash
node tools/e2e/serial_ports_model_test.mjs
```

`serial_ports_e2e.mjs` fakes `GET /api/serial-ports` (the Pi 5 bench: an FTDI
with a by-id link, a CH340, HAT ch0 listened to, HAT ch1 polled, a board UART
not mapped, a hidden console) and checks the list (kinds, states), the
listened port aria-disabled with its reason, the not-mapped port's
`--device` and compose lines, Refresh, keyboard selection, the tap's rules,
the POST carrying the by-id link, the device page (its own port picked, a
new pick in the PUT), Romanian, and a phone width. It creates one device
(`sp-e2e`, disabled) and deletes it.

```bash
tools/e2e/fixture_up.sh safety 18087
CHROMIUM_PATH=<chrome> MBG_URL=http://127.0.0.1:18087 node tools/e2e/serial_ports_e2e.mjs
```

## Device types and identity (ADR-0027)

`device_types_e2e.mjs` drives Templates → Device types (a type of one's own
added from the keyboard with English and Romanian words, an icon and what it
behaves like; a clash with a built-in refused in words; the templates for a
type), the template editor's kind and "also suits these types", a device given
that type from its ⋯ menu (its wire kind is the type's `behaves_like`), the
identity attribute in its attributes editor, ⋯ → About (a wrong label named, labels shown as
chips), the type and the label as Find facets, a type in use refused for
deletion with its users named, and the same pages in Romanian. It cleans up
after itself, so it can run again on the same gateway.

```bash
tools/e2e/fixture_up.sh types 18093
CHROMIUM_PATH=<chrome> MBG_URL=http://127.0.0.1:18093 node tools/e2e/device_types_e2e.mjs
docker rm -f mbg-fx-types
```

## Release 3.97 design conformance (QA)

`qa_397_conformance_e2e.mjs` holds one check per user-visible decision of
ADR-0025..0029, in sections (`ONLY=A,B,…` runs some): A new devices
write-locked and the Write protection card; B Test, SunSpec walk and a
bridge's bus scan refused on a listened bus, before any port is opened; C a
poll onto a listened bus and a listener onto a polled one refused at the
field (wizard, device page, the bridge's bus hints); D Settings → Gateway
identity; E refused topic prefixes in Add Device; F two tabs on one device
(the second save must be told — open until L3, FINDINGS QA-D-08); G the
serial port picker; H the type catalog, "+ Add type", Find facets, template
filter and editor; I identity attributes, the published meta and the privacy
switches; J the InfluxDB heartbeat; K the removed switches; L Home Assistant
discovery following MQTT; M en/ro for every key the UI asks for.

The `qa` fixture holds a tap and a poller on serial paths that do not exist
(a refusal that came after an open would say "no such file"), a tap and a
poller on a bridge on 127.0.0.1, a meter-only device and an installation with
roles. Its MQTT goes to 127.0.0.1:1883 of the gateway's own network
namespace, where a throwaway broker answers; the meta and the discovery
entries are read from it (`QA_BROKER`; without it those checks are skipped).
It writes: start the fixture fresh.

```bash
MBG_FX_NAME=qa3-a tools/e2e/fixture_up.sh qa 18431
docker run -d --rm --name qa3-broker --network container:qa3-a eclipse-mosquitto:2.1.2-alpine
CHROMIUM_PATH=<chrome> MBG_URL=http://127.0.0.1:18431 QA_BROKER=qa3-broker \
  node tools/e2e/qa_397_conformance_e2e.mjs
docker rm -f qa3-broker qa3-a
```

## Refused live-data socket (WS-LOG-1)

`ws_session_gone_e2e.mjs` ends a signed-in page's session behind its back and
restarts the gateway: the page must show the login screen and stop dialling
`/ws`; with `/api/auth/status` stubbed to say the session lives, it must back
off (3, 6, 12… s) instead of re-dialling every 3 s. It restarts the container.

```bash
MBG_FX_NAME=mbg-fx-wslog tools/e2e/fixture_up.sh drive 18190 --login <pass>
MBG_URL=http://127.0.0.1:18190 MBG_PASS=<pass> MBG_CONTAINER=mbg-fx-wslog \
  CHROMIUM_PATH=<chrome> node tools/e2e/ws_session_gone_e2e.mjs
docker rm -f mbg-fx-wslog
```
