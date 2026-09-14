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
import logging
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from . import expressions
from .rules import (KINDS, NORMAL, STALE, Decision, RuleDef, RuleState, parse_rule_def,
                    validate_rule_def)

logger = logging.getLogger(__name__)

MQTT_ROOT = 'mbg/rules'
SWEEP_MIN_INTERVAL_S = 60.0
FAILURES_BEFORE_ALERT = 3


# Device ids carry hyphens (`pv-u1`), which the expression grammar would read
# as a subtraction. A reference `pv-u1.voltage_l1_n` is rewritten to
# `pv__u1.voltage_l1_n` before parsing and mapped back when resolved — the
# operator writes ids as they are; nothing else changes.
_DEV_REF = re.compile(r'(?<![\w.])([A-Za-z_][\w-]*-[\w-]*)(?=\.[A-Za-z_])')
_DASH = '__'


def _prepare(expr: str) -> str:
    return _DEV_REF.sub(lambda m: m.group(1).replace('-', _DASH), expr or '')


def _unprepare(name: str) -> str:
    if '.' in name:
        dev, reg = name.split('.', 1)
        return f"{dev.replace(_DASH, '-')}.{reg}"
    return name


def _wrap_resolver(resolve):
    def wrapped(name):
        return resolve(_unprepare(name))
    return wrapped


def _validate_expr(expr: str) -> Optional[str]:
    ok, err, _refs = expressions.validate_expression(_prepare(expr))
    if not ok:
        return err
    if not all('.' in r for r in _refs):
        return "every reference must name its device (device.register)"
    return None


