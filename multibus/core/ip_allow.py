# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Client-IP allowlist matching, shared by the allowlist middleware (api) and the
/metrics route (routes) — kept in core so both layers may import it (OBS-PROM-1)."""
from __future__ import annotations

import ipaddress as _ipaddr
import logging

logger = logging.getLogger("multibus.api")  # the allowlist's messages kept their logger


def parse_networks(entries, label: str) -> list:
    """An allowlist-style list of IPs/CIDRs → ip_network objects. An entry that
    does not parse fails CLOSED: an allowlist the operator wrote but that does
    not parse must not silently become "everyone" (F-18, 3.83.0); a no-match
    network denies every client until config.yaml is fixed."""
    nets = []
    for entry in entries or []:
        entry = str(entry).strip()
        if not entry:
            continue
        try:
            nets.append(_ipaddr.ip_network(entry, strict=False))
        except ValueError:
            logger.critical("%s: entry %r does not parse — DENYING all "
                            "clients until it is fixed", label, entry)
            nets.append(_ipaddr.ip_network("255.255.255.255/32"))
    return nets


def peer_in_networks(peer: str, nets) -> bool:
    """True when ``peer`` (the client address — uvicorn already resolved it from
    X-Forwarded-For when the hop is in ui.trusted_proxies) is loopback or inside
    one of ``nets``."""
    try:
        ip = _ipaddr.ip_address(peer)
    except ValueError:
        return False
    # Dual-stack sockets report a v4 client as ::ffff:a.b.c.d — normalize so an
    # IPv4 allowlist entry (192.168.1.0/24) and loopback detection still match
    # (else legit admins/healthchecks get locked out on an IPv6-bound listener).
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return True
    return any(ip in n for n in nets)


def metrics_peer_allowed(config, peer: str) -> bool:
    """Who may scrape /metrics (OBS-PROM-1). Loopback always; a peer inside
    ``security.metrics_allowlist``; and, when ``security.allowlist`` is set, a
    peer it admits (the pre-existing path). With both lists empty: loopback
    only (A-MED-5)."""
    sec = getattr(config, "security", None)
    if peer_in_networks(peer, parse_networks(getattr(sec, "metrics_allowlist", None),
                                             "security.metrics_allowlist")):
        return True
    app_nets = parse_networks(getattr(sec, "allowlist", None), "security.allowlist")
    return bool(app_nets) and peer_in_networks(peer, app_nets)
