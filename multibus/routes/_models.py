# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
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

from typing import Any, Dict, List, Optional, Union

import math

from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator


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
    """Request model for batch register query. The list is capped: a viewer
    may call this route, and an unbounded batch is bus contention against
    the pollers (F-70, 3.83.0)."""
    registers: List[RegisterQuery] = Field(..., max_length=64)
    device_id: Optional[str] = None


class ThresholdConfig(BaseModel):
    """Threshold configuration for color coding."""
    enabled: bool = True
    dangerLow: Optional[float] = None
    warningLow: Optional[float] = None
    warningHigh: Optional[float] = None
    dangerHigh: Optional[float] = None
    # judge only while another field of the device is above a value:
    # {"field": "current_l1", "above": 2}
    onlyWhen: Optional[Dict[str, Any]] = None


class SelectedRegisterUpdate(BaseModel):
    """Request model for updating selected registers.

    MUST mirror every field the persistence format understands
    (config._parse_selected_payload) — a field missing here is silently
    dropped on every UI save (G5, audit 2026-09-11: nan/monotonic/HA
    typing were lost on round-trip, corrupting SunSpec selections)."""
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
    offset: float = 0.0
    scale_from: str = ""
    nan: Optional[Any] = None            # not-available sentinel (True/value/list)
    monotonic: bool = False              # cumulative counter guard
    enum: Optional[Dict[str, str]] = None
    bits: Optional[Dict[str, str]] = None
    mask: Optional[int] = None
    shift: Optional[int] = None
    device_class: str = ""               # HA typing overrides
    state_class: str = ""
    entity_category: str = ""
    enabled_by_default: Optional[bool] = None
    icon: str = ""
    suggested_display_precision: Optional[int] = None
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
    # a CAN signal (can_signal.py): a save must keep where it is read from
    can_id: Optional[Union[int, str]] = None
    can_id_extended: bool = False
    start_bit: Optional[int] = None
    bit_length: Optional[int] = None
    byte_order: str = ""


class ModbusConfigUpdate(BaseModel):
    """Request model for Modbus configuration update."""
    host: Optional[str] = None
    port: Optional[int] = None
    unit_id: Optional[int] = None
    timeout: Optional[int] = None
    retry_attempts: Optional[int] = None
    retry_delay: Optional[float] = None


class PreviewBudgetUpdate(BaseModel):
    """The read preview's budget (config.PREVIEW_BUDGET): whole numbers in range."""
    model_config = {"extra": "forbid"}
    max_requests: Optional[StrictInt] = Field(default=None, ge=1, le=20)
    listen_s: Optional[StrictInt] = Field(default=None, ge=1, le=60)


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
    heartbeat_interval: Optional[int] = Field(default=None, ge=0)
    allow_write_entities: Optional[bool] = None
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
    # ADR-0029: whole seconds 0..86400, 0 = off — config.heartbeat_seconds,
    # the check every heartbeat value goes through ("60", true, 1.5, NaN refused)
    heartbeat_interval: Optional[int] = None

    @model_validator(mode="before")
    @classmethod
    def _no_nan(cls, data):
        # NaN/inf as text, so the 422 that refuses it can itself be sent as
        # JSON (a non-finite float in the error body made it a 500)
        v = data.get("heartbeat_interval") if isinstance(data, dict) else None
        if isinstance(v, float) and not math.isfinite(v):
            data = {**data, "heartbeat_interval": str(v)}
        return data

    @field_validator("heartbeat_interval", mode="before")
    @classmethod
    def _whole_seconds(cls, v):
        if v is None:
            return None
        from ..config import heartbeat_seconds
        s = heartbeat_seconds(v)
        if s is None:
            raise ValueError("whole seconds, 0..86400")
        return s
    default_bucket_pattern: Optional[str] = None
