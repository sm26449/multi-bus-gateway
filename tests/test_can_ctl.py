# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""CAN interface control via ip (ADR-0046 L2): the ip argument lists are correct, and ``apply``
brings the link down, sets the type, and brings it back up — the sequence an operator runs."""
from multibus.transports.can import can_ctl


def test_ip_argv_mode_and_bitrate():
    assert can_ctl.ip_argv("can0", down=True) == ["link", "set", "can0", "down"]
    assert can_ctl.ip_argv("can0", up=True) == ["link", "set", "can0", "up"]
    assert can_ctl.ip_argv("can0", listen_only=False) == \
        ["link", "set", "can0", "type", "can", "listen-only", "off"]
    assert can_ctl.ip_argv("can0", listen_only=True) == \
        ["link", "set", "can0", "type", "can", "listen-only", "on"]
    assert can_ctl.ip_argv("can0", bitrate=500000, listen_only=False, restart_ms=100) == \
        ["link", "set", "can0", "type", "can", "bitrate", "500000", "restart-ms", "100", "listen-only", "off"]


def test_apply_down_set_type_up(monkeypatch):
    runs = []
    monkeypatch.setattr(can_ctl, "is_up", lambda name: True)
    monkeypatch.setattr(can_ctl, "_run", lambda argv: runs.append(argv) or "")
    can_ctl.apply("can0", mode="transmit")
    assert runs[0] == ["link", "set", "can0", "down"]
    assert runs[1] == ["link", "set", "can0", "type", "can", "listen-only", "off"]   # transmit = off
    assert runs[2] == ["link", "set", "can0", "up"]                                  # restored up


def test_apply_up_only_no_type_change(monkeypatch):
    runs = []
    monkeypatch.setattr(can_ctl, "is_up", lambda name: False)
    monkeypatch.setattr(can_ctl, "_run", lambda argv: runs.append(argv) or "")
    can_ctl.apply("can0", up=True)
    assert runs == [["link", "set", "can0", "up"]]


def test_run_maps_not_permitted_to_permissionerror(monkeypatch):
    class R:
        returncode, stdout, stderr = 1, "", "RTNETLINK answers: Operation not permitted"
    monkeypatch.setattr(can_ctl.subprocess, "run", lambda *a, **k: R())
    import pytest
    with pytest.raises(PermissionError):
        can_ctl._run(["link", "set", "can0", "down"])
