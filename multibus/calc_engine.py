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

import json
import logging
import threading
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional, Set

from . import expressions
from .config import SelectedRegister
from .value_decode import decode_register

logger = logging.getLogger(__name__)

# Synthetic address space for calculated registers: index-based, far above any
# real Modbus address so they can live in the same store as polled values.
CALC_ADDR_BASE = 8_000_000
# a device measured by meters is evaluated at most this often (a meter fed
# by MQTT or a bus tap can report per message)
LINKED_MIN_GAP_S = 0.5


class CalcEngine:
    def __init__(self, config, store_for: Callable[[str], Optional[Dict]],
                 publishers: Callable[[], tuple]):
        self.config = config
        self._store_for = store_for
        self._publishers = publishers
        self.store: Dict[str, list] = {}          # device_id -> built entries
        # what a device's meters give it (linked_meter.py), evaluated when a
        # METER reports — meter id -> the devices it measures
        self._linked_index: Dict[str, Set[str]] = {}
        self.pairs: Dict[str, dict] = {}          # device_id -> its input/output pair settings
        self.pair_verdicts: Dict[str, dict] = {}  # device_id -> the last evaluation's verdict
        self._linked_locks: Dict[str, threading.Lock] = {}
        self._linked_last: Dict[str, float] = {}
        self._linked_state: Dict[str, dict] = {}  # device_id -> the pair's input history
        self._linked_pending: Set[str] = set()    # a trailing evaluation is scheduled
        self._sigs: Dict[str, tuple] = {}         # device_id -> what its formulas read through (see _load)
        self._index_lock = threading.Lock()

    def dependents(self, meter_id: str) -> Set[str]:
        """The devices ``meter_id`` measures (their linked values follow it)."""
        return set(self._linked_index.get(meter_id, ()))

    def rebuild_linked_index(self) -> None:
        """meter -> devices, from every device's meter links (cheap: the
        config's links, no file read)."""
        from .meter_links import links_of
        idx: Dict[str, Set[str]] = {}
        for d in getattr(self.config, 'devices', None) or []:
            for ln in links_of(d):
                idx.setdefault(ln['device'], set()).add(d.id)
        with self._index_lock:
            self._linked_index = idx

    def load(self, device_id: str, cascade: bool = True):
        """(Re)build the runtime calc list for a device from its saved config.
        ``cascade``: the devices this one measures are rebuilt too — what
        they compute from it (its unit, its pace) may have changed."""
        built = self._load(device_id)
        self.rebuild_linked_index()
        if cascade:
            for dep in sorted(self.dependents(device_id) - {device_id}):
                try:
                    self._load(dep)
                except Exception as e:  # noqa: BLE001 — one device's formulas never stop another's
                    logger.warning("calc %s: reload after its meter %s changed failed — %s",
                                   dep, device_id, e)
        return built

    def _load(self, device_id: str):
        """(Re)build the runtime calc list for a device from its saved config."""
        # Purge stale calc values first: synthetic addresses are index-based
        # (CALC_ADDR_BASE + i), so deleting/reordering a calc would otherwise leave
        # a ghost value at the old address forever (in /api/values, /api/meters,
        # the dashboard) — or a different formula would inherit the old address.
        store = self._store_for(device_id)
        if store is not None:
            for _a in [a for a in list(store) if isinstance(a, int) and a >= CALC_ADDR_BASE]:
                store.pop(_a, None)
        # what an unchanged formula remembers (prev(), ema(), hysteresis())
        # survives a reload — a meter-option save must not reset a device's
        # own calculated values (review m15)
        kept = {(e['_reg'].name, e['expr'], e.get('_pair')): e['_state']
                for e in self.store.get(device_id, []) if e.get('_reg') is not None}
        built = []
        entries = list(self.config.load_calculated(device_id))
        # a meter measuring this device gives it power_electric / energy_electric
        from .linked_meter import linked_calcs, pair_expire_after, pair_settings
        dev = next((d for d in getattr(self.config, 'devices', []) if d.id == device_id), None)
        self.pairs.pop(device_id, None)
        if dev is not None:
            try:
                ps = pair_settings(self.config, dev)
            except Exception:  # noqa: BLE001 — unreadable meter selection: no pair values
                logger.warning("calc %s: its input/output meters could not be read", device_id, exc_info=True)
                ps = None
            if ps is not None:
                self.pairs[device_id] = ps
            else:
                self.pair_verdicts.pop(device_id, None)
            # a name the device reads itself — register or calculated — wins
            mine = {e.get('name') for e in entries}
            units: Dict[str, str] = {}
            try:
                units = {r.name: str(r.unit or '') for r in self.config.unit_registers(dev)}
                mine |= set(units)
            except Exception:  # noqa: BLE001 — unreadable selection: the calculated names still guard
                pass
            entries += [c for c in linked_calcs(self.config, dev, ps) if c['name'] not in mine]
            # what its formulas read through: its own map and units, the devices
            # they name (their map and address), its meters. When that changes
            # (a unit Wh -> kWh, another meter behind the same id), a rate's
            # memory (prev(), dt) would mix two worlds for one round: it starts
            # again. ema() and hysteresis() keep theirs (review N7).
            refs = set()
            for e in entries:
                for r in (expressions.validate_expression(e.get('expr', ''))[2] or []):
                    full = expressions.unprepare_ref(str(r))
                    if '.' in full:
                        refs.add(full.split('.', 1)[0])

            def _dev_sig(d):
                c = getattr(d, 'connection', None)
                return (d.id, d.template, d.protocol, getattr(c, 'host', ''), getattr(c, 'port', None),
                        getattr(c, 'unit_id', None))
            others = sorted(_dev_sig(d) for d in getattr(self.config, 'devices', []) if d.id in refs)
            sig = (_dev_sig(dev), tuple(sorted(units.items())), tuple(others),
                   json.dumps(ps, sort_keys=True, default=str) if ps else '')
            if device_id in self._sigs and self._sigs[device_id] != sig:
                for st in kept.values():
                    if 'prev' in st:
                        st['prev'] = {}
                        st['ts_mono'] = None
            self._sigs[device_id] = sig
        for i, e in enumerate(entries):
            reg = SelectedRegister(
                address=CALC_ADDR_BASE + i,
                name=e.get('name') or f'CALC_{i}',
                label=e.get('label') or e.get('name') or f'CALC_{i}',
                unit=e.get('unit', ''), data_type='float',
                poll_group=e.get('poll_group') or 'normal',
                # An explicit topic/measurement lets a DERIVED value land on the
                # branch it belongs to (`status/text`) instead of its flat name.
                # Deliberately no canonical fallback: every calc register that
                # exists today publishes to its flat name, and routing identity
                # must not shift under a user who only upgraded.
                mqtt_topic=str(e.get('topic') or ''),
                influxdb_measurement=str(e.get('measurement') or ''),
                category=str(e.get('category') or ''),
                mqtt_enabled=bool(e.get('mqtt', True)),
                influxdb_enabled=bool(e.get('influxdb', True)),
                # a computed CODE becomes text through the same decoder real
                # status registers use — one decode path, not two
                enum=e.get('enum') or None,
                aggregates=(dict(e['aggregates'])
                            if isinstance(e.get('aggregates'), dict) else None),
            )
            if e.get('_pair'):
                # a pair's value: its HA entity is known by its NAME, not by
                # its index (a calculated field added later would renumber
                # it); it expires in HA when the gateway stops sending it
                # (an absent value, linked_meter.py). The consumption link's
                # values keep the address-based identity they always had.
                reg.discovery_key = f"linked_{reg.name}"
                reg.ha_expire_after = (pair_expire_after(ps) if ps else None)
                # an input/output pair's value: computed by linked_meter, not a formula
                built.append({'expr': '', 'decimals': e.get('decimals'), 'poll_group': reg.poll_group,
                              '_reg': reg, '_tree': None, '_invalid': False, '_refs': [],
                              '_linked': True, '_pair': e['_pair'],
                              '_state': kept.get((reg.name, '', e['_pair']), {'shown': False})})
                continue
            _ok, _err, refs = expressions.validate_expression(e.get('expr', ''))
            # An entry that fails validation is kept IN PLACE but marked
            # invalid — the API save path 422s before it lands here, so this
            # only catches a hand-edited registers file or an imported backup
            # that dodged route validation. It must not be dropped (synthetic
            # addresses are index-based — dropping would shift later calcs to
            # different addresses) and must not be evaluated (the whitelist
            # lives in validate; a bare ast.parse would run it anyway).
            if not _ok:
                logger.warning("calc %s: invalid expression skipped (%s)",
                               reg.name, _err)
            # compile the AST ONCE here (not per poll) — the hot path reuses it
            _tree = None
            if _ok:
                try:
                    _tree = expressions.compile_expression(e.get('expr', ''))
                except Exception:  # noqa: BLE001 — invalid expr: evaluate() will surface it
                    _tree = None
            built.append({'expr': e.get('expr', ''), 'decimals': e.get('decimals'),
                          'poll_group': reg.poll_group, '_reg': reg, '_tree': _tree,
                          '_invalid': not _ok, '_linked': bool(e.get('_linked')),
                          '_refs': refs,
                          '_state': kept.get((reg.name, e.get('expr', ''), None),
                                             {'prev': {}, 'ts_mono': None})})
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
                    out[nm] = (it.get('value'), it.get('timestamp'),
                               it.get('mono'), it.get('interval'))
            return out
        def _totals_index(endpoint_id):
            from .endpoint_aggregator import latest_totals
            got = latest_totals(endpoint_id)
            if not got:
                return {}
            agg, wall, mono, interval = got
            ts = datetime.fromtimestamp(wall).isoformat()
            return {k: (v, ts, mono, interval) for k, v in agg.items()
                    if isinstance(v, (int, float)) and not isinstance(v, bool)}
        this_map = _index(this_store)
        others: Dict[str, Dict] = {}

        def _pair(name):
            if '.' in name:
                dev, reg = expressions.unprepare_ref(name).split('.', 1)
                if dev not in others:
                    _st = self._store_for(dev)
                    # not a device: an installation's totals, as its last cycle left them
                    others[dev] = _index(_st) if _st is not None else _totals_index(dev)
                return others[dev].get(reg)
            return this_map.get(name)

        def is_stale(name):
            """Too old to trust: a device that stopped answering keeps its last
            value in the store; first() must not prefer it to a live estimate."""
            pair = _pair(name)
            if pair is None:
                return True
            _v, _ts, mono, interval = pair
            if mono is None:
                return False
            return (time.monotonic() - mono) > max(3.0 * float(interval or 5), 60.0)

        def age_of(name):
            """Seconds since a reference was measured (its oldest input, for a
            calculated one); None when unknown."""
            pair = _pair(name)
            if pair is None or pair[2] is None:
                return None
            return max(0.0, time.monotonic() - pair[2])

        def resolve(name):
            pair = _pair(name)
            if pair is None:
                return None
            value, ts, mono, interval = pair
            if ts is not None:
                resolve.touched_ts.append(ts)
            if mono is not None:
                resolve.touched_mono.append(mono)
            if interval is not None:
                resolve.touched_interval.append(interval)
            return value
        resolve.touched_ts = []
        resolve.touched_mono = []
        resolve.touched_interval = []
        resolve.is_stale = is_stale
        resolve.age_of = age_of
        return resolve

    def run_linked(self, calc_key, values_store, *, gone: Optional[List] = None, force: bool = False,
                   rerun: Optional[Callable[[], object]] = None, **routing):
        """Evaluate what a device's meters give it — called when one of its
        METERS reports (``dependents``), so a device measured by meters is
        computed even when it reads nothing itself, or its own link is down.
        Only for a device that exists and is ENABLED (a disabled device
        publishes nothing, as it never did). One evaluation at a time per
        device, at most every LINKED_MIN_GAP_S; a report that falls inside
        the gap, or while an evaluation runs, is not lost: ``rerun`` is
        called once at the end of the gap (the trailing edge). ``gone``:
        filled with the registers whose value went absent this round (the
        caller clears their retained topic)."""
        dev = self.config.get_device(calc_key)
        if calc_key not in self.store or dev is None or not getattr(dev, 'enabled', True):
            return {}
        lock = self._linked_locks.setdefault(calc_key, threading.Lock())
        if not lock.acquire(blocking=False):
            # an evaluation is running now: it reads the same stores — run
            # once more after it, so this report is judged too
            self._trail(calc_key, LINKED_MIN_GAP_S, rerun)
            return {}
        try:
            now = time.monotonic()
            wait = LINKED_MIN_GAP_S - (now - self._linked_last.get(calc_key, -1e9))
            if not force and wait > 0:
                self._trail(calc_key, wait, rerun)
                return {}
            self._linked_last[calc_key] = now
            return self.run(calc_key, '', values_store, linked=True, gone=gone, **routing)
        finally:
            lock.release()

    def _trail(self, calc_key: str, delay: float, rerun: Optional[Callable[[], object]]) -> None:
        """One evaluation at the end of the gap, for the reports it held back
        (at most one pending per device)."""
        if rerun is None or calc_key in self._linked_pending:
            return
        self._linked_pending.add(calc_key)

        def go():
            self._linked_pending.discard(calc_key)
            try:
                rerun()
            except Exception:  # noqa: BLE001 — a late evaluation never raises into a timer thread
                logger.debug("trailing evaluation of %s", calc_key, exc_info=True)
        t = threading.Timer(max(0.01, delay), go)
        t.daemon = True
        t.start()

    def _push_group(self, device_id: str) -> Optional[str]:
        """The group a push-fed device's own values arrive on (``mqtt`` for
        an MQTT input, ``tap`` for a listening RTU tap) — what its calculated
        values were tagged with when its own report computed them; None for
        a polled device."""
        dev = self.config.get_device(device_id)
        if dev is None:
            return None
        protos = [str(getattr(s, 'protocol', '') or '').lower() for s in (getattr(dev, 'sources', None) or [])]
        protos = protos or [str(getattr(dev, 'protocol', '') or '').lower()]
        if protos and all(p == 'mqtt' for p in protos):
            return 'mqtt'
        if protos and all(p == 'rtu_tap' for p in protos):
            return 'tap'
        return None

    def forget(self, device_id: str) -> None:
        """A device is gone: nothing of it is kept — a device later created
        with the same id starts with no memory of this one."""
        self.store.pop(device_id, None)
        self.pairs.pop(device_id, None)
        self.pair_verdicts.pop(device_id, None)
        self._linked_locks.pop(device_id, None)
        self._linked_last.pop(device_id, None)
        self._linked_state.pop(device_id, None)
        self._sigs.pop(device_id, None)
        # (its config entry may still be there while it is torn down)
        with self._index_lock:
            self._linked_index = {m: ds - {device_id} for m, ds in self._linked_index.items()
                                  if m != device_id and ds - {device_id}}

    def _run_pair(self, calc_key, entries, resolve, values_store, batch, gone):
        """An input / output pair's values (linked_meter.evaluate_pair): each
        published, or ABSENT — its live value removed (rules, /api/values and
        the WebSocket see no value) and, if it was shown, listed in ``gone``."""
        from .linked_meter import evaluate_pair
        ps = self.pairs.get(calc_key)
        pair = [e for e in entries if e.get('_pair')]
        if ps is None or not pair:
            return
        state = self._linked_state.setdefault(calc_key, {})
        rating = None
        if ps.get('rated') and expressions._attr_value is not None:
            try:
                rating = expressions._attr_value(calc_key, 'rating', 'rated_ac_kw')
            except Exception:  # noqa: BLE001 — no rating: no load
                rating = None
        vals, verdict = evaluate_pair(ps, resolve, state, rating)
        _ts = [x for x in (resolve.touched_ts or []) if x]
        _mono = [x for x in (resolve.touched_mono or []) if x is not None]
        _iv = [x for x in (resolve.touched_interval or []) if x is not None]
        result_ts = min(_ts) if _ts else datetime.now().isoformat()
        ts_num = datetime.fromisoformat(min(_ts)).timestamp() if _ts else None
        mono = min(_mono) if _mono else None
        interval = max(_iv) if _iv else None
        for e in pair:
            reg = e['_reg']
            st = e['_state']
            val = vals.get(e['_pair'])
            if val is None:
                values_store.pop(reg.address, None)
                if st.get('shown') and gone is not None:
                    gone.append(reg)
                st['shown'] = False
                continue
            if e.get('decimals') is not None:
                val = round(float(val), int(e['decimals']))
            values_store[reg.address] = {
                'value': val, 'name': reg.name, 'label': reg.label, 'unit': reg.unit,
                'poll_group': reg.poll_group, 'timestamp': result_ts, 'calculated': True,
                'ts': ts_num, 'mono': mono, 'interval': interval, 'aggregates': None,
            }
            batch[reg.address] = {'value': val, 'register': reg, 'ts': ts_num, 'mono': mono}
            st['shown'] = True
        self.pair_verdicts[calc_key] = {**verdict, 'at': time.time(),
                                        'thresholds': {k: ps[k] for k in ('min_load_w', 'tolerance_pct',
                                                                          'max_skew_s')}}

    def run(self, calc_key, poll_group, values_store, *, topic_prefix,
            bucket, device_tag, device_id, mqtt_on, influx_on, influx_tags=None,
            mqtt_policy=None, influx_policy=None, linked: bool = False,
            gone: Optional[List] = None):
        """Evaluate the device's calc registers assigned to ``poll_group`` from the
        current live values, inject them into the store, and route to its sinks.
        ``linked``: only what its meters give it (run_linked) — a device's own
        poll never runs those."""
        entries = self.store.get(calc_key)
        if not entries:
            return {}
        resolve = self.resolver(values_store)
        # A push-driven source publishes on its synthetic group ('mqtt' for
        # the MQTT input, 'tap' for the listen-only RTU tap), which no calc
        # register is assigned to — so match ALL calc groups there, else
        # calculated registers on a push-driven device would never evaluate.
        # Polled sources keep matching their exact group.
        _wildcard = poll_group in ('mqtt', 'tap')
        batch = {}
        if linked:
            resolve.touched_ts, resolve.touched_mono, resolve.touched_interval = [], [], []
            self._run_pair(calc_key, entries, resolve, values_store, batch, gone)
        for e in entries:
            if bool(e.get('_linked')) != linked or e.get('_pair'):
                continue
            if not linked and not _wildcard and e['poll_group'] != poll_group:
                continue
            if e.get('_invalid'):
                continue              # failed validation at load() — never evaluate
            st = e['_state']
            # dt for rate/integral formulas is on the MONOTONIC clock — a wall
            # step (NTP/chrony, e.g. the 2026-07-28 +138s jump) between two calc
            # runs would otherwise corrupt the derived value persisted to Influx.
            now_mono = time.monotonic()
            dt = (now_mono - st['ts_mono']) if st['ts_mono'] is not None else 0.0
            prevmap = st['prev']

            def _prev(name, _m=prevmap):                # refs are stored with the device id as written
                return _m.get(expressions.unprepare_ref(name))
            resolve.touched_ts = []                # collect input freshness this run
            resolve.touched_mono = []
            resolve.touched_interval = []
            try:
                # reuse the AST compiled at load() — no re-parse per poll
                _tree = e.get('_tree')
                if _tree is not None:
                    val = expressions.evaluate_tree(_tree, resolve,
                                                    prev_resolve=_prev, dt=dt,
                                                    stale=resolve.is_stale, age=resolve.age_of,
                                                    memo=st.setdefault('memo', {}))
                else:
                    val = expressions.evaluate(e['expr'], resolve,
                                               prev_resolve=_prev, dt=dt,
                                               stale=resolve.is_stale, age=resolve.age_of,
                                               memo=st.setdefault('memo', {}))
                ok = True
            except (expressions.MissingValue, expressions.ExpressionError):
                ok = False                        # missing input / math error / first prev()
            except Exception:  # noqa: BLE001 — a calc bug must NEVER kill the poller
                logger.debug("calc %s: unexpected error", e['_reg'].name, exc_info=True)
                ok = False
            # the inputs THIS evaluation read, before the prev() snapshot below
            # reads every reference again (a stale one that first() or fresh()
            # skipped would otherwise stamp the result with its age)
            _touched_ts = list(resolve.touched_ts)
            _touched_mono = list(getattr(resolve, 'touched_mono', []))
            _touched_iv = list(getattr(resolve, 'touched_interval', []))
            # Snapshot this run's inputs + time so prev()/dt work next round — even
            # when we skipped (e.g. the very first run has no history yet).
            st['prev'] = {ref: resolve(ref) for ref in e.get('_refs', [])}
            st['ts_mono'] = now_mono
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
            if reg.enum:
                val = decode_register(val, reg)
                if val is None:
                    continue                  # undecodable → publish nothing
            # The result is only as fresh as its OLDEST input (vmeter 'sum'
            # rule) — a calc referencing a dead device must not be re-stamped
            # now() every cycle and read as good forever. ISO timestamps sort
            # lexicographically in the same order as time, so min() is correct.
            _in_ts = [t for t in _touched_ts if t]
            _result_ts = min(_in_ts) if _in_ts else datetime.now().isoformat()
            # numeric freshness clock inherits the OLDEST contributing input's
            # time; None when no input carried one → the vmeter fails closed
            # rather than treating a fabricated now() as fresh.
            _in_num = [datetime.fromisoformat(t).timestamp() for t in _in_ts]
            _in_mono = _touched_mono
            _ts = min(_in_num) if _in_num else None
            _mono = min(_in_mono) if _in_mono else None
            # the result refreshes no faster than its SLOWEST input — carry that
            # cadence so the vmeter derives the right per-row bound
            _in_iv = _touched_iv
            _interval = max(_in_iv) if _in_iv else None
            values_store[reg.address] = {
                'value': val, 'name': reg.name, 'label': reg.label,
                'unit': reg.unit, 'poll_group': reg.poll_group,
                'timestamp': _result_ts, 'calculated': True,
                'ts': _ts, 'mono': _mono, 'interval': _interval,
                'aggregates': getattr(reg, 'aggregates', None),
            }
            # same item shape as the poller batch: downstream publishers stamp
            # points with the OLDEST input's time, not a fabricated now()
            batch[reg.address] = {'value': val, 'register': reg,
                                  'ts': _ts, 'mono': _mono}
        if not batch:
            return {}
        mqtt_batches = [(batch, mqtt_policy or {})]
        if linked:
            # a meter's value carries the group the device's own poll gave it
            # (its InfluxDB `poll_group` tag): the group a push-fed device
            # reports on (`mqtt`, `tap`), else the device's own group
            poll_group = (self._push_group(calc_key)
                          or next(iter(batch.values()))['register'].poll_group)
            # a pair's values are re-sent while present, so Home Assistant's
            # expire_after only fires when they are absent (linked_meter)
            ps = self.pairs.get(calc_key)
            pair = {a: it for a, it in batch.items() if getattr(it['register'], 'ha_expire_after', None)}
            if ps and pair:
                from .linked_meter import pair_heartbeat
                hb = pair_heartbeat(ps)
                own = (mqtt_policy or {}).get('heartbeat')
                mqtt_batches = [({a: it for a, it in batch.items() if a not in pair}, mqtt_policy or {}),
                                (pair, {**(mqtt_policy or {}), 'heartbeat': min(hb, own) if own else hb})]
        mqtt_publisher, influxdb_publisher = self._publishers()
        if mqtt_publisher and mqtt_on:
            for part, policy in mqtt_batches:
                if not part:
                    continue
                try:
                    mqtt_publisher.publish_register_data(poll_group, part, topic_prefix=topic_prefix,
                                                         **policy)
                except Exception as ex:  # noqa: BLE001
                    logger.warning(f"calc MQTT publish failed for {calc_key}: {ex}")
        if influxdb_publisher and influx_on:
            try:
                influxdb_publisher.write_register_data(poll_group, batch, bucket=bucket,
                                                       device_tag=device_tag, device_id=device_id,
                                                       extra_tags=influx_tags, **(influx_policy or {}))
            except Exception as ex:  # noqa: BLE001
                logger.warning(f"calc Influx write failed for {calc_key}: {ex}")
        return batch
