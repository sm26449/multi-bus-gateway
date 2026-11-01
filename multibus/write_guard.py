# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
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
"""One guard for every write to a device's stored state (ADR-0030).

``guard(fn, mode, check, tag)`` wraps a FastAPI endpoint:

- ``mode='check'`` (a write that changes the stored entry) and ``'own-rev'``
  (device state kept in its own file, with its own revision): ``check(if_match,
  kw)`` runs before the endpoint when the request sends ``If-Match`` and raises
  the 412 itself;
- ``mode='runtime'`` (an action that changes nothing stored): never checked;
- every answer — a 2xx, a refusal, a 412 — carries ``ETag: tag(kw)`` (the
  version now) when there is one, and ``MBG-If-Match: checked | ignored`` so
  a client knows whether the tag it got may replace the one it holds.

The endpoint keeps its own signature: a ``Request`` / ``Response`` it does
not declare is added as a keyword-only parameter and taken out again before
the call. The route-table walk reads ``fn._mbg_device_write``.
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import inspect
import json
import secrets
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException, Request
from fastapi.responses import Response

MODES = ('check', 'own-rev', 'runtime')

# keyed per process, as the device ETag is: a bare hash would let a viewer
# confirm a guessed value; a tag held across a restart is stale (412, re-read)
_KEY = secrets.token_bytes(32)


def rev(value: Any) -> str:
    """A quoted, keyed hash of a JSON value: the same while it does not change."""
    body = json.dumps(value, sort_keys=True, default=str, separators=(',', ':'))
    return '"' + hmac.new(_KEY, body.encode('utf-8'), hashlib.sha256).hexdigest()[:32] + '"'


def guarded(mode: str, check: Optional[Callable] = None, tag: Optional[Callable] = None,
            lock: Optional[Callable] = None) -> Callable:
    """``guard`` as a decorator: ``@guarded('own-rev', check=…, tag=…)``."""
    return lambda fn: guard(fn, mode, check=check, tag=tag, lock=lock)


def matches(if_match: str, current: Optional[str]) -> bool:
    """``If-Match`` names the current version: one of its tags (weak or
    strong), or ``*``."""
    tags = [t.strip() for t in str(if_match).split(',') if t.strip()]
    if '*' in tags:
        return True
    return current is not None and any((t[2:] if t.startswith('W/') else t) == current for t in tags)


def _maybe_await_sync(v):
    if inspect.isawaitable(v):            # pragma: no cover — a sync endpoint takes sync hooks
        raise TypeError("a sync endpoint needs sync check/tag hooks")
    return v


def guard(fn: Callable, mode: str, check: Optional[Callable] = None,
          tag: Optional[Callable] = None, lock: Optional[Callable] = None) -> Callable:
    """``lock(kw)`` → a ``threading.Lock`` (or None): held around the check,
    the write and the tag, so two writes to one device with one tag never
    both pass the check. Taken BEFORE anything the endpoint locks (the
    device-mutation lock): one order, no deadlock."""
    if mode not in MODES:
        raise ValueError(mode)
    sig = inspect.signature(fn, eval_str=True)
    params = list(sig.parameters.values())
    req = next((p.name for p in params if p.annotation is Request), None)
    resp = next((p.name for p in params if p.annotation is Response), None)
    added = []
    if req is None:
        req = '_wg_request'
        added.append(inspect.Parameter(req, inspect.Parameter.KEYWORD_ONLY, annotation=Request))
    if resp is None:
        resp = '_wg_response'
        added.append(inspect.Parameter(resp, inspect.Parameter.KEYWORD_ONLY, annotation=Response))
    own = {p.name for p in added}
    said = 'ignored' if mode == 'runtime' else 'checked'

    def _split(kw: Dict) -> tuple:
        request, response = kw.get(req), kw.get(resp)
        call = {k: v for k, v in kw.items() if k not in own}
        return request, response, call

    def _headers(t: Optional[str]) -> Dict[str, str]:
        h = {'MBG-If-Match': said}
        if t:
            h['ETag'] = t
        return h

    def _tagged(result, response, t):
        h = _headers(t)
        target = result if isinstance(result, Response) else response
        if target is not None:
            for k, v in h.items():
                target.headers[k] = v
        return result

    def _refused(e: HTTPException, t):
        e.headers = {**(getattr(e, 'headers', None) or {}), **_headers(t)}
        return e

    def _lock_of(call):
        if lock is None or mode == 'runtime':
            return None
        return lock(call)

    # the tag is a courtesy of the answer: one that cannot be computed (an id
    # that names nothing, a path refused) leaves the answer as it was
    def _stag(call):
        if tag is None:
            return None
        try:
            return _maybe_await_sync(tag(call))
        except Exception:  # noqa: BLE001
            return None

    if inspect.iscoroutinefunction(fn):
        async def _await(v):
            return (await v) if inspect.isawaitable(v) else v

        async def _atag(call):
            if tag is None:
                return None
            try:
                return await _await(tag(call))
            except Exception:  # noqa: BLE001
                return None

        async def _run(request, response, a, call):
            try:
                if check is not None and mode != 'runtime' and request is not None:
                    im = request.headers.get('if-match')
                    if im is not None:
                        await _await(check(im, call))
                result = await fn(*a, **call)
            except HTTPException as e:
                raise _refused(e, await _atag(call))
            return _tagged(result, response, await _atag(call))

        @functools.wraps(fn)
        async def wrapper(*a, **kw):
            request, response, call = _split(kw)
            lk = _lock_of(call)
            if lk is None:
                return await _run(request, response, a, call)
            import anyio
            await anyio.to_thread.run_sync(lk.acquire)     # never block the event loop
            try:
                return await _run(request, response, a, call)
            finally:
                lk.release()
    else:
        def _run(request, response, a, call):
            try:
                if check is not None and mode != 'runtime' and request is not None:
                    im = request.headers.get('if-match')
                    if im is not None:
                        _maybe_await_sync(check(im, call))
                result = fn(*a, **call)
            except HTTPException as e:
                raise _refused(e, _stag(call))
            return _tagged(result, response, _stag(call))

        @functools.wraps(fn)
        def wrapper(*a, **kw):
            request, response, call = _split(kw)
            lk = _lock_of(call)
            if lk is None:
                return _run(request, response, a, call)
            with lk:
                return _run(request, response, a, call)

    wrapper.__signature__ = sig.replace(parameters=params + added)
    wrapper._mbg_device_write = mode
    return wrapper
