# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The gateway's role: active unless set from outside, an epoch that only
grows, kept across a restart."""
import json

import pytest

from multibus.role import GatewayRole, StandbyRefused


def test_a_gateway_is_active_until_told_otherwise(tmp_path):
    r = GatewayRole(str(tmp_path / 'role.json'))
    assert r.active and r.status()['epoch'] == 0
    r.check('a write')                                    # nothing refused
    assert not (tmp_path / 'role.json').exists()          # nothing written either


def test_standby_refuses_and_is_kept_across_a_restart(tmp_path):
    p = str(tmp_path / 'role.json')
    r = GatewayRole(p)
    r.set('standby', 1, by='mbg-ha', reason='partner active')
    with pytest.raises(StandbyRefused, match='standby gateway'):
        r.check('a write')
    again = GatewayRole(p)
    assert again.role == 'standby' and again.epoch == 1 and again.set_by == 'mbg-ha'


def test_the_epoch_only_grows(tmp_path):
    r = GatewayRole(str(tmp_path / 'role.json'))
    r.set('standby', 5, by='ha')
    with pytest.raises(ValueError, match='older'):
        r.set('active', 4, by='a controller that fell behind')
    with pytest.raises(ValueError, match='new epoch'):
        r.set('active', 5, by='ha')                       # a change needs a new epoch
    assert r.set('standby', 5, by='ha')['role'] == 'standby'   # the same, again: fine
    assert r.set('active', 6, by='ha')['role'] == 'active'
    for bad in (('master', 7), ('active', -1), ('active', 7.5), ('active', True)):
        with pytest.raises(ValueError):
            r.set(*bad, by='ha')


def test_listeners_hear_a_change_only(tmp_path):
    r = GatewayRole(str(tmp_path / 'role.json'))
    heard = []
    r.on_change(lambda old, new: heard.append((old, new)))
    r.on_change(lambda old, new: 1 / 0)                   # a broken listener stops no one
    r.set('standby', 1, by='ha')
    r.set('standby', 2, by='ha')                          # a new epoch, the same role
    r.set('active', 3, by='ha')
    assert heard == [('active', 'standby'), ('standby', 'active')]


def test_an_unreadable_role_file_starts_quiet(tmp_path):
    p = tmp_path / 'role.json'
    p.write_text('{not json')
    assert GatewayRole(str(p)).role == 'standby'
    p.write_text(json.dumps({'role': 'active', 'epoch': 3, 'since': 1.0, 'set_by': 'ha'}))
    assert GatewayRole(str(p)).status()['epoch'] == 3
