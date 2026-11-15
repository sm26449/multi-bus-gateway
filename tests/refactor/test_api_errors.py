"""The api/errors.py domain exceptions map to HTTP exactly like HTTPException.

ADR-0038 #4. Lots B–F move ``raise HTTPException(status, detail)`` out of
``create_api`` into services as ``raise Validation(detail)`` etc. That move is
only safe if the response is byte-identical. This pins the contract: for every
domain exception, the handler produces the same status, the same
``{"detail": ...}`` body, and the same headers that FastAPI gives an
``HTTPException`` with that status and detail.
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from multibus.api import errors


# (exception class, status, a representative detail from the real api) ---------
CASES = [
    (errors.BadRequest, 400, "bad request"),
    (errors.Unauthorized, 401, "login required"),
    (errors.Forbidden, 403, {"detail-ish": "nope"}),
    (errors.NotFound, 404, "device not found"),
    (errors.Conflict, 409, {"errors": ["a claim conflict"]}),
    (errors.LengthRequired, 411, "length required"),
    (errors.PreconditionFailed, 412, {"errors": ["etag mismatch"]}),
    (errors.PayloadTooLarge, 413, {"errors": ["too big"]}),
    (errors.Validation, 422, {"errors": ["x: bad"], "fields": {"x": "bad"}}),
    (errors.PreconditionRequired, 428, {"errors": ["If-Match required"]}),
    (errors.TooManyRequests, 429, "bus busy"),
    (errors.Unavailable, 503, {"detail": "role not ready"}),
    (errors.Internal, 500, "boom"),
]


def _app_raising(exc: BaseException) -> TestClient:
    app = FastAPI()
    errors.register_error_handlers(app)

    @app.get("/boom")
    def _boom():
        raise exc

    return TestClient(app, raise_server_exceptions=False)


def test_status_codes_are_fixed_by_subclass():
    for cls, status, _ in CASES:
        assert cls.status_code == status, f"{cls.__name__} should map to {status}"


def test_domain_error_matches_httpexception_byte_for_byte():
    for cls, status, detail in CASES:
        domain = _app_raising(cls(detail)).get("/boom")
        # the reference: a plain HTTPException with the same status+detail
        ref = _app_raising(HTTPException(status_code=status, detail=detail)).get("/boom")
        assert domain.status_code == ref.status_code == status
        assert domain.json() == ref.json() == {"detail": detail}


def test_headers_pass_through_like_httpexception():
    hdr = {"Retry-After": "42"}
    domain = _app_raising(errors.TooManyRequests("busy", headers=hdr)).get("/boom")
    ref = _app_raising(
        HTTPException(status_code=429, detail="busy", headers=hdr)
    ).get("/boom")
    assert domain.status_code == ref.status_code == 429
    assert domain.headers.get("Retry-After") == ref.headers.get("Retry-After") == "42"
    assert domain.json() == ref.json() == {"detail": "busy"}


def test_one_handler_catches_every_subclass_via_mro():
    # registering for the base ApiError must catch a subclass (Starlette walks
    # the raised exception's __mro__)
    r = _app_raising(errors.NotFound("device not found")).get("/boom")
    assert r.status_code == 404
    assert r.json() == {"detail": "device not found"}
