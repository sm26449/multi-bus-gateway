# Security hardening checklist

What to do, in order, before a Multi-Bus Gateway serves anything that
matters. Every item maps to a switch or a file described elsewhere in the
docs; this page only puts them in sequence. The model behind the switches is
in [SECURITY.md](../SECURITY.md); the mechanics in
[MANUAL.md §16](MANUAL.md).

The gateway is built for a trusted LAN behind your own firewall, VPN or
reverse proxy. It is not designed to be exposed to the public internet.

## 1. First boot

- [ ] **Copy the generated admin password** from the first-boot log
      (`FIRST RUN — generated admin credentials`) and log in. It is shown
      once; if you lose it, delete `config/config.yaml`'s `ui.auth_*` keys
      and restart to get a new one.
- [ ] **Change it** (Settings → Security). The gateway refuses to enable
      login with a blank or `admin` password from any entry point.
- [ ] **Enrol a passkey** on the machine you administer from, if the UI is
      reachable over HTTPS (WebAuthn needs a secure context). Passkeys
      enrolled while login was off become admin credentials when login is
      enabled — the enable flow lists them; remove any you do not recognise.

## 2. Accounts and roles

- [ ] Create a **viewer** account for dashboards and Grafana-style consumers
      and an **operator** account for people who act on devices but never
      change configuration. Operators cannot bypass a template's write
      envelope; raw (`unguarded`) writes are an admin act.
- [ ] Keep the **lockout** defaults (5 attempts / 5 minutes per IP) unless
      you sit behind a proxy that hides client addresses — then set
      `ui.trusted_proxies` so each client gets its own lockout bucket and
      its own audit identity.
- [ ] Sessions slide for 7 days and end after 30 regardless. Rotating any
      password revokes every session.

## 3. Network exposure

- [ ] **Bind only what you use.** The compose file publishes the UI (8080),
      the virtual-meter range (1502–1512) and Modbus 502. Remove the port
      lines you do not need; the bundled broker, InfluxDB, Grafana and
      MQTT Explorer publish their own ports — bind them to `127.0.0.1:`
      or drop the services if another host provides them.
- [ ] **IP allowlist**: `security.allowlist` (CIDRs) sits in front of
      everything, `/health` included. Use it when the LAN has guests.
- [ ] **TLS**: terminate at your reverse proxy (see
      [install.md §G](install.md)) or in-process (`ui.tls_enabled` with
      `tls_cert`/`tls_key`). Behind a proxy set `ui.trusted_proxies` and
      `ui.canonical_url`; the session cookie carries `Secure` only when the
      request is known to be HTTPS.
- [ ] **Do not port-forward** the gateway to the internet. If remote access
      is needed, use a VPN or an authenticating reverse proxy in front of
      the gateway's own login.

## 4. The broker

- [ ] **No anonymous broker.** Set `mqtt.username`/`mqtt.password` (and TLS
      if the broker is not on the same host). The bundled Mosquitto requires
      `MQTT_USERNAME`/`MQTT_PASSWORD` in `.env` since 3.81.0 and regenerates
      its password file from them at every start; give every consumer the
      same pair, or add users with `mosquitto_passwd` on the data volume.
- [ ] **ACLs on the write topics.** With `mqtt.allow_write_entities` on,
      whoever can publish to `<prefix>/cmd/#`, the HA `.../set` topics and
      `mbg/rules/+/set` can act on hardware. The gateway refuses those
      features while its own broker session is anonymous, but it cannot
      see who published — the broker's ACL is the authentication. Restrict
      publish rights on those topics to the controllers that need them.
- [ ] Retained commands are never replayed into hardware; still, clear
      retained `cmd/#` topics when you retire a controller
      ([mqtt-contract.md](mqtt-contract.md), cleanup section).

## 5. Hardware writes

- [ ] Leave `security.allow_writes` **off** until a write is actually
      needed. Reads work without it.
- [ ] When arming, keep the primary `write_locked` (default) and unlock
      only the device that must be written, from its Outputs tab.
- [ ] Prefer **commands** (template recipes with bounds, read-back and a
      revert timer) over raw register writes; give every command that
      changes a setpoint a **lease** (`lease_s`) so a dead controller
      reverts the device to its safe value.
- [ ] Set an **`API_KEY`** for machine clients that write over HTTP; the
      key is required on every state-changing request but not on the login
      itself, so browser users are unaffected.

## 6. Data at rest

- [ ] `config/` is the only writable path and holds secrets
      (`config.yaml` with broker/Influx credentials, `sessions.json`,
      `passkeys.json`). The gateway writes them `0600`; keep the host
      directory owned by the container user (uid 10001) and out of any
      world-readable backup.
- [ ] **Exports** strip secrets by default; `include_secrets=true` needs
      an admin session or the API key and is audit-logged. Treat a
      with-secrets bundle like the password file it is.
- [ ] The **audit log** (`config/audit.jsonl`, ~5 MB rotating) is mirrored
      as `AUDIT` lines in the container log — ship the container log to
      your log collector if you need tamper-resistant history.

## 7. The image and the host

- [ ] Run the published image by **tag** (`MBG_VERSION=X.Y.Z`); it is
      built from a hash-pinned dependency set and carries SLSA provenance
      and an SBOM. Verify with `docker pull` of the exact tag.
- [ ] Keep the compose service's `cap_drop`/`no-new-privileges` block; the
      process runs as uid 10001 with no effective capabilities after the
      entrypoint's chown.
- [ ] Update on releases: [operations.md](operations.md) covers upgrade
      and rollback by tag; the changelog marks breaking changes at the top
      of each section. Security fixes are announced in the release notes
      and in [SECURITY.md](../SECURITY.md).

## 8. Before you report a problem

Read the "By design" section of [SECURITY.md](../SECURITY.md): admin-set
egress targets (InfluxDB, broker, webhooks, ESPHome) are trusted on
purpose, MQTT write authentication is the broker's job, and the bundled
stack is a LAN convenience. Everything else is a bug — report it privately
as SECURITY.md describes.
