"""CSV register-map importer.

Turns a CSV register list — the format vendors and communities actually publish —
into canonical device-template register rows, so a new device becomes *data, not
code*. Headers are matched loosely (case-insensitive, common aliases), the
delimiter is sniffed (`,` `;` or tab), addresses accept decimal or ``0x`` hex, and
data-type aliases are normalized. Row-level problems are reported with the source
line number so the UI can flag them; unusable rows are skipped, not fatal.
"""

import csv
import io
from typing import Dict, List

# canonical field -> accepted header aliases (normalized: lowercase, no separators)
_COLUMNS = {
    'address':    ('address', 'addr', 'reg', 'register', 'offset', 'modbusaddress', 'registeraddress'),
    'name':       ('name', 'key', 'tag', 'variable', 'signal', 'point'),
    'label':      ('label', 'description', 'desc', 'parameter', 'measurement', 'title', 'quantity'),
    'unit':       ('unit', 'units', 'uom'),
    'data_type':  ('datatype', 'type', 'format', 'dtype'),
    'scale':      ('scale', 'factor', 'multiplier', 'gain'),
    'category':   ('category', 'group', 'cat'),
    'poll_group': ('pollgroup', 'poll', 'rate'),
    'access':     ('access', 'rw', 'readwrite', 'mode'),
    'json_path':  ('jsonpath', 'path', 'json'),
    'register_type': ('registertype', 'regtype', 'rtype', 'fc', 'table', 'block'),
}

_INPUT_REGISTER_ALIASES = {'input', 'inputregister', 'inputregisters', 'ir', 'fc4', '4'}

_DATA_TYPE_ALIASES = {
    'float': 'float', 'float32': 'float', 'real': 'float', 'real32': 'float', 'ieee754': 'float', 'f32': 'float',
    'double': 'double', 'float64': 'double', 'real64': 'double', 'f64': 'double',
    'int16': 'int16', 's16': 'int16', 'short': 'int16', 'int': 'int16', 'sint': 'int16',
    'uint16': 'uint16', 'u16': 'uint16', 'word': 'uint16', 'ushort': 'uint16', 'uint': 'uint16',
    'int32': 'int32', 's32': 'int32', 'long': 'int32', 'dint': 'int32', 'long32': 'int32',
    'uint32': 'uint32', 'u32': 'uint32', 'dword': 'uint32', 'udint': 'uint32', 'ulong': 'uint32',
    'int64': 'int64', 's64': 'int64', 'long64': 'int64', 'lint': 'int64',
    'uint64': 'uint64', 'u64': 'uint64', 'ulint': 'uint64',
    'string': 'string', 'str': 'string', 'char': 'string', 'ascii': 'string',
}


def _norm(h: str) -> str:
    return (h or '').strip().lower().replace(' ', '').replace('_', '').replace('-', '').replace('.', '')


def _parse_addr(s: str) -> int:
    s = str(s).strip()
    if not s:
        raise ValueError('empty')
    base = 16 if s.lower().startswith('0x') else 10
    return int(s, base)


def parse_csv(text: str, *, default_data_type: str = 'float',
              default_poll_group: str = '') -> Dict:
    """Parse CSV register-map text.

    Returns ``{registers, errors, warnings, columns}``. ``errors`` are fatal
    (header can't be understood); ``warnings`` are per-row skips/coercions.
    """
    text = (text or '').replace('\r\n', '\n').replace('\r', '\n').lstrip('﻿')
    sample = text[:4000]
    delim = ';' if sample.count(';') > sample.count(',') else (
        '\t' if sample.count('\t') > sample.count(',') else ',')
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delim)
            if any((c or '').strip() for c in r)]
    if not rows:
        return {'registers': [], 'errors': ['empty CSV'], 'warnings': [], 'columns': []}

    header = rows[0]
    colmap: Dict[str, int] = {}
    for idx, h in enumerate(header):
        nh = _norm(h)
        for canon, aliases in _COLUMNS.items():
            if nh in aliases and canon not in colmap:
                colmap[canon] = idx
                break

    errors: List[str] = []
    if 'address' not in colmap and 'json_path' not in colmap:
        errors.append("no 'address' (or 'json_path') column — header seen: " + ', '.join(header))
    if 'name' not in colmap:
        errors.append("no 'name' column — header seen: " + ', '.join(header))
    if errors:
        return {'registers': [], 'errors': errors, 'warnings': [], 'columns': list(colmap)}

    def cell(row: List[str], canon: str) -> str:
        i = colmap.get(canon)
        return (row[i].strip() if i is not None and i < len(row) else '')

    registers: List[Dict] = []
    warnings: List[str] = []
    seen = set()
    for ln, row in enumerate(rows[1:], start=2):
        name = cell(row, 'name')
        if not name:
            warnings.append(f"line {ln}: no name — skipped")
            continue
        rec: Dict = {'name': name}

        addr_s, jp = cell(row, 'address'), cell(row, 'json_path')
        if addr_s:
            try:
                a = _parse_addr(addr_s)
                if not (0 <= a <= 65535):
                    raise ValueError('out of 0..65535')
                rec['address'] = a
            except ValueError as e:
                warnings.append(f"line {ln} ({name}): bad address {addr_s!r} ({e}) — skipped")
                continue
        elif jp:
            rec['address'] = None            # HTTP-only; synthetic address assigned below
        else:
            warnings.append(f"line {ln} ({name}): no address and no json_path — skipped")
            continue

        key = (rec['address'], name)
        if rec['address'] is not None and key in seen:
            warnings.append(f"line {ln} ({name}): duplicate address+name — skipped")
            continue
        seen.add(key)

        dts = _norm(cell(row, 'data_type'))
        if not dts:
            rec['data_type'] = default_data_type
        else:
            dt = _DATA_TYPE_ALIASES.get(dts)
            if dt is None:
                warnings.append(f"line {ln} ({name}): unknown type {cell(row, 'data_type')!r} → {default_data_type}")
                dt = default_data_type
            rec['data_type'] = dt

        sc = cell(row, 'scale')
        if sc:
            try:
                f = float(sc.replace(',', '.'))
                if f == 0:
                    raise ValueError
                rec['scale'] = f
            except ValueError:
                warnings.append(f"line {ln} ({name}): bad scale {sc!r} → 1")

        for f in ('label', 'unit', 'category', 'poll_group', 'json_path'):
            v = cell(row, f)
            if v:
                rec[f] = v
        if 'poll_group' not in rec and default_poll_group:
            rec['poll_group'] = default_poll_group
        if _norm(cell(row, 'access')) in ('rw', 'readwrite', 'wr', 'w'):
            rec['access'] = 'RD/WR'
        if _norm(cell(row, 'register_type')) in _INPUT_REGISTER_ALIASES:
            rec['register_type'] = 'input'

        registers.append(rec)

    # HTTP-only rows (json_path, no address) get the next free synthetic address
    used = {r['address'] for r in registers if r.get('address') is not None}
    nxt = 0
    for r in registers:
        if r.get('address') is None:
            while nxt in used:
                nxt += 1
            r['address'] = nxt
            used.add(nxt)

    return {'registers': registers, 'errors': [], 'warnings': warnings, 'columns': list(colmap)}
