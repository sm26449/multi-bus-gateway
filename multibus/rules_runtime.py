# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The rules runtime — wires the pure engine (multibus/rules.py) to the live
store, the commands, MQTT, the event log and the alerts.

One thread ticks every ``every_s`` (the smallest across rules): resolve each
rule's signal through the calculated-register resolver, read each target
unit's read-back, ask the RuleState for a Decision, and act on it — a command
through the same path every other face uses (``via: rule:<id>``), a sweep of
the read-back's poll group, an event, a retained state topic.

Rules live in ``rules.yaml`` next to ``config.yaml``; the little that must
survive a restart (clamps, overrides) in ``rules_state.json``.
"""
from __future__ import annotations

import json
import math
import logging
import os
import threading
import time
import weakref
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait as _wait_futures
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from . import clock, expressions, input_check
from . import role as gateway_role
from .arbiter import COMBINE, Candidate, ChannelState, beats
from .commands import assign_channels, normalize_params, turns_on
from .rules import (LAYERS, MANUAL, MAX_FORCE_S, NORMAL, STALE, TUNING_RULE_KEYS, TUNING_TIMING_KEYS, Decision,
                    RuleDef, RuleState, parse_rule_def, tuning_errors, tuning_of, validate_rule_def)

logger = logging.getLogger(__name__)

# the longest a controller may pause a rule for one command (F-09, 3.83.0):
# an unbounded override_s left a safety rule silenced for years
MAX_OVERRIDE_S = 24 * 3600.0
MAX_CLAMP_EXPIRES_S = 7 * 24 * 3600.0   # F-74

MQTT_ROOT = 'mbg/rules'
SWEEP_MIN_INTERVAL_S = 60.0
FAILURES_BEFORE_ALERT = 3
# a release trusts a read-back only this young (and newer than the rule's
# last write): a Fronius controls group is polled hourly, its "fresh" would
# be hours old
RELEASE_READBACK_MAX_AGE_S = 60.0
# a command a rule sends runs on its device's own writer, never on the rules
# thread: a device that does not answer (retries, seconds) used to hold every
# other rule — safety ones too — until it gave up. A tick still waits up to
# this long for the commands it sent to devices that answered last time (a
# command settles ~1 s, then reads back), so a healthy device is written —
# and its result known — in the same tick, as before; one that is failing is
# not waited for.
COMMAND_WAIT_S = 5.0
STALL_TICKS = 10                  # the watchdog: no tick for this many periods (≥ 30 s) → alert


# Device ids carry hyphens (`pv-u1`), which the expression grammar would read
# as a subtraction. A reference `pv-u1.voltage_l1_n` is rewritten before
# parsing and mapped back when resolved — the operator writes ids as they are.
# One implementation for rules and calculated fields (expressions.py), which
# knows the gateway's device ids.
def _prepare(expr: str) -> str:
    return expressions.prepare_refs(expr)


def _unprepare(name: str) -> str:
    return expressions.unprepare_ref(name)


def _wrap_resolver(resolve):
    def wrapped(name):
        return resolve(_unprepare(name))
    return wrapped


def _validate_expr(expr: str) -> Optional[str]:
    ok, err, _refs = expressions.validate_expression(_prepare(expr))
    if not ok:
        return err
    if not all('.' in r for r in _refs):
        # an id the gateway does not have, written with a hyphen, parses as a
        # subtraction: name THAT, not the symptom (the message only — this
        # branch refused the rule before and still does)
        dashed = expressions.dashed_unknown(expr)
        if dashed:
            return expressions.dashed_unknown_text(dashed[0])
        return "every reference must name its device (device.register)"
    return None


# the expressions a rule reads its inputs through
_INPUT_KEYS = ('signal', 'when', 'release')


class RulesFileUnreadable(Exception):
    """rules.yaml cannot be read: nothing may be written over it."""


class RuleStore:
    """rules.yaml: a list under `rules:`. Written whole, atomically."""

    def __init__(self, path: Path):
        self.path = Path(path)
        # why the last load returned [] although the file EXISTS — the runtime
        # raises an alert on it: every armed protection rule silently
        # vanishing behind one unreadable file is an operator emergency, not
        # a log line (audit 2026-10-01)
        self.load_error: Optional[str] = None
        # entries of the last load that are not a rule (a hand edit gone
        # wrong): kept so a save writes them back as they were — an operator's
        # edit is fixed by the operator, never erased by the next UI save
        self.skipped: List[Any] = []
        self.missing = False

    def load(self) -> List[Dict]:
        self.load_error = None
        self.skipped = []
        self.missing = False
        try:
            text = self.path.read_text(encoding='utf-8')
        except FileNotFoundError:
            self.missing = True
            return []
        except Exception as e:  # noqa: BLE001
            logger.error("rules.yaml unreadable: %s", e)
            self.load_error = str(e)
            return []
        if not text.strip():
            # an editor that truncates before it writes, a copy gone wrong:
            # "no rules" is said by the API as `rules: []`, never by nothing
            self.load_error = "rules.yaml is empty"
            return []
        try:
            data = yaml.safe_load(text) or {}
        except Exception as e:  # noqa: BLE001
            logger.error("rules.yaml unreadable: %s", e)
            self.load_error = str(e)
            return []
        rules = data.get('rules') if isinstance(data, dict) else data
        out = [r for r in (rules or []) if isinstance(r, dict)]
        if isinstance(rules, list):
            self.skipped = [r for r in rules if not isinstance(r, dict)]
        # `rules: []` is a gateway with no rules (the last one deleted); what
        # is not a list, or a list of no rules, is a file gone wrong
        if not out and (rules or (data and not isinstance(rules, list))):
            self.load_error = "rules.yaml exists but holds no rule entries"
        return out

    def rows_to_rewrite(self, holding: bool = False) -> List[Dict]:
        """The file's rules, to rewrite with a change — refused while the file
        cannot be read (or is gone while the gateway holds rules): writing
        over it would erase the operator's file and every other rule with it."""
        rows = self.load()
        if self.load_error:
            raise RulesFileUnreadable(self.load_error)
        if self.missing and holding:
            raise RulesFileUnreadable("rules.yaml is missing")
        return rows

    def save(self, rules: List[Dict]) -> None:
        # temp + fsync + replace: a power loss mid-save must leave either the
        # old file or the new one, never a torn/empty rules.yaml whose next
        # load would silently disable every armed rule
        tmp = self.path.with_suffix('.yaml.tmp')
        payload = yaml.safe_dump({'rules': list(rules) + list(self.skipped)},
                                 sort_keys=False, allow_unicode=True)
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self.path)


