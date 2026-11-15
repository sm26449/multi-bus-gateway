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
"""Domain exceptions for the API, and the one handler that maps them to HTTP.

ADR-0038 #4. A **service** owns the logic and raises a *domain* exception from
this module; it names no FastAPI/Starlette type. A **router** calls the service
and lets the exception propagate; the single handler registered by
``register_error_handlers`` turns each one into an HTTP response.

The map is built to be a drop-in for the hand-written ``HTTPException`` raises
that live inside ``create_api`` today, so that moving a
``raise HTTPException(status_code=422, detail=X)`` in a route to a
``raise Validation(X)`` in a service is **byte-identical on the wire**:

- FastAPI renders ``HTTPException(status_code=s, detail=d)`` as
  ``JSONResponse(status_code=s, content={"detail": d}, headers=h)``. The handler
  below produces exactly that.
- ``detail`` is whatever the call site passed — a ``str`` (e.g. ``"device not
  found"``) or a ``dict`` (e.g. ``{"errors": [...], "fields": {...}}``). The
  exception carries it verbatim; the handler wraps it in ``{"detail": ...}``.
- ``headers`` passes through for the few responses that set them (e.g. the 429
  ``Retry-After`` on a busy bus).

The status is fixed by the subclass, so a service says *what kind* of error it
is (``NotFound``, ``Validation``, …) and never repeats a bare status code. The
subclasses cover every status the API raises today: 400, 401, 403, 404, 409,
411, 412, 413, 422, 428, 429, 500, 503.

NOTE: ``core.config.ConfigSaveBlocked`` keeps its own dedicated 409 handler
(a config that failed to load refuses every save in words, QA-D-11); it is not
an ``ApiError`` and services do not raise ``Conflict`` for that case.
"""
from __future__ import annotations

from typing import Optional, Union

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

#: An HTTP ``detail`` payload — a message string or a structured error body.
Detail = Union[str, dict]


class ApiError(Exception):
    """Base of every API domain error.

    Carries the HTTP status (fixed by the subclass) and the ``detail`` payload
    that the handler places under ``{"detail": ...}``. A service raises one of
    the subclasses; it never imports a FastAPI type.
    """

    #: The HTTP status this error maps to. Overridden by each subclass.
    status_code: int = 500

    def __init__(self, detail: Detail, *, headers: Optional[dict] = None) -> None:
        self.detail = detail
        self.headers = headers
        # a readable str() for logs/tests without losing a dict payload
        super().__init__(detail if isinstance(detail, str) else repr(detail))


class BadRequest(ApiError):
    """400 — the request itself is malformed."""

    status_code = 400


class Unauthorized(ApiError):
    """401 — authentication is required or the credential is invalid."""

    status_code = 401


class Forbidden(ApiError):
    """403 — authenticated but not allowed to do this."""

    status_code = 403


class NotFound(ApiError):
    """404 — the named device, template, endpoint, … does not exist."""

    status_code = 404


class Conflict(ApiError):
    """409 — the request conflicts with current state (a claim, a duplicate)."""

    status_code = 409


class LengthRequired(ApiError):
    """411 — a Content-Length is required and was not given."""

    status_code = 411


class PreconditionFailed(ApiError):
    """412 — an ``If-Match`` was given but did not match the current ETag (ADR-0030)."""

    status_code = 412


class PayloadTooLarge(ApiError):
    """413 — the request body is larger than the API accepts."""

    status_code = 413


class Validation(ApiError):
    """422 — the payload is well-formed but fails a domain rule.

    The detail is usually ``{"errors": [...], "fields": {...}}``.
    """

    status_code = 422


class PreconditionRequired(ApiError):
    """428 — a write that changes stored state needs an ``If-Match`` (ADR-0030)."""

    status_code = 428


class TooManyRequests(ApiError):
    """429 — the bus/channel is busy; the detail or a ``Retry-After`` header says so."""

    status_code = 429


class Unavailable(ApiError):
    """503 — a dependency (a sink, a role, a client) is not ready to serve this."""

    status_code = 503


class Internal(ApiError):
    """500 — an unexpected server-side failure the caller cannot fix."""

    status_code = 500


async def _api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    """Map any :class:`ApiError` to the response FastAPI gives an ``HTTPException``.

    Body is ``{"detail": <the exception's detail>}``; any ``headers`` pass
    through. Registered for the base class, so every subclass is matched via the
    exception's MRO.
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers,
    )


def register_error_handlers(app: FastAPI) -> None:
    """Register the one domain-exception handler on ``app`` (ADR-0038 #4).

    Registering for :class:`ApiError` catches every subclass: Starlette resolves
    a handler by walking the raised exception's ``__mro__``.
    """
    app.add_exception_handler(ApiError, _api_error_handler)
