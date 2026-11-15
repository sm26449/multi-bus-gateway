# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""A stable handle on the live publishers (ADR-0038 #3).

/api/config/apply can swap the MQTT or InfluxDB publisher for a fresh object
(MQTT enabled after boot, a reconnect). Before, create_api kept the two as
closure locals and `nonlocal`-rebound them, so every reader had to be a closure
over that scope. The holder is one object, created once and never replaced:
callers keep a reference to it and read ``holder.mqtt`` / ``holder.influx`` at
use time, and the apply path mutates those attributes. A publisher swap is then
visible everywhere — services, routes, the calc engine — without rebinding
anything or re-wiring each reader by hand."""
from typing import Optional


class PublisherHolder:
    """The current MQTT and InfluxDB publishers. Mutated in place, never rebound.

    ``mqtt`` / ``influx`` are the live publisher objects (or None when that sink
    is off). Code that sends holds the holder, not the publisher, so a swap on
    /api/config/apply reaches it with no further wiring."""

    __slots__ = ("mqtt", "influx")

    def __init__(self, mqtt=None, influx=None) -> None:
        self.mqtt: Optional[object] = mqtt
        self.influx: Optional[object] = influx
