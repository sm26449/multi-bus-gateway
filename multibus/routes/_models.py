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
"""Pydantic request models shared by route modules (moved from api.py;
api.py re-imports them so external references keep working)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class RegisterQuery(BaseModel):
    """Request model for register query."""
    address: int
    data_type: str = "float"
    register_type: str = "holding"
    # Which device to read from. None / the primary id → the primary client;
    # any other id routes the read to that device's own client (so a secondary
    # device's Measurements 'Query now' reads the RIGHT bus, not the primary).
    device_id: Optional[str] = None
    # The register's scale DIVISOR (engineering value = raw / scale). None/0/1 →
    # raw. Applied to on-demand reads so 'Query now' matches the polled value
    # (e.g. a voltage with scale 10: raw 2429 → 242.9 V, not 2429).
    scale: Optional[float] = None


class RegisterBatchQuery(BaseModel):
    """Request model for batch register query."""
    registers: List[RegisterQuery]
    device_id: Optional[str] = None


class ThresholdConfig(BaseModel):
    """Threshold configuration for color coding."""
    enabled: bool = True
    dangerLow: Optional[float] = None
    warningLow: Optional[float] = None
    warningHigh: Optional[float] = None
    dangerHigh: Optional[float] = None


class SelectedRegisterUpdate(BaseModel):
    """Request model for updating selected registers."""
    address: int
    name: str
    label: str
    unit: str = ""
    description: str = ""  # Human-readable description
    data_type: str = "float"
    poll_group: str = "normal"
    json_path: str = ""
    topic: str = ""            # MQTT input: the subscribe topic for this register
    scale: float = 1.0
    register_type: str = "holding"
    mqtt_enabled: bool = True
    mqtt_topic: str = ""
    influxdb_enabled: bool = True
    influxdb_measurement: str = ""
    influxdb_tags: Dict[str, str] = {}
    ui_show_on_dashboard: bool = True
    ui_widget: str = "value"
    ui_config: Dict[str, Any] = {}
    thresholds: Optional[ThresholdConfig] = None


class ModbusConfigUpdate(BaseModel):
    """Request model for Modbus configuration update."""
    host: Optional[str] = None
    port: Optional[int] = None
    unit_id: Optional[int] = None
    timeout: Optional[int] = None
    retry_attempts: Optional[int] = None
    retry_delay: Optional[float] = None


class MQTTConfigUpdate(BaseModel):
    """Request model for MQTT configuration update."""
    enabled: Optional[bool] = None
    broker: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None
    topic_prefix: Optional[str] = None
    retain: Optional[bool] = None
    qos: Optional[int] = Field(default=None, ge=0, le=2)   # MQTT qos is 0, 1 or 2
    publish_mode: Optional[str] = None
    ha_discovery_enabled: Optional[bool] = None
    ha_discovery_prefix: Optional[str] = None
    ha_device_name: Optional[str] = None
    tls_enabled: Optional[bool] = None
    tls_ca_cert: Optional[str] = None
    tls_client_cert: Optional[str] = None
    tls_client_key: Optional[str] = None
    tls_insecure: Optional[bool] = None
    default_topic_pattern: Optional[str] = None


class InfluxDBConfigUpdate(BaseModel):
    """Request model for InfluxDB configuration update."""
    enabled: Optional[bool] = None
    url: Optional[str] = None
    token: Optional[str] = None
    org: Optional[str] = None
    bucket: Optional[str] = None
    write_interval: Optional[int] = None
    publish_mode: Optional[str] = None
    default_bucket_pattern: Optional[str] = None
