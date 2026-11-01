# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Every device write carries the ETag (ADR-0030).

Every route that changes a device's stored entry checks `If-Match` when it is
sent (a stale one: 412 {errors, current, etag}, nothing changed) and answers
with the tag after the write. Runtime actions never answer 412 but say the
current tag too. A route-table walk keeps a new device write from slipping
past the rule. A device's register selection and its Energy fields live in
their own files and carry their own revision, as attributes do."""
import pathlib
import re

from fastapi.routing import APIRoute

from tests.test_device_components import PORTAL
from tests.test_devices_api import make_app, needs_tc

SETUP = f"""
devices:
  - id: inv1
    name: Inverter
    template: eastron_sdm630
    enabled: false
    connection: {{protocol: tcp, host: 192.0.2.80, unit_id: 1}}
  - id: em2
    name: Meter
    template: eastron_sdm120
    enabled: false
    connection: {{protocol: tcp, host: 192.0.2.81, unit_id: 2}}
  - id: gx
    name: Victron GX
    template: victron_gx_mqtt
    enabled: false
    connection: {{protocol: mqtt, broker: 192.0.2.10, port: 8883, tls: true, username: u, topic: 'N/{PORTAL}/#',
                  password: secret, vars: {{portal: {PORTAL}, vebus: '274'}}}}
    mqtt: {{topic_prefix: mbg/devices/gx, ha_discovery: false}}
    components:
      - {{type: battery, instance: 288, name: SmartShunt}}
