# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Config integrity primitives: the last-known-good regression guard, a file signature, and the
JSON .good/.bad self-heal shared by config.yaml and the register files. Split out of io.py
(ADR-0047 P1); a mixin on Config, so `self.*` resolves across the other config mixins."""
import json
import logging
import os

import yaml

from multibus.core.config.util import _copy_private, _devices_in

logger = logging.getLogger("multibus.core.config")


class ConfigHealthMixin:
    def _good_would_regress(self, new_data: dict, good_path) -> bool:
        """True when promoting ``new_data`` over the existing .good snapshot
        would LOSE devices — the truncation signature (a cut file keeps the
        early modbus/mqtt sections and drops the devices tail). Comparison
        errors fail open (promote), so a weird snapshot never wedges loads."""
        try:
            if not good_path.exists():
                return False
            with open(good_path, 'r') as f:
                good = yaml.safe_load(f) or {}
            return _devices_in(new_data) < _devices_in(good)
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _file_sig(path):
        """What says a file changed: inode, size, mtime (ns); None when it
        cannot be read."""
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (st.st_ino, st.st_size, st.st_mtime_ns)

    def _load_json_with_heal(self, path, *, required_key: str, label: str):
        """Load a JSON config file with the same .good/.bad self-heal contract
        as config.yaml (external audit: selected_registers.json had NONE — a
        truncated file silently emptied the whole selection, stopping every
        poller while the meter looked merely stale).

        - Parse error OR implausible shape (not a dict / ``required_key``
          missing — every MBG save writes that key, even when its list is
          empty) → the broken file is copied to ``<path>.bad`` and the last
          known-good snapshot is loaded instead.
        - A plausible load refreshes the ``<path>.good`` snapshot — but never
          with an implausible husk, so .good always holds a real selection.
        Returns the parsed dict, or None when nothing loadable exists."""
        good = path.with_suffix(path.suffix + '.good')

        def _plausible(d) -> bool:
            return isinstance(d, dict) and required_key in d

        try:
            with open(path, 'r') as f:
                data = json.load(f)
            if not _plausible(data):
                raise ValueError(f"parsed but looks truncated (missing {required_key!r} key)")
        except Exception as e:  # noqa: BLE001
            bad = path.with_suffix(path.suffix + '.bad')
            try:
                _copy_private(path, bad)          # 0600 + atomic (F-39, 3.83.0)
                logger.error(f"{label}: {e} — broken file copied to {bad}")
            except Exception:  # noqa: BLE001
                logger.error(f"{label}: {e}")
            if good.exists():
                try:
                    with open(good, 'r') as f:
                        data = json.load(f)
                    if _plausible(data):
                        logger.error(f"SELF-HEAL: {label} loaded from last "
                                     f"known-good snapshot {good}")
                        return data
                    logger.error(f"{label}: snapshot {good} is implausible too")
                except Exception as e2:  # noqa: BLE001
                    logger.error(f"{label}: snapshot load also failed: {e2}")
            return None
        # the snapshot is refreshed once per version of the file, not on every read
        sig = self._file_sig(path)
        if sig is None or self._good_sigs.get(str(path)) != sig or not good.exists():
            try:
                _copy_private(path, good)         # 0600 + atomic (F-39, 3.83.0)
                if sig is not None:
                    self._good_sigs[str(path)] = sig
            except Exception:  # noqa: BLE001
                pass
        return data
