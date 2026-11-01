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
"""One edit of a device in sections (ADR-0031 §4–§5): ``PATCH /api/devices/{id}``.

The pure half: what a section may say, the validators the old sub-routes
and PATCH share, how a section changes the stored entry, and the diff a dry
run shows. The API module composes them with the device save pipeline
(``_device_put_raw``) so PUT, the sub-routes and PATCH judge a value the
same way.

Sections::

    connection  {protocol, host, port, unit_id, serial_port, … advanced keys}
    polling     {enabled, poll_groups: {name: interval | {interval, description} | null}}
    identity    {name, type}
    outputs     {mqtt: bool | {enabled, publish_mode, heartbeat_interval},
                 influxdb: bool | {enabled, publish_mode, write_interval, heartbeat_interval},
                 ha_discovery: bool, http_output: bool | {enabled},
                 opcua: {enabled, fields}, rest_push: {enabled, url, interval_s, …}}
    model       {template}

The write lock is not a section: it has its own route and dialog.

The primary meter takes the same sections through an adapter (ADR-0032 §6):
``PRIMARY_SECTION_KEYS`` says what applies to it, ``primary_refusals`` refuses
the rest in the words of its edit locks (``PRIMARY_FIXED``).
"""
from __future__ import annotations

import copy
import math
from typing import Any, Dict, List, Optional, Tuple

SECTIONS = ('connection', 'polling', 'identity', 'outputs', 'model')
SECTION_KEYS = {
    'polling': ('enabled', 'poll_groups'),
    'identity': ('name', 'type'),
    'outputs': ('mqtt', 'influxdb', 'ha_discovery', 'http_output', 'opcua', 'rest_push'),
    'model': ('template',),
}
# never a section, whatever is sent: each has its own route
OWN_ROUTES = {'write_locked': 'POST /api/devices/{id}/write-lock',
              'id': 'fixed after creation',
              'components': 'the Components routes'}

from .save_merge import MASKED as MASK  # noqa: E402 — the one mask of a secret

# where a stored key is shown in the diff: (section, field)
_FIELD_OF = {
    'name': ('identity', 'name'),
    'kind': ('identity', 'type'),
    'template': ('model', 'template'),
    'enabled': ('polling', 'enabled'),
    'mqtt.enabled': ('outputs', 'mqtt.enabled'),
    'mqtt.ha_discovery': ('outputs', 'ha_discovery'),
    'mqtt.publish_mode': ('outputs', 'mqtt.publish_mode'),
    'mqtt.heartbeat_interval': ('outputs', 'mqtt.heartbeat_interval'),
    'influxdb.enabled': ('outputs', 'influxdb.enabled'),
    'influxdb.publish_mode': ('outputs', 'influxdb.publish_mode'),
    'influxdb.write_interval': ('outputs', 'influxdb.write_interval'),
    'influxdb.heartbeat_interval': ('outputs', 'influxdb.heartbeat_interval'),
    'http_output': ('outputs', 'http_output'),
    'opcua': ('outputs', 'opcua'),
    'rest_push': ('outputs', 'rest_push'),
}
_SECRET_KEYS = ('password', 'token', 'secret')


class PatchError(Exception):
    """A refused section: ``errors`` (``section.field: why``)."""

    def __init__(self, errors: List[str]):
        super().__init__('; '.join(errors))
        self.errors = list(errors)


# ── what a body may say ──────────────────────────────────────────────────────

def parse(body: Any) -> Dict[str, Dict[str, Any]]:
    """The sections of a PATCH body, each a dict; raises PatchError."""
    if not isinstance(body, dict) or not isinstance(body.get('sections'), dict):
        raise PatchError(["sections: an object {connection?, polling?, identity?, outputs?, model?}"])
    errs: List[str] = []
    extra = sorted(set(body) - {'sections'})
    if extra:
        errs.append(f"{extra[0]}: not part of a PATCH body (only sections)")
    secs = body['sections']
    out: Dict[str, Dict[str, Any]] = {}
    for name, val in secs.items():
        if name in OWN_ROUTES:
            errs.append(f"sections.{name}: not a section — {OWN_ROUTES[name]}")
            continue
        if name not in SECTIONS:
            errs.append(f"sections.{name}: not a section ({', '.join(SECTIONS)})")
            continue
        if not isinstance(val, dict):
            errs.append(f"{name}: an object")
            continue
        allowed = SECTION_KEYS.get(name)
        if allowed is not None:
            bad = sorted(set(val) - set(allowed))
            for k in bad:
                errs.append(f"{name}.{k}: " + (f"not part of {name} — {OWN_ROUTES[k]}" if k in OWN_ROUTES
                                               else f"not part of {name} ({', '.join(allowed)})"))
        elif 'write_locked' in val:
            errs.append("connection.write_locked: not part of connection — " + OWN_ROUTES['write_locked'])
        out[name] = val
    if not out and not errs:
        errs.append("sections: name at least one section")
    if errs:
        raise PatchError(errs)
    return out


