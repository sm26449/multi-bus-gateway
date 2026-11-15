# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""The applied wire (ADR-0042) follows every client that is built or dropped — FINDINGS
RECON-AW-1 / RECON-AW-2. The retarget guard compares the live config's wire to the wire the
RUNNING client was built with; that record used to be filled at boot only, so a device started
later (the ADR-0047 P2 reconcile, a hot add) had no entry and the guard was skipped for it."""
import io
import zipfile

import yaml

from multibus.core.config.models import (DeviceConfig, ModbusConfig, SourceConfig,
                                         device_wire_identity)
from multibus.devices.device_registry import DeviceRegistry
from tests.test_commands import _Inverter
from tests.test_commands_api import _app, _Facade
from tests.test_devices_api import needs_tc


def _dev(did, host="1.1.1.1", primary=False):
    return DeviceConfig(id=did, name=did, primary=primary,
                        sources=[SourceConfig(id="s", protocol="tcp",
                                              connection=ModbusConfig(host=host, port=502))])


def _registry():
    r = DeviceRegistry("p", {})
    r.register(_dev("p", primary=True), "PRIMARY")
    r.register(_dev("a"), "A")
    return r


# ── the registry owns the record ─────────────────────────────────────────────────────────

def test_boot_registration_records_the_wire():
    r = _registry()
    assert r.applied_wire == {"p": device_wire_identity(_dev("p")),
                              "a": device_wire_identity(_dev("a"))}


def test_a_device_added_with_a_client_is_recorded():
    r = _registry()
    r.add(_dev("b", host="2.2.2.2"), "B")
    assert r.applied_wire["b"] == device_wire_identity(_dev("b", host="2.2.2.2"))


def test_a_device_without_a_client_has_no_applied_wire():
    r = _registry()
    r.add(_dev("b"), None)
    r.replace("c", _dev("c"), add_if_missing=True)
    assert "b" not in r.applied_wire and "c" not in r.applied_wire


def test_replace_with_a_new_client_records_the_new_wire():
    """A rebuild (edit / restart / reconcile start) puts a client on the CURRENT wire."""
    r = _registry()
    r.replace("a", _dev("a", host="9.9.9.9"), client="A2")
    assert r.applied_wire["a"] == device_wire_identity(_dev("a", host="9.9.9.9"))
    r.replace("n", _dev("n", host="3.3.3.3"), client="N", add_if_missing=True)
    assert r.applied_wire["n"] == device_wire_identity(_dev("n", host="3.3.3.3"))


def test_replace_that_keeps_the_client_keeps_the_applied_wire():
    """A config-only swap keeps the client, so the wire it writes did not move."""
    r = _registry()
    r.replace("a", _dev("a", host="9.9.9.9"))
    assert r.applied_wire["a"] == device_wire_identity(_dev("a"))


def test_replace_that_drops_the_client_forgets_the_wire():
    r = _registry()
    r.replace("a", _dev("a"), client=None)
    assert "a" not in r.applied_wire


def test_an_in_place_reconnect_records_the_new_wire():
    """The primary keeps its client object and reconnects onto the edited target."""
    r = _registry()
    moved = _dev("p", host="9.9.9.9", primary=True)
    r.resync([moved, _dev("a")], "PRIMARY")
    assert r.applied_wire["p"] == device_wire_identity(_dev("p"))       # not yet reconnected
    r.client_rebuilt(moved)
    assert r.applied_wire["p"] == device_wire_identity(moved)


def test_remove_forgets_the_wire():
    r = _registry()
    r.remove("a")
    assert "a" not in r.applied_wire


def test_resync_keeps_kept_devices_and_forgets_dropped_ones():
    """resync re-pairs kept clients (their wire is still the OLD one — that is the retarget
    the guard must see) and drops removed devices from the pairs."""
    r = _registry()
    r.add(_dev("b"), "B")
    r.resync([_dev("p", primary=True), _dev("a", host="9.9.9.9"), _dev("new")], "PRIMARY")
    assert r.applied_wire["a"] == device_wire_identity(_dev("a"))      # still the built wire
    assert "b" not in r.applied_wire                                    # dropped
    assert "new" not in r.applied_wire                                  # paired with no client


# ── through an import, the path the ADR-0047 P2 reconcile runs on ────────────────────────

def _bundle(doc):
    b = io.BytesIO()
    with zipfile.ZipFile(b, 'w') as z:
        z.writestr('config.yaml', yaml.safe_dump(doc))
    return b.getvalue()


def _doc(cfg):
    return yaml.safe_load(cfg.config_path.read_text())


def _units(doc, units):
    doc['endpoints'][0]['groups'][0]['units'] = units
    return doc


def _retarget(doc, host='192.0.2.99'):
    for src in doc['endpoints'][0]['groups'][0]['sources']:
        if src.get('protocol') == 'tcp':
            src['host'] = host
    return doc


def _import(client, doc):
    r = client.post('/api/config/import?lock_writes=false', content=_bundle(doc))
    assert r.status_code == 200, r.text


@needs_tc
def test_the_api_reads_the_registrys_record(tmp_path):
    _cfg, app, _client = _app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    assert app.state.applied_wire is app.state.registry.applied_wire
    assert 'pv-u1' in app.state.applied_wire


@needs_tc
def test_a_device_started_by_the_reconcile_is_guarded_on_a_later_retarget(tmp_path):
    """RECON-AW-1: the reconcile starts pv-u3; a later import moves its wire and keeps its
    client. The write must be refused like a boot-time device's — not sent to the old wire."""
    cfg, app, client = _app(tmp_path, {'pv-u1': _Inverter(sf=-2)})
    cfg.save_yaml_config()
    _import(client, _units(_doc(cfg), [1, 2, 3]))
    _i, dev3, built = app.state.registry.find('pv-u3')
    assert built is not None
    assert app.state.applied_wire.get('pv-u3') == device_wire_identity(dev3)
    old_wire = _Inverter(sf=-2)                       # observe what the kept client would write
    app.state.registry._pairs[_i] = (dev3, _Facade(old_wire))
    _import(client, _retarget(_doc(cfg)))
    command = client.post('/api/devices/pv-u3/commands/power_limit', json={'value': 60})
    assert command.status_code == 403 and 'retarget' in command.json()['reason'].lower()
    assert not old_wire.writes


@needs_tc
def test_a_removed_device_readded_on_another_wire_is_not_refused_as_retargeted(tmp_path):
    """RECON-AW-2: removing a device forgets its wire, so re-adding the id elsewhere builds a
    client on the NEW wire that is recorded as such — no stale "retargeted" refusal."""
    cfg, app, client = _app(tmp_path, {'pv-u1': _Inverter(sf=-2), 'pv-u2': _Inverter(sf=-2)})
    cfg.save_yaml_config()
    full = _doc(cfg)
    _import(client, _units(_doc(cfg), [1]))
    assert app.state.registry.find('pv-u2')[1] is None
    assert 'pv-u2' not in app.state.applied_wire
    _import(client, _retarget(full))
    _i, dev2, built = app.state.registry.find('pv-u2')
    assert built is not None
    assert app.state.applied_wire.get('pv-u2') == device_wire_identity(dev2)
    fresh = _Inverter(sf=-2)
    app.state.registry._pairs[_i] = (dev2, _Facade(fresh))
    command = client.post('/api/devices/pv-u2/commands/power_limit', json={'value': 60})
    assert 'retarget' not in command.text.lower()
    assert fresh.writes, command.text
