"""Value objects shared by the SPEC-11 queue operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    """The result of checking the enqueue path against R-6/R-8."""

    accepted: bool
    queue_depth: int
    plan_code: str | None = None
    position: int | None = None
    status_code: int = 202
    error: str | None = None
    retry_after_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """One inspectable job retained after retries were exhausted."""

    job_id: str
    deployment_id: str
    org_id: str
    project_id: str
    attempts: int
    max_attempts: int
    priority: int
    last_error: str | None
    git_sha: str
    git_ref: str
    environment: str
    deployment_status: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ReconciledDeployment:
    """A queued deployment that the reconciler turned back into a build job."""

    deployment_id: str
    job_id: str
    org_id: str
    project_id: str
