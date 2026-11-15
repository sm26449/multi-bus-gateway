# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""§4 C2 characterization — the catalog seams inverted behind
``multibus.core.catalog_port`` (device-kind normalization, the overridable-kind
set, and the user-device-type predicates). A device's ``kind:`` override goes
through ``connection._kind_override``, which now asks the port instead of
importing ``catalog`` directly; the resolved values must be unchanged.
"""
from __future__ import annotations

from multibus.config import Config

CONFIG = """
primary_device: false
modbus: {host: 192.0.2.5}
mqtt: {broker: b, password: p}
influxdb: {token: t}
device_types:
  - id: chiller
    labels: {en: Chiller, ro: Răcitor}
    behaves_like: heat_pump
devices:
  - id: usertype
    connection: {protocol: tcp, host: 192.0.2.1, port: 502, unit_id: 1}
    kind: chiller
  - id: builtin
    connection: {protocol: tcp, host: 192.0.2.2, port: 502, unit_id: 1}
    kind: ups
  - id: bogus
    connection: {protocol: tcp, host: 192.0.2.3, port: 502, unit_id: 1}
    kind: nonsense
  - id: nokind
    connection: {protocol: tcp, host: 192.0.2.4, port: 502, unit_id: 1}
"""


def _cfg(tmp_path) -> Config:
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    return Config(str(tmp_path / "config.yaml"))


def test_kind_override_resolution_through_the_port(tmp_path):
    cfg = _cfg(tmp_path)
    assert getattr(cfg, "_load_failed", False) is False
    kinds = {d.id: d.kind for d in cfg.devices}
    # user type kept as-is; built-in overridable kept; unknown dropped to ''
    assert kinds == {"usertype": "chiller", "builtin": "ups",
                     "bogus": "", "nokind": ""}


def test_user_device_type_is_registered_from_config(tmp_path):
    cfg = _cfg(tmp_path)
    assert [t.get("id") for t in cfg.device_types] == ["chiller"]
