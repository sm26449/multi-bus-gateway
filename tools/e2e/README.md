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
