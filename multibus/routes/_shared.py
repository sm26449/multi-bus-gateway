"""Helpers used by more than one route module (and, transitionally, by the
closure routes still living in api.py)."""
from __future__ import annotations


def device_influx(config, device: str):
    """Resolve a device id to its (bucket, device_tag) for InfluxDB reads.
    Primary / absent → (None, None) so queries hit the default bucket with
    no device filter (byte-identical to the single-device path)."""
    if not device or device == config.primary_device.id:
        return None, None
    dev = config.get_device(device)
    if dev is None:
        return None, None
    return dev.influxdb_bucket, dev.influxdb_device_tag
