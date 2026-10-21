"""Value objects crossing the SPEC-09 build pipeline.

The pipeline deliberately has one job record, one outcome record and a handful
of ports. The worker, cache, sandbox and reporter all speak these types, so a
failure observed in one stage is reported by another without re-parsing SQL rows
or exception text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping, Protocol


class Stage(StrEnum):
    FETCH = "fetch"
    DETECT = "detect"
    BUILD = "build"
    PUBLISH = "publish"
    REPORT = "report"


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """One ``build_jobs`` row plus the deployment inputs it builds."""

    job_id: str
    deployment_id: str
    org_id: str
    project_id: str
    project_name: str
    project_slug: str
    provider: str
    provider_repo_id: int
    git_sha: str
    git_ref: str
    environment: str
    attempts: int
    max_attempts: int
    build_env_keys: tuple[str, ...] = ()
    manifest: dict[str, Any] | None = None
    manifest_hash: str | None = None
    build_image: str | None = None
    preview_hostname: str | None = None
    pr_number: int | None = None
    delivery_id: str | None = None
    lease_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BuildOutcome:
    """The result of one claim, whether it succeeded or failed."""

    job_id: str
    deployment_id: str
    success: bool
    stage: Stage
    exit_code: int | None = None
    retryable: bool = False
    error: str | None = None
    error_code: str | None = None
    manifest: dict[str, Any] | None = None
    manifest_hash: str | None = None
    build_image: str | None = None
    log_uri: str | None = None
    cache_key: str | None = None
    cache_hit: bool = False
    output_hash: str | None = None
    node_version: int | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CacheOutcome:
    key: str
    hit: bool
    output_hash: str | None
    verified: bool = False


class Redactor(Protocol):
    """The SPEC-18 redaction surface used by the log ingester."""

    def redact(self, text: str) -> str: ...


class SecretProvider(Protocol):
    """Return only the named build secrets for one claim.

    Values never enter the outcome, cache key or logs. The resolver is injected
    by the control plane so the build plane never reaches the secret store.
    """

    async def resolve_build_env(self, claim: ClaimedJob) -> Mapping[str, str]: ...


class BuildLogHandle(Protocol):
    """A sink plus the URI the reporter stores on the deployment."""

    uri: str

    def write(self, text: str) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class LogRecord:
    text: str
    at: datetime


@dataclass(frozen=True, slots=True)
class BuildInputs:
    """The five reproducibility inputs from SPEC-09 R-22."""

    git_sha: str
    manifest_hash: str
    build_image_digest: str
    build_env_keys: tuple[str, ...]
    node_version: int | None

    def __post_init__(self) -> None:
        if not self.git_sha:
            raise ValueError("git_sha is required")
        if not self.manifest_hash:
            raise ValueError("manifest_hash is required")
        if not self.build_image_digest:
            raise ValueError("build_image_digest is required")
        if tuple(sorted(set(self.build_env_keys))) != self.build_env_keys:
            raise ValueError("build_env_keys must be unique and sorted")


@dataclass(frozen=True, slots=True)
class Artifact:
    """A local artifact produced by the build stage before SPEC-12 pushes it."""

    path: str
    output_hash: str
    size_bytes: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SandboxResult:
    exit_code: int
    signal: int | None = None
    duration_seconds: float | None = None
    container_pid: int | None = None