# ── validators the sub-routes share ──────────────────────────────────────────

PUBLISHING_KEYS = {'mqtt': ('publish_mode', 'heartbeat_interval'),
                   'influxdb': ('publish_mode', 'write_interval', 'heartbeat_interval')}


def publishing_clean(payload: Dict, heartbeat_ok) -> Tuple[Dict, List[str]]:
    """A device's own publishing rhythm, checked: ({mqtt: {…}, influxdb: {…}},
    errors) — the rule of POST /api/devices/{id}/publishing. ``heartbeat_ok``
    is config.heartbeat_seconds (whole seconds or None)."""
    errors: List[str] = []
    out: Dict[str, Dict] = {}
    for sect, keys in PUBLISHING_KEYS.items():
        block = payload.get(sect)
        if block is None:
            continue
        if not isinstance(block, dict) or set(block) - set(keys):
            errors.append(f"{sect}: an object with {', '.join(keys)}")
            continue
        clean = {}
        for k, v in block.items():
            if v is None:
                clean[k] = None
            elif k == 'publish_mode':
                if v not in ('changed', 'all'):
                    errors.append(f"{sect}.publish_mode: changed or all (null = as in Settings)")
                clean[k] = v
            elif (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                  or v < 0 or v > 86400):
                errors.append(f"{sect}.{k}: seconds, 0..86400 (null = as in Settings)")
            elif sect == 'influxdb' and k == 'heartbeat_interval' and heartbeat_ok(v) is None:
                errors.append(f"{sect}.{k}: whole seconds, 0..86400 (null = as in Settings)")
            else:
                clean[k] = int(v) if k == 'heartbeat_interval' else float(v)
        out[sect] = clean
    return out, errors


def rest_push_clean(payload: Dict, old_headers: Dict, mask: str, flag) -> Tuple[Dict, List[str]]:
    """The REST push sink, checked: (cfg, errors) — the rule of POST
    /api/devices/{id}/rest-push. A header value equal to ``mask`` keeps the
    stored one."""
    errors: List[str] = []
    enabled = flag(payload, 'enabled', False)
    url = str(payload.get('url', '') or '').strip()
    if enabled and not (url.startswith('http://') or url.startswith('https://')):
        errors.append("url: required (http:// or https://) when enabled")
    try:
        interval = int(payload.get('interval_s', 30))
        if interval < 5:
            errors.append("interval_s: minimum 5 seconds")
    except (TypeError, ValueError):
        errors.append("interval_s: must be an integer")
        interval = 30
    fmt = str(payload.get('format', 'native'))
    if fmt not in ('native', 'flat'):
        errors.append("format: must be 'native' or 'flat'")
    old_h = dict(old_headers or {})
    new_h = payload.get('headers')
    if isinstance(new_h, dict):
        # the mask keeps a stored value — only under the name it is stored
        # with; a renamed header has no value to keep (it was stored as the
        # mask itself)
        for k, v in new_h.items():
            if k and v == mask and k not in old_h:
                errors.append(f"headers.{k}: type its value again (a renamed header's value is not known)")
    if errors:
        return {}, errors
    headers = old_h
    if isinstance(new_h, dict):
        headers = {k: (old_h.get(k, v) if v == mask else v) for k, v in new_h.items() if k}
    return {'enabled': enabled, 'url': url, 'interval_s': interval,
            'headers': headers, 'format': fmt,
            'verify_tls': flag(payload, 'verify_tls', True),
            'timeout': int(payload.get('timeout', 10) or 10)}, []


def poll_groups_clean(groups_in: Any) -> Tuple[Dict[str, Optional[Dict]], Optional[str]]:
    """Poll-group intervals, checked: ({name: {interval[, description]} | None},
    error) — the rule of POST /api/devices/{id}/poll-groups (its 400 text)."""
    clean: Dict[str, Optional[Dict]] = {}
    if not isinstance(groups_in, dict):
        return {}, "poll_groups: an object {name: interval}"
    for name, g in groups_in.items():
        if g is None:                          # back to the default rhythm
            clean[str(name)] = None
            continue
        try:
            iv = float(g.get("interval") if isinstance(g, dict) else g)
        except (TypeError, ValueError):
            return {}, f"{name}: interval must be a number"
        if not (0.05 <= iv <= 86400):
            return {}, f"{name}: interval must be 0.05..86400 s"
        # a description not sent is kept (the save merges per key)
        clean[str(name)] = {"interval": iv, **({"description": str(g["description"] or "")}
                                               if isinstance(g, dict) and "description" in g else {})}
    if not clean:
        return {}, "no poll groups given"
    return clean, None


