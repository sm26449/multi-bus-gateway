# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Active power limit on a SunSpec inverter (model 123, Immediate Controls).

The gateway does not decide WHEN to limit — that is a controller's policy
(over-voltage protection, a tariff schedule). It owns HOW a limit is applied
safely on this datalogger, which is not trivial:

- the DataManager is documented to hand back buffer garbage: verify the model
  id (123) before trusting anything, and the scale factor against the values
  Fronius actually uses ({-2, -1, 0}) and the last good one seen;
- the five registers WMaxLimPct … WMaxLim_Ena must land in ONE write (FC16),
  or the inverter applies a half-written command;
- restoring to 100 % must also clear WMaxLim_Ena, or the inverter stays
  throttled at "100 %";
- read back after a settle, and say plainly whether the limit took.

Pure over a connection object with ``read_registers(addr, count)`` and
``write(addr, register_type='holding', values=[...])`` — the API layer picks
the connection, the audit and the lease; this module is testable with a fake.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, Optional

MODEL_BASE = 40227          # model 123 header on a Fronius (int+SF map), 0-based —
                            # verified live: [123, 24, …, Conn, WMaxLimPct, …, SF] starts here
MODEL_LEN = 26              # header (2) + 24 data registers
OFF_CONN = 4                # Conn
OFF_WMAX = 5                # WMaxLimPct
OFF_ENA = 9                 # WMaxLim_Ena
OFF_SF = 23                 # WMaxLimPct_SF
VALID_SF = (-2, -1, 0)      # the only scale factors Fronius ships
WRITE_BASE = MODEL_BASE + OFF_WMAX   # 40232: [WMaxLimPct, WinTms, RvrtTms, RmpTms, WMaxLim_Ena]
RAW_NAN = 0xFFFF

REVERT_DEFAULT_S = 600      # the inverter drops the limit on its own after this
                            # unless the controller re-issues it — a dead
                            # controller must never leave a plant throttled


def _signed(v: int) -> int:
    return v - 65536 if v >= 32768 else v


def read_controls(conn) -> Optional[Dict]:
    """The inverter's current model-123 state, or None when the read is not
    trustworthy (no answer, or the header is not model 123)."""
    regs = conn.read_registers(MODEL_BASE, MODEL_LEN)
    if not regs or len(regs) < MODEL_LEN or regs[0] != 123:
        return None
    sf = _signed(regs[OFF_SF])
    raw = regs[OFF_WMAX]
    return {
        'sf': sf,
        'limit_pct': None if raw == RAW_NAN else raw * (10 ** sf),
        'enabled': regs[OFF_ENA] == 1,
        'connected': regs[OFF_CONN] == 1,
        'revert_s': regs[OFF_WMAX + 2],
        'ramp_s': regs[OFF_WMAX + 3],
    }


