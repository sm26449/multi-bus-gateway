# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Register-selection IO: load the primary's selected_registers.json (with self-heal) and
docs/modbus_data.json, parse a selection payload (shared with the per-device files), and save the
selection atomically while preserving the file's other sections. Split out of io.py (ADR-0047 P1);
a mixin on Config, so `self.*` resolves across the other config mixins (`_load_json_with_heal`,
`_file_lock`)."""
import json
import logging
import os
from pathlib import Path
from typing import Dict, List

from multibus.codec.value_decode import valid_bound
from multibus.core.protocols import _can_kwargs, normalize_register_type
from multibus.core.config.models import PollGroup, SelectedRegister
from multibus.core.config.util import validate_register_identity

logger = logging.getLogger("multibus.core.config")


class RegisterIoMixin:
    def _load_selected_registers(self):
        """Load selected registers configuration."""
        if not self.registers_path.exists():
            logger.warning(f"Selected registers file not found: {self.registers_path}")
            return

        data = self._load_json_with_heal(self.registers_path,
                                         required_key='registers',
                                         label='selected registers')
        if data is None:
            return
        try:
            # Poll groups from registers file
            if 'poll_groups' in data:
                for name, group in data['poll_groups'].items():
                    self.poll_groups[name] = PollGroup(
                        interval=group.get('interval', 5),
                        description=group.get('description', ''),
                    )

            # Registers
            self.selected_registers = self._parse_selected_payload(data)

            logger.info(f"Loaded {len(self.selected_registers)} selected registers")

        except Exception as e:
            logger.error(f"Error loading selected registers: {e}")

    @staticmethod
    def _parse_selected_payload(data: Dict) -> List[SelectedRegister]:
        """Parse a selected-registers payload (shared by the legacy file and
        the per-device files — identical schema)."""
        out: List[SelectedRegister] = []
        for reg in data.get('registers', []):
            mqtt = reg.get('mqtt', {})
            influx = reg.get('influxdb', {})
            ui = reg.get('ui', {})

            out.append(SelectedRegister(
                address=reg['address'],
                name=reg['name'],
                label=reg.get('label', reg['name']),
                unit=reg.get('unit', ''),
                data_type=reg.get('data_type', 'float'),
                poll_group=reg.get('poll_group', 'normal'),
                description=reg.get('description', ''),
                json_path=reg.get('json_path', ''),
                topic=reg.get('topic', ''),
                scale=float(reg.get('scale', 1) or 1),
                offset=float(reg.get('offset', 0) or 0),
                scale_from=str(reg.get('scale_from', '') or ''),
                nan=reg.get('nan'),
                valid_min=valid_bound(reg.get('valid_min')),
                valid_max=valid_bound(reg.get('valid_max')),
                monotonic=bool(reg.get('monotonic', False)),
                daily=bool(reg.get('daily', False)),
                enum=reg.get('enum'),
                bits=reg.get('bits'),
                mask=reg.get('mask'),
                shift=reg.get('shift'),
                device_class=str(reg.get('device_class', '') or ''),
                state_class=str(reg.get('state_class', '') or ''),
                entity_category=str(reg.get('entity_category', '') or ''),
                enabled_by_default=reg.get('enabled_by_default'),
                icon=str(reg.get('icon', '') or ''),
                suggested_display_precision=reg.get('suggested_display_precision'),
                register_type=normalize_register_type(reg.get('register_type') or reg.get('fc')),
                mqtt_enabled=mqtt.get('enabled', True),
                mqtt_topic=mqtt.get('topic', ''),
                influxdb_enabled=influx.get('enabled', True),
                influxdb_measurement=influx.get('measurement', ''),
                influxdb_tags=influx.get('tags', {}),
                category=str(reg.get('category', '') or ''),
                ui_show_on_dashboard=ui.get('show_on_dashboard', True),
                ui_widget=ui.get('widget', 'value'),
                ui_config=ui,
                thresholds=reg.get('thresholds'),
                aggregates=(dict(reg['aggregates'])
                            if isinstance(reg.get('aggregates'), dict) else None),
                **_can_kwargs(reg),
            ))
        return out

    def _load_all_registers(self):
        """Load all available registers from modbus_data.json."""
        if not self.all_registers_path.exists():
            logger.warning(f"All registers file not found: {self.all_registers_path}")
            return

        try:
            with open(self.all_registers_path, 'r') as f:
                self.all_registers = json.load(f)
            logger.info(f"Loaded all registers from {self.all_registers_path}")
        except Exception as e:
            logger.error(f"Error loading all registers: {e}")

    @staticmethod
    def _kept_sections(path: Path) -> Dict:
        """What else a register file holds besides the selection — calculated
        fields, energy fields, whatever a later version adds. A rewrite of the
        selection carries it over; dropping it lost a device's calculated
        fields on every save from the Measurements page."""
        try:
            with open(path) as f:
                old = json.load(f) or {}
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in old.items() if k not in ("version", "registers")}

    def save_selected_registers(self, registers: List[Dict]):
        """Save selected registers to file."""
        validate_register_identity(registers)      # audit DP-9
        # hold _file_lock like every other config writer: for the PRIMARY device
        # this file is shared with save_energy_fields/save_calculated/... which
        # take the lock; without it here a concurrent save could lose fields or
        # clobber the shared .tmp
        with self._file_lock:
            data = {
                **self._kept_sections(self.registers_path),
                "version": "1.0",
                "registers": registers,
                "poll_groups": {
                    name: {"interval": group.interval, "description": group.description}
                    for name, group in self.poll_groups.items()
                },
            }
            tmp = self.registers_path.with_suffix(self.registers_path.suffix + '.tmp')
            with open(tmp, 'w') as f:                   # atomic: crash-safe write
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())           # durable before the rename
            os.replace(tmp, self.registers_path)

        # Reload
        self._load_selected_registers()
        logger.info(f"Saved {len(registers)} selected registers")
