# Changelog

## 3.0.0 — 2026-07-06

First release under the **Multi-Bus Gateway** name (successor of the
Janitza UMG 512 monitor; same engine, new identity). Highlights vs 2.7.0:
multi-device southbound (Modbus TCP/RTU-master, HTTP/JSON, MQTT-in),
device-template catalog, composite virtual meters with staleness policies
and an in-band quality block (61440, convention v1), calculated registers,
REST push, diagnostics page (frame-level bus monitor, register probe,
SunSpec walk, error taxonomy), MQTT topic browse + json_path picker,
config snapshots/rollback/last-known-good, audit trail,
admin/operator/viewer roles, passkeys (WebAuthn), /metrics (Prometheus),
reverse-proxy support (ui.trusted_proxies).
