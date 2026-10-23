# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""What the page asks of its dictionaries, against what they hold.

    python tools/i18n_check.py            # the report
    python tools/i18n_check.py --strict   # exit 1 on a key used but defined nowhere

A key is "used" when the code names it literally: ``t('k', …)``, ``tn('k', …)``
(plural forms ``k.one`` / ``k.few`` / ``k.other``) or ``data-i18n*="k"`` in the
shell or in a rendered template string. A key built at run time
(``t(`x.${y}`)``) cannot be checked here and is listed by its prefix.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / 'ui'
LANGS = ('en', 'ro')
PLURALS = ('zero', 'one', 'two', 'few', 'many', 'other')

def _q(g):
    """A quoted literal, either quote; group g is the quote, g+1 the text."""
    return r"""(['"])((?:(?!\%d)[^\\\n]|\\.)*)\%d""" % (g, g)


_CALL = re.compile(r"\b(t|tn)\(\s*" + _q(2) + r"(?:\s*,\s*(?:\w+\s*,\s*)?" + _q(4) + r")?")
_DYN = re.compile(r"\b(?:t|tn)\(\s*`([^`]*)`")
_ATTR = re.compile(r'data-i18n(?:-aria|-placeholder|-title|-html)?="([^"$]+)"')
_KEY = re.compile(r'^[a-zA-Z][\w-]*(\.[\w-]+)+$')


def _sources():
    yield from sorted((UI / 'js').glob('*.js'))
    yield UI / 'templates' / 'index.html'


def scan():
    """(used: {key: [(file, line, fallback, plural)]}, dynamic: {prefix: [(file, line)]})"""
    used, dynamic = {}, {}
    for f in _sources():
        rel = f.relative_to(ROOT).as_posix()
        for n, line in enumerate(f.read_text(encoding='utf-8').splitlines(), 1):
            for m in _CALL.finditer(line):
                key = m.group(3)
                if line[m.end(3) + 1:].lstrip().startswith('+'):   # 'prefix_' + x: built at run time
                    dynamic.setdefault(key, []).append((rel, n))
                    continue
                if not _KEY.match(key):
                    continue
                used.setdefault(key, []).append((rel, n, m.group(5), m.group(1) == 'tn'))
            for m in _ATTR.finditer(line):
                if _KEY.match(m.group(1)):
                    used.setdefault(m.group(1), []).append((rel, n, None, False))
            for m in _DYN.finditer(line):
                if '${' in m.group(1):
                    dynamic.setdefault(m.group(1).split('${')[0], []).append((rel, n))
    return used, dynamic


def load():
    return {lg: json.loads((UI / 'languages' / f'{lg}.json').read_text(encoding='utf-8')) for lg in LANGS}


def _defined(key, plural, d):
    return key in d or (plural and any(f'{key}.{p}' in d for p in PLURALS))


def report():
    used, dynamic = scan()
    d = load()
    en, ro = d['en'], d['ro']
    out = {
        'used': len(used),
        'missing_en': sorted(k for k, refs in used.items() if not _defined(k, any(r[3] for r in refs), en)),
        'missing_ro': sorted(k for k, refs in used.items() if not _defined(k, any(r[3] for r in refs), ro)),
        'only_en': sorted(set(en) - set(ro)),
        # a plural form English has no use for (Romanian `few`) is RO's own,
        # as long as English has the same count's `other`
        'only_ro': sorted(k for k in set(ro) - set(en)
                          if not (k.rsplit('.', 1)[-1] in PLURALS and f"{k.rsplit('.', 1)[0]}.other" in en)),
        'empty': sorted(f'{lg}:{k}' for lg in LANGS for k, v in d[lg].items()
                        if isinstance(v, str) and not v.strip()),
        'ro_same_as_en': sorted(k for k in set(en) & set(ro) if isinstance(en[k], str)
                                and en[k] == ro[k] and re.search(r'[a-z]{4,}', en[k])),
        'fallback_differs': sorted({k for k, refs in used.items() for r in refs
                                    if r[2] and k in en and r[2].replace("\\'", "'") != en[k]}),
        'unused': sorted(k for k in en if k not in used
                         and not any(k.startswith(p) for p in dynamic if p)
                         and k.rsplit('.', 1)[0] not in used),
        'dynamic_prefixes': sorted(dynamic),
    }
    return out, used


def main():
    out, used = report()
    strict = '--strict' in sys.argv
    for name, val in out.items():
        if isinstance(val, list):
            print(f'{name}: {len(val)}')
            if '-v' in sys.argv or (name.startswith('missing') and val):
                for k in val:
                    where = used.get(k, [('', 0, None, False)])[0]
                    print(f'    {k}    ({where[0]}:{where[1]})' if where[0] else f'    {k}')
        else:
            print(f'{name}: {val}')
    if strict and (out['missing_en'] or out['missing_ro'] or out['only_en'] or out['only_ro'] or out['empty']):
        sys.exit(1)


if __name__ == '__main__':
    main()
