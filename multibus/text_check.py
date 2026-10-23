# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
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
"""Characters a copy/paste brings in that a user cannot see.

A formula copied from a document or a chat arrives with a typographic minus
(−), a no-break space, a zero-width space or curly quotes. Some of them make
the formula fail with a message nobody understands. Others pass: a Cyrillic
"а" that looks like a Latin "a" names a register that does not exist, so the
formula saves and never has a value. Each check here names the character,
where it is and what to type instead, so the user fixes it before saving.
"""
from __future__ import annotations

import unicodedata
from typing import Optional

# what to type instead of what a copy/paste brings
_INSTEAD = {
    '−': '-', '‐': '-', '‑': '-', '‒': '-', '–': '-', '—': '-',
    ' ': 'a normal space', ' ': 'a normal space', ' ': 'a normal space',
    ' ': 'a normal space', '　': 'a normal space',
    '‘': "'", '’': "'", '‚': "'", '“': '"', '”': '"', '„': '"',
    '«': '"', '»': '"',
    '×': '*', '⋅': '*', '·': '*', '÷': '/', '∕': '/',
    '≤': '<=', '≥': '>=', '≠': '!=', '→': '(nothing: not an operator)',
    '（': '(', '）': ')', '，': ',', '٫': '.', '․': '.',
}
# never visible, never meant: delete them
_INVISIBLE = {'​', '‌', '‍', '⁠', '﻿', '­', '‎', '‏',
              '‪', '‫', '‬', '‭', '‮', '⁦', '⁧', '⁨', '⁩'}


def _describe(ch: str) -> str:
    name = unicodedata.name(ch, '') or 'a control character'
    return f"U+{ord(ch):04X} {name}"


def _issue(ch: str, pos: int, where: str) -> str:
    if ch in _INVISIBLE or (unicodedata.category(ch) in ('Cc', 'Cf') and ch not in '\t\n\r'):
        return (f"{where}: an invisible character at position {pos} ({_describe(ch)}) — "
                "delete it (it usually comes with a copy/paste)")
    if ch in _INSTEAD:
        return (f"{where}: '{ch}' at position {pos} ({_describe(ch)}) is not allowed — "
                f"type {_INSTEAD[ch]} instead")
    if unicodedata.category(ch).startswith('L'):
        return (f"{where}: '{ch}' at position {pos} ({_describe(ch)}) is not a plain letter — "
                "names in a formula use a–z, 0–9 and _ (a lookalike from another alphabet "
                "or a diacritic names a value that does not exist)")
    return f"{where}: '{ch}' at position {pos} ({_describe(ch)}) is not allowed here"


def formula_issue(text: str, where: str = 'formula') -> Optional[str]:
    """The first character a formula must not carry, said plainly; None when
    every character is printable ASCII (tabs and line breaks count as spaces)."""
    for i, ch in enumerate(text or '', 1):
        if ch in '\t\n\r' or ' ' <= ch <= '~':
            continue
        return _issue(ch, i, where)
    return None


def topic_issue(text: str, where: str = 'topic') -> Optional[str]:
    """An MQTT topic may carry any printable text (a zigbee2mqtt name with a
    space or a diacritic is real), but never an invisible character, a
    look-alike space, or a space at either end — those subscribe to another
    topic, and nothing ever arrives."""
    t = text or ''
    for i, ch in enumerate(t, 1):
        if ch in _INVISIBLE or unicodedata.category(ch) in ('Cc', 'Cf', 'Zl', 'Zp') \
                or (unicodedata.category(ch) == 'Zs' and ch != ' '):
            return _issue(ch, i, where)
    if t != t.strip(' '):
        return f"{where}: it starts or ends with a space — remove it"
    return None
