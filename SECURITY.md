# Security Policy

Multi-Bus Gateway is a network-facing service that reads and re-serves live
energy-metering data (Modbus TCP/RTU, HTTP/JSON, MQTT) and can be given write
access to devices. Please treat security reports seriously and privately.

## Supported versions

Security fixes are applied to the latest released `3.x` line. Older tags are not
maintained — please upgrade before reporting.

| Version | Supported |
|---------|-----------|
| latest `3.x` | ✅ |
| < latest `3.x` | ❌ |

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

- Preferred: open a private [GitHub Security Advisory](https://github.com/sm26449/multi-bus-gateway/security/advisories/new) ("Report a vulnerability").
- Or email **sm26449@diysolar.ro** with `SECURITY` in the subject.

Please include: affected version/commit, a description, reproduction steps or a
proof-of-concept, and the impact you observed. If you can, say whether the issue
is reachable pre-authentication.

You'll get an acknowledgement within a few days. Once a fix is available we'll
coordinate a disclosure timeline with you and credit you in the release notes
(unless you prefer to stay anonymous).

## Scope

In scope: authentication/session handling, CSRF, SSRF in the outbound HTTP
drivers, path traversal in device/template handling, injection in the InfluxDB/
MQTT paths, the write path and its bounds, and any way to make the gateway serve
wrong/stale data as live into a downstream consumer (e.g. an ESS grid meter).

Out of scope: issues that require an already-compromised host, physical access
to the RS-485 bus, or a misconfiguration explicitly warned against in the docs
(e.g. exposing the gateway directly to the public internet, or `verify_tls:
false` on a device you don't control).

## Security model (summary)

Defense in depth — every layer applies independently:

1. **Login is on from the first run**: a fresh install generates an admin
   password (stored PBKDF2-SHA256-hashed, printed once to the log) and
   enables authentication. There are no default credentials; enabling auth
   with a blank or `admin` password is refused at every entry point
   (UI route, config import, snapshot restore, hand-edit).
2. **Three roles** — admin / operator (live actions, no config) / viewer
   (read-only) — enforced by middleware on all ~140 routes; WebAuthn
   passkeys as a password alternative; per-IP login lockout; sessions are
   HttpOnly sliding cookies persisted as SHA-256 token hashes (0600).
3. **Optional API key** (`API_KEY`) required on every state-changing HTTP
   request and on the OTA-capable builder WebSocket (header or
   `mbg-api-key.<base64url>` subprotocol).
4. **Optional IP allowlist** in front of everything, including `/health`.
5. **Hardware-write gating chain**: `security.allow_writes` (default off)
   AND an authenticated caller AND a template allowlist entry (writability,
   bounds, encoding come from the template — never the caller) AND a
   per-IP rate limit; every write is read-back-verified, audit-logged, and
   can carry a dead-man lease that auto-reverts to a safe value.
6. **Browser hardening**: CSRF rejection (Sec-Fetch-Site/Origin), security
   headers, canonical-URL output escaping, no secrets in exports or logs
   (redaction on env/config endpoints), retained MQTT commands are never
   replayed into hardware.
7. **Process hardening**: containers run non-root (gateway uid 10001,
   serial bridge uid 10002 with /dev read-only + cgroup-scoped tty access);
   bare-metal default bind is loopback; TLS optional in-process or via
   your reverse proxy.

The reliability side of the same design — fail-safes, data-delivery
guarantees, config self-healing — is cataloged in
[`docs/reliability.md`](docs/reliability.md).

## Hardening reminder

This gateway is designed to run on a trusted LAN behind your own reverse
proxy/VPN. Do not expose it to the public internet. Keep authentication on,
rotate the generated first-run password, and use TLS (in-process or at the
proxy). See the README "Security" section and `docs/MANUAL.md` §16.
