"""Row models returned by the SPEC-13 lifecycle service."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping


@dataclass(frozen=True)
class Deployment:
    """One immutable build/run fact, plus the lifecycle status it currently has.

    ``manifest`` is the resolved SPEC-06 document recorded at trigger time.
    ``build_env_keys`` holds names only (SPEC-03 §4.3).
    """

    id: uuid.UUID
    org_id: uuid.UUID
    project_id: uuid.UUID
    git_sha: str
    git_ref: str
    environment: str
    status: str
    stage: str | None
    manifest: Mapping[str, Any]
    manifest_hash: str
    build_image: str
    build_env_keys: tuple[str, ...]
    revision: str | None
    image_digest: str | None
    exit_code: int | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    superseded_by: uuid.UUID | None

    @property
    def is_terminal(self) -> bool:
        return self.status in {"failed", "cancelled", "superseded"}


@dataclass(frozen=True)
class TriggerResult:
    """``created`` is False when the trigger was an idempotent duplicate (R-10)."""

    deployment: Deployment
    created: bool


@dataclass(frozen=True)
class PromotionResult:
    """The outcome of a promotion or rollback cutover.

    ``changed`` is False for a repeated call that found the route already on the
    target; no history or audit row is written in that case.
    """

    deployment: Deployment
    routes_updated: int
    superseded: tuple[uuid.UUID, ...]
    changed: bool


def deployment_from_row(row: Any) -> Deployment:
    manifest = row["manifest"]
    if isinstance(manifest, bytes):
        manifest = json.loads(manifest.decode("utf-8"))
    elif isinstance(manifest, str):
        manifest = json.loads(manifest)

    return Deployment(
        id=row["id"],
        org_id=row["org_id"],
        project_id=row["project_id"],
        git_sha=str(row["git_sha"]),
        git_ref=str(row["git_ref"]),
        environment=str(row["environment"]),
        status=str(row["status"]),
        stage=row["stage"],
        manifest=manifest,
        manifest_hash=str(row["manifest_hash"]),
        build_image=str(row["build_image"]),
        build_env_keys=tuple(str(key) for key in row["build_env_keys"]),
        revision=row["revision"],
        image_digest=row["image_digest"],
        exit_code=row["exit_code"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        superseded_by=row["superseded_by"],
    )


__all__ = [
    "Deployment",
    "PromotionResult",
    "TriggerResult",
    "deployment_from_row",
]
