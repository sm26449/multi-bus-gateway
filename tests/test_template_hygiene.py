# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Counter hygiene flags (monotonic, daily) come from the template even when
the device's register set on disk predates them."""
from types import SimpleNamespace

from multibus.config import SelectedRegister
from multibus.device_runtime import apply_template_hygiene


def _sel(name, address, **kw):
    return SelectedRegister(address=address, name=name, label=name, unit='Wh',
                            data_type='float', poll_group='normal', **kw)


def _tpl(*regs):
    return SimpleNamespace(registers=[SimpleNamespace(**r) for r in regs])


def test_a_template_flag_reaches_a_selection_written_before_it_existed():
    regs = [_sel('energy_today', 7), _sel('energy_lifetime', 9), _sel('power_pv', 3)]
    tpl = _tpl({'name': 'energy_today', 'address': 7, 'daily': True, 'monotonic': False},
               {'name': 'energy_lifetime', 'address': 9, 'daily': False, 'monotonic': True},
               {'name': 'power_pv', 'address': 3, 'daily': False, 'monotonic': False})
    out, changed = apply_template_hygiene(regs, tpl)
    assert out is regs and changed == ['energy_today:daily', 'energy_lifetime:monotonic']
    assert regs[0].daily and not regs[0].monotonic
    assert regs[1].monotonic and not regs[1].daily
    assert not regs[2].daily and not regs[2].monotonic


def test_matching_falls_back_to_the_address_and_never_clears_a_flag():
    regs = [_sel('e_day', 7), _sel('counter', 40093, monotonic=True)]
    tpl = _tpl({'name': 'energy_today', 'address': 7, 'daily': True, 'monotonic': False},
               {'name': 'counter', 'address': 40093, 'daily': False, 'monotonic': False})
    out, changed = apply_template_hygiene(regs, tpl)
    assert changed == ['e_day:daily'] and regs[0].daily
    assert regs[1].monotonic                      # the selection's own flag stays


def test_no_template_or_no_match_changes_nothing():
    regs = [_sel('energy_today', 7)]
    assert apply_template_hygiene(regs, None) == (regs, [])
    assert apply_template_hygiene(regs, _tpl({'name': 'other', 'address': 99, 'daily': True, 'monotonic': True})) == (regs, [])
    assert not regs[0].daily and not regs[0].monotonic
