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

## Hardening reminder

This gateway is designed to run on a trusted LAN behind your own reverse proxy/
VPN. Do not expose it to the public internet. Enable authentication, change all
default credentials, and use TLS. See the README "Security" section.
