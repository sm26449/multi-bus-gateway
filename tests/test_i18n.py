# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
"""Every word the page shows comes from the dictionaries: a key
the code asks for is defined in English and in Romanian, the two hold the
same keys (Romanian's own plural forms aside), and nothing is empty."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    'i18n_check', Path(__file__).resolve().parent.parent / 'tools' / 'i18n_check.py')
i18n = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(i18n)
REPORT, USED = i18n.report()


def _where(keys):
    return [f"{k} ({USED[k][0][0]}:{USED[k][0][1]})" if k in USED else k for k in keys]


def test_every_key_the_page_asks_for_is_in_english():
    assert REPORT['missing_en'] == [], _where(REPORT['missing_en'])


def test_every_key_the_page_asks_for_is_in_romanian():
    assert REPORT['missing_ro'] == [], _where(REPORT['missing_ro'])


def test_both_dictionaries_hold_the_same_keys():
    assert REPORT['only_en'] == [] and REPORT['only_ro'] == [], (REPORT['only_en'], REPORT['only_ro'])


def test_no_value_is_empty():
    assert REPORT['empty'] == []


def test_the_check_sees_the_code():
    # a broken scan would pass everything above
    assert REPORT['used'] > 2000 and 'devices.tab.logs' in USED and 'pq.events' in USED


def test_a_value_is_never_written_into_a_fallback():
    """`t('k', `…${x}…`)` loses x as soon as the key exists: values go in as params."""
    import re
    pat = re.compile(r"\btn?\(\s*'[a-zA-Z][\w.-]+'\s*,\s*`([^`]*)`", re.S)
    bad = []
    for f in sorted((i18n.UI / 'js').glob('*.js')):
        src = f.read_text(encoding='utf-8')
        bad += [f"{f.name}:{src.count(chr(10), 0, m.start()) + 1}" for m in pat.finditer(src) if '${' in m.group(1)]
    assert bad == []


def test_a_text_set_as_plain_text_carries_no_markup():
    """`data-i18n` sets textContent: a <b> in its value is shown as "<b>".
    A marked-up text uses `data-i18n-html`, limited to a few tags."""
    import re
    shell = (i18n.UI / 'templates' / 'index.html').read_text(encoding='utf-8')
    plain = set(re.findall(r'data-i18n="([^"]+)"', shell))
    marked = set(re.findall(r'data-i18n-html="([^"]+)"', shell))
    d = i18n.load()
    for lang in i18n.LANGS:
        for k in plain:
            assert not re.search(r'</?(b|strong|em|code|br|i|span|a|p|div|small)\b', d[lang].get(k, '')), (lang, k, d[lang].get(k, '')[:80])
        for k in marked:
            assert set(re.findall(r'</?([a-zA-Z][\w-]*)', d[lang].get(k, ''))) <= {'b', 'strong', 'em', 'code', 'br'}, (lang, k)