"""

MUTATING = {'POST', 'PUT', 'PATCH', 'DELETE'}
DEVICE_PATH = re.compile(r'^/api/devices/\{[^}]+\}(/|$)')

# Runtime actions: they never change the stored entry, so If-Match is not
# checked — but the answer says the current tag (ADR-0030 §1).
EXEMPT = {
    ('POST', '/api/devices/{device_id}/write'),
    ('POST', '/api/devices/{device_id}/commands/{name}'),
    ('POST', '/api/devices/{device_id}/commands/{name}/dry-run'),
    ('POST', '/api/devices/{device_id}/actions/power_limit'),
    ('POST', '/api/devices/{device_id}/test'),
    ('POST', '/api/devices/{device_id}/payload-sample'),
    ('POST', '/api/devices/{device_id}/rest-push/test'),
    ('POST', '/api/devices/{device_id}/calculated/test'),
    ('POST', '/api/devices/{device_id}/components/discover'),
    ('POST', '/api/devices/{device_id}/preview'),
}

# Device state kept outside the entry, named by a `device` query parameter:
# its own revision (as attributes have theirs).
OWN_REV = {
    ('POST', '/api/registers/selected'),
    ('POST', '/api/energy/fields'),
}

# The device's entry, written by a route that names the device by its
# `device` query parameter: checked against the device's own ETag.
QUERY_CHECKED = {
    ('POST', '/api/pq/config'),
}

# Mutating routes that take a `device` parameter but change no device state.
NOT_DEVICE_STATE = {
    ('DELETE', '/api/devices/restorable/{device_id}'),   # a deleted device's kept settings: no entry, no tag
}


def _mark(route):
    return getattr(route.endpoint, '_mbg_device_write', None)


def _flat(routes):
    """Every APIRoute, the ones of included routers too (FastAPI keeps an
    included router as one entry; this app includes them without a prefix)."""
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        elif hasattr(r, 'original_router'):
            yield from _flat(r.original_router.routes)
        elif hasattr(r, 'routes') and not hasattr(r, 'app'):
            yield from _flat(r.routes)


def _routes(app):
    for r in _flat(app.routes):
        for m in sorted(r.methods & MUTATING):
            yield m, r


def _takes_device(route):
    d = route.dependant
    names = {p.name for p in d.query_params} | {p.name for p in d.body_params} | {p.name for p in d.path_params}
    return bool(names & {'device', 'device_id', 'id'})


def _url(path, **over):
    vals = {'device_id': 'inv1', 'name': 'x', 'key': 'x', 'component_id': 'x', **over}
    return re.sub(r'\{(\w+)\}', lambda m: vals[m.group(1)], path)


def _etag(client, did):
    r = client.get(f"/api/devices/{did}")
    assert r.status_code == 200, r.text
    return r.headers['etag']


def _send(client, method, url, body=None, tag=None):
    headers = {'If-Match': tag} if tag else {}
    if method == 'DELETE':
        return client.request('DELETE', url, headers=headers)
    return client.request(method, url, json={} if body is None else body, headers=headers)


# ── the route walk ──────────────────────────────────────────────────────────

@needs_tc
def test_every_device_write_is_guarded_or_listed_as_runtime(tmp_path):
    _cfg, client = make_app(tmp_path, extra_yaml=SETUP)
    missing, runtime, checked = [], set(), set()
    for m, r in _routes(client.app):
        if not DEVICE_PATH.match(r.path):
            continue
        mark = _mark(r)
        if mark == 'check':
            checked.add((m, r.path))
        elif mark == 'runtime':
            runtime.add((m, r.path))
        else:
            missing.append(f"{m} {r.path}")
    assert not missing, f"a device write without @_device_write (or listed as runtime): {missing}"
    assert runtime == EXEMPT, f"runtime routes changed: +{runtime - EXEMPT} -{EXEMPT - runtime}"
    assert ('PUT', '/api/devices/{device_id}') in checked and ('DELETE', '/api/devices/{device_id}') in checked
    assert len(checked) >= 20, sorted(checked)


@needs_tc
def test_writes_naming_a_device_by_parameter_are_guarded_too(tmp_path):
    """Device state kept outside /api/devices/{id}: the selection, the Energy
    fields. Each mutating route that names a device as a parameter has its
    own revision, or is listed as changing no device state."""
    _cfg, client = make_app(tmp_path, extra_yaml=SETUP)
    found = set()
    for m, r in _routes(client.app):
        if DEVICE_PATH.match(r.path) or not _takes_device(r):
            continue
        found.add((m, r.path))
        if (m, r.path) in NOT_DEVICE_STATE:
            continue
        want = 'check' if (m, r.path) in QUERY_CHECKED else 'own-rev'
        assert _mark(r) == want, f"{m} {r.path} names a device but carries no revision ({_mark(r)})"
    assert found - NOT_DEVICE_STATE == OWN_REV | QUERY_CHECKED, sorted(found)


@needs_tc
def test_the_pq_recorder_is_checked_against_the_device_tag(tmp_path):
    _cfg, client = make_app(tmp_path, extra_yaml=SETUP)
    tag = _etag(client, 'inv1')
    res = client.post('/api/pq/config?device=inv1', json={'enabled': False}, headers={'If-Match': '"stale"'})
    assert res.status_code == 412 and res.headers.get('etag') == tag
    res = client.post('/api/pq/config?device=inv1', json={'enabled': False}, headers={'If-Match': tag})
    assert res.status_code == 200, res.text
    assert res.headers.get('etag') == _etag(client, 'inv1')


# ── a stale If-Match: 412 everywhere it is checked ──────────────────────────

@needs_tc
def test_every_checked_route_refuses_a_stale_if_match(tmp_path):
    cfg, client = make_app(tmp_path, extra_yaml=SETUP)
    path = pathlib.Path(cfg.config_path)
    cur = _etag(client, 'inv1')
    n = 0
    for m, r in _routes(client.app):
        if not DEVICE_PATH.match(r.path) or _mark(r) != 'check':
            continue
        before = path.read_bytes()
        res = _send(client, m, _url(r.path), tag='"stale"')
        assert res.status_code == 412, f"{m} {r.path}: {res.status_code} {res.text[:200]}"
        d = res.json()['detail']
        assert d['errors'] and d['current']['id'] == 'inv1' and d['etag'] == cur, (m, r.path)
        assert res.headers.get('etag') == cur, (m, r.path)
        assert path.read_bytes() == before, f"{m} {r.path} changed the file on a 412"
        n += 1
    assert n >= 20


@needs_tc
def test_runtime_routes_never_answer_412_and_say_the_tag(tmp_path):
    _cfg, client = make_app(tmp_path, extra_yaml=SETUP)
    cur = _etag(client, 'inv1')
    for m, p in sorted(EXEMPT):
        res = _send(client, m, _url(p), tag='"stale"')
        assert res.status_code != 412, f"{m} {p}: 412"
        assert res.headers.get('etag') == cur, f"{m} {p}: {res.status_code} etag={res.headers.get('etag')}"
        assert res.headers.get('mbg-if-match') == 'ignored', (m, p)


# ── a current If-Match goes through and hands back the new tag ──────────────

GOOD = [
    # (method, path, body, device the tag belongs to)
    ('PUT', '/api/devices/inv1', {'id': 'inv1', 'name': 'Renamed'}, 'inv1'),
    ('POST', '/api/devices/inv1/write-lock', {'locked': False}, 'inv1'),
    ('POST', '/api/devices/inv1/publishing', {'mqtt': {'publish_mode': 'all'}}, 'inv1'),
    ('POST', '/api/devices/inv1/outputs', {'mqtt': False}, 'inv1'),
    ('POST', '/api/devices/inv1/http-output', {'enabled': True}, 'inv1'),
    ('POST', '/api/devices/inv1/opcua-output', {'enabled': False}, 'inv1'),
    ('POST', '/api/devices/inv1/rest-push', {'enabled': False, 'url': ''}, 'inv1'),
    ('PATCH', '/api/devices/inv1/organise', {'type': 'meter'}, 'inv1'),
    ('POST', '/api/devices/inv1/poll-groups', {'poll_groups': {'normal': 7}}, 'inv1'),
    ('PUT', '/api/devices/inv1/meters',
     {'meters': []}, 'inv1'),
    ('PUT', '/api/devices/inv1/energy-meter', {}, 'inv1'),
    ('POST', '/api/devices/inv1/calculated', {'calculated': [{'name': 'twice', 'expr': '2 * 2'}]}, 'inv1'),
    ('POST', '/api/devices/inv1/refresh-from-template', None, 'inv1'),
    ('PUT', '/api/devices/gx/commands/ess_setpoint/enabled', {'enabled': True}, 'gx'),
    ('PUT', '/api/devices/gx/commands/ess_setpoint', {'lease_s': 0}, 'gx'),
    ('PUT', '/api/devices/gx/channels/ess_setpoint', {'min_interval_s': 30}, 'gx'),
    ('POST', '/api/devices/gx/components', {'type': 'temperature', 'instance': '23', 'name': 'Out'}, 'gx'),
    ('PATCH', '/api/devices/gx/components/gx-battery-288', {'name': 'Shunt'}, 'gx'),
    ('DELETE', '/api/devices/gx/components/gx-temperature-23', None, 'gx'),
]


@needs_tc
def test_a_current_if_match_goes_through_and_answers_the_new_tag(tmp_path):
    _cfg, client = make_app(tmp_path, extra_yaml=SETUP)
    for m, url, body, did in GOOD:
        tag = _etag(client, did)
        res = _send(client, m, url, body, tag=tag)
        assert 200 <= res.status_code < 300, f"{m} {url}: {res.status_code} {res.text[:300]}"
        now = _etag(client, did)
        assert res.headers.get('etag') == now, f"{m} {url}: answered {res.headers.get('etag')}, stored {now}"
        assert res.headers.get('mbg-if-match') == 'checked', (m, url)
    # the device page's chain: every write above, each with the tag the
    # previous answer handed back — never a false 412
    tag = _etag(client, 'inv1')
    for m, url, body, did in GOOD:
        if did != 'inv1':
            continue
        res = _send(client, m, url, body, tag=tag)
        assert res.status_code < 300, f"chained {m} {url}: {res.status_code} {res.text[:300]}"
        tag = res.headers['etag']
    assert tag == _etag(client, 'inv1')


@needs_tc
def test_a_change_elsewhere_is_caught_by_every_kind_of_write(tmp_path):
    """Tab A renames; tab B, still holding the old tag, changes anything."""
    _cfg, client = make_app(tmp_path, extra_yaml=SETUP)
    old = _etag(client, 'inv1')
    assert client.put('/api/devices/inv1', json={'id': 'inv1', 'name': 'Tab A'}).status_code == 200
    for m, url, body, did in GOOD:
        if did != 'inv1':
            continue
        res = _send(client, m, url, body, tag=old)
        assert res.status_code == 412, f"{m} {url}: {res.status_code}"
        assert res.json()['detail']['current']['name'] == 'Tab A'


@needs_tc
def test_without_if_match_nothing_changes(tmp_path):
    _cfg, client = make_app(tmp_path, extra_yaml=SETUP)
    for m, url, body, _did in GOOD:
        res = _send(client, m, url, body)
        assert res.status_code < 300, f"{m} {url}: {res.status_code} {res.text[:300]}"
        assert res.headers.get('etag'), (m, url)


@needs_tc
def test_delete_and_restore_carry_the_tag(tmp_path):
    _cfg, client = make_app(tmp_path, extra_yaml=SETUP)
    assert client.delete('/api/devices/em2', headers={'If-Match': '"stale"'}).status_code == 412
    res = client.delete('/api/devices/em2', headers={'If-Match': _etag(client, 'em2')})
    assert res.status_code == 200, res.text
    assert 'etag' not in res.headers                  # nothing left to name
    res = client.post('/api/devices/em2/restore')
    assert res.status_code == 200, res.text
    assert res.headers.get('etag') == _etag(client, 'em2')


@needs_tc
def test_a_refusal_says_the_current_tag(tmp_path):
    _cfg, client = make_app(tmp_path, extra_yaml=SETUP)
    tag = _etag(client, 'inv1')
    res = client.post('/api/devices/inv1/publishing', json={'mqtt': {'publish_mode': 'sometimes'}},
                      headers={'If-Match': tag})
    assert res.status_code == 422 and res.headers.get('etag') == tag


# ── own revisions: the selection and the Energy fields ──────────────────────

def _rows(client, qs):
    r = client.get('/api/registers/selected' + qs)
    assert r.status_code == 200, r.text
    rows = [x for x in r.json()['registers'] if x["address"] < 8_000_000]
    return rows, r.headers.get('etag')


@needs_tc
def test_the_selection_has_its_own_revision(tmp_path):
    _cfg, client = make_app(tmp_path, extra_yaml=SETUP)
    qs = '?device=inv1'
    rows, tag = _rows(client, qs)
    assert tag and _rows(client, qs)[1] == tag              # stable while nothing changes
    dev_tag = _etag(client, 'inv1')
    one = rows[:1] or [{'address': 0, 'name': 'v', 'label': 'V', 'data_type': 'float'}]
    res = client.post('/api/registers/selected' + qs, json=one, headers={'If-Match': tag})
    assert res.status_code == 200, res.text
    new = res.headers.get('etag')
    assert new and new == _rows(client, qs)[1]
    assert _etag(client, 'inv1') == dev_tag                 # the entry's tag is not the selection's
    stale = client.post('/api/registers/selected' + qs, json=[], headers={'If-Match': tag})
    assert stale.status_code == 412, stale.text
    d = stale.json()['detail']
    assert d['errors'] and d['etag'] == new and len(d['current']['registers']) == len(one)
    assert len(_rows(client, qs)[0]) == len(one)            # nothing changed
    # no header: as before
    assert client.post('/api/registers/selected' + qs, json=one).status_code == 200


@needs_tc
def test_the_energy_fields_have_their_own_revision(tmp_path):
    _cfg, client = make_app(tmp_path, extra_yaml=SETUP)
    r = client.get('/api/energy/fields?device=inv1')
    tag = r.headers.get('etag')
    assert r.status_code == 200 and tag
    body = {'fields': [{'name': 'energy_active_import', 'label': 'Import', 'unit': 'kWh'}]}
    res = client.post('/api/energy/fields?device=inv1', json=body, headers={'If-Match': tag})
    assert res.status_code == 200 and res.headers.get('etag') != tag
    assert res.headers['etag'] == client.get('/api/energy/fields?device=inv1').headers['etag']
    stale = client.post('/api/energy/fields?device=inv1', json={'fields': []}, headers={'If-Match': tag})
    assert stale.status_code == 412 and stale.json()['detail']['current']['selected'][0]['name'] == 'energy_active_import'
    assert client.post('/api/energy/fields?device=inv1', json={'fields': []}).status_code == 200


# ── two writes with one tag at once: one goes through, one is refused ──────

def _slow_reads(cfg, monkeypatch, seconds=0.3):
    """Every device lookup takes a while: the window between a write's check
    and its change, where a second write with the same tag used to pass."""
    import time
    real = cfg.get_device

    def slow(*a, **kw):
        time.sleep(seconds)
        return real(*a, **kw)
    monkeypatch.setattr(cfg, 'get_device', slow)


def _together(*calls):
    import threading
    out = [None] * len(calls)

    def run(i, fn):
        out[i] = fn()
    ts = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(calls)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(20)
    return out


@needs_tc
def test_two_writes_with_one_tag_at_once_never_both_pass(tmp_path, monkeypatch):
    """Check, write and tag run under the entry's lock: the second write
    with the same tag sees the first one's change (MINOR 3)."""
    cfg, client = make_app(tmp_path, extra_yaml=SETUP)
    tag = _etag(client, 'inv1')
    _slow_reads(cfg, monkeypatch)
    a, b = _together(
        lambda: client.post('/api/devices/inv1/outputs', json={'mqtt': False}, headers={'If-Match': tag}),
        lambda: client.post('/api/devices/inv1/http-output', json={'enabled': True}, headers={'If-Match': tag}))
    assert sorted([a.status_code, b.status_code]) == [200, 412], (a.text, b.text)


@needs_tc
def test_a_component_removed_twice_at_once_with_one_tag(tmp_path, monkeypatch):
    cfg, client = make_app(tmp_path, extra_yaml=SETUP)
    assert client.post('/api/devices/gx/components', json={'type': 'temperature', 'instance': '23'}).status_code == 200
    tag = _etag(client, 'gx')
    _slow_reads(cfg, monkeypatch)
    a, b = _together(
        lambda: client.patch('/api/devices/gx/components/gx-battery-288', json={'name': 'A'}, headers={'If-Match': tag}),
        lambda: client.delete('/api/devices/gx/components/gx-temperature-23', headers={'If-Match': tag}))
    assert sorted([a.status_code, b.status_code]) == [200, 412], (a.text, b.text)


@needs_tc
def test_the_checked_routes_take_the_entry_lock_before_the_mutation_lock(tmp_path):
    """One lock order: the guard (entry lock) sits above @_serialized_mutation,
    so no route takes them the other way round (a deadlock)."""
    import inspect
    from multibus import api
    src = inspect.getsource(api)
    assert '@_serialized_mutation\n    @_device_write' not in src