class RulesRuntime:
    # every live runtime, for test teardown: a leaked background thread from
    # one test used to sweep ITS units while a later test ran (the 9162777
    # flake fixed one victim; conftest stops the source for all of them)
    _instances = weakref.WeakSet()

    def __init__(self, *, config_dir: Path, resolver_factory: Callable, find_device: Callable,
                 endpoint_devices: Callable, commands_for: Callable, run_command: Callable,
                 poll_now: Callable, event_log=None, alert_mgr=None, mqtt=None, audit_log=None,
                 influx=None,
                 gates: Callable[[], bool] = lambda: True, clock: Callable[[], float] = time.time,
                 mqtt_refused: Callable[[], Optional[str]] = lambda: None,
                 mono: Callable[[], float] = time.monotonic,
                 fields_of: input_check.FieldsOf = lambda _id: None):
        self.store = RuleStore(Path(config_dir) / 'rules.yaml')
        self.mqtt_refused = mqtt_refused      # why a control message over MQTT must be ignored, or None
        self._state_path = Path(config_dir) / 'rules_state.json'
        self._resolver_factory = resolver_factory      # store -> resolve(name)
        self._find_device = find_device                # id -> (idx, cfg, client)
        self._endpoint_devices = endpoint_devices      # endpoint id -> [cfg]
        self._commands_for = commands_for              # cfg -> [{'def': CommandDef, 'enabled': bool}]
        self._run_command = run_command                # (cfg, client, name, params, who, ip, via) -> (code, res)
        self._poll_now = poll_now                      # (cfg, client, group) -> None
        self.event_log, self.alert_mgr, self.mqtt, self.audit_log = event_log, alert_mgr, mqtt, audit_log
        self.influx = influx                           # the decisions' history: rule_event in the unit's bucket
        self._gates = gates                            # writes allowed at all?
        self._clock = clock
        self._mono = mono                              # ages come from the monotonic stamps in the store
        self._fields_of = fields_of                    # id -> the names it reads (input_check); never under the lock
        self.rules: Dict[str, RuleDef] = {}
        self.raw: Dict[str, Dict] = {}
        self.states: Dict[Tuple[str, str], RuleState] = {}
        self.decisions: Dict[str, deque] = {}
        self._last_action: Dict[Tuple[str, str], str] = {}
        self._last_pub: Dict[str, str] = {}
        self._last_sweep: Dict[str, float] = {}
        self._alerted: Dict[Tuple[str, str], bool] = {}
        self.errors: Dict[str, str] = {}               # rule id -> why it cannot run (bad binding)
        self.tick_errors: Dict[str, str] = {}          # rule id -> why its last evaluation failed
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_tick: Optional[float] = None
        self._saved_at: float = 0.0
        self.channels: Dict[Tuple[str, str], ChannelState] = {}   # (device, channel) → on/off, starts
        self._chan_cache: Dict[Tuple[str, str], Optional[Tuple[str, Dict, Any]]] = {}
        self._refs: Dict[str, bool] = {}               # expression -> reads any live value?
        RulesRuntime._instances.add(self)
        # rule id → the units its last disarm / delete is returning to safe
        # values in the background (the API says so; the event log says how)
        self.released: Dict[str, List[str]] = {}
        expressions.set_rule_stats_provider(self._rule_stat)

    # ── loading and binding ──────────────────────────────────────────────────

    def load(self) -> None:
        with self._lock:
            raws = self.store.load()
            if self.store.load_error:
                # every armed protection rule silently vanishing behind one
                # unreadable file is an operator emergency, not a log line
                self._alert('error', 'rules:store',
                            f'rules.yaml could not be loaded — ALL rules are '
                            f'inactive until it is repaired: {self.store.load_error}')
            persisted = self._load_state()
            self.__dict__.setdefault('_chan_cache', {}).clear()
            saved = persisted.get('_channels') or {}
            if saved and not self.__dict__.get('channels'):
                # aged by the time the gateway was down
                gone = max(0.0, time.time() - float(saved.get('saved_at') or time.time()))
                now = self._mono()
                chans = self.__dict__.setdefault('channels', {})
                for key, d in (saved.get('channels') or {}).items():
                    dev, _, cid = key.partition('\t')
                    if isinstance(d, dict) and dev and cid:
                        aged = {k: (None if d.get(k) is None else float(d[k]) + gone)
                                for k in ('on_since', 'off_since', 'last_write')}
                        chans[(dev, cid)] = ChannelState.from_mirror(
                            {**d, **aged, 'starts': [float(a) + gone for a in d.get('starts') or []]}, now)
            driving = {rid for rid, r in self.rules.items()
                       if r.mode == 'armed' and r.enabled and rid not in self.errors}
            self.rules, self.raw, self.errors = {}, {}, {}
            keep = {}
            for raw in list(raws) + list(getattr(self.store, 'skipped', None) or []):
                # one rule that cannot be read (a hand edit: `target: true`, a
                # list where a mapping goes) is that rule's error — never a
                # load that stops every rule, the armed protection ones too
                if not isinstance(raw, dict):
                    logger.error("rules.yaml: an entry that is not a rule was skipped: %r", raw)
                    self._alert('error', 'rules:entry', 'rules.yaml holds an entry that is not a rule; it was skipped')
                    continue
                try:
                    rid = str(raw.get('id', '') or '')
                    if rid in self.rules:
                        # hand-edited rules.yaml with a duplicate id: the API
                        # upsert refuses these, so say which copy wins here
                        logger.warning("rules.yaml: duplicate rule id %r — "
                                       "the LAST definition wins", rid)
                    errs = validate_rule_def(raw, validate_expr=_validate_expr)
                    rule = parse_rule_def(raw)
                    self.raw[rid] = raw
                    self.rules[rid] = rule
                    bind_err = self._bind(rule) if not errs else None
                    if errs or bind_err:
                        self.errors[rid] = '; '.join(errs) if errs else bind_err
                        if errs:
                            # its signal is broken, perhaps not its target: what its
                            # release needs (the command's safe values) comes from the
                            # command its target offers now, or the rule has none
                            self._bind(rule)
                    for cfg, _client in self._targets(rule):
                        key = (rid, cfg.id)
                        st = self.states.get(key) or RuleState(rule)
                        st.rule = rule
                        p = persisted.get(f"{rid}:{cfg.id}") or {}
                        if p.get('clamp') and st.clamp is None:
                            st.clamp = dict(p['clamp'])
                        if p.get('paused_until') and st.paused_until is None:
                            st.paused_until = float(p['paused_until'])
                        if p.get('forced') and st.forced is None and float(p['forced'].get('until') or 0) > self._clock():
                            st.forced = {'params': dict(p['forced'].get('params') or {}), 'until': float(p['forced']['until'])}
                        if p.get('counters') and not st.on_total_s and not st.on_today_s:
                            st.restore_counters(p['counters'], clock.day_key(self._clock()))
                        keep[key] = st
                    self.decisions.setdefault(rid, deque(maxlen=200))

                except Exception as e:  # noqa: BLE001 — this rule only
                    rid = str(raw.get('id', '') or '')
                    logger.exception("rule %r could not be loaded", rid)
                    # a stand-in keeps it listed and deletable (and so fixable):
                    # off and in shadow, never bound, never evaluated
                    label = raw.get('label')
                    self.rules[rid] = parse_rule_def({'id': rid, 'mode': 'shadow', 'enabled': False,
                                                      'label': label if isinstance(label, str) else rid})
                    self.raw[rid] = raw
                    self.errors[rid] = f'could not be loaded: {e}'
                    self._alert('error', f'rules:{rid}:load',
                                f"rule '{rid}' could not be loaded and is inactive: {e}")
            self.states = keep
            # a reload (an import, a restore, a hand edit) that stops a rule
            # driving takes back its unsent writes too — the API paths do it
            # in _release (§7)
            for rid in driving:
                r = self.rules.get(rid)
                if r is None or r.mode != 'armed' or not r.enabled or rid in self.errors:
                    self._cancel_queued(rid)
            # an armed rule gone from the file as read now (a hand edit, a file
            # replaced or half-written, a rule mis-indented) is NOT released: a
            # read can be wrong in many ways, and lifting an OV limit on a wrong
            # read is the dangerous direction. Its target holds the last value;
            # the operator is told, and releases it from the device if meant
            # (a delete from the API releases, as always) — RL-LOAD-R4.
            vanished = sorted(driving - set(self.rules))
            if vanished:
                why = (self.store.load_error
                       or ('file missing' if self.store.missing
                           else 'not in the file as read now'))
                self._alert('error', 'rules:vanished',
                            f"armed rule(s) {', '.join(vanished)} stopped ({why}) — nothing was released: "
                            f"their targets hold their last values. Repair rules.yaml, or send the safe "
                            f"value from the device page if the rule was meant to go")
            self._sync_mqtt()

    def _bind(self, rule: RuleDef) -> Optional[str]:
        """Take the command's value parameter, safe values and verify tolerance
        from what the first target unit offers."""
        targets = self._targets(rule)
        if not targets:
            return "target: no such device or group"
        cfg, _client = targets[0]
        cname = rule.target.get('command')
        cm = next((c for c in self._commands_for(cfg) if c['def'].name == cname), None)
        if cm is None:
            return f"target: '{cfg.id}' offers no command '{cname}'"
        if not cm['enabled']:
            return f"target: command '{cname}' is not enabled on '{cfg.id}'"
        cd = cm['def']
        if cd.alias:
            return f"target: '{cname}' is an alias; point the rule at '{cd.alias.get('command')}'"
        if rule.param not in cd.params:
            return f"target.param: '{rule.param}' is not a parameter of '{cname}'"
        rule.safe_params = dict(cd.safe or {})
        tol = 1.0
        for v in cd.verify or []:
            if v.get('read') and v.get('tolerance') is not None:
                tol = float(v['tolerance'])
                break
        rule.tolerance = tol
        rb = next((v['read'] for v in (cd.verify or []) if v.get('read')), None) \
            or (cd.writes[0]['register'] if cd.writes else None)
        rule.target['_readback'] = rb
        rule.target['_readback_group'] = cd.readback_group
        return None

    def _targets(self, rule: RuleDef) -> List[Tuple[Any, Any]]:
        t = rule.target or {}
        out = []
        if t.get('device'):
            _i, cfg, client = self._find_device(t['device'])
            if cfg is not None:
                out.append((cfg, client))
        elif t.get('endpoint') and t.get('group'):
            for cfg in self._endpoint_devices(t['endpoint']):
                if (getattr(cfg, 'group_id', '') or 'units') == t['group']:
                    _i, _c, client = self._find_device(cfg.id)
                    out.append((cfg, client))
        return out

    def _load_state(self) -> Dict:
        try:
            return json.loads(self._state_path.read_text(encoding='utf-8')) or {}
        except FileNotFoundError:
            return {}
        except Exception as e:  # noqa: BLE001
            # operator clamps (setpoint ceilings!) and overrides silently
            # vanishing is worth more than a debug line
            logger.error("rules_state.json unreadable — clamps/overrides lost: %s", e)
            self._alert('warn', 'rules:state',
                        f'rules_state.json unreadable — operator clamps and '
                        f'overrides were lost ({e})')
            return {}

    def _save_state(self) -> None:
        data = {}
        for (rid, dev), st in self.states.items():
            rec = {}
            if st.clamp or st.paused_until:
                rec = {'clamp': st.clamp, 'paused_until': st.paused_until}
            if st.forced:
                rec['forced'] = dict(st.forced)
            if st.saved_counters():
                rec['counters'] = st.saved_counters()
            if rec:
                data[f"{rid}:{dev}"] = rec
        errors = self.__dict__.get('errors') or {}
        store = self.__dict__.get('store')
        unreadable = bool(store is not None and (store.load_error or store.missing))
        if errors or unreadable:
            # a rule in error has no state here (it is not evaluated): what was
            # saved for it — an operator clamp, a pause, its counters — waits
            # for it to be repaired instead of being dropped (RL-LOAD-R4)
            try:
                old = self._load_state()
            except Exception:  # noqa: BLE001
                old = {}
            for k, v in old.items():
                # only for a unit with no live state: one that has a state
                # said what it holds now (a force ended, a clamp cleared)
                if k != '_channels' and k not in data and tuple(k.split(':', 1)) not in self.states \
                        and (unreadable or k.split(':', 1)[0] in errors):
                    data[k] = v
        # the channels' protection (a motor's last start, its starts of the
        # hour) outlives a restart too: ages, plus when they were taken
        chans = self.__dict__.get('channels') or {}
        if chans:
            now = self._mono()
            data['_channels'] = {'saved_at': time.time(),
                                 'channels': {f"{d}\t{c}": st.export_mirror(now) for (d, c), st in chans.items()}}
        try:
            # temp + fsync + replace — a crash mid-write used to leave a torn
            # file whose next load silently dropped every clamp/override
            tmp = self._state_path.with_suffix('.json.tmp')
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(json.dumps(data))
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(self._state_path)
        except Exception as e:  # noqa: BLE001
            logger.warning("rules_state.json: %s", e)

    # ── the thread ───────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='rules', daemon=True)
        self._thread.start()
        self._watchdog = threading.Thread(target=self._watch, name='rules-watchdog', daemon=True)
        self._watchdog.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)
            self._thread = None
        for w in self.__dict__.get('_writers', {}).values():
            w.shutdown(wait=False)

    def _watch(self) -> None:
        """The rules' own failsafe: a rules thread that stopped ticking (stuck
        or dead) is said, as an alert, instead of silently deciding nothing."""
        stalled = False
        while not self._stop.wait(10.0):
            try:
                self.check_stall()
                stalled = False
            except Exception:  # noqa: BLE001
                if not stalled:
                    logger.exception("rules watchdog")
                stalled = True

    def check_stall(self, now: Optional[float] = None) -> Optional[str]:
        """Alert once when no tick ran for STALL_TICKS periods (at least
        30 s) while some rule is enabled; say when it is back."""
        now = self._mono() if now is None else now
        last = self.__dict__.get('_last_tick_mono')
        live = any(r.enabled for r in self.rules.values())
        limit = max(30.0, STALL_TICKS * self._period())
        stalled = live and last is not None and now - last > limit
        was = self.__dict__.get('_stall_alerted', False)
        if stalled and not was:
            self._stall_alerted = True
            why = f"the rules engine has not run for {int(now - last)} s — no rule is deciding"
            logger.error(why)
            self._alert('error', 'rules:stalled', why)
            return why
        if not stalled and was:
            self._stall_alerted = False
            self._alert('info', 'rules:stalled', 'the rules engine runs again')
        return None

    def _period(self) -> float:
        ev = [r.timing.every_s for r in self.rules.values() if r.enabled and r.id not in self.errors]
        return max(0.5, min(ev)) if ev else 2.0

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not gateway_role.wire_allowed():
                # a standby gateway runs no rule: the active one does. Its
                # state stays as it was, and the stall watchdog knows the
                # pause is on purpose.
                self.__dict__['_last_tick_mono'] = self._mono()
                self._stop.wait(self._period())
                continue
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 — one bad tick must not stop the controller
                logger.exception("rules tick: %s", e)
            self._stop.wait(self._period())

    # ── one tick ─────────────────────────────────────────────────────────────

    def _signal(self, rule: RuleDef, now: float) -> Tuple[Any, Optional[float], Optional[float]]:
        """Returns (value, age_s, sample_mono). ``sample_mono`` is the OLDEST
        input's raw monotonic stamp — the stable identity of this reading.
        It must travel to RuleState.evaluate unchanged: reconstructing it as
        now-age jitters by the walk time between two clock reads, so every
        tick looked like a new sample — the debounce counted ticks, and the
        max_step plausibility guard confirmed a spike against ITSELF on the
        next tick (the 2026-09-18 273 V incident class; audit 2026-10-01)."""
        if rule.kind == 'schedule' and not rule.when.strip():
            return True, 0.0, self._mono()            # the clock alone: always current
        expr = rule.signal if rule.kind in ('steps', 'trim') else rule.when
        exprs = [expr] + ([rule.release] if rule.kind == 'condition' and rule.release else [])
        timeless = not any(self._reads_values(e) for e in exprs)
        resolve = self._resolver_factory({})
        # fresh(), count_fresh() and age() answer from the same resolver;
        # first() keeps its rules behaviour (no staleness passed to it)
        _fresh = ((lambda n: resolve.is_stale(_unprepare(n))) if hasattr(resolve, 'is_stale') else None)
        _age = ((lambda n: resolve.age_of(_unprepare(n))) if hasattr(resolve, 'age_of') else None)
        # what ema() and hysteresis() remember, per rule and per formula
        memos = self.__dict__.setdefault('_expr_memo', {})
        try:
            tree = expressions.compile_expression(_prepare(expr))
            val = expressions.evaluate_tree(tree, _wrap_resolver(resolve), fresh=_fresh, age=_age,
                                            memo=memos.setdefault((rule.id, expr), {}))
            # hysteresis: the release is read with the same resolver, so its
            # inputs count in the signal's age (the oldest input decides)
            rel = None
            if rule.kind == 'condition' and rule.release:
                rel = expressions.evaluate_tree(expressions.compile_expression(_prepare(rule.release)),
                                                _wrap_resolver(resolve), fresh=_fresh, age=_age,
                                                memo=memos.setdefault((rule.id, rule.release), {}))
        except expressions.MissingValue:
            return None, None, None
        except expressions.ExpressionError as e:
            logger.debug("rule %s: %s", rule.id, e)
            return None, None, None
        # the store stamps every value with a monotonic time; a value without
        # one has no known age and is treated as stale (fail closed)
        mono = [m for m in (getattr(resolve, 'touched_mono', None) or []) if m is not None]
        smono = min(mono) if mono else None
        age = (self._mono() - smono) if smono is not None else None
        if timeless:
            # only the clock and the rules' own counters: always current, and
            # every tick is a new sample
            smono = self._mono()
            age = 0.0
        if rule.kind == 'condition':
            return ((bool(val), bool(rel)) if rule.release else bool(val)), age, smono
        if rule.kind == 'schedule':
            return bool(val), age, smono
        return val, age, smono

    # ── channels and the referee (rules phase 3) ───────────────────────────

    def _chan(self, cfg, command: str) -> Optional[Tuple[str, Dict, Any]]:
        """(channel id, the template's channel settings, the command's
        definition — an alias resolved) for a command of a device."""
        cache = self.__dict__.setdefault('_chan_cache', {})
        key = (cfg.id, command)
        if key not in cache:
            cmds = self._commands_for(cfg) or []
            defs = {c['def'].name: c['def'] for c in cmds}
            if command not in defs:
                cache[key] = None
            else:
                cid = assign_channels(defs)[command]
                ccfg = dict(((cmds[0].get('channels') or {}) if cmds else {}).get(cid) or {})
                d = defs[command]
                if d.alias and d.alias.get('command') in defs:
                    d = defs[d.alias['command']]
                cache[key] = (cid, ccfg, d)
        return cache[key]

    # ── to a partner gateway (high availability) ──
    def export_mirror(self) -> Dict[str, Any]:
        """Every rule's state on every unit and every channel's protection, for
        a standby partner to hold: what it needs to carry on, not restart."""
        now = self._mono()
        with self._lock:
            states = {f"{rid}\t{dev}": st.export_mirror(now) for (rid, dev), st in self.states.items()}
            chans = {f"{dev}\t{cid}": c.export_mirror(now)
                     for (dev, cid), c in (self.__dict__.get('channels') or {}).items()}
        return {'states': states, 'channels': chans}

    def import_mirror(self, data: Dict[str, Any]) -> Dict[str, int]:
        """Take the partner's states and channels. Rules or units this gateway
        does not have are left out (counted)."""
        now = self._mono()
        took = left = 0
        with self._lock:
            for key, d in (data.get('states') or {}).items():
                rid, _, dev = key.partition('\t')
                rule = self.rules.get(rid) if isinstance(self.rules, dict) else None
                if rule is None or not isinstance(d, dict):
                    left += 1
                    continue
                st = self.states.get((rid, dev)) or RuleState(rule)
                st.import_mirror(d, now)
                self.states[(rid, dev)] = st
                took += 1
            chans = self.__dict__.setdefault('channels', {})
            for key, d in (data.get('channels') or {}).items():
                dev, _, cid = key.partition('\t')
                if isinstance(d, dict) and dev and cid:
                    chans[(dev, cid)] = ChannelState.from_mirror(d, now)
        return {'states': took, 'states_left_out': left, 'channels': len(data.get('channels') or {})}

    def _chan_state(self, dev: str, cid: str) -> ChannelState:
        chans = self.__dict__.setdefault('channels', {})
        return chans.setdefault((dev, cid), ChannelState())

    def _eff_prio(self, rule: RuleDef, st: Optional[RuleState]) -> Tuple[int, bool]:
        forced = bool(st is not None and st.forced and st.forced.get('until', 0) > self._clock())
        return (max(rule.prio(), MANUAL) if forced else rule.prio()), forced

    def _candidates(self, dev: str, cid: str, exclude: str = '') -> List[Candidate]:
        """The armed rules with an opinion on this channel of this device."""
        out = []
        now = self._clock()
        for rid, rule in list(self.rules.items()):
            if rid == exclude or not rule.enabled or rule.mode != 'armed' or rid in self.errors:
                continue
            st = self.states.get((rid, dev))
            if st is None or st.want is None or (st.paused_until is not None and now < st.paused_until):
                continue
            ch = next((self._chan(c, rule.target.get('command')) for c, _ in self._targets(rule) if c.id == dev), None)
            if ch is None or ch[0] != cid:
                continue
            prio, forced = self._eff_prio(rule, st)
            from .rules import _num
            out.append(Candidate(rid, prio, _num(st.want.get(rule.param)), forced))
        return out

    def _gate(self, rule: RuleDef, cfg):
        """The referee's word for one rule on one unit, asked at the moment
        it would write: yield to a rule that wins the channel, or hold for
        the channel's own protection (minimum run / stop, starts per hour)."""
        ch = self._chan(cfg, rule.target.get('command'))
        if ch is None:
            return None
        cid, ccfg, cdef = ch
        combine = ccfg.get('combine', 'priority') if ccfg.get('combine') in COMBINE else 'priority'

        def gate(want, urgent):
            from .rules import _num
            st = self.states.get((rule.id, cfg.id))
            prio, forced = self._eff_prio(rule, st)
            me = Candidate(rule.id, prio, _num((want or {}).get(rule.param)), forced)
            others = self._candidates(cfg.id, cid, exclude=rule.id)
            for o in sorted(others, key=lambda c: (-c.prio, c.rule)):
                if beats(o, me, combine):
                    how = (f"{rule.param} {o.value:g} is {'lower' if combine == 'min' else 'higher'}"
                           if combine in ('min', 'max') and o.value is not None else
                           f"priority {o.prio}{' (forced)' if o.forced else ''} over {prio}")
                    return ('yield', f"yields to {o.rule} on {cid}: {how}", o.rule)
            if urgent:
                return None
            why = self._chan_state(cfg.id, cid).hold(ccfg, turns_on(cdef, dict(want or {})), self._mono())
            return ('hold', f'{cid}: {why}', '') if why else None
        return gate

    def channel_hold(self, cfg, command: str, params: Dict) -> Optional[str]:
        """For a command from outside (API, MQTT, HA): why the channel's
        protection holds it now, or None."""
        ch = self._chan(cfg, command)
        if ch is None:
            return None
        cid, ccfg, cdef = ch
        why = self._chan_state(cfg.id, cid).hold(ccfg, turns_on(cdef, dict(params or {})), self._mono())
        return f'{cid}: {why}' if why else None

    def channel_note(self, cfg, command: str, params: Dict) -> None:
        """A write went out on the channel — whoever sent it."""
        ch = self._chan(cfg, command)
        if ch is not None:
            self._chan_state(cfg.id, ch[0]).note(turns_on(ch[2], dict(params or {})), self._mono())

    def safety_holder(self, device_id: str, command: str) -> Optional[str]:
        """An armed safety rule (priority ≥ 90) with an opinion on this
        command's channel: an outside command may not pause it."""
        cfg = next((c for r in self.rules.values() for c, _ in self._targets(r) if c.id == device_id), None)
        ch = self._chan(cfg, command) if cfg is not None else None
        if ch is None:
            return None
        safe = [c for c in self._candidates(device_id, ch[0]) if c.prio >= LAYERS['safety']]
        return safe[0].rule if safe else None

    def pause_channel(self, device_id: str, command: str, seconds: float, *, who: str, via: str) -> None:
        """An outside command takes the channel for a while: every armed
        rule on it under the safety layer is paused."""
        cfg = next((c for r in self.rules.values() for c, _ in self._targets(r) if c.id == device_id), None)
        ch = self._chan(cfg, command) if cfg is not None else None
        for rid, rule in list(self.rules.items()):
            if rule.mode != 'armed' or not rule.enabled or rule.prio() >= LAYERS['safety']:
                continue
            mine = next((self._chan(c, rule.target.get('command')) for c, _ in self._targets(rule) if c.id == device_id), None)
            if mine is not None and ch is not None and mine[0] == ch[0]:
                self.override(rid, seconds, who=who, via=via)

    def channel_view(self, cfg) -> List[Dict[str, Any]]:
        """Every channel of a device: its commands, settings, the rules on it
        (winner first) and the motor's state."""
        cmds = self._commands_for(cfg) or []
        defs = {c['def'].name: c['def'] for c in cmds}
        ids = assign_channels(defs) if defs else {}
        settings = (cmds[0].get('channels') or {}) if cmds else {}
        now = self._mono()
        wall = self._clock()
        out = []
        for cid in sorted(set(ids.values())):
            ccfg = dict(settings.get(cid) or {})
            combine = ccfg.get('combine', 'priority')
            rules = []
            paused = 0.0
            for rid, rule in self.rules.items():
                for c, _ in self._targets(rule):
                    ch = self._chan(c, rule.target.get('command')) if c.id == cfg.id else None
                    if ch is None or ch[0] != cid:
                        continue
                    st = self.states.get((rid, cfg.id))
                    if (st is not None and st.paused_until is not None and rule.mode == 'armed'
                            and rule.enabled and rid not in self.errors):   # a rule in error drives nothing
                        paused = max(paused, st.paused_until - wall)
                    prio, forced = self._eff_prio(rule, st)
                    rules.append({'rule': rid, 'mode': rule.mode, 'enabled': rule.enabled, 'layer': rule.layer or 'schedule',
                                  'priority': prio, 'forced': forced, 'want': st.want if st else None,
                                  'yielding_to': st.yielding_to if st else None})
            cands = self._candidates(cfg.id, cid)
            from .arbiter import winner as _winner
            w = _winner(cands, combine)
            names = sorted(n for n, i in ids.items() if i == cid)
            # who an outside command meets on this channel: the rule driving
            # it (a paused one is not), an armed safety rule it may not pause,
            # and how long a pause still runs
            out.append({'channel': cid, 'label': ccfg.get('label', ''), 'combine': combine,
                        'commands': names, 'settings': ccfg,
                        'winner': w.rule if w else None, 'rules': sorted(rules, key=lambda x: -x['priority']),
                        'state': self._chan_state(cfg.id, cid).snapshot(now),
                        'owner': self.owner_of(cfg.id, names[0], now=wall) if names else None,
                        'safety_holder': self.safety_holder(cfg.id, names[0]) if names else None,
                        'paused_until_s': round(paused, 1) if paused > 0 else None,
                        'max_override_s': MAX_OVERRIDE_S})
        return out

    def _reads_values(self, expr: str) -> bool:
        """Does the expression read any live value (a device.field)? One
        that reads only the clock or the counters has no age to go stale."""
        seen = self.__dict__.setdefault('_refs', {})
        if expr not in seen:
            ok, _err, refs = expressions.validate_expression(expr)
            seen[expr] = bool(refs) or not ok
        return seen[expr]

    def _rule_stat(self, rid: str, stat: str) -> Optional[float]:
        """run_today_h("id") & co.: hours on are the longest of the rule's
        units, starts are summed; None for a rule that does not exist."""
        units = [st for (r, _d), st in list(self.states.items()) if r == rid]
        if not units:
            return None
        if stat == 'run_today_h':
            return round(max(u.on_today_s for u in units) / 3600.0, 4)
        if stat == 'run_total_h':
            return round(max(u.on_total_s for u in units) / 3600.0, 4)
        if stat == 'starts_today':
            return sum(u.starts_today for u in units)
        if stat == 'starts_last_h':
            now = self._mono()
            return sum(u.starts_last_h(now) for u in units)
        return None

    def _code_of(self, cfg, name: str, text: str) -> Any:
        """A state register reads back as its text ("On"); the rule asks for
        its code (1). Map the text back through the register's own states, so
        "the device already holds it" and drift work on such registers too.
        Unknown → the text unchanged (the state machine then never compares it)."""
        try:
            _i, _c, client = self._find_device(cfg.id)
            regs = list(getattr(client, 'registers', None) or [])
            # a device read several ways keeps its maps on its parts
            for part in getattr(client, 'parts', None) or []:
                drv = part[1] if isinstance(part, tuple) else part
                regs += list(getattr(drv, 'registers', None) or [])
        except Exception:  # noqa: BLE001
            regs = []
        reg = next((r for r in regs if getattr(r, 'name', None) == name), None)
        for code, label in ((getattr(reg, 'enum', None) or {}).items() if reg is not None else ()):
            if str(label) == text:
                try:
                    return float(code)
                except (TypeError, ValueError):
                    return text
        return text

    def _actual(self, rule: RuleDef, cfg) -> Tuple[Any, Optional[float], Optional[float]]:
        rb = rule.target.get('_readback')
        if not rb:
            return None, None, None
        resolve = self._resolver_factory({})
        try:
            val = resolve(f"{cfg.id}.{rb}")
        except Exception:  # noqa: BLE001
            val = None
        if val is None:
            return None, None, None
        if isinstance(val, str):
            val = self._code_of(cfg, rb, val)
        mono = [m for m in (getattr(resolve, 'touched_mono', None) or []) if m is not None]
        iv = getattr(resolve, 'touched_interval', None) or []
        age = (self._mono() - max(mono)) if mono else None
        stale = 3.0 * float(iv[0]) if iv and iv[0] else 3.0 * 3600
        return val, age, stale

    def tick(self, now: Optional[float] = None) -> List[Dict]:
        now = self._clock() if now is None else now
        # The state machine's INTERVAL math (rate limit, reassert, staleness,
        # debounce identity) runs on the monotonic clock — a chrony step (the
        # 2026-07-28 +138 s jump) froze or prematurely expired those windows
        # on the wall clock, delaying the reassert that restores a Fronius
        # limit after RvrtTms (audit 2026-10-01). `now` stays the wall clock
        # for Decision timestamps and the wall-persisted paused_until/clamp.
        mono_now = self._mono()
        out = []
        with self._lock:
            rules = [r for r in self.rules.values() if r.enabled and r.id not in self.errors]
            idle = [r for r in self.rules.values() if not r.enabled or r.id in self.errors]
            # the generation each rule had when it was read: a rule that leaves
            # armed while this tick still evaluates it writes nothing (§7)
            self._tick_gens = dict(self.__dict__.get('_gen') or {})
        self.__dict__.setdefault('_chan_cache', {}).clear()      # commands may have changed
        self._settle()                                           # answers that came back since the last tick
        # the higher priority decides first: a lower rule then sees the
        # channel already taken in the same tick, never writes over it
        rules.sort(key=lambda r: -r.prio())
        for rule in rules:
            try:
                out.extend(self._tick_rule(rule, now, mono_now))
                self.tick_errors.pop(rule.id, None)
            except Exception as e:  # noqa: BLE001 — one rule's fault must not stop the others
                # visible where the operator looks (the card, an alert) — a rule
                # that silently stopped deciding must not look healthy
                why = f"evaluation failed: {type(e).__name__}: {e}"
                if self.tick_errors.get(rule.id) != why:
                    logger.exception("rule %s: %s", rule.id, why)
                    if self.alert_mgr is not None:
                        try:
                            self.alert_mgr.fire('error', f'rule-fail:{rule.id}', 'rules', f"rule '{rule.id}': {why}")
                        except Exception:  # noqa: BLE001
                            pass
                self.tick_errors[rule.id] = why
        # a rule that is not evaluated still says so on the broker — its
        # retained state must not keep the last one it had while it ran
        # (2026-10-20: disabled mid-curtail, it went on reading "lowering")
        for rule in idle:
            try:
                self._publish_state(rule)
            except Exception:  # noqa: BLE001
                pass
        self._wait_sent()
        self.last_tick = now
        self.__dict__['_last_tick_mono'] = self._mono()
        if now - getattr(self, '_saved_at', 0.0) >= 60:     # the counters survive a restart
            self._saved_at = now
            if any(st.saved_counters() for st in getattr(self, 'states', {}).values()) \
                    or self.__dict__.get('channels'):
                self._save_state()
        return out

    def _tick_rule(self, rule: RuleDef, now: float, mono_now: float) -> List[Dict]:
        out = []
        signal, age, s_mono = self._signal(rule, now)
        lead = None
        for cfg, client in self._targets(rule):
            key = (rule.id, cfg.id)
            st = self.states.get(key)
            if st is None:
                st = self.states[key] = RuleState(rule)
            # a trim on a group moves the units together: the first decides
            st.leader = lead if rule.kind == 'trim' else None
            if lead is None:
                lead = st
            actual, a_age, a_stale = self._actual(rule, cfg)
            st.gate = self._gate(rule, cfg)
            d = st.evaluate(mono_now, signal, age, actual=actual, actual_age_s=a_age, actual_stale_s=a_stale,
                            sample_ts=s_mono, wall=now)
            self._act(rule, cfg, client, st, d, now)
            out.append({'rule': rule.id, 'device': cfg.id, **d.to_dict()})
        self._publish_state(rule)
        return out

    def _act(self, rule: RuleDef, cfg, client, st: RuleState, d: Decision, now: float) -> None:
        key = (rule.id, cfg.id)
        notable = d.changed or d.action in ('run', 'reassert', 'shadow') \
            or d.action != self._last_action.get(key)
        self._last_action[key] = d.action
        rec = {'ts': round(now, 3), 'device': cfg.id, **{k: v for k, v in d.to_dict().items() if k != 'ts'}}
        if d.action in ('run', 'reassert'):
            if not self._gates():
                rec['result'] = 'rejected'
                rec['reason'] += ' — writes are disabled (security.allow_writes)'
                st.note_result(False)
            elif self._inflight_for(cfg.id):
                # the last command to this device has not answered yet: this
                # one waits for it (in order, never piled up) — sent next tick
                st.pending = True
                rec['result'] = 'queued'
                rec['reason'] += ' — the last command to this device is still running'
                if st.rearm_urgent(queued=True):      # urgent_retries (opt-in): not at the rate limit
                    rec['reason'] += ', sent again as urgent'
            else:
                self.__dict__.setdefault('_gen', {})
                gen = (self.__dict__.get('_tick_gens') or {}).get(rule.id, 0)
                fut = self._writer(cfg.id).submit(self._run_command, cfg, client, rule.target['command'],
                                                  dict(d.want or {}), who=f"rule {rule.id}", ip='-', via=f"rule:{rule.id}",
                                                  still_wanted=lambda r=rule.id, g=gen: self._gen.get(r, 0) == g)
                rec['result'] = 'sent'
                self.__dict__.setdefault('_inflight', {})[key] = (fut, rule, cfg, st, d, rec)
                if st.failures == 0:
                    self.__dict__.setdefault('_sent_now', []).append(fut)
            if rec.get('result') == 'rejected':
                self._check_failures(rule, cfg, st, rec)
        elif d.action == 'sweep':
            last = self._last_sweep.get(cfg.id, 0.0)
            _m = self._mono()                  # interval: step-immune
            if _m - last >= SWEEP_MIN_INTERVAL_S:
                self._last_sweep[cfg.id] = _m
                try:
                    self._poll_now(cfg, client, rule.target.get('_readback_group') or '')
                except Exception:  # noqa: BLE001
                    pass
        if d.changed:
            self._event(rule, cfg, d)
        if d.changed and d.action not in ('run', 'reassert'):
            self._influx_event(rule, cfg, d, rec)
        elif d.action in ('run', 'reassert') and rec.get('result') != 'sent':
            self._influx_event(rule, cfg, d, rec)     # a sent one is recorded with its result (_settle)
        if notable:
            self.decisions.setdefault(rule.id, deque(maxlen=200)).appendleft(rec)

    # ── commands off the rules thread ────────────────────────────────────────

    def wait_released(self, timeout: float = 30.0) -> bool:
        """Wait for the safe writes of disarmed / deleted rules (tests, and a
        clean stop that wants them out first). True when all are done."""
        pend = self.__dict__.get('_releasing') or []
        done, _ = _wait_futures(pend, timeout=timeout)
        self._releasing = [f for f in pend if f not in done]
        return not self._releasing

    def _writer(self, dev: str) -> ThreadPoolExecutor:
        """One writer per device: its commands stay in order, devices do not
        wait for one another, and the rules thread waits for none."""
        ws = self.__dict__.setdefault('_writers', {})
        w = ws.get(dev)
        if w is None:
            w = ws[dev] = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"rule-cmd-{dev}")
        return w

    def _inflight_for(self, dev: str) -> bool:
        return any(k[1] == dev and not v[0].done() for k, v in self.__dict__.get('_inflight', {}).items())

    def _settle(self) -> None:
        """Apply the answers of the commands that came back: the result in
        the decision log and InfluxDB, a failure counted (and alerted after
        three), a failed want sent again at the rate limit."""
        inflight = self.__dict__.get('_inflight', {})
        for key in [k for k, v in inflight.items() if v[0].done()]:
            fut, rule, cfg, st, d, rec = inflight.pop(key)
            if fut.cancelled():
                res = {'status': 'cancelled', 'reason': 'its rule left armed before it was sent — nothing written'}
            else:
                try:
                    _code, res = fut.result()
                except Exception as e:  # noqa: BLE001 — a crashed command is a failed one
                    res = {'status': 'error', 'reason': f"{type(e).__name__}: {e}"}
            if res.get('status') == 'cancelled':
                # neither a success nor a failure: the rule stopped driving
                # this target (§7); what it last "commanded" never landed
                rec['result'], rec['result_reason'] = 'cancelled', res.get('reason', '')
                # it never landed: if the rule drives again (re-armed within the
                # same tick, a reload that bound it back) its want is still to
                # send — never left to the reassert window. A rule that no
                # longer drives ignores both.
                st.confirmed, st.commanded = False, None
                st.pending = st.want is not None
                self._influx_event(rule, cfg, d, rec)
                continue
            ok = res.get('status') == 'success'
            rec['result'] = res.get('status')
            if res.get('reason'):
                rec['result_reason'] = res['reason']
            st.note_result(ok)
            # the answer confirms the want it carried — not a newer one the
            # rule decided meanwhile and has not sent yet (REL-BOOT)
            st.confirmed = ok and d.want == st.commanded
            if not ok:
                st.pending = True                 # try again at the rate limit
                if st.rearm_urgent():             # urgent_retries (opt-in): sooner, the same want
                    rec['retry'] = 'urgent'
                logger.warning("rule %s on %s: %s %s", rule.id, cfg.id, res.get('status'), res.get('reason', ''))
            self._check_failures(rule, cfg, st, rec)
            self._influx_event(rule, cfg, d, rec)

    def _check_failures(self, rule: RuleDef, cfg, st: RuleState, rec: Dict) -> None:
        key = (rule.id, cfg.id)
        if st.failures >= FAILURES_BEFORE_ALERT and not self._alerted.get(key):
            self._alerted[key] = True
            self._alert('error', f"rule:{rule.id}:{cfg.id}",
                        f"rule '{rule.label or rule.id}' cannot apply {rule.target['command']} on {cfg.id}: "
                        f"{st.failures} failures in a row ({rec.get('result_reason') or rec.get('result')})")
        elif st.failures == 0:
            self._alerted[key] = False

    def _wait_sent(self) -> None:
        """Give this tick's commands a moment: a device that answers is
        written — and its result known — within the tick, as before."""
        pending = [f for f in self.__dict__.pop('_sent_now', []) if not f.done()]
        if pending:
            _wait_futures(pending, timeout=COMMAND_WAIT_S)
        self._settle()

    # ── outward ──────────────────────────────────────────────────────────────

    def _event(self, rule: RuleDef, cfg, d: Decision) -> None:
        msg = f"{rule.label or rule.id} · {cfg.id}: {d.state} — {d.reason}"
        if self.event_log is not None:
            try:
                self.event_log.add('warn' if d.state == STALE else 'info', f"rule:{rule.id}", msg)
            except Exception:  # noqa: BLE001
                pass
        self._mqtt_pub(f"{MQTT_ROOT}/{rule.id}/event",
                       {'rule': rule.id, 'device': cfg.id, 'state': d.state, 'signal': d.signal,
                        'want': d.want, 'actual': d.actual, 'action': d.action, 'reason': d.reason,
                        'ts': round(d.ts, 3)}, retain=False)

    def _influx_event(self, rule: RuleDef, cfg, d: Decision, rec: Dict) -> None:
        """One ``rule_event`` point in the unit's bucket for every state/want
        change and every command the rule sends — the history the UI's OV
        panels and the alerts read (what Node-RED's ``ov_event`` used to be).
        Tags: device, rule, state, action, result. Fields: signal, want_value,
        actual, reason, result_reason."""
        if self.influx is None or not getattr(cfg, 'influxdb_enabled', True):
            return
        try:
            from influxdb_client import Point, WritePrecision
            p = (Point('rule_event').tag('device', getattr(cfg, 'influxdb_device_tag', '') or cfg.id)
                 .tag('rule', rule.id).tag('state', str(d.state)).tag('action', str(d.action))
                 .tag('result', str(rec.get('result') or ''))
                 .field('reason', str(d.reason or '')[:200]))
            if isinstance(d.signal, (int, float)) and not isinstance(d.signal, bool):
                p = p.field('signal', float(d.signal))
            wv = (d.want or {}).get(rule.param) if isinstance(d.want, dict) else None
            if isinstance(wv, (int, float)) and not isinstance(wv, bool):
                p = p.field('want_value', float(wv))
            if isinstance(d.actual, (int, float)) and not isinstance(d.actual, bool):
                p = p.field('actual', float(d.actual))
            if rec.get('result_reason'):
                p = p.field('result_reason', str(rec['result_reason'])[:200])
            p = p.time(int(d.ts * 1e9), WritePrecision.NS)
            self.influx.write_point(p, ts=d.ts, bucket=getattr(cfg, 'influxdb_bucket', None) or None)
        except Exception as e:  # noqa: BLE001 — history must never stall a decision
            logger.debug("rule %s: influx event not written: %s", rule.id, e)

    def _alert(self, severity: str, key: str, message: str) -> None:
        if self.alert_mgr is not None:
            try:
                self.alert_mgr.fire(severity, key, 'rules', message)
            except Exception:  # noqa: BLE001
                pass
        if self.event_log is not None:
            try:
                self.event_log.add('error', 'rules', message)
            except Exception:  # noqa: BLE001
                pass

    def _severity(self, rule: RuleDef, state: str) -> int:
        if state == STALE:
            return 99
        if state in (NORMAL, 'false', 'off', 'holding', 'at min'):
            return 0
        if state in ('true', 'raising', 'lowering', 'at max', 'resting', 'forced'):
            return 1
        if rule.kind == 'schedule':
            return 0
        for i, s in enumerate(rule.steps):
            if s.label == state:
                return i + 1
        return 0

    def live(self, rid: str) -> Dict:
        """The rule's live picture: worst unit, per-unit detail."""
        rule = self.rules[rid]
        units = {}
        worst, worst_sev = NORMAL, -1
        signal = None
        cfgs = {c.id: c for c, _ in self._targets(rule)}
        for (r, dev), st in self.states.items():
            if r != rid:
                continue
            key = (rid, dev)
            snap = st.snapshot()
            signal = snap['signal']
            actual, a_age, _s = self._actual(rule, cfgs[dev]) if dev in cfgs else (None, None, None)
            units[dev] = {'state': st.state, 'want': st.want, 'commanded': st.commanded, 'since': st.since,
                          'actual': actual, 'actual_age_s': None if a_age is None else round(a_age, 1),
                          'clamp': snap['clamp'], 'paused_until': st.paused_until, 'failures': st.failures,
                          'pending': st.pending, 'last_action': self._last_action.get(key),
                          'ignored': st.ignored, 'guarded': st.guarded, **st.counters(),
                          'forced': dict(st.forced) if st.forced else None, 'yielding_to': st.yielding_to}
            sev = self._severity(rule, st.state)
            if sev > worst_sev:
                worst, worst_sev = st.state, sev
        last = next(iter(self.decisions.get(rid, [])), None)
        return {'state': worst if units else 'unbound', 'signal': signal, 'units': units,
                'last': last, 'error': self.errors.get(rid) or self.tick_errors.get(rid), 'last_tick': self.last_tick}

    def _publish_state(self, rule: RuleDef) -> None:
        lv = self.live(rule.id)
        # not evaluated: what it last decided no longer holds, so neither the
        # rule nor its units keep a state or a want (consumers key alerts on it)
        idle = ('error' if rule.id in self.errors and str(self.errors[rule.id]).startswith('could not be loaded')
                else 'disabled' if not rule.enabled else ('error' if rule.id in self.errors else None))
        units = {d: {k: u.get(k) for k in ('state', 'want', 'clamp', 'paused_until', 'last_action', 'ignored',
                                           'guarded', 'run_today_min', 'run_total_h', 'starts_today', 'forced',
                                           'yielding_to')}
                 for d, u in lv['units'].items()}
        if idle:
            for u in units.values():
                u['state'], u['want'] = idle, None
        body = {'id': rule.id, 'label': rule.label or rule.id, 'mode': rule.mode, 'enabled': rule.enabled,
                'state': idle or lv['state'], 'signal': None if idle else lv['signal'],
                'units': units,
                'decision': (lv['last'] or {}).get('action'), 'reason': (lv['last'] or {}).get('reason'),
                'ts': round(self._clock(), 3)}
        if lv.get('error'):
            body['error'] = lv['error']
        sig = json.dumps({k: v for k, v in body.items() if k != 'ts'}, sort_keys=True, default=str)
        if self._last_pub.get(rule.id) == sig:
            return
        self._last_pub[rule.id] = sig
        self._mqtt_pub(f"{MQTT_ROOT}/{rule.id}/state", body, retain=True)

    def _mqtt_pub(self, topic: str, body: Dict, retain: bool) -> None:
        m = self.mqtt
        if not m or not getattr(m, 'connected', False):
            return
        try:
            m.client.publish(topic, json.dumps(body, default=str), qos=1, retain=retain)
        except Exception:  # noqa: BLE001
            pass

    def _sync_mqtt(self) -> None:
        m = self.mqtt
        if not m or not hasattr(m, 'register_command'):
            return
        m.unregister_commands(MQTT_ROOT + '/')
        for rid in self.rules:
            m.register_command(f"{MQTT_ROOT}/{rid}/set", lambda payload, r=rid: self._mqtt_set(r, payload))

    def _mqtt_set(self, rid: str, payload: str) -> None:
        """`enabled`, `clamp`, `override` over MQTT (with the broker gate);
        arming stays with the API and the UI."""
        try:
            body = json.loads(payload) if str(payload).strip() else {}
        except ValueError:
            return
        if not isinstance(body, dict):
            return
        # the same gate as the command faces: enabling, clamping or pausing a
        # rule from the broker is a control act, refused on an anonymous
        # broker session (3.80.2; the docs had claimed a gate that was not there)
        refused = self.mqtt_refused()
        if refused:
            logger.warning("rule %s: MQTT set ignored: %s", rid, refused)
            return
        who = str(body.get('source') or 'mqtt')
        if 'enabled' in body:
            # the broker may DISABLE a rule (a controller taking over) but
            # never enable one: enabling an armed rule is arming it, and
            # arming stays with the API and the UI (F-74, 3.83.0)
            en = body['enabled']
            if en is True:
                logger.warning("rule %s: enable over MQTT refused — arming stays with the API", rid)
            elif not (en is False or str(en).strip().lower() in ('false', '0', 'off', 'no')):
                # bool("false") is True: an unreadable value is neither a
                # disable nor an enable — say so, the controller is waiting
                logger.warning("rule %s: MQTT enabled must be true or false (got %r) — ignored", rid, en)
            else:
                errs = self.set_enabled(rid, False, who=who, via='mqtt')
                if errs:
                    # a controller taking over believes the rule stopped
                    logger.warning("rule %s: disable over MQTT refused — %s", rid, errs[0])
                    self._alert('warning', f'rule:{rid}:mqtt-disable',
                                f'rule {rid}: a disable over MQTT was refused — {errs[0]}')
        if 'clamp' in body:
            c = body['clamp']
            if c is None:
                self.clear_clamp(rid, who=who, via='mqtt')
            elif isinstance(c, dict) and c.get('max') is not None:
                errs = self.set_clamp(rid, c.get('max'), c.get('expires_s'), who=who, via='mqtt')
                if errs:
                    logger.warning("rule %s: clamp over MQTT rejected — %s", rid, errs[0])
        if 'force' in body:
            f = body['force']
            errs = (self.force(rid, None, 0, who=who, via='mqtt') if f is None else
                    self.force(rid, f.get('params') if isinstance(f.get('params'), dict) else None,
                               f.get('seconds'), value=f.get('value'), who=who, via='mqtt')
                    if isinstance(f, dict) else ["force: {value, seconds} or null"])
            if errs:
                logger.warning("rule %s: force over MQTT rejected — %s", rid, errs[0])
        if body.get('override_s') is not None:
            errs = self.override(rid, body['override_s'], who=who, via='mqtt')
            if errs:
                logger.warning("rule %s: override over MQTT rejected — %s", rid, errs[0])

    # ── operations (API, UI, MQTT) ───────────────────────────────────────────

    def _audit(self, who: str, via: str, action: str, target: str, detail: Dict) -> None:
        if self.audit_log is None:
            return
        try:
            self.audit_log.append(user=who, ip='-', action=action, target=target, status='ok',
                                  detail={**detail, 'via': via})
        except Exception:  # noqa: BLE001
            pass

    def input_problems(self, raw: Optional[Dict]) -> List[Dict[str, str]]:
        """What the rule's formulas read that this gateway does not have:
        ``[{in, ref, problem, hint?}]`` (input_check). Said, never enforced on
        a rule that runs — a rule reading a device that went away stays stale
        and follows its on_stale. Called outside the lock: the names may come
        from disk, and the armed rules' tick must not wait for them."""
        out: List[Dict[str, str]] = []
        if not isinstance(raw, dict):
            return out
        for key in _INPUT_KEYS:
            expr = str(raw.get(key) or '').strip()
            if expr:
                out += [{'in': key, **p} for p in input_check.problems(input_check.refs_of(expr), self._fields_of)]
        return out

    def _view(self, rid: str, r: RuleDef) -> Dict:
        return {**r.to_dict(), 'safe_params': dict(r.safe_params or {}), 'live': self.live(rid),
                'raw': self.raw.get(rid)}

    def list(self) -> List[Dict]:
        with self._lock:
            out = [self._view(rid, r) for rid, r in self.rules.items()]
        for d in out:
            d['inputs'] = self.input_problems(d['raw'])
        return out

    def get(self, rid: str) -> Optional[Dict]:
        with self._lock:
            r = self.rules.get(rid)
            d = None if r is None else self._view(rid, r)
        if d is not None:
            d['inputs'] = self.input_problems(d['raw'])
        return d

    def validate(self, raw: Dict, *, existing: Optional[str] = None) -> List[str]:
        errs = validate_rule_def(raw, validate_expr=_validate_expr)
        if errs:
            return errs
        rule = parse_rule_def(raw)
        inputs = self.input_problems(raw)          # before the lock (see input_problems)
        with self._lock:
            if existing is None and rule.id in self.rules:
                return [f"id: '{rule.id}' already exists"]
            if existing is not None and existing != rule.id:
                return ["id: cannot be changed"]
            e = self._bind(rule)
            if e:
                return [e]
            prev_raw = self.raw.get(existing) if existing is not None else None
            if existing is not None and existing in self.errors and existing in self.rules:
                # a rule that could not be loaded: its raw may not even parse,
                # and its stand-in (off, shadow) is what ran — a PUT repairs it
                try:
                    prev = parse_rule_def(prev_raw) if prev_raw is not None else None
                except Exception:  # noqa: BLE001
                    prev = self.rules[existing]
            else:
                prev = parse_rule_def(prev_raw) if prev_raw is not None else None
            changed = prev is None or self._shape_changed(prev, rule)
            # leaving armed in the same save as an edit (one PUT from the API)
            # is a disarm: never refused, or the rule would stay armed. The
            # release goes to the target it drove (upsert releases `prev`).
            leaving = (prev is not None and prev.mode == 'armed' and prev.enabled
                       and not (rule.mode == 'armed' and rule.enabled))
            if prev is not None and changed and prev.mode == 'armed' and prev.enabled and not leaving:
                # an armed rule's target is held: moving it would leave the old
                # target at its last value and restart every state
                for k, a, b in (('target', prev.target.get('device') or (prev.target.get('endpoint'), prev.target.get('group')),
                                 rule.target.get('device') or (rule.target.get('endpoint'), rule.target.get('group'))),
                                ('target.command', prev.target.get('command'), rule.target.get('command')),
                                ('kind', prev.kind, rule.kind)):
                    if a != b:
                        return [f"{k}: the rule is armed — put it in shadow before changing its {k.split('.')[-1]}"]
            # what it asks for must be something the command accepts — checked
            # when its content changes or it is armed, never on the way to
            # shadow or disabled (disarming is never refused)
            becoming_armed = rule.mode == 'armed' and rule.enabled and (
                prev is None or prev.mode != 'armed' or not prev.enabled)
            if (changed or becoming_armed) and not leaving:
                errs = self._want_errors(rule)
                if errs:
                    return errs
            tuning_changed = tuning_of(prev_raw) != tuning_of(raw)
            if (changed or tuning_changed or becoming_armed) and not leaving:
                errs = tuning_errors(raw)
                if errs:
                    return errs
            # a rule that drives its target cannot read a device that is not
            # here: it could only ever be stale. In shadow it is saved and the
            # reference is said (`inputs`) — a rule imported from another
            # installation is fixed after it arrives
            if (changed or becoming_armed) and rule.mode == 'armed' and rule.enabled:
                gone = [p for p in inputs if p['problem'] == 'no_device']
                if gone:
                    tail = '' if changed else ' — put it in shadow, fix the reference, then arm it'
                    return [f"{p['in']}: {input_check.text(p)}{tail}" for p in gone]
            # several armed rules may share a channel (phase 3) — the referee
            # decides by priority, or by the lowest / highest value; two at
            # the SAME priority on a priority channel could only fight
            if rule.mode != 'armed' or not rule.enabled:
                return []
            for c, _ in self._targets(rule):
                ch = self._chan(c, rule.target['command'])
                if ch is None:
                    continue
                combine = ch[1].get('combine', 'priority')
                for oid, other in self.rules.items():
                    if oid == rule.id or not other.enabled or other.mode != 'armed' or oid in self.errors:
                        continue
                    if not any(oc.id == c.id and (self._chan(oc, other.target.get('command')) or ('',))[0] == ch[0]
                               for oc, _ in self._targets(other)):
                        continue
                    if combine == 'priority' and other.prio() == rule.prio():
                        return [f"target: rule '{oid}' already drives {ch[0]} on {c.id} at priority {rule.prio()}; "
                                f"give one of them another layer or priority (the higher wins), or let the "
                                f"template's channel combine by min / max"]
        return []

    def _want_errors(self, rule: RuleDef) -> List[str]:
        """Every value the rule may ask for, as the command would receive it
        ({**params, **base}), checked by the command itself: a name it does
        not know, a text, a value out of its range is refused at save time
        instead of failing on every write once armed."""
        targets = self._targets(rule)
        if not targets:
            return []
        cname = rule.target.get('command')
        cm = next((c for c in self._commands_for(targets[0][0]) if c['def'].name == cname), None)
        if cm is None:
            return []
        p = rule.param
        bases: List[Tuple[str, Dict[str, Any]]] = []
        if rule.kind == 'steps':
            bases += [(f"steps[{i}]", {p: s.value}) for i, s in enumerate(rule.steps)]
            if rule.normal is not None:
                bases.append(('normal', dict(rule.normal)))
        elif rule.kind == 'condition':
            for name, x in (('then', rule.then), ('else', rule.otherwise)):
                if isinstance(x, dict):
                    bases.append((name, dict(x.get('params') or {})))
        elif rule.kind == 'schedule':
            for i, e in enumerate(rule.schedule):
                if isinstance(e.get('params'), dict):
                    bases.append((f"schedule[{i}]", dict(e['params'])))
                elif e.get('value') is not None:
                    bases.append((f"schedule[{i}]", {p: e['value']}))
        elif rule.kind == 'trim':
            for name, v in (('min', rule.vmin), ('max', rule.vmax), ('start', rule.start)):
                if v is not None:
                    bases.append((name, {p: v}))
        if rule.off is not None:
            bases.append(('off_value', {p: rule.off}))
        if isinstance(rule.on_stale, (int, float)) and not isinstance(rule.on_stale, bool):
            bases.append(('on_stale', {p: rule.on_stale}))
        errs = []
        for where, base in bases:
            try:
                normalize_params(cm['def'], {**rule.params, **base})
            except ValueError as e:
                errs.append(f"{where}: {e}")
        return errs

    def upsert(self, raw: Dict, *, who='?', via='api') -> Tuple[List[str], Optional[Dict]]:
        existing = str(raw.get('id', '') or '') if str(raw.get('id', '') or '') in self.rules else None
        errs = self.validate(raw, existing=existing)
        if errs:
            return errs, None
        rid = str(raw['id'])
        with self._lock:
            prev = self.rules.get(rid)
            if prev is not None and rid in self.raw and rid not in self.errors and \
                    parse_rule_def(raw).to_dict() == parse_rule_def(self.raw[rid]).to_dict() and \
                    tuning_of(raw) == tuning_of(self.raw[rid]):
                # an unchanged save: nothing to store, no state to reset (a rule
                # in error is re-bound instead — its cause may be fixed now)
                same = self._view(rid, prev)
            else:
                same = None
                try:
                    rows = self.store.rows_to_rewrite(holding=bool(self.rules))
                except RulesFileUnreadable as e:
                    return [f"rules.yaml cannot be read ({e}) — repair the file first; nothing was saved"], None
                # a rule that drove its target stops: release it once — a rule
                # already disabled released when it was disabled, and may since
                # have lost the channel to another rule it must not overwrite
                if prev is not None and prev.mode == 'armed' and prev.enabled \
                        and (raw.get('mode') != 'armed' or not raw.get('enabled', True)):
                    self.__dict__.setdefault('released', {})[rid] = self._release(prev, f"{'shadow' if raw.get('mode') != 'armed' else 'disabled'} by {who}")
                rules = [raw if str(r.get('id')) == rid else r for r in rows]
                if prev is None:
                    rules.append(raw)
                self.store.save(rules)
                if prev is None or self._shape_changed(prev, parse_rule_def(raw)):
                    # a new shape starts from scratch — the debounce rebuilds it
                    for key in [k for k in self.states if k[0] == rid]:
                        self.states.pop(key)
                elif raw.get('mode') == 'armed' and raw.get('enabled', True) and \
                        (prev.mode != 'armed' or not prev.enabled):
                    # it starts driving now (armed from shadow, or enabled again):
                    # what it "sent" before was never on the wire, or was undone by
                    # the release — it asks for its current want now (unless the
                    # device holds it)
                    for key, st in self.states.items():
                        if key[0] == rid:
                            st.commanded, st.last_cmd_ts, st.pending = None, None, st.want is not None
                            if prev.kind == 'trim':
                                # a trim's want is a position reached step by
                                # step: armed, it starts again from what the
                                # device holds (read back), never from where the
                                # shadow wandered to
                                st.want, st.pending, st._trim = None, False, None
                                st._trim_dir, st._trim_count, st._trim_since = 0, 0, None
                self.load()
        if same is not None:
            same['inputs'] = self.input_problems(same['raw'])
            return [], same
        self._audit(who, via, 'rule ' + ('updated' if prev else 'created'), rid, {'mode': raw.get('mode'), 'kind': raw.get('kind')})
        return [], self.get(rid)

    @staticmethod
    def _shape_changed(a: RuleDef, b: RuleDef) -> bool:
        da, db = a.to_dict(), b.to_dict()
        for k in ('mode', 'enabled', 'label') + TUNING_RULE_KEYS:
            da.pop(k, None); db.pop(k, None)
        # the opt-in tuning keys change how a want is sent, not what is
        # wanted: setting them keeps the live step (ADR-0012)
        for d in (da, db):
            for k in TUNING_TIMING_KEYS:
                (d.get('timing') or {}).pop(k, None)
        return da != db

    def delete(self, rid: str, *, who='?', via='api') -> bool:
        with self._lock:
            rule = self.rules.get(rid)
            if rule is None:
                return False
            try:
                rows = self.store.rows_to_rewrite(holding=bool(self.rules))
            except RulesFileUnreadable as e:
                raise ValueError(f"rules.yaml cannot be read ({e}) — repair the file first; nothing was saved") from e
            if rule.mode == 'armed' and rule.enabled:
                self.__dict__.setdefault('released', {})[rid] = self._release(rule, f"deleted by {who}")
            self.store.save([r for r in rows if str(r.get('id')) != rid])
            for key in [k for k in self.states if k[0] == rid]:
                self.states.pop(key)
            self.decisions.pop(rid, None)
            self.rules.pop(rid, None)           # released above: the reload must not take it as vanished
            self.load()
            self._mqtt_pub(f"{MQTT_ROOT}/{rid}/state", {}, retain=True)
        self._audit(who, via, 'rule deleted', rid, {})
        return True

    def set_mode(self, rid: str, mode: str, *, who='?', via='api') -> List[str]:
        if mode not in ('shadow', 'armed'):
            return ["mode: shadow or armed"]
        with self._lock:
            raw = self.raw.get(rid)
            if raw is None:
                return ["no such rule"]
            if self.errors.get(rid) and mode == 'armed':
                return [f"cannot arm: {self.errors[rid]}"]
            if str(raw.get('mode', 'shadow')) == mode:
                return []                              # already so: nothing to do, nothing to audit
        errs, _ = self.upsert({**raw, 'mode': mode}, who=who, via=via)
        if errs and mode == 'shadow':
            errs = self._leave_unchecked(rid, {'mode': 'shadow'}, errs[0], who=who)
        if not errs:
            self._audit(who, via, 'rule ' + ('armed' if mode == 'armed' else 'shadowed'), rid, {})
        return errs

    def set_enabled(self, rid: str, enabled: bool, *, who='?', via='api') -> List[str]:
        with self._lock:
            raw = self.raw.get(rid)
            if raw is None:
                return ["no such rule"]
            if bool(raw.get('enabled', True)) == bool(enabled):
                return []                              # already so: nothing to do, nothing to audit
        errs, _ = self.upsert({**raw, 'enabled': bool(enabled)}, who=who, via=via)
        if errs and not enabled:
            errs = self._leave_unchecked(rid, {'enabled': False}, errs[0], who=who)
        if not errs:
            self._audit(who, via, 'rule ' + ('enabled' if enabled else 'disabled'), rid, {})
        return errs

    def _leave_unchecked(self, rid: str, change: Dict[str, Any], why: str, *, who: str) -> List[str]:
        """To shadow, or disabled, for a rule its own checks refuse (a device it
        reads or drives is gone, its command was disabled, a hand-edited
        rules.yaml): leaving is never refused — a rule that cannot be stopped
        holds its target at the last value it sent. The change is stored as
        it is, without the checks, and an armed rule releases its target with
        the safe values it last ran with."""
        with self._lock:
            raw = self.raw.get(rid)
            if raw is None:
                return ["no such rule"]
            prev = self.rules.get(rid)
            if prev is not None and prev.mode == 'armed' and prev.enabled:
                self.__dict__.setdefault('released', {})[rid] = self._release(
                    prev, f"{'shadow' if change.get('mode') == 'shadow' else 'disabled'} by {who}")
            new = {**raw, **change}
            try:
                rows = self.store.rows_to_rewrite(holding=bool(self.rules))
            except RulesFileUnreadable as e:
                # leaving is never refused: the rule stops driving now, in
                # memory — the unreadable file is not written over, and says
                # what it says again once repaired
                import dataclasses
                if prev is not None:
                    left = dataclasses.replace(prev, **{k: v for k, v in change.items()
                                                       if k in ('mode', 'enabled')})
                    self.rules[rid] = left
                    for key, st in self.states.items():
                        if key[0] == rid:
                            st.rule = left          # the tick evaluates the states' rule
                self.raw[rid] = new
                self._cancel_queued(rid)
                if prev is not None:
                    self._publish_state(left)       # the card and MQTT say it now, not next tick
                self._alert('error', 'rules:store',
                            f"rules.yaml cannot be read ({e}): rule '{rid}' was "
                            f"{'put in shadow' if change.get('mode') == 'shadow' else 'disabled'} in memory "
                            f"only — repair the file, or the next reload follows what it says")
                return []
            self.store.save([new if str(r.get('id')) == rid else r for r in rows])
            self.load()
        logger.warning("rule %s: %s although its checks refuse it (%s)", rid,
                       'put in shadow' if change.get('mode') == 'shadow' else 'disabled', why)
        return []

    def set_clamp(self, rid: str, max_value: float, expires_s=None, *, who='?', via='api') -> List[str]:
        # a clamp is a setpoint ceiling: a finite, non-negative number, with a
        # finite expiry of at most a week (F-74, 3.83.0)
        try:
            max_value = float(max_value)
        except (TypeError, ValueError):
            return ["clamp max must be a number"]
        if not math.isfinite(max_value) or max_value < 0:
            return ["clamp max must be a finite, non-negative number"]
        if expires_s not in (None, 0, ''):
            try:
                expires_s = float(expires_s)
            except (TypeError, ValueError):
                return ["clamp expires_s must be a number"]
            if not math.isfinite(expires_s) or expires_s < 0 or expires_s > MAX_CLAMP_EXPIRES_S:
                return [f"clamp expires_s must be within 0..{int(MAX_CLAMP_EXPIRES_S)} seconds"]
        with self._lock:
            if rid not in self.rules:
                return ["no such rule"]
            exp = (self._clock() + float(expires_s)) if expires_s else None
            for key, st in self.states.items():
                if key[0] == rid:
                    st.set_clamp(max_value, exp)
            self._save_state()
        self._audit(who, via, 'rule clamp', rid, {'max': max_value, 'expires_s': expires_s})
        return []

    def clear_clamp(self, rid: str, *, who='?', via='api') -> List[str]:
        with self._lock:
            if rid not in self.rules:
                return ["no such rule"]
            for key, st in self.states.items():
                if key[0] == rid:
                    st.clear_clamp()
            self._save_state()
        self._audit(who, via, 'rule clamp cleared', rid, {})
        return []

    def override(self, rid: str, seconds: float, *, who='?', via='api') -> List[str]:
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            return ["override_s must be a number"]
        if seconds != seconds or seconds < 0 or seconds > MAX_OVERRIDE_S:
            return [f"override_s must be within 0..{int(MAX_OVERRIDE_S)} seconds"]
        with self._lock:
            if rid not in self.rules:
                return ["no such rule"]
            until = self._clock() + max(0.0, float(seconds))
            for key, st in self.states.items():
                if key[0] == rid:
                    st.paused_until = until if seconds > 0 else None
            self._save_state()
        self._audit(who, via, 'rule override', rid, {'seconds': seconds})
        return []

    def force(self, rid: str, params: Optional[Dict[str, Any]], seconds, *, value=None,
              who='?', via='api') -> List[str]:
        """Ask for ``params`` (or ``value`` on the rule's parameter) for
        ``seconds``, whatever the signal says, then go back to deciding; 0
        ends a force now. The clamp still caps it and the on/off timers still
        hold a start."""
        try:
            seconds = float(seconds or 0)
        except (TypeError, ValueError):
            return ["seconds: a number"]
        if not math.isfinite(seconds) or seconds < 0 or seconds > MAX_FORCE_S:
            return [f"seconds: 0..{int(MAX_FORCE_S)} (0 ends the force)"]
        with self._lock:
            rule = self.rules.get(rid)
            if rule is None:
                return ["no such rule"]
            if seconds > 0:
                if params is None:
                    try:
                        v = float(value)
                    except (TypeError, ValueError):
                        return ["value: a number (or params: {...})"]
                    if not math.isfinite(v):
                        return ["value: a finite number"]
                    params = {rule.param: v}
                bad = [k for k, v in params.items() if isinstance(v, bool) or not isinstance(v, (int, float))
                       or not math.isfinite(float(v))]
                if not params or bad:
                    return [f"params: numbers ({', '.join(bad) or 'empty'})"]
            until = (self._clock() + seconds) if seconds > 0 else None
            for key, st in self.states.items():
                if key[0] == rid:
                    st.force(params or {}, until)
            self._save_state()
        self._audit(who, via, 'rule force' if seconds else 'rule force ended', rid,
                    {'params': params, 'seconds': seconds})
        return []

    def owner_of(self, device_id: str, command: str, now: Optional[float] = None) -> Optional[str]:
        """The armed rule that drives this command on this device, if any
        (an overridden rule is not an owner while its pause lasts)."""
        now = self._clock() if now is None else now
        with self._lock:
            for rid, rule in self.rules.items():
                if not rule.enabled or rule.mode != 'armed' or rid in self.errors:
                    continue
                mine = next((c for c, _ in self._targets(rule) if c.id == device_id), None)
                if mine is None:
                    continue
                theirs, ours = self._chan(mine, command), self._chan(mine, rule.target.get('command'))
                if rule.target.get('command') != command and (theirs is None or ours is None or theirs[0] != ours[0]):
                    continue                          # another channel of this device
                st = self.states.get((rid, device_id))
                if st is not None and st.paused_until is not None and now < st.paused_until:
                    continue
                return rid
        return None

    def pause_owner(self, rid: str, seconds: float, *, who: str, via: str) -> None:
        self.override(rid, seconds, who=who, via=via)

    def _cancel_queued(self, rid: str) -> None:
        """A rule that stops driving (disarmed, disabled, deleted) takes back
        the writes it has not put on the wire (OV-F4, design §7): one not yet
        started is cancelled; one waiting for the bus asks `still_wanted`
        once the bus is free and writes nothing. The release, if any, goes
        after them on the same writer."""
        gen = self.__dict__.setdefault('_gen', {})
        gen[rid] = gen.get(rid, 0) + 1
        for key, v in list(self.__dict__.get('_inflight', {}).items()):
            if key[0] == rid:
                v[0].cancel()

    def _release(self, rule: RuleDef, why: str) -> List[str]:
        """An armed rule stops driving its target: `on_disable: safe` runs the
        command's safe parameters on every unit it moved away from them.

        The writes go to each device's writer, in order with whatever that
        device was already sent, and the caller does not wait: on a slow
        device (a Fronius Datamanager answers in ~5 s) four units held the
        API request — and every other one, behind the rules lock — for 19 s
        (2026-10-20). Answers the units being released."""
        self._cancel_queued(rule.id)
        warns = self.__dict__.setdefault('release_warnings', {})
        warns.pop(rule.id, None)
        if rule.on_disable != 'safe':
            return []
        targets = self._targets(rule)
        if rule.id in self.errors and (not targets or not rule.safe_params):
            # a broken rule leaving armed: never a silent "nothing to release",
            # and never a promise of a write its target would refuse
            what = (f"its target {rule.target.get('device') or rule.target.get('endpoint')} is not here"
                    if not targets else f"its command cannot be sent ({self.errors[rule.id]})")
            warns[rule.id] = (f"{what}: nothing was sent back to safe — check the device and send "
                              f"'{rule.target.get('command')}' by hand if needed")
            logger.warning("rule %s release: %s", rule.id, warns[rule.id])
            if self.event_log is not None:
                self.event_log.add('warn', f"rule:{rule.id}", f"{rule.label or rule.id}: {warns[rule.id]} — {why}")
            # a controller taking over must hear it, not only the event log
            self._alert('warning', f'rule:{rule.id}:release', f'rule {rule.id} left armed: {warns[rule.id]}')
            return []
        if not rule.safe_params:
            return []
        out = []
        left = []
        overridden = []
        for cfg, client in targets:
            # another armed rule drives this channel now: it keeps it — the
            # safe values would write over what it holds (and it re-sends only
            # after its reassert window)
            ch = self._chan(cfg, rule.target.get('command'))
            holders = self._candidates(cfg.id, ch[0], exclude=rule.id) if ch is not None else []
            if holders:
                left.append(f"{cfg.id} left to rule {holders[0].rule} "
                            f"(it drives the same channel; nothing was written over it)")
                continue
            st = self.states.get((rule.id, cfg.id))
            # paused: another system was let to command this target. Only
            # what it really set is left alone — a fresh read-back, taken
            # after the rule's own last write, that differs from what the
            # rule sent. A pause with nobody writing leaves the rule's own
            # limit, and that is released like any other (R2-1)
            if st is not None and st.paused_until is not None and self._clock() < st.paused_until \
                    and self._set_by_another(rule, cfg, st):
                until = time.strftime('%H:%M', time.localtime(st.paused_until))
                overridden.append(f"{cfg.id} left to the override (paused until {until}; "
                                  f"another source set it; nothing was written)")
                continue
            if self._at_safe(rule, cfg, st):
                continue
            params = dict(rule.params); params.update(rule.safe_params)
            fut = self._writer(cfg.id).submit(self._release_one, rule, cfg, client, params, why)
            self.__dict__.setdefault('_releasing', []).append(fut)
            out.append(cfg.id)
        if left:
            warns[rule.id] = '; '.join(left)
            logger.info("rule %s release: %s", rule.id, warns[rule.id])
            if self.event_log is not None:
                self.event_log.add('info', f"rule:{rule.id}", f"{rule.label or rule.id}: {warns[rule.id]} — {why}")
        if overridden:
            # not back to safe, and the rule no longer watches it: someone
            # must know, as when writes are off
            note = '; '.join(overridden)
            warns[rule.id] = '; '.join(filter(None, [warns.get(rule.id), note]))
            logger.warning("rule %s release: %s", rule.id, note)
            if self.event_log is not None:
                self.event_log.add('warn', f"rule:{rule.id}", f"{rule.label or rule.id}: {note} — {why}")
            self._alert('warning', f'rule:{rule.id}:release', f'rule {rule.id} left armed: {note}')
        if out and not self._gates():
            # writes off or a standby gateway: the release is tried and the
            # device refuses it like any write — said now, not only later in
            # the event log, so nobody believes the target went back to safe
            note = (f"writes are not allowed on this gateway (security.allow_writes, or it is the standby): "
                    f"{', '.join(out)} not sent back to safe — send '{rule.target.get('command')}' by hand if needed")
            warns[rule.id] = '; '.join(filter(None, [warns.get(rule.id), note]))
            logger.warning("rule %s release: %s", rule.id, note)
            if self.event_log is not None:
                self.event_log.add('warn', f"rule:{rule.id}", f"{rule.label or rule.id}: {note} — {why}")
            self._alert('warning', f'rule:{rule.id}:release', f'rule {rule.id} left armed: {note}')
        return out

    def _at_safe(self, rule: RuleDef, cfg, st: Optional[RuleState]) -> bool:
        """True when a unit needs no release (REL-BOOT). What the device reads
        back decides first: a fresh read-back at its safe value needs nothing,
        any other value is released. Without a fresh read-back, only a
        CONFIRMED last send of the safe value counts — a restart forgets what
        was sent, a write can fail, a shadow want never left — so an unknown
        unit is released, to be sure."""
        from .rules import _num
        p = rule.param
        safe = _num(rule.safe_params.get(p))
        if safe is None:
            return False
        # a write still on its way (or queued, or sent and not verified) can
        # land after any read-back: the safe values go after it, on the same
        # writer, so the device ends where the release says
        if self._inflight_for(cfg.id) or (st is not None and st.pending):
            return False
        held = self._held_now(rule, cfg, st)
        if held is not None:
            return abs(held - safe) <= rule.tolerance
        if st is None or st.commanded is None or not st.confirmed:
            return False
        sent = _num(st.commanded.get(p))
        return sent is not None and abs(sent - safe) <= rule.tolerance

    def _held_now(self, rule: RuleDef, cfg, st: Optional[RuleState]) -> Optional[float]:
        """What the unit holds, as a release may trust it: a read-back at
        most RELEASE_READBACK_MAX_AGE_S old, taken after the rule's last write
        to it — else None."""
        from .rules import _num
        actual, age, _stale = self._actual(rule, cfg)
        if actual is None or age is None or age > RELEASE_READBACK_MAX_AGE_S:
            return None
        if st is not None and st.last_cmd_ts is not None and self._mono() - age <= st.last_cmd_ts:
            return None
        return _num(actual)

    def _set_by_another(self, rule: RuleDef, cfg, st: RuleState) -> bool:
        """During a pause: the unit holds something other than what the rule
        sent, read after the rule's last write — another source set it."""
        from .rules import _num
        if self._inflight_for(cfg.id) or st.pending or not st.commanded:
            return False
        held, sent = self._held_now(rule, cfg, st), _num(st.commanded.get(rule.param))
        return held is not None and sent is not None and abs(held - sent) > rule.tolerance

    def _release_one(self, rule: RuleDef, cfg, client, params: Dict[str, Any], why: str) -> None:
        """One unit back to its safe values — on the device's writer."""
        outcome, reason = 'released to safe', ''
        try:
            _code, res = self._run_command(cfg, client, rule.target['command'], params,
                                           who=f"rule {rule.id}", ip='-', via=f"rule:{rule.id}:release")
            if (res or {}).get('status') not in ('success', 'mismatch'):
                # the verdict is the truth, not the attempt (F-10, 3.83.0)
                outcome, reason = 'release to safe FAILED', str((res or {}).get('reason') or (res or {}).get('status'))
        except Exception as e:  # noqa: BLE001
            outcome, reason = 'release to safe FAILED', str(e)
        if reason:
            logger.warning("rule %s release on %s: %s", rule.id, cfg.id, reason)
        if self.event_log is not None:
            self.event_log.add('info' if not reason else 'warn', f"rule:{rule.id}",
                               f"{rule.label or rule.id} · {cfg.id}: {outcome} — {why}"
                               + (f" ({reason})" if reason else ''))

    def evaluate_now(self, raw: Dict) -> Dict:
        """What the rule would see and want right now, with no state kept —
        the editor's live preview."""
        errs = validate_rule_def(raw, validate_expr=_validate_expr)
        if errs:
            return {'errors': errs}
        rule = parse_rule_def(raw)
        e = self._bind(rule)
        if e:
            return {'errors': [e]}
        now = self._clock()
        signal, age, _s_mono = self._signal(rule, now)
        out = {'signal': signal, 'signal_age_s': None if age is None else round(age, 1),
               'stale': signal is None or (age is not None and age > rule.stale_after_s), 'units': {}}
        probe = RuleState(rule)
        live = next((st for (rid, _d), st in self.states.items() if rid == rule.id), None)
        if signal is not None and rule.kind == 'trim':
            v = float(signal)
            sv = rule.signal_valid
            if (sv.get('min') is not None and v < float(sv['min'])) or (sv.get('max') is not None and v > float(sv['max'])):
                out['invalid'] = True
                state, base = 'invalid', None
            else:
                if live is not None:
                    probe._trim = live._trim
                first = self._targets(rule)[:1]
                a = self._actual(rule, first[0][0]) if first else (None, None, None)
                state, base = probe.preview_trim(v, probe._fresh(*a))
        elif signal is not None and rule.kind == 'schedule':
            probe._wall = now
            state, base = probe._schedule_desired(signal)
        elif signal is not None and rule.kind == 'steps':
            v = float(signal)
            sv = rule.signal_valid
            if (sv.get('min') is not None and v < float(sv['min'])) or (sv.get('max') is not None and v > float(sv['max'])):
                out['invalid'] = True
                state, base = 'invalid', None
            else:
                # in the dead band the answer depends on where the rule is:
                # the live state when it runs, else what the first unit holds
                if live is not None:
                    probe.state, probe._base, probe.want = live.state, live._base, live.want
                first = self._targets(rule)[:1]
                a = self._actual(rule, first[0][0]) if first else (None, None, None)
                state, base, _fast = probe._desired(v, a[0])
        elif signal is not None:
            # the live state's latch, so the preview answers what the rule would do now
            if live is not None:
                probe.state = live.state
            truth = probe._truth(signal)
            if truth is None:
                state, base = 'dead band — waiting for a threshold', None
            else:
                state = 'true' if truth else 'false'
                oc = rule.then if truth else rule.otherwise
                base = dict((oc or {}).get('params') or {}) if oc is not None else None
        else:
            state, base = 'stale', None
        out['state'] = state
        out['want'] = probe._want_for(base) if base is not None else None
        for cfg, _client in self._targets(rule):
            actual, a_age, _s = self._actual(rule, cfg)
            out['units'][cfg.id] = {'actual': actual, 'actual_age_s': None if a_age is None else round(a_age, 1)}
        out['inputs'] = self.input_problems(raw)
        return out