# ── how an output changes the stored entry (a devices[] entry) ───────────────

def apply_sinks(raw: Dict, mqtt: Optional[bool] = None, influxdb: Optional[bool] = None,
                ha_discovery: Optional[bool] = None) -> None:
    if mqtt is not None:
        raw.setdefault('mqtt', {})['enabled'] = bool(mqtt)
    if ha_discovery is not None:
        raw.setdefault('mqtt', {})['ha_discovery'] = bool(ha_discovery)
    if influxdb is not None:
        raw.setdefault('influxdb', {})['enabled'] = bool(influxdb)


def apply_publishing(target: Dict, mqtt: Optional[Dict] = None, influxdb: Optional[Dict] = None) -> None:
    for sect, keys in (('mqtt', mqtt), ('influxdb', influxdb)):
        for k, v in (keys or {}).items():
            block = target.setdefault(sect, {})
            if v is None:
                block.pop(k, None)
            else:
                block[k] = v


def apply_http_output(raw: Dict, enabled: bool) -> None:
    raw.setdefault('http_output', {})['enabled'] = bool(enabled)


def apply_opcua(raw: Dict, block: Dict) -> None:
    raw['opcua'] = dict(block)


def apply_rest_push(raw: Dict, cfg: Dict) -> None:
    raw['rest_push'] = dict(cfg or {})


# ── the outputs section ──────────────────────────────────────────────────────

def outputs_plan(sec: Dict, *, old_rest_headers: Dict, mask: str, flag, heartbeat_ok,
                 opcua_block) -> Tuple[Dict[str, Any], List[str]]:
    """The outputs section checked, as the steps to apply: {sinks: {…},
    publishing: {…}, http_output: bool, opcua: {…}, rest_push: {…}} and the
    errors (``outputs.<key>…: why``). ``opcua_block`` raises on a bad block
    (the API's own check) — its message is carried."""
    errors: List[str] = []
    plan: Dict[str, Any] = {}
    sinks: Dict[str, bool] = {}
    pub_in: Dict[str, Dict] = {}
    for key in ('mqtt', 'influxdb'):
        if key not in sec:
            continue
        v = sec[key]
        if isinstance(v, bool):
            sinks[key] = v
        elif isinstance(v, dict):
            v = dict(v)
            if 'enabled' in v:
                if not isinstance(v['enabled'], bool):
                    errors.append(f"outputs.{key}.enabled: true or false")
                else:
                    sinks[key] = v['enabled']
                v.pop('enabled')
            if v:
                pub_in[key] = v
        else:
            errors.append(f"outputs.{key}: true or false, or {{enabled, {', '.join(PUBLISHING_KEYS[key])}}}")
    if 'ha_discovery' in sec:
        if isinstance(sec['ha_discovery'], bool):
            sinks['ha_discovery'] = sec['ha_discovery']
        else:
            errors.append("outputs.ha_discovery: true or false")
    if pub_in:
        pub, perr = publishing_clean(pub_in, heartbeat_ok)
        errors += [f"outputs.{e}" for e in perr]
        if not perr:
            plan['publishing'] = pub
    if sinks:
        plan['sinks'] = sinks
    if 'http_output' in sec:
        v = sec['http_output']
        v = v.get('enabled') if isinstance(v, dict) and set(v) <= {'enabled'} else v
        if isinstance(v, bool):
            plan['http_output'] = v
        else:
            errors.append("outputs.http_output: true or false (or {enabled})")
    if 'opcua' in sec:
        try:
            plan['opcua'] = opcua_block(sec['opcua'])
        except Exception as e:  # noqa: BLE001 — the API's HTTPException: its errors
            det = getattr(e, 'detail', None)
            msgs = det.get('errors') if isinstance(det, dict) else [str(e)]
            errors += [f"outputs.{m}" if str(m).startswith('opcua') else f"outputs.opcua: {m}" for m in msgs]
    if 'rest_push' in sec:
        v = sec['rest_push']
        if not isinstance(v, dict):
            errors.append("outputs.rest_push: an object {enabled, url, interval_s, headers, format, …}")
        else:
            cfg, rerr = rest_push_clean(v, old_rest_headers, mask, flag)
            errors += [f"outputs.rest_push.{e}" for e in rerr]
            if not rerr:
                plan['rest_push'] = cfg
    return plan, errors


