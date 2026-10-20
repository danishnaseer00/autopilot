"""Value objects shared by the SPEC-08 git integration.

The models are deliberately storage-agnostic. `GitStore` (store.py) is the
single port the service uses; a Postgres adapter or the in-memory test adapter
can implement it without the service knowing about SQL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from buildplane.gitware.validation import (
    validate_delivery_id,
    validate_git_ref,
    validate_git_sha,
    validate_pr_number,
    validate_provider_repo_id,
)


class Environment(StrEnum):
    PRODUCTION = "production"
    PREVIEW = "preview"


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    # SPEC-08 R-18 and the token-revoked failure row use states that SPEC-03's
    # baseline CHECK does not allow yet. Kept in the domain so the persistence
    # gap is explicit; see DECISIONS.md.
    ORPHANED = "orphaned"
    CREDENTIAL_INVALID = "credential_invalid"


class TriggerSource(StrEnum):
    WEBHOOK = "webhook"
    MANUAL = "manual"
    API = "api"
    AGENT = "agent"


class VerificationMethod(StrEnum):
    TXT = "txt"
    DEPLOY_BRANCH = "deploy_branch"


class DeploymentStatus(StrEnum):
    QUEUED = "queued"
    BUILDING = "building"
    READY = "ready"
    LIVE = "live"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class RouteSource(StrEnum):
    EXACT = "exact"
    DEFAULT = "default"
    CATCH_ALL = "catch_all"


@dataclass(frozen=True, slots=True)
class Project:
    """The subset of a project row needed to route a webhook.

    `verification_token`/`verified` stand in for state that SPEC-03 does not
    have columns for yet. They live here (and behind `GitStore`) so the
    migration is a storage change, not a service change.
    """

    id: str
    org_id: str
    provider: str
    provider_repo_id: int
    slug: str
    default_branch: str
    status: ProjectStatus = ProjectStatus.ACTIVE
    verification_token: str = ""
    verified: bool = False
    verification_method: VerificationMethod | None = None
    verified_at: datetime | None = None
    verification_checked_at: datetime | None = None
    verification_error: str | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.org_id:
            raise ValueError("project id and org_id are required")
        if not isinstance(self.provider, str) or not self.provider:
            raise ValueError("provider is required")
        if not isinstance(self.slug, str) or not self.slug:
            raise ValueError("project slug is required")
        validate_provider_repo_id(self.provider_repo_id)
        validate_git_ref(f"refs/heads/{self.default_branch}")
        if not isinstance(self.status, ProjectStatus):
            object.__setattr__(self, "status", ProjectStatus(self.status))
        if self.verification_method is not None and not isinstance(
            self.verification_method, VerificationMethod
        ):
            object.__setattr__(
                self,
                "verification_method",
                VerificationMethod(self.verification_method),
            )


@dataclass(frozen=True, slots=True)
class BranchRule:
    """One row of `project_branches`, in the order the store returns it."""

    git_ref: str
    environment: Environment
    auto_deploy: bool = True

    def __post_init__(self) -> None:
        if self.git_ref != "*":
            validate_git_ref(self.git_ref)
        if not isinstance(self.environment, Environment):
            object.__setattr__(self, "environment", Environment(self.environment))
        if not isinstance(self.auto_deploy, bool):
            raise ValueError("auto_deploy must be a boolean")
        if self.git_ref == "*" and self.environment is not Environment.PREVIEW:
            # R-10: "the catch-all is preview". A production catch-all would
            # deploy every unlisted branch to production; reject the config at
            # the domain boundary rather than trusting the UI.
            raise ValueError("the catch-all rule must target preview")


@dataclass(frozen=True, slots=True)
class RouteMatch:
    """The result of matching a push ref against the ordered branch rules."""

    environment: Environment
    auto_deploy: bool
    matched_ref: str
    source: RouteSource


@dataclass(frozen=True, slots=True)
class DeploymentIntent:
    """What a webhook (or console click) asks the queue to build (R-14).

    The four reproducible inputs (manifest, manifest_hash, build_image,
    build_env_keys) are not known at webhook time; SPEC-09's fetch/detect
    stages fill them. A store adapter therefore records this intent and lets
    SPEC-11/09 own deployment-row creation. See DECISIONS.md.
    """

    project_id: str
    org_id: str
    provider: str
    provider_repo_id: int
    git_sha: str
    git_ref: str
    environment: Environment
    trigger: TriggerSource = TriggerSource.WEBHOOK
    delivery_id: str | None = None
    pr_number: int | None = None
    preview_hostname: str | None = None
    matched_ref: str | None = None

    def __post_init__(self) -> None:
        validate_provider_repo_id(self.provider_repo_id)
        validate_git_sha(self.git_sha)
        validate_git_ref(self.git_ref)
        if not isinstance(self.environment, Environment):
            object.__setattr__(self, "environment", Environment(self.environment))
        if not isinstance(self.trigger, TriggerSource):
            object.__setattr__(self, "trigger", TriggerSource(self.trigger))
        if self.pr_number is not None:
            validate_pr_number(self.pr_number)
        if self.delivery_id is not None:
            validate_delivery_id(self.delivery_id)
        if self.environment is Environment.PRODUCTION:
            if self.preview_hostname is not None or self.pr_number is not None:
                raise ValueError("production deployments cannot have preview fields")
        elif not self.preview_hostname:
            raise ValueError("preview deployments need a hostname")


@dataclass(frozen=True, slots=True)
class DeploymentRecord:
    """The store's view of a queued deployment for superseding and tests."""

    id: str
    project_id: str
    org_id: str
    git_sha: str
    git_ref: str
    environment: Environment
    status: DeploymentStatus
    trigger: TriggerSource
    preview_hostname: str | None = None
    pr_number: int | None = None
    matched_ref: str | None = None
    delivery_id: str | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None
    superseded_by: str | None = None


@dataclass(frozen=True, slots=True)
class RecordResult:
    deployment_id: str
    created: bool
    superseded: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WebhookEventRecord:
    """Metadata written to the audit side-channel (R-13).

    There is intentionally no payload, commit message, author email or header
    here: those are untrusted and may contain secrets. The full payload never
    leaves the request.
    """

    delivery_id: str
    project_id: str
    event: str
    action: str | None
    outcome: str
    received_at: datetime
    git_ref: str | None = None
    git_sha: str | None = None
    provider_repo_id: int | None = None
    deployment_id: str | None = None


@dataclass(frozen=True, slots=True)
class WebhookOutcome:
    """The receiver's status and a deliberately small response body."""

    status_code: int
    action: str
    deployment_id: str | None = None
    error: str | None = None
    detail: dict[str, str] = field(default_factory=dict)

    def body(self) -> dict[str, object]:
        if self.status_code >= 400:
            body: dict[str, object] = {"error": self.error or "request_rejected"}
            if self.action:
                body["action"] = self.action
            return body
        body = {"status": "accepted", "action": self.action}
        if self.deployment_id is not None:
            body["deployment_id"] = self.deployment_id
        if self.detail:
            body.update(self.detail)
        return body
