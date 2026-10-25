"""RFC 7807 problem responses and request tracing (SPEC-04 §3.1, SPEC-36 §2).

Every error the console can receive is one of the codes in SPEC-36 §4. The
handler deliberately does not echo exception text or stack traces; support gets
the `traceparent` and can find the server-side log line.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from typing import Any, Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

PROBLEM_BASE = "https://autopilot.invalid/problems"
TENANT_FIELDS = frozenset({"org_id", "tenant_id", "k8s_namespace"})

_TITLES = {
    "unauthenticated": "Authentication required",
    "forbidden": "Forbidden",
    "not_found": "Not found",
    "conflict": "Conflict",
    "quota_exceeded": "Quota exceeded",
    "quota_pending": "Build queue is full",
    "tenant_suspended": "Organization suspended",
    "domain_unverified": "Domain is not verified",
    "validation_failed": "Request validation failed",
    "rate_limited": "Too many requests",
    "upstream_unavailable": "A dependency is unavailable",
    "internal_error": "Internal error",
}


def new_traceparent() -> str:
    """Return a W3C traceparent with a fresh trace and span id."""

    return f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"


class ApiProblem(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        detail: str,
        *,
        title: str | None = None,
        fields: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.title = title or _TITLES.get(code, "Request failed")
        self.fields = dict(fields or {})
        self.headers = dict(headers or {})
        self.extra = dict(extra or {})


def _problem_response(request: Request, problem: ApiProblem) -> JSONResponse:
    traceparent = getattr(request.state, "traceparent", None) or new_traceparent()
    request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex
    body: dict[str, Any] = {
        "type": f"{PROBLEM_BASE}/{problem.code}",
        "title": problem.title,
        "status": problem.status,
        "detail": problem.detail,
        "code": problem.code,
        "traceparent": traceparent,
        "request_id": request_id,
    }
    if problem.fields:
        body["errors"] = problem.fields
    body.update(problem.extra)
    headers = {
        "Content-Type": "application/problem+json",
        "X-Request-Id": request_id,
        "traceparent": traceparent,
        **problem.headers,
    }
    return JSONResponse(
        status_code=problem.status,
        content=body,
        headers=headers,
    )


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach the id/trace and reject client-supplied tenant fields.

    SPEC-04 R-3: a body containing `org_id`, `tenant_id` or `k8s_namespace` is
    rejected with 400, never silently ignored. The check is global so a new
    endpoint cannot forget it.
    """

    async def dispatch(self, request: Request, call_next):
        request.state.request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
        request.state.traceparent = (
            request.headers.get("traceparent") or new_traceparent()
        )
        if request.method in {"POST", "PUT", "PATCH"}:
            content_type = request.headers.get("content-type", "")
            if "application/json" in content_type:
                try:
                    body = await request.json()
                except Exception:  # noqa: BLE001 - let FastAPI report the syntax
                    body = None
                if isinstance(body, dict):
                    forbidden = TENANT_FIELDS.intersection(body)
                    if forbidden:
                        response = _problem_response(
                            request,
                            ApiProblem(
                                400,
                                "validation_failed",
                                "tenant fields are derived from the session and "
                                "must not be sent in the body",
                                fields={
                                    key: "derived from the session"
                                    for key in sorted(forbidden)
                                },
                            ),
                        )
                        response.headers["X-Request-Id"] = request.state.request_id
                        return response
        response = await call_next(request)
        response.headers.setdefault("X-Request-Id", request.state.request_id)
        response.headers.setdefault("traceparent", request.state.traceparent)
        return response


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiProblem)
    async def _api_problem(request: Request, exc: ApiProblem) -> JSONResponse:
        return _problem_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields: dict[str, str] = {}
        for error in exc.errors():
            location = ".".join(
                str(part) for part in error.get("loc", ()) if part != "body"
            )
            fields[location or "body"] = str(error.get("msg", "invalid value"))
        return _problem_response(
            request,
            ApiProblem(
                422,
                "validation_failed",
                "one or more fields are invalid",
                fields=fields,
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            401: "unauthenticated",
            403: "forbidden",
            404: "not_found",
            409: "conflict",
            429: "rate_limited",
        }.get(exc.status_code, "internal_error")
        return _problem_response(
            request,
            ApiProblem(exc.status_code, code, str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled request error: %s", type(exc).__name__)
        return _problem_response(
            request,
            ApiProblem(
                500,
                "internal_error",
                "the server could not complete the request",
            ),
        )


def not_found(what: str = "resource") -> ApiProblem:
    # Deliberately generic: a 403 would confirm that the id exists.
    return ApiProblem(404, "not_found", f"{what} not found")


def forbidden(detail: str = "your role cannot perform this action") -> ApiProblem:
    return ApiProblem(403, "forbidden", detail)


def unauthenticated() -> ApiProblem:
    return ApiProblem(401, "unauthenticated", "sign in to continue")