def apply_outputs(raw: Dict, plan: Dict[str, Any]) -> None:
    """The checked outputs applied to a devices[] entry."""
    if plan.get('sinks'):
        apply_sinks(raw, **plan['sinks'])
    if plan.get('publishing'):
        apply_publishing(raw, **plan['publishing'])
    if 'http_output' in plan:
        apply_http_output(raw, plan['http_output'])
    if 'opcua' in plan:
        apply_opcua(raw, plan['opcua'])
    if 'rest_push' in plan:
        apply_rest_push(raw, plan['rest_push'])


# ── the diff a dry run shows ─────────────────────────────────────────────────

def _flat(v: Any, pfx: str, out: Dict[str, Any]) -> None:
    if isinstance(v, dict) and v:
        for k, x in v.items():
            _flat(x, f"{pfx}.{k}" if pfx else str(k), out)
    else:
        out[pfx] = v


def _secret(path: str) -> bool:
    last = path.rsplit('.', 1)[-1].lower()
    return (any(s in last for s in _SECRET_KEYS)
            or path.startswith('connection.headers.') or path.startswith('rest_push.headers.'))


_URL_KEYS = ('url',)


def _shown(path: str, v: Any) -> Any:
    if v is None:
        return v
    if _secret(path):
        return MASK
    if path.rsplit('.', 1)[-1] in _URL_KEYS and isinstance(v, str):
        from .redact import redact_url        # credentials in a URL (userinfo, a token)
        return redact_url(v)
    return v


def _place(path: str) -> Tuple[str, str]:
    if path.startswith('connection.') or path == 'connection':
        return 'connection', path[len('connection.'):] if '.' in path else ''
    for key, where in _FIELD_OF.items():
        if path == key:
            return where
        if path.startswith(key + '.'):
            return where[0], where[1] + path[len(key):]
    head = path.split('.', 1)[0]
    return ('outputs' if head in ('mqtt', 'influxdb') else 'other'), path


def changes(before: Dict, after: Dict, *, ignore: Tuple[str, ...] = ()) -> List[Dict[str, Any]]:
    """[{section, field, from, to}] between two stored entries; secrets are
    masked (a changed password says it changed, never what it is)."""
    a: Dict[str, Any] = {}
    b: Dict[str, Any] = {}
    _flat(copy.deepcopy(before or {}), '', a)
    _flat(copy.deepcopy(after or {}), '', b)
    out = []
    for path in sorted(set(a) | set(b)):
        if any(path == i or path.startswith(i + '.') for i in ignore):
            continue
        va, vb = a.get(path), b.get(path)
        if va == vb:
            continue
        section, field = _place(path)
        out.append({'section': section, 'field': field, 'from': _shown(path, va), 'to': _shown(path, vb)})
    return out


def poll_group_changes(before: Dict[str, Any], clean: Dict[str, Optional[Dict]]) -> List[Dict[str, Any]]:
    """The poll-group intervals a polling section changes: [{section, field, from, to}]."""
    out = []
    for name, g in sorted(clean.items()):
        old = (before or {}).get(name)
        old_iv = old.get('interval') if isinstance(old, dict) else old
        new_iv = g.get('interval') if isinstance(g, dict) else None
        if g is None:
            if old is not None:
                out.append({'section': 'polling', 'field': f"poll_groups.{name}", 'from': old_iv, 'to': None})
            continue
        if old_iv is None or float(old_iv) != float(new_iv) or (
                'description' in g and (old or {}).get('description', '') != g['description']):
            out.append({'section': 'polling', 'field': f"poll_groups.{name}", 'from': old_iv, 'to': new_iv})
    return out


# ── the primary meter (ADR-0032 §6, FINDINGS L3-F19) ─────────────────────────
#
# The gateway's own meter is not a devices[] entry: its connection is the flat
# ``modbus:`` section, its HA flag the gateway's, its outputs the flat
# ``http_output:`` / ``opcua_output:`` / ``rest_push:`` sections, its
# intervals its register file. PATCH takes the same sections for it; what does
# not apply is refused in the words of its edit locks, never dropped.

