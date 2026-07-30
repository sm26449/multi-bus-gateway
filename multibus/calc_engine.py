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
"""Calculated-register engine — formula-derived measurements.

Owns the runtime side of calculated registers: building the per-device entry
list from saved config, resolving live-value references (same-device by name,
cross-device as ``device.register``), evaluating expressions each poll and
injecting the results into the device's value store + routing them to its
sinks. Extracted verbatim from ``create_api()`` (api.py) — behavior, synthetic
addressing and routing are byte-identical to the closure version; the golden
tests in tests/test_golden_routing.py pin that contract.

Wiring notes (both matter for correctness, not style):

* ``store_for(device_id)`` — injected accessor for a device's live value store
  (None if unknown). The engine never owns stores; the primary's store aliasing
  and per-device isolation stay the API layer's business.
* ``publishers()`` — zero-arg callable returning ``(mqtt, influx)`` resolved AT
  CALL TIME. ``/api/config/apply`` can rebind the publishers (nonlocal) when a
  sink is enabled after boot; holding direct references here would silently
  keep publishing into the dead pre-apply object (the same stale-ref bug the
  vmeter manager had to fix).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Callable, Dict, Optional

from . import expressions
from .config import SelectedRegister

logger = logging.getLogger(__name__)

# Synthetic address space for calculated registers: index-based, far above any
# real Modbus address so they can live in the same store as polled values.
CALC_ADDR_BASE = 8_000_000


class CalcEngine:
    def __init__(self, config, store_for: Callable[[str], Optional[Dict]],
                 publishers: Callable[[], tuple]):
        self.config = config
        self._store_for = store_for
        self._publishers = publishers
        self.store: Dict[str, list] = {}          # device_id -> built entries

    def load(self, device_id: str):
        """(Re)build the runtime calc list for a device from its saved config."""
        # Purge stale calc values first: synthetic addresses are index-based
        # (CALC_ADDR_BASE + i), so deleting/reordering a calc would otherwise leave
        # a ghost value at the old address forever (in /api/values, /api/meters,
        # the dashboard) — or a different formula would inherit the old address.
        store = self._store_for(device_id)
        if store is not None:
            for _a in [a for a in list(store) if isinstance(a, int) and a >= CALC_ADDR_BASE]:
                store.pop(_a, None)
        built = []
        for i, e in enumerate(self.config.load_calculated(device_id)):
            reg = SelectedRegister(
                address=CALC_ADDR_BASE + i,
                name=e.get('name') or f'CALC_{i}',
                label=e.get('label') or e.get('name') or f'CALC_{i}',
                unit=e.get('unit', ''), data_type='float',
                poll_group=e.get('poll_group') or 'normal',
            )
            _ok, _err, refs = expressions.validate_expression(e.get('expr', ''))
            # compile the AST ONCE here (not per poll) — the hot path reuses it
            try:
                _tree = expressions.compile_expression(e.get('expr', ''))
            except Exception:  # noqa: BLE001 — invalid expr: evaluate() will surface it
                _tree = None
            built.append({'expr': e.get('expr', ''), 'decimals': e.get('decimals'),
                          'poll_group': reg.poll_group, '_reg': reg, '_tree': _tree,
                          '_refs': refs, '_state': {'prev': {}, 'ts': None}})
        self.store[device_id] = built
        return built

    def resolver(self, this_store: Dict[int, Dict]):
        """Resolve a reference to a live value: bare name -> this device;
        ``dev.reg`` -> another device. Builds name->(value, ts) maps lazily.

        The returned callable also records, in ``resolve.touched_ts``, the
        measurement timestamp of every input it resolves, so a calc result can
        inherit the OLDEST contributing input's freshness (a formula must not
        launder a dead device's frozen value as fresh). Clear the list before
        each evaluation."""
        def _index(store):
            # snapshot before iterating: a poller thread may insert a new address
            # concurrently (dict changed size during iteration otherwise)
            out = {}
            for it in list(store.values()):
                nm = it.get('name')
                if nm:
                    out[nm] = (it.get('value'), it.get('timestamp'))
            return out
        this_map = _index(this_store)
        others: Dict[str, Dict] = {}

        def resolve(name):
            if '.' in name:
                dev, reg = name.split('.', 1)
                if dev not in others:
                    others[dev] = _index(self._store_for(dev) or {})
                pair = others[dev].get(reg)
            else:
                pair = this_map.get(name)
            if pair is None:
                return None
            value, ts = pair
            if ts is not None:
                resolve.touched_ts.append(ts)
            return value
        resolve.touched_ts = []
        return resolve

    def run(self, calc_key, poll_group, values_store, *, topic_prefix,
            bucket, device_tag, device_id, mqtt_on, influx_on):
        """Evaluate the device's calc registers assigned to ``poll_group`` from the
        current live values, inject them into the store, and route to its sinks."""
        entries = self.store.get(calc_key)
        if not entries:
            return {}
        resolve = self.resolver(values_store)
        # A push-driven source (MQTT input) publishes on the synthetic 'mqtt'
        # group, which no calc register is assigned to — so match ALL calc groups
        # there, else calculated registers on an MQTT-in device would never
        # evaluate. Polled sources keep matching their exact group.
        _wildcard = (poll_group == 'mqtt')
        batch = {}
        for e in entries:
            if not _wildcard and e['poll_group'] != poll_group:
                continue
            st = e['_state']
            now = time.time()
            dt = (now - st['ts']) if st['ts'] is not None else 0.0
            prevmap = st['prev']
            resolve.touched_ts = []                # collect input freshness this run
            try:
                # reuse the AST compiled at load() — no re-parse per poll
                _tree = e.get('_tree')
                if _tree is not None:
                    val = expressions.evaluate_tree(_tree, resolve,
                                                    prev_resolve=prevmap.get, dt=dt)
                else:
                    val = expressions.evaluate(e['expr'], resolve,
                                               prev_resolve=prevmap.get, dt=dt)
                ok = True
            except (expressions.MissingValue, expressions.ExpressionError):
                ok = False                        # missing input / math error / first prev()
            except Exception:  # noqa: BLE001 — a calc bug must NEVER kill the poller
                logger.debug("calc %s: unexpected error", e['_reg'].name, exc_info=True)
                ok = False
            # Snapshot this run's inputs + time so prev()/dt work next round — even
            # when we skipped (e.g. the very first run has no history yet).
            st['prev'] = {ref: resolve(ref) for ref in e.get('_refs', [])}
            st['ts'] = now
            if not ok:
                continue
            if isinstance(val, bool):
                val = 1 if val else 0
            elif isinstance(val, (int, float)) and e.get('decimals') is not None:
                try:
                    val = round(float(val), int(e['decimals']))
                except (TypeError, ValueError):
                    pass
            reg = e['_reg']
            # The result is only as fresh as its OLDEST input (vmeter 'sum'
            # rule) — a calc referencing a dead device must not be re-stamped
            # now() every cycle and read as good forever. ISO timestamps sort
            # lexicographically in the same order as time, so min() is correct.
            _in_ts = [t for t in resolve.touched_ts if t]
            _result_ts = min(_in_ts) if _in_ts else datetime.now().isoformat()
            # numeric freshness clock inherits the OLDEST contributing input's
            # time; None when no input carried one → the vmeter fails closed
            # rather than treating a fabricated now() as fresh.
            _in_num = [datetime.fromisoformat(t).timestamp() for t in _in_ts]
            values_store[reg.address] = {
                'value': val, 'name': reg.name, 'label': reg.label,
                'unit': reg.unit, 'poll_group': reg.poll_group,
                'timestamp': _result_ts, 'calculated': True,
                'ts': min(_in_num) if _in_num else None,
            }
            batch[reg.address] = {'value': val, 'register': reg}
        if not batch:
            return {}
        mqtt_publisher, influxdb_publisher = self._publishers()
        if mqtt_publisher and mqtt_on:
            try:
                mqtt_publisher.publish_register_data(poll_group, batch, topic_prefix=topic_prefix)
            except Exception as ex:  # noqa: BLE001
                logger.warning(f"calc MQTT publish failed for {calc_key}: {ex}")
        if influxdb_publisher and influx_on:
            try:
                influxdb_publisher.write_register_data(poll_group, batch, bucket=bucket,
                                                       device_tag=device_tag, device_id=device_id)
            except Exception as ex:  # noqa: BLE001
                logger.warning(f"calc Influx write failed for {calc_key}: {ex}")
        return batch
