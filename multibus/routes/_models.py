"""Pydantic request models shared by route modules (moved from api.py;
api.py re-imports them so external references keep working)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class RegisterQuery(BaseModel):
    """Request model for register query."""
    address: int
    data_type: str = "float"
    register_type: str = "holding"


class RegisterBatchQuery(BaseModel):
    """Request model for batch register query."""
    registers: List[RegisterQuery]


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
