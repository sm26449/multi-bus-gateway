# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Helpers used by more than one route module (and, transitionally, by the
closure routes still living in api.py)."""
from __future__ import annotations


def device_influx(config, device: str):
    """Resolve a device OR PLANT id to its (bucket, device_tag) for InfluxDB
    reads. Primary / absent → (None, None) so queries hit the default bucket
    with no device filter (byte-identical to the single-device path).

    A plant resolves like a device: it writes its own aggregate series into the
    plant's bucket tagged ``device=<plant id>``, so a plant total charts
    exactly the way a unit's does.

    An id that is neither raises ``ValueError`` — returning (None, None) sent
    the query to the PRIMARY's bucket with no filter, so a typo answered with
    somebody else's data instead of saying it did not know the device.
    """
    if not device or device == config.primary_device.id:
        return None, None
    dev = config.get_device(device)
    if dev is not None:
        return dev.influxdb_bucket, dev.influxdb_device_tag
    plant = None
    if hasattr(config, "get_raw_plant"):
        plant = config.get_raw_plant(device)
    if plant is not None:
        from ..plant_aggregator import plant_bucket
        return plant_bucket(plant, device), device
    raise ValueError(f"unknown device or plant: {device!r}")