class RuleStore:
    """rules.yaml: a list under `rules:`. Written whole, atomically."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> List[Dict]:
        try:
            data = yaml.safe_load(self.path.read_text(encoding='utf-8')) or {}
        except FileNotFoundError:
            return []
        except Exception as e:  # noqa: BLE001
            logger.error("rules.yaml unreadable: %s", e)
            return []
        rules = data.get('rules') if isinstance(data, dict) else data
        return [r for r in (rules or []) if isinstance(r, dict)]

    def save(self, rules: List[Dict]) -> None:
        tmp = self.path.with_suffix('.yaml.tmp')
        tmp.write_text(yaml.safe_dump({'rules': rules}, sort_keys=False, allow_unicode=True), encoding='utf-8')
        tmp.replace(self.path)


class RulesRuntime:
    def __init__(self, *, config_dir: Path, resolver_factory: Callable, find_device: Callable,
                 endpoint_devices: Callable, commands_for: Callable, run_command: Callable,
                 poll_now: Callable, event_log=None, alert_mgr=None, mqtt=None, audit_log=None,
                 influx=None,
                 gates: Callable[[], bool] = lambda: True, clock: Callable[[], float] = time.time,
                 mono: Callable[[], float] = time.monotonic):
        self.store = RuleStore(Path(config_dir) / 'rules.yaml')
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
        self.rules: Dict[str, RuleDef] = {}
        self.raw: Dict[str, Dict] = {}
        self.states: Dict[Tuple[str, str], RuleState] = {}
        self.decisions: Dict[str, deque] = {}
        self._last_action: Dict[Tuple[str, str], str] = {}
        self._last_pub: Dict[str, str] = {}
        self._last_sweep: Dict[str, float] = {}
        self._alerted: Dict[Tuple[str, str], bool] = {}
        self.errors: Dict[str, str] = {}               # rule id -> why it cannot run (bad binding)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_tick: Optional[float] = None

    # ── loading and binding ──────────────────────────────────────────────────

    def load(self) -> None:
        with self._lock:
            raws = self.store.load()
            persisted = self._load_state()
            self.rules, self.raw, self.errors = {}, {}, {}
            keep = {}
            for raw in raws:
                rid = str(raw.get('id', '') or '')
                errs = validate_rule_def(raw, validate_expr=_validate_expr)
                rule = parse_rule_def(raw)
                self.raw[rid] = raw
                self.rules[rid] = rule
                bind_err = self._bind(rule) if not errs else None
                if errs or bind_err:
                    self.errors[rid] = '; '.join(errs) if errs else bind_err
                for cfg, _client in self._targets(rule):
                    key = (rid, cfg.id)
                    st = self.states.get(key) or RuleState(rule)
                    st.rule = rule
                    p = persisted.get(f"{rid}:{cfg.id}") or {}
                    if p.get('clamp') and st.clamp is None:
                        st.clamp = dict(p['clamp'])
                    if p.get('paused_until') and st.paused_until is None:
                        st.paused_until = float(p['paused_until'])
                    keep[key] = st
                self.decisions.setdefault(rid, deque(maxlen=200))
            self.states = keep
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
        except Exception:  # noqa: BLE001
            return {}

    def _save_state(self) -> None:
        data = {}
        for (rid, dev), st in self.states.items():
            if st.clamp or st.paused_until:
                data[f"{rid}:{dev}"] = {'clamp': st.clamp, 'paused_until': st.paused_until}
        try:
            self._state_path.write_text(json.dumps(data), encoding='utf-8')
        except Exception as e:  # noqa: BLE001
            logger.warning("rules_state.json: %s", e)

    # ── the thread ───────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='rules', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)
            self._thread = None

    def _period(self) -> float:
        ev = [r.timing.every_s for r in self.rules.values() if r.enabled and r.id not in self.errors]
        return max(0.5, min(ev)) if ev else 2.0

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 — one bad tick must not stop the controller
                logger.exception("rules tick: %s", e)
            self._stop.wait(self._period())

    # ── one tick ─────────────────────────────────────────────────────────────

    def _signal(self, rule: RuleDef, now: float) -> Tuple[Any, Optional[float]]:
        expr = rule.signal if rule.kind == 'steps' else rule.when
        resolve = self._resolver_factory({})
        try:
            tree = expressions.compile_expression(_prepare(expr))
            val = expressions.evaluate_tree(tree, _wrap_resolver(resolve))
        except expressions.MissingValue:
            return None, None
        except expressions.ExpressionError as e:
            logger.debug("rule %s: %s", rule.id, e)
            return None, None
        # the store stamps every value with a monotonic time; a value without
        # one has no known age and is treated as stale (fail closed)
        mono = [m for m in (getattr(resolve, 'touched_mono', None) or []) if m is not None]
        age = (self._mono() - min(mono)) if mono else None
        if rule.kind == 'condition':
            return bool(val), age
        return val, age

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
        mono = [m for m in (getattr(resolve, 'touched_mono', None) or []) if m is not None]
        iv = getattr(resolve, 'touched_interval', None) or []
        age = (self._mono() - max(mono)) if mono else None
        stale = 3.0 * float(iv[0]) if iv and iv[0] else 3.0 * 3600
        return val, age, stale

    def tick(self, now: Optional[float] = None) -> List[Dict]:
        now = self._clock() if now is None else now
        out = []
        with self._lock:
            rules = [r for r in self.rules.values() if r.enabled and r.id not in self.errors]
        for rule in rules:
            signal, age = self._signal(rule, now)
            for cfg, client in self._targets(rule):
                key = (rule.id, cfg.id)
                st = self.states.get(key)
                if st is None:
                    st = self.states[key] = RuleState(rule)
                actual, a_age, a_stale = self._actual(rule, cfg)
                d = st.evaluate(now, signal, age, actual=actual, actual_age_s=a_age, actual_stale_s=a_stale)
                self._act(rule, cfg, client, st, d, now)
                out.append({'rule': rule.id, 'device': cfg.id, **d.to_dict()})
            self._publish_state(rule)
        self.last_tick = now
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
            else:
                code, res = self._run_command(cfg, client, rule.target['command'], dict(d.want or {}),
                                              who=f"rule {rule.id}", ip='-', via=f"rule:{rule.id}")
                ok = res.get('status') == 'success'
                rec['result'] = res.get('status')
                if res.get('reason'):
                    rec['result_reason'] = res['reason']
                st.note_result(ok)
                if not ok:
                    st.pending = True                 # try again at the rate limit
                    logger.warning("rule %s on %s: %s %s", rule.id, cfg.id, res.get('status'), res.get('reason', ''))
            if st.failures >= FAILURES_BEFORE_ALERT and not self._alerted.get(key):
                self._alerted[key] = True
                self._alert('error', f"rule:{rule.id}:{cfg.id}",
                            f"rule '{rule.label or rule.id}' cannot apply {rule.target['command']} on {cfg.id}: "
                            f"{st.failures} failures in a row ({rec.get('result_reason') or rec.get('result')})")
            elif st.failures == 0:
                self._alerted[key] = False
        elif d.action == 'sweep':
            last = self._last_sweep.get(cfg.id, 0.0)
            if now - last >= SWEEP_MIN_INTERVAL_S:
                self._last_sweep[cfg.id] = now
                try:
                    self._poll_now(cfg, client, rule.target.get('_readback_group') or '')
                except Exception:  # noqa: BLE001
                    pass
        if d.changed:
            self._event(rule, cfg, d)
        if d.changed or d.action in ('run', 'reassert'):
            self._influx_event(rule, cfg, d, rec)
        if notable:
            self.decisions.setdefault(rule.id, deque(maxlen=200)).appendleft(rec)

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
        if state in (NORMAL, 'false'):
            return 0
        if state == 'true':
            return 1
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
                          'pending': st.pending, 'last_action': self._last_action.get(key)}
            sev = self._severity(rule, st.state)
            if sev > worst_sev:
                worst, worst_sev = st.state, sev
        last = next(iter(self.decisions.get(rid, [])), None)
        return {'state': worst if units else 'unbound', 'signal': signal, 'units': units,
                'last': last, 'error': self.errors.get(rid), 'last_tick': self.last_tick}

    def _publish_state(self, rule: RuleDef) -> None:
        lv = self.live(rule.id)
        body = {'id': rule.id, 'label': rule.label or rule.id, 'mode': rule.mode, 'enabled': rule.enabled,
                'state': lv['state'], 'signal': lv['signal'],
                'units': {d: {k: u.get(k) for k in ('state', 'want', 'clamp', 'paused_until', 'last_action')}
                          for d, u in lv['units'].items()},
                'decision': (lv['last'] or {}).get('action'), 'reason': (lv['last'] or {}).get('reason'),
                'ts': round(self._clock(), 3)}
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
        who = str(body.get('source') or 'mqtt')
        if 'enabled' in body:
            self.set_enabled(rid, bool(body['enabled']), who=who, via='mqtt')
        if 'clamp' in body:
            c = body['clamp']
            if c is None:
                self.clear_clamp(rid, who=who, via='mqtt')
            elif isinstance(c, dict) and c.get('max') is not None:
                self.set_clamp(rid, float(c['max']), c.get('expires_s'), who=who, via='mqtt')
        if body.get('override_s'):
            self.override(rid, float(body['override_s']), who=who, via='mqtt')

    # ── operations (API, UI, MQTT) ───────────────────────────────────────────

    def _audit(self, who: str, via: str, action: str, target: str, detail: Dict) -> None:
        if self.audit_log is None:
            return
        try:
            self.audit_log.append(user=who, ip='-', action=action, target=target, status='ok',
                                  detail={**detail, 'via': via})
        except Exception:  # noqa: BLE001
            pass

    def list(self) -> List[Dict]:
        with self._lock:
            return [{**r.to_dict(), 'live': self.live(rid), 'raw': self.raw.get(rid)} for rid, r in self.rules.items()]

    def get(self, rid: str) -> Optional[Dict]:
        with self._lock:
            r = self.rules.get(rid)
            return None if r is None else {**r.to_dict(), 'live': self.live(rid), 'raw': self.raw.get(rid)}

    def validate(self, raw: Dict, *, existing: Optional[str] = None) -> List[str]:
        errs = validate_rule_def(raw, validate_expr=_validate_expr)
        if errs:
            return errs
        rule = parse_rule_def(raw)
        with self._lock:
            if existing is None and rule.id in self.rules:
                return [f"id: '{rule.id}' already exists"]
            if existing is not None and existing != rule.id:
                return ["id: cannot be changed"]
            e = self._bind(rule)
            if e:
                return [e]
            # one owner per target+command
            mine = {(c.id, rule.target['command']) for c, _ in self._targets(rule)}
            for oid, other in self.rules.items():
                if oid == rule.id or not other.enabled:
                    continue
                theirs = {(c.id, other.target.get('command')) for c, _ in self._targets(other)}
                clash = mine & theirs
                if clash and (other.mode == 'armed' or rule.mode == 'armed'):
                    dev, cmd = next(iter(clash))
                    return [f"target: rule '{oid}' already drives {cmd} on {dev}; two armed rules cannot share a target"]
        return []

    def upsert(self, raw: Dict, *, who='?', via='api') -> Tuple[List[str], Optional[Dict]]:
        existing = str(raw.get('id', '') or '') if str(raw.get('id', '') or '') in self.rules else None
        errs = self.validate(raw, existing=existing)
        if errs:
            return errs, None
        rid = str(raw['id'])
        with self._lock:
            prev = self.rules.get(rid)
            if prev is not None and prev.mode == 'armed' and (raw.get('mode') != 'armed' or not raw.get('enabled', True)):
                self._release(prev, f"{'shadow' if raw.get('mode') != 'armed' else 'disabled'} by {who}")
            rules = [raw if str(r.get('id')) == rid else r for r in self.store.load()]
            if prev is None:
                rules.append(raw)
            self.store.save(rules)
            if prev is None or self._shape_changed(prev, parse_rule_def(raw)):
                # a new shape starts from scratch — the debounce rebuilds it
                for key in [k for k in self.states if k[0] == rid]:
                    self.states.pop(key)
            elif prev.mode != 'armed' and raw.get('mode') == 'armed':
                # what shadow "sent" was never on the wire: the armed rule
                # asks for its current want now (unless the device holds it)
                for key, st in self.states.items():
                    if key[0] == rid:
                        st.commanded, st.last_cmd_ts, st.pending = None, None, st.want is not None
            self.load()
        self._audit(who, via, 'rule ' + ('updated' if prev else 'created'), rid, {'mode': raw.get('mode'), 'kind': raw.get('kind')})
        return [], self.get(rid)

    @staticmethod
    def _shape_changed(a: RuleDef, b: RuleDef) -> bool:
        da, db = a.to_dict(), b.to_dict()
        for k in ('mode', 'enabled', 'label'):
            da.pop(k, None); db.pop(k, None)
        return da != db

    def delete(self, rid: str, *, who='?', via='api') -> bool:
        with self._lock:
            rule = self.rules.get(rid)
            if rule is None:
                return False
            if rule.mode == 'armed' and rule.enabled:
                self._release(rule, f"deleted by {who}")
            self.store.save([r for r in self.store.load() if str(r.get('id')) != rid])
            for key in [k for k in self.states if k[0] == rid]:
                self.states.pop(key)
            self.decisions.pop(rid, None)
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
        errs, _ = self.upsert({**raw, 'mode': mode}, who=who, via=via)
        if not errs:
            self._audit(who, via, 'rule ' + ('armed' if mode == 'armed' else 'shadowed'), rid, {})
        return errs

    def set_enabled(self, rid: str, enabled: bool, *, who='?', via='api') -> List[str]:
        with self._lock:
            raw = self.raw.get(rid)
            if raw is None:
                return ["no such rule"]
        errs, _ = self.upsert({**raw, 'enabled': bool(enabled)}, who=who, via=via)
        if not errs:
            self._audit(who, via, 'rule ' + ('enabled' if enabled else 'disabled'), rid, {})
        return errs

    def set_clamp(self, rid: str, max_value: float, expires_s=None, *, who='?', via='api') -> List[str]:
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

    def owner_of(self, device_id: str, command: str, now: Optional[float] = None) -> Optional[str]:
        """The armed rule that drives this command on this device, if any
        (an overridden rule is not an owner while its pause lasts)."""
        now = self._clock() if now is None else now
        with self._lock:
            for rid, rule in self.rules.items():
                if not rule.enabled or rule.mode != 'armed' or rid in self.errors:
                    continue
                if rule.target.get('command') != command:
                    continue
                if not any(c.id == device_id for c, _ in self._targets(rule)):
                    continue
                st = self.states.get((rid, device_id))
                if st is not None and st.paused_until is not None and now < st.paused_until:
                    continue
                return rid
        return None

    def pause_owner(self, rid: str, seconds: float, *, who: str, via: str) -> None:
        self.override(rid, seconds, who=who, via=via)

    def _release(self, rule: RuleDef, why: str) -> None:
        """An armed rule stops driving its target: `on_disable: safe` runs the
        command's safe parameters on every unit it moved away from them."""
        if rule.on_disable != 'safe' or not rule.safe_params:
            return
        p = rule.param
        for cfg, client in self._targets(rule):
            st = self.states.get((rule.id, cfg.id))
            if st is None or st.commanded is None:
                continue
            if isinstance(st.commanded.get(p), (int, float)) and rule.safe_params.get(p) is not None \
                    and abs(float(st.commanded[p]) - float(rule.safe_params[p])) <= rule.tolerance:
                continue
            params = dict(rule.params); params.update(rule.safe_params)
            try:
                self._run_command(cfg, client, rule.target['command'], params,
                                  who=f"rule {rule.id}", ip='-', via=f"rule:{rule.id}:release")
            except Exception as e:  # noqa: BLE001
                logger.warning("rule %s release on %s: %s", rule.id, cfg.id, e)
            if self.event_log is not None:
                self.event_log.add('info', f"rule:{rule.id}", f"{rule.label or rule.id} · {cfg.id}: released to safe — {why}")

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
        signal, age = self._signal(rule, now)
        out = {'signal': signal, 'signal_age_s': None if age is None else round(age, 1),
               'stale': signal is None or (age is not None and age > rule.stale_after_s), 'units': {}}
        probe = RuleState(rule)
        if signal is not None and rule.kind == 'steps':
            v = float(signal)
            sv = rule.signal_valid
            if (sv.get('min') is not None and v < float(sv['min'])) or (sv.get('max') is not None and v > float(sv['max'])):
                out['invalid'] = True
                state, base = 'invalid', None
            else:
                state, base, _fast = probe._desired(v)
        elif signal is not None:
            state = 'true' if signal else 'false'
            oc = rule.then if signal else rule.otherwise
            base = dict((oc or {}).get('params') or {}) if oc is not None else None
        else:
            state, base = 'stale', None
        out['state'] = state
        out['want'] = probe._want_for(base) if base is not None else None
        for cfg, _client in self._targets(rule):
            actual, a_age, _s = self._actual(rule, cfg)
            out['units'][cfg.id] = {'actual': actual, 'actual_age_s': None if a_age is None else round(a_age, 1)}
        return out