def apply_power_limit(conn, limit_pct: float, *, revert_s: int = REVERT_DEFAULT_S,
                      ramp_s: int = 0, last_good_sf: Optional[int] = None,
                      settle_s: float = 1.0, sleep: Callable[[float], None] = time.sleep) -> Dict:
    """Apply ``limit_pct`` (0..100) and report what happened.

    Returns a dict with ``status`` — ``success`` (read back within 1 %),
    ``mismatch`` (the inverter holds a different limit), ``unverified`` (the
    write went out, the read-back did not answer), ``rejected`` (refused
    before touching the wire) or ``error`` (the write failed) — plus
    ``before_pct``/``after_pct``, ``enabled``, ``sf``, ``ms`` and ``reason``.
    """
    t0 = time.monotonic()
    out: Dict = {'status': 'rejected', 'limit_pct': limit_pct,
                 'revert_s': revert_s, 'ramp_s': ramp_s}
    try:
        limit_pct = float(limit_pct)
        revert_s, ramp_s = int(revert_s), int(ramp_s)
    except (TypeError, ValueError):
        out['reason'] = 'limit_pct must be a number, revert_s and ramp_s whole seconds'
        return out
    out.update(limit_pct=limit_pct, revert_s=revert_s, ramp_s=ramp_s)
    if not (0.0 <= limit_pct <= 100.0):
        out['reason'] = f'limit {limit_pct} % outside 0..100'
        return out
    if not (0 <= int(revert_s) <= 65535 and 0 <= int(ramp_s) <= 65535):
        out['reason'] = 'revert_s and ramp_s must be 0..65535'
        return out

    before = read_controls(conn)
    if before is None:
        # one more try: the datalogger answers garbage now and then
        before = read_controls(conn)
    if before is None:
        out.update(status='error', reason='model 123 did not answer or the header is not 123 — nothing written')
        out['ms'] = round((time.monotonic() - t0) * 1000, 1)
        return out
    sf = before['sf']
    if sf not in VALID_SF or (last_good_sf is not None and sf != last_good_sf):
        out.update(status='rejected', sf=sf,
                   reason=f'implausible WMaxLimPct_SF={sf} (valid {list(VALID_SF)}, last good {last_good_sf}) '
                          '— likely datalogger buffer corruption, nothing written')
        out['ms'] = round((time.monotonic() - t0) * 1000, 1)
        return out
    out.update(sf=sf, before_pct=before['limit_pct'], before_enabled=before['enabled'])

    raw = int(round(limit_pct / (10 ** sf)))
    enable = 0 if limit_pct >= 100.0 else 1
    words = [raw, 0, int(revert_s), int(ramp_s), enable]
    out['written'] = words
    ok, err = conn.write(WRITE_BASE, register_type='holding', values=words)
    if not ok:
        out.update(status='error', reason=f'write failed: {err}')
        out['ms'] = round((time.monotonic() - t0) * 1000, 1)
        return out

    sleep(settle_s)
    after = read_controls(conn)
    if after is None:
        out.update(status='unverified', reason='written, but the read-back did not answer')
    else:
        out.update(after_pct=after['limit_pct'], enabled=after['enabled'])
        if after['limit_pct'] is not None and abs(after['limit_pct'] - limit_pct) < 1.0 \
                and after['enabled'] == bool(enable):
            out['status'] = 'success'
        else:
            out.update(status='mismatch',
                       reason=f'expected ~{limit_pct} % (enabled={bool(enable)}), '
                              f'read back {after["limit_pct"]} % (enabled={after["enabled"]})')
    out['ms'] = round((time.monotonic() - t0) * 1000, 1)
    return out


def parse_command(payload) -> Dict:
    """A command as it arrives over MQTT: a bare number, or JSON with
    ``limit_pct`` and optional ``revert_s`` / ``ramp_s`` / ``source`` — the
    legacy collector's ``revert_timeout`` / ``ramp_time`` are accepted too, so
    an existing controller only re-points its topics. Raises ValueError."""
    import json
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode('utf-8', 'replace')
    if isinstance(payload, str):
        s = payload.strip()
        if not s:
            raise ValueError('empty command')
        try:
            data = json.loads(s)
        except ValueError:
            data = s
    else:
        data = payload
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        return {'limit_pct': float(data), 'revert_s': REVERT_DEFAULT_S, 'ramp_s': 0, 'source': 'mqtt'}
    if isinstance(data, str):
        try:
            return {'limit_pct': float(data), 'revert_s': REVERT_DEFAULT_S, 'ramp_s': 0, 'source': 'mqtt'}
        except ValueError:
            raise ValueError(f'not a number or a command object: {data[:40]!r}')
    if not isinstance(data, dict):
        raise ValueError('command must be a number or an object')
    if 'limit_pct' not in data:
        raise ValueError('limit_pct is required')
    try:
        limit = float(data['limit_pct'])
        revert = int(data.get('revert_s', data.get('revert_timeout', REVERT_DEFAULT_S)) or 0)
        ramp = int(data.get('ramp_s', data.get('ramp_time', 0)) or 0)
    except (TypeError, ValueError) as e:
        raise ValueError(f'invalid command parameter: {e}')
    return {'limit_pct': limit, 'revert_s': revert, 'ramp_s': ramp,
            'source': str(data.get('source') or 'mqtt')[:64]}
