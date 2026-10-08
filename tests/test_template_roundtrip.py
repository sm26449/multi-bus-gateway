# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""A template saved from the editor must come back as it went in: the
editor posts the whole template, the server parses and re-serialises it, so
any field the model forgets to write out is silently lost on the first save
(register `aggregates` were, until 3.91)."""
import glob
import json

import pytest

from multibus.device_template import parse_template

_DEFAULTS = {'scale': 1, 'offset': 0, 'register_type': 'holding', 'writable': False,
             'monotonic': False, 'daily': False, 'access': 'RD', 'category': 'other',
             'description': '', 'label': '', 'unit': '', 'poll_group': '', 'json_path': '',
             'topic': '', 'scale_from': ''}


def _meaningful(row: dict) -> dict:
    out = {k: v for k, v in row.items() if v not in (None, {}, []) and _DEFAULTS.get(k, object()) != v}
    if not row.get('writable'):          # guards only travel with a writable register
        for k in ('write_min', 'write_max', 'write_allowed', 'write_safe'):
            out.pop(k, None)
    return out


@pytest.mark.parametrize('path', sorted(glob.glob('multibus/device_templates/*.json')))
def test_every_bundled_template_survives_a_save(path):
    src = json.load(open(path))
    t = src['device_template']
    back = parse_template(src).to_dict()['device_template']
    for i, (a, b) in enumerate(zip(t['registers'], back['registers'])):
        lost = {k: v for k, v in _meaningful(a).items() if k not in ('fc',) and b.get(k) != v
                and not (k == 'data_type' and str(v).lower() == str(b.get(k)).lower())}
        assert not lost, f"{path} register #{i} {a.get('name')}: lost on save {lost}"
    for blk in ('commands', 'display', 'identify'):
        assert back.get(blk, {}) == t.get(blk, {}), f"{path}: {blk} changed on save"
    assert len(back.get('calculated', [])) == len(t.get('calculated', []))
    for a, b in zip(t.get('calculated', []), back.get('calculated', [])):
        assert _meaningful(a) == {k: v for k, v in _meaningful(b).items() if k in _meaningful(a) or v}, \
            f"{path}: calculated {a.get('name')} changed on save"
