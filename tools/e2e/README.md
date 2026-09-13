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
It is the regression walk behind `docs/ui-audit-installations.md`.

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
