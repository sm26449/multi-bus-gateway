# Changelog

## 3.0.0

The 3.0.0 line — successor to the Janitza UMG 512 monitor under the
**Multi-Bus Gateway** name (same engine, new identity). Dated entries below,
newest first.

### 2026-07-10 — relicensed to AGPL-3.0

- **License change**: the project is now **GNU Affero General Public License
  v3.0** (was PolyForm Noncommercial 1.0.0). It is free and open-source under
  the AGPL — commercial use is allowed, but distributing a modified version, or
  offering it as a **network/SaaS service**, requires making the **complete
  source code** available to those users under the same license. The
  paid-commercial-license model no longer applies.
- `LICENSE` is the verbatim AGPL-3.0 text; the full FSF notice header was added
  to every first-party source file (90 files). README badges and license
  sections (EN + RO) updated. No code behavior change.

### 2026-07-10 — sessions, canonical address & illustrated guide

Post-hardening follow-ups within 3.0.0:
- **7-day sliding sessions** (was 12 h) — the session cookie renews on each
  request, so day-to-day operators aren't re-prompted for login constantly.
- **Canonical-address redirect** (`ui.canonical_url`) — a client-side steer
  onto the canonical HTTPS hostname so cookies, passkeys and HSTS bind to one
  origin; a `?local` escape hatch (sticky `mbg-stay-local` flag) keeps the raw
  IP reachable when DNS or the proxy is down.
- **Illustrated UI guide** (`docs/GHID-UI.md`) — a page-by-page walkthrough
  with per-page screenshots captured from the live production instance.
- Docs: MANUAL §18c (what consumers see when a source is lost), an R&D
  integration test plan & procurement list, and a pinned Zigbee coordinator
  recommendation.

### 2026-07-07 — hardening & performance pass

Pre-release hardening of 3.0.0: a nine-domain senior architecture review and a
seven-domain performance audit (both adversarially verified), then fixes.
Output remains byte-identical; 503 tests pass.

#### Correctness & safety (from the architecture review)
- **Virtual meters are read-only**: writes are refused with a Modbus exception
  so a consumer cannot inject a value (e.g. a false grid reading into an ESS
  control loop).
- **Restart equivalence**: the boot path resolves each device's template
  byte-order exactly like the runtime path — a non-big device no longer decodes
  word-swapped garbage after a restart.
- **Staleness is never laundered**: the value store carries the measurement
  time (not callback time); MQTT-in drops retained deliveries by default;
  calculated registers inherit the oldest contributing input's freshness.
- **Config lifecycle**: device edits are transactional (a failed edit can't
  drop the device); publishers are resolved live on shutdown so the InfluxDB
  buffer flushes; `update_ui_security` validates-then-commits.
- **Reconnection**: pollers restart unconditionally after a blip; the influx/
  mqtt monitor threads resume instead of dying on a stale stop flag; MQTT-in
  uses `connect_async` so a broker down at startup is retried.
- **Security**: response headers (CSP/HSTS/X-Frame/nosniff); audit-CSV
  formula-injection escape; session revocation on password change; operator
  role matcher segment-anchored; SSRF redirect policy (no HTTPS→HTTP downgrade,
  strip auth cross-host); passkey opaque-origin rejection; a pre-auth OOM via
  unbounded body buffering closed; snapshot download gated even when auth is off.
- **Drivers**: `reg.scale` applied consistently on HTTP/MQTT-in; NaN/inf
  rejected on every driver and never published; the InfluxDB unit heuristic
  tests most-specific first (VA is no longer voltage, varh no longer reactive
  power); poison replay chunks (4xx) are dropped instead of blocking the buffer.

#### Performance / constrained hardware (from the perf audit)
- GZip on responses (register catalog ~988 KB → ~40 KB over the LAN).
- `threading.stack_size(512K)` before spawning threads (virtual footprint at
  40 threads ~320 → ~32 MB).
- Poll-group interval floor (50 ms; `0` refused) — sub-second is preserved for
  the one control-loop register.
- Register catalog memoized + served with an ETag (304 on revalidation; no
  event-loop stall on UI boot / device switch); SPA shell cached on mtime.
- Calculated-register ASTs compiled once at load (−65 % per eval).
- One `TemplateRegistry` (was loaded twice); production image ships runtime
  deps only (227 → 213 MB).
- Documented constrained-hardware profile & capacity envelope (MANUAL §18b):
  an RPi 3 comfortably runs ~4–5 devices + ~3 virtual meters at realtime ≥ 1 s;
  the single GIL at a fast cadence — not RAM — is the only wall.

### 2026-07-06 — initial release under the Multi-Bus Gateway name

First release under the **Multi-Bus Gateway** name (successor of the
Janitza UMG 512 monitor; same engine, new identity). Highlights vs 2.7.0:
multi-device southbound (Modbus TCP/RTU-master, HTTP/JSON, MQTT-in),
device-template catalog, composite virtual meters with staleness policies
and an in-band quality block (61440, convention v1), calculated registers,
REST push, diagnostics page (frame-level bus monitor, register probe,
SunSpec walk, error taxonomy), MQTT topic browse + json_path picker,
config snapshots/rollback/semantic-diff/last-known-good, audit trail,
admin/operator/viewer roles, passkeys (WebAuthn), /metrics (Prometheus),
reverse-proxy support (ui.trusted_proxies), built-in HTTPS, MQTT TLS
(mutual), IP allowlist, and a gated Modbus write path with dead-man leases.
