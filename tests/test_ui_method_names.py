# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Every ui/js/app-*.js file adds methods to one prototype (Object.assign);
a name defined twice is silently overridden by whichever file loads last.
It happened twice: copyText (a device's Copy button copied nothing) and
_devWizWire (the device wizard stopped opening)."""
import collections
import glob
import re

_DEF = re.compile(r'^    (?:async )?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{')


def test_no_method_is_defined_twice_across_ui_files():
    seen = collections.defaultdict(list)
    for path in sorted(glob.glob('ui/js/*.js')):
        for n, line in enumerate(open(path, encoding='utf-8'), 1):
            m = _DEF.match(line)
            if m and m.group(1) not in ('if', 'for', 'while', 'switch', 'catch', 'function'):
                seen[m.group(1)].append(f"{path}:{n}")
    dups = {k: v for k, v in seen.items() if len(v) > 1}
    assert not dups, f"defined more than once (the last file loaded wins): {dups}"