# what its connection is (the keys PUT /api/devices/{primary} has always read)
PRIMARY_CONNECTION_KEYS = ('protocol', 'host', 'port', 'unit_id', 'timeout', 'retry_attempts', 'retry_delay')
PRIMARY_SECTION_KEYS = {
    'connection': list(PRIMARY_CONNECTION_KEYS),
    'polling': ['poll_groups'],
    'identity': [],
    'outputs': ['http_output', 'opcua', 'rest_push'],
    'model': [],
}
# lock field → why (device_summary.locks serves them with the code 'primary';
# a PATCH that changes one is refused with the same words)
PRIMARY_FIXED = {
    'identity.name': "the primary device's name is fixed: it is the gateway's own meter",
    'identity.type': "the primary device's kind comes from its template",
    'template': "the primary device reads its own template (janitza_umg512_pro): it is fixed",
    'polling.enabled': ("the primary device always polls: it is the gateway's own meter "
                        "(a gateway without one says primary_device: false)"),
    'outputs.mqtt': "the primary device always publishes to MQTT, at the rhythm of Settings → MQTT",
    'outputs.influxdb': "the primary device always writes to InfluxDB, at the rhythm of Settings → InfluxDB",
    # the gateway's own switch, not the primary's: it applies to every device
    # (review 2026-11-01 #1; PUT's legacy ha_discovery_enabled still maps it)
    'outputs.ha_discovery': ("Home Assistant discovery of the primary is the gateway's own switch: "
                             "Settings → MQTT (it applies to every device)"),
}


def edit_sections(primary: bool, whole_refused: bool = False) -> Dict[str, Optional[List[str]]]:
    """What each PATCH section takes for a device: a list of keys, None for
    "any key its connection has" (a devices[] entry), {} for a device edited
    elsewhere (a component, an installation's unit — its locks say where)."""
    if whole_refused:
        return {}
    if primary:
        return {k: list(v) for k, v in PRIMARY_SECTION_KEYS.items()}
    return {'connection': None, **{k: list(v) for k, v in SECTION_KEYS.items()}}


def primary_refusals(secs: Dict[str, Dict[str, Any]], current: Dict[str, Any]) -> List[str]:
    """What a PATCH asks of the primary that does not apply to it: a key its
    connection has not, a fixed field set to another value (said again as it
    is, it is no change and passes). ``current``: {name, type, template,
    ha_discovery, connection} — ``connection`` what GET serves: a key it
    serves that the meter does not use (serial_port, url, …) sent back as
    served is no change (the GET → PATCH round trip)."""
    errs: List[str] = []
    keys = ', '.join(PRIMARY_CONNECTION_KEYS[1:])
    served = current.get('connection') or {}
    for k, v in (secs.get('connection') or {}).items():
        if k not in PRIMARY_CONNECTION_KEYS and not (k in served and v == served[k]):
            errs.append(f"connection.{k}: not part of the primary's connection — it is Modbus TCP: {keys}")
    pol = secs.get('polling') or {}
    if 'enabled' in pol and pol['enabled'] is not True:
        errs.append(f"polling.enabled: {PRIMARY_FIXED['polling.enabled']}")
    ident = secs.get('identity') or {}
    for k in ('name', 'type'):
        if k in ident and ident[k] != current.get(k):
            errs.append(f"identity.{k}: {PRIMARY_FIXED['identity.' + k]}")
    model = secs.get('model') or {}
    if 'template' in model and model['template'] != current.get('template'):
        errs.append(f"model.template: {PRIMARY_FIXED['template']}")
    out = secs.get('outputs') or {}
    if 'ha_discovery' in out and out['ha_discovery'] != current.get('ha_discovery'):
        errs.append(f"outputs.ha_discovery: {PRIMARY_FIXED['outputs.ha_discovery']}")
    for k in ('mqtt', 'influxdb'):
        if k not in out:
            continue
        v, why = out[k], PRIMARY_FIXED['outputs.' + k]
        if isinstance(v, dict):
            if 'enabled' in v and v['enabled'] is not True:
                errs.append(f"outputs.{k}: {why}")
            # its rhythm is Settings': null (= as in Settings) is what it is
            errs += [f"outputs.{k}.{x}: {why}" for x, val in v.items() if x != 'enabled' and val is not None]
        elif v is not True:
            errs.append(f"outputs.{k}: {why}")
    return errs


def primary_outputs(sec: Dict[str, Any]) -> Dict[str, Any]:
    """The outputs section without what primary_refusals judged (its MQTT and
    InfluxDB, which it always has, and the gateway's HA switch): what
    outputs_plan checks."""
    return {k: v for k, v in sec.items() if k not in ('mqtt', 'influxdb', 'ha_discovery')}


def primary_connection_keys(conn: Dict[str, Any]) -> Dict[str, Any]:
    """The keys of a connection the primary's validator reads (the others
    were judged by primary_refusals: said as GET serves them)."""
    return {k: v for k, v in conn.items() if k in PRIMARY_CONNECTION_KEYS}
