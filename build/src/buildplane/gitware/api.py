"""FastAPI route for the SPEC-08 webhook receiver.

The control plane mounts this router; this package does not own the application
shell or authentication. The path is exactly the contract endpoint:

    POST /webhooks/github

An optional `?repo_id=<numeric id>` can be put in the webhook URL when the
connection is registered. It lets the receiver verify the signature before
parsing the body at all; without it the receiver reads only `repository.id`
to select the derived secret. See DECISIONS.md for the R-3/R-5 note.

The route never raises an unhandled 5xx for a signed, routable event:
infrastructure failures are an explicit 503 so GitHub retries, and a retry is
safe because the delivery ID is the idempotency key.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from buildplane.gitware.models import WebhookOutcome
from buildplane.gitware.service import WebhookService

_logger = logging.getLogger("buildplane.gitware.api")


class PayloadTooLarge(Exception):
    """The request body exceeded the receiver's hard limit."""


async def read_bounded_body(request: Request, limit: int) -> bytes:
    """Read at most `limit` bytes before doing any parsing work.

    `Content-Length` is rejected before the stream when it is already too
    large. Chunked bodies are counted as they arrive, so a missing or lying
    Content-Length cannot force unbounded buffering.
    """

    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise PayloadTooLarge
        except ValueError:
            # Let the server/stream deal with a malformed Content-Length.
            pass

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise PayloadTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def create_github_webhook_router(service: WebhookService) -> APIRouter:
    """Build the router without creating an application or a DB pool."""

    router = APIRouter(tags=["git"])

    @router.post("/webhooks/github", status_code=202)
    async def github_webhook(
        request: Request,
        repo_id: Annotated[
            int | None, Query(ge=1, le=2**63 - 1)
        ] = None,
    ) -> JSONResponse:
        try:
            raw = await read_bounded_body(request, service.max_body_bytes)
        except PayloadTooLarge:
            outcome = WebhookOutcome(
                status_code=413,
                action="payload_too_large",
                error="payload_too_large",
            )
            return JSONResponse(
                status_code=outcome.status_code, content=outcome.body()
            )
        except Exception:
            # A broken request stream is not something GitHub can retry into a
            # useful delivery. Return 503 rather than letting FastAPI emit an
            # unhandled 500; the body is not logged.
            _logger.exception("reading GitHub webhook body failed")
            return JSONResponse(
                status_code=503,
                content={"error": "temporarily_unavailable"},
                headers={"Retry-After": "5"},
            )

        try:
            outcome = await service.handle(
                raw, request.headers, repo_id_hint=repo_id
            )
        except Exception:
            # Never log the body, a signature, a token or an Authorization
            # header. GitHub will retry; the delivery ID makes that safe.
            _logger.exception("GitHub webhook processing failed")
            return JSONResponse(
                status_code=503,
                content={"error": "temporarily_unavailable"},
                headers={"Retry-After": "5"},
            )
        return JSONResponse(
            status_code=outcome.status_code, content=outcome.body()
        )

    return router
