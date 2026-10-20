"""Parsing GitHub webhook payloads into typed events.

Only the fields needed to route a deployment are retained. Commit messages,
author details and other free-form payload content are deliberately dropped at
this boundary (SPEC-08 R-13).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from buildplane.gitware.errors import (
    InvalidGitRef,
    InvalidGitSha,
    InvalidPrNumber,
    InvalidProviderRepoId,
    WebhookPayloadError,
)
from buildplane.gitware.validation import (
    is_zero_sha,
    validate_git_ref,
    validate_git_sha,
    validate_pr_number,
    validate_provider_repo_id,
)

PREVIEW_PR_ACTIONS = frozenset({"opened", "synchronize"})


@dataclass(frozen=True, slots=True)
class PingEvent:
    repo_id: int | None


@dataclass(frozen=True, slots=True)
class PushEvent:
    repo_id: int
    ref: str
    before: str | None
    after: str
    deleted: bool

    @property
    def is_branch(self) -> bool:
        return self.ref.startswith("refs/heads/")


@dataclass(frozen=True, slots=True)
class PullRequestEvent:
    repo_id: int
    action: str
    number: int
    head_sha: str
    head_repo_id: int | None


@dataclass(frozen=True, slots=True)
class RepositoryEvent:
    repo_id: int
    action: str


@dataclass(frozen=True, slots=True)
class UnsupportedEvent:
    event_type: str


Event = PingEvent | PushEvent | PullRequestEvent | RepositoryEvent | UnsupportedEvent


def decode_payload(raw: bytes) -> dict[str, Any] | None:
    """Decode a bounded webhook body, returning None for malformed JSON.

    The caller must verify the signature before using any field other than the
    repository ID. JSON parsing is bounded by the HTTP body limit.
    """

    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def repository_id_from_payload(payload: object) -> int | None:
    """Return `repository.id` if it is a valid positive integer.

    This is the only pre-verification use of the parsed body. R-5 requires the
    ID to choose the derived secret; every other field is used only after
    verification succeeds (see the R-3/R-5 note in DECISIONS.md).
    """

    if not isinstance(payload, dict):
        return None
    repository = payload.get("repository")
    if not isinstance(repository, dict):
        return None
    try:
        return validate_provider_repo_id(repository.get("id"))
    except InvalidProviderRepoId:
        return None


def event_action(payload: object) -> str | None:
    """Return a payload's `action` for the audit record, if present."""

    if not isinstance(payload, dict):
        return None
    action = payload.get("action")
    return action if isinstance(action, str) else None


def _require_mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WebhookPayloadError(f"{field} must be an object")
    return value


def _require_repo_id(payload: dict[str, Any]) -> int:
    repository = _require_mapping(payload.get("repository"), "repository")
    try:
        return validate_provider_repo_id(repository.get("id"))
    except InvalidProviderRepoId as exc:
        raise WebhookPayloadError("repository.id is not a valid repo ID") from exc


def _parse_push(payload: dict[str, Any]) -> PushEvent:
    repo_id = _require_repo_id(payload)
    try:
        ref = validate_git_ref(payload.get("ref"))
        after = validate_git_sha(payload.get("after"))
    except (InvalidGitRef, InvalidGitSha) as exc:
        raise WebhookPayloadError(str(exc)) from exc

    before_value = payload.get("before")
    before: str | None
    if before_value is None:
        before = None
    else:
        try:
            before = validate_git_sha(before_value)
        except InvalidGitSha as exc:
            raise WebhookPayloadError("push.before is not a valid SHA") from exc

    deleted_value = payload.get("deleted", False)
    if not isinstance(deleted_value, bool):
        raise WebhookPayloadError("push.deleted must be a boolean")

    # GitHub sends the all-zero SHA when a branch is deleted. Either signal is
    # sufficient; do not route a deletion as a build.
    deleted = deleted_value or is_zero_sha(after)
    return PushEvent(
        repo_id=repo_id,
        ref=ref,
        before=before,
        after=after,
        deleted=deleted,
    )


def _parse_pull_request(payload: dict[str, Any]) -> PullRequestEvent:
    repo_id = _require_repo_id(payload)
    action = payload.get("action")
    if not isinstance(action, str) or not action:
        raise WebhookPayloadError("pull_request.action is required")
    try:
        number = validate_pr_number(payload.get("number"))
    except InvalidPrNumber as exc:
        raise WebhookPayloadError("pull_request.number is invalid") from exc

    pull_request = _require_mapping(payload.get("pull_request"), "pull_request")
    head = _require_mapping(pull_request.get("head"), "pull_request.head")
    try:
        head_sha = validate_git_sha(head.get("sha"))
    except InvalidGitSha as exc:
        raise WebhookPayloadError("pull_request.head.sha is invalid") from exc

    if "repo" not in head:
        raise WebhookPayloadError("pull_request.head.repo is required")
    head_repo_value = head.get("repo")
    head_repo_id: int | None
    if head_repo_value is None:
        # The fork was deleted after the PR was opened.
        head_repo_id = None
    else:
        head_repo = _require_mapping(head_repo_value, "pull_request.head.repo")
        try:
            head_repo_id = validate_provider_repo_id(head_repo.get("id"))
        except InvalidProviderRepoId as exc:
            raise WebhookPayloadError("pull_request.head.repo.id is invalid") from exc

    base = _require_mapping(pull_request.get("base"), "pull_request.base")
    base_repo = _require_mapping(base.get("repo"), "pull_request.base.repo")
    try:
        base_repo_id = validate_provider_repo_id(base_repo.get("id"))
    except InvalidProviderRepoId as exc:
        raise WebhookPayloadError("pull_request.base.repo.id is invalid") from exc
    if base_repo_id != repo_id:
        raise WebhookPayloadError("pull request base does not match repository")

    return PullRequestEvent(
        repo_id=repo_id,
        action=action,
        number=number,
        head_sha=head_sha,
        head_repo_id=head_repo_id,
    )


def _parse_repository(payload: dict[str, Any]) -> RepositoryEvent:
    repo_id = _require_repo_id(payload)
    action = payload.get("action")
    if not isinstance(action, str) or not action:
        raise WebhookPayloadError("repository.action is required")
    return RepositoryEvent(repo_id=repo_id, action=action)


def parse_event(event_type: str, payload: dict[str, Any]) -> Event:
    """Parse a verified event body. Malformed fields raise `WebhookPayloadError`.

    Unknown event types are returned as `UnsupportedEvent` so the receiver can
    answer 202 without treating a harmless subscription change as a failure.
    """

    if not isinstance(event_type, str) or not event_type:
        raise WebhookPayloadError("missing event type")
    if event_type == "ping":
        repository = payload.get("repository")
        repo_id: int | None = None
        if isinstance(repository, dict):
            try:
                repo_id = validate_provider_repo_id(repository.get("id"))
            except InvalidProviderRepoId:
                repo_id = None
        return PingEvent(repo_id=repo_id)
    if event_type == "push":
        return _parse_push(payload)
    if event_type == "pull_request":
        return _parse_pull_request(payload)
    if event_type == "repository":
        return _parse_repository(payload)
    return UnsupportedEvent(event_type=event_type)
