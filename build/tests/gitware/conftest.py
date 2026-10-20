"""Fixtures for the SPEC-08 acceptance suite.

The webhook tests use the real HTTP router through Starlette's TestClient and
the same in-memory store as the service tests. No live GitHub or Postgres is
required; the coordinator's E2E suite owns those.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from buildplane.gitware import (
    InMemoryGitStore,
    Project,
    ProjectStatus,
    WebhookService,
    create_github_webhook_router,
    derive_webhook_secret,
    sign_payload,
)

MASTER_SECRET = b"spec-08-test-master-secret-0123456789abcdef"
PREVIEW_DOMAIN = "preview.example.com"
_UNSET = object()


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def master_secret() -> bytes:
    return MASTER_SECRET


@pytest.fixture
def encode():
    return encode_payload


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store() -> InMemoryGitStore:
    return InMemoryGitStore()


@pytest.fixture
def make_project():
    def _make(**overrides: Any) -> Project:
        values: dict[str, Any] = {
            "id": "project-1",
            "org_id": "org-1",
            "provider": "github",
            "provider_repo_id": 123456,
            "slug": "demo-app",
            "default_branch": "main",
            "status": ProjectStatus.ACTIVE,
            "verification_token": "a" * 32,
            "verified": True,
        }
        values.update(overrides)
        return Project(**values)

    return _make


@pytest.fixture
def make_service(store, clock):
    def _make(**overrides: Any) -> WebhookService:
        values: dict[str, Any] = {
            "master_secret": MASTER_SECRET,
            "preview_domain": PREVIEW_DOMAIN,
            "now": clock,
        }
        values.update(overrides)
        return WebhookService(store, **values)

    return _make


@pytest.fixture
def make_client():
    def _make(service: WebhookService) -> TestClient:
        app = FastAPI()
        app.include_router(create_github_webhook_router(service))
        return TestClient(app)

    return _make


@pytest.fixture
def make_push_payload():
    def _make(
        *,
        repo_id: int = 123456,
        ref: str = "refs/heads/main",
        sha: str = "a" * 40,
        deleted: bool = False,
        name: str = "renamed-repo",
    ) -> dict[str, Any]:
        return {
            "ref": ref,
            "before": "b" * 40,
            "after": sha,
            "deleted": deleted,
            "repository": {
                "id": repo_id,
                "name": name,
                "full_name": f"acme/{name}",
                "private": True,
            },
            # These must never reach the audit record (R-13).
            "pusher": {"name": "Alice", "email": "alice@example.com"},
            "commits": [
                {
                    "id": sha,
                    "message": "super-secret commit message",
                    "author": {"email": "alice@example.com"},
                }
            ],
        }

    return _make


@pytest.fixture
def make_pr_payload():
    def _make(
        *,
        repo_id: int = 123456,
        number: int = 42,
        sha: str = "b" * 40,
        action: str = "opened",
        head_repo_id: object = _UNSET,
        base_repo_id: int | None = None,
        name: str = "renamed-repo",
    ) -> dict[str, Any]:
        resolved_head = repo_id if head_repo_id is _UNSET else head_repo_id
        resolved_base = repo_id if base_repo_id is None else base_repo_id
        head_repo = (
            None
            if resolved_head is None
            else {"id": resolved_head, "full_name": "fork/demo"}
        )
        return {
            "action": action,
            "number": number,
            "pull_request": {
                "head": {"sha": sha, "repo": head_repo, "ref": "feature"},
                "base": {
                    "repo": {"id": resolved_base, "full_name": "acme/demo"},
                    "ref": "main",
                },
                "title": "do not persist this title",
                "body": "do not persist this body",
            },
            "repository": {
                "id": repo_id,
                "name": name,
                "full_name": f"acme/{name}",
            },
        }

    return _make


@pytest.fixture
def make_repository_payload():
    def _make(
        *, repo_id: int = 123456, action: str = "deleted"
    ) -> dict[str, Any]:
        return {
            "action": action,
            "repository": {
                "id": repo_id,
                "name": "renamed-repo",
                "full_name": "acme/renamed-repo",
            },
        }

    return _make


def encode_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


@pytest.fixture
def signed():
    def _signed(
        payload: dict[str, Any],
        *,
        repo_id: int = 123456,
        event: str = "push",
        delivery_id: str = "delivery-1",
        signature: str | None = None,
        master: bytes = MASTER_SECRET,
    ) -> tuple[bytes, dict[str, str]]:
        raw = encode_payload(payload)
        secret = derive_webhook_secret(master, repo_id)
        header = sign_payload(raw, secret) if signature is None else signature
        headers = {
            "X-Hub-Signature-256": header,
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery_id,
            "Content-Type": "application/json",
        }
        return raw, headers

    return _signed


@pytest.fixture
def handle():
    def _handle(service: WebhookService, raw: bytes, headers: dict[str, str], **kwargs):
        return asyncio.run(service.handle(raw, headers, **kwargs))

    return _handle
