"""Postgres adapter for the SPEC-08 git integration port.

The webhook path runs before an org context exists, so this adapter is intended
for the webhook/system role (or a SECURITY DEFINER read path), not the tenant
`autopilot_app` role that RLS constrains. That is a deployment concern; the SQL
and transaction boundaries here are the same either way.

Two schema details are mapped rather than leaked into the domain:

* `projects.verification_method` stores `'branch'`, while the domain enum uses
  `'deploy_branch'`.
* `deployments.manifest`/`manifest_hash`/`build_image` are intentionally null
  at intent time. Migration 1008 removes `superseded` from the build-inputs
  constraint so queued work can be superseded before those inputs exist.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator, Sequence

import asyncpg

from buildplane.gitware.models import (
    BranchRule,
    DeploymentIntent,
    Environment,
    Project,
    ProjectStatus,
    RecordResult,
    VerificationMethod,
    WebhookEventRecord,
)
from buildplane.gitware.store import GitStore

_TXT = "txt"
_BRANCH = "branch"


class PostgresGitStore(GitStore):
    def __init__(self, database) -> None:
        self._db = database

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[asyncpg.Connection]:
        if hasattr(self._db, "acquire"):
            async with self._db.acquire() as connection:
                yield connection
        else:
            yield self._db

    # -- projects -----------------------------------------------------------

    async def find_project(
        self, provider: str, provider_repo_id: int
    ) -> Project | None:
        async with self._connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, org_id, provider, provider_repo_id, slug,
                       default_branch, status, verification_token,
                       verification_method, verified_at,
                       verification_checked_at, verification_error
                FROM projects
                WHERE provider = $1 AND provider_repo_id = $2
                """,
                provider,
                provider_repo_id,
            )
        return _project_from_row(row) if row is not None else None

    async def get_project(self, project_id: str) -> Project | None:
        async with self._connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, org_id, provider, provider_repo_id, slug,
                       default_branch, status, verification_token,
                       verification_method, verified_at,
                       verification_checked_at, verification_error
                FROM projects
                WHERE id = $1
                """,
                _uuid(project_id),
            )
        return _project_from_row(row) if row is not None else None

    async def list_branch_rules(
        self, project_id: str
    ) -> Sequence[BranchRule]:
        async with self._connection() as conn:
            rows = await conn.fetch(
                """
                SELECT git_ref, environment, auto_deploy
                FROM project_branches
                WHERE project_id = $1
                ORDER BY position, git_ref
                """,
                _uuid(project_id),
            )
        return tuple(
            BranchRule(
                git_ref=row["git_ref"],
                environment=Environment(row["environment"]),
                auto_deploy=row["auto_deploy"],
            )
            for row in rows
        )

    async def allocate_preview_hostname(
        self, project_id: str, key: str, candidate: str
    ) -> str:
        ref_kind, ref_key = _preview_key(key)
        project_uuid = _uuid(project_id)
        async with self._connection() as conn:
            async with conn.transaction():
                org_id = await conn.fetchval(
                    "SELECT org_id FROM projects WHERE id = $1", project_uuid
                )
                if org_id is None:
                    raise ValueError("project not found")
                existing = await conn.fetchval(
                    """
                    SELECT hostname FROM preview_hostnames
                    WHERE project_id = $1 AND ref_kind = $2 AND ref_key = $3
                    """,
                    project_uuid,
                    ref_kind,
                    ref_key,
                )
                if existing is not None:
                    return existing
                try:
                    inserted = await conn.fetchval(
                        """
                        INSERT INTO preview_hostnames
                            (org_id, project_id, ref_kind, ref_key, hostname)
                        VALUES ($1, $2, $3, $4, $5)
                        ON CONFLICT (project_id, ref_kind, ref_key) DO NOTHING
                        RETURNING hostname
                        """,
                        org_id,
                        project_uuid,
                        ref_kind,
                        ref_key,
                        candidate,
                    )
                except asyncpg.UniqueViolationError as exc:
                    raise ValueError("preview hostname collision") from exc
                if inserted is not None:
                    return inserted
                # Another transaction won the stable-key race. Return its
                # hostname; the caller's candidate is intentionally discarded.
                return await conn.fetchval(
                    """
                    SELECT hostname FROM preview_hostnames
                    WHERE project_id = $1 AND ref_kind = $2 AND ref_key = $3
                    """,
                    project_uuid,
                    ref_kind,
                    ref_key,
                )

    # -- deployment intents -------------------------------------------------

    async def record_intent(
        self, intent: DeploymentIntent, *, received_at: datetime
    ) -> RecordResult:
        project_uuid = _uuid(intent.project_id)
        org_uuid = _uuid(intent.org_id)
        environment = str(intent.environment)
        lock_key = (
            f"autopilot-intent:{intent.project_id}:{intent.git_ref}:{environment}"
        )

        async with self._connection() as conn:
            async with conn.transaction():
                # Serialize the duplicate check and the supersede pass for one
                # project/ref/environment. GitHub's at-least-once retries and
                # two nearly simultaneous pushes must not both win.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))", lock_key
                )

                existing = await conn.fetchval(
                    """
                    SELECT id FROM deployments
                    WHERE project_id = $1
                      AND git_sha = $2
                      AND git_ref = $3
                      AND environment = $4
                      AND status IN ('queued','building','ready','live')
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    project_uuid,
                    intent.git_sha,
                    intent.git_ref,
                    environment,
                )
                if existing is not None:
                    return RecordResult(str(existing), created=False)

                deployment_id = uuid.uuid4()
                await conn.execute(
                    """
                    INSERT INTO deployments
                        (id, project_id, org_id, git_sha, git_ref, environment,
                         trigger_source, status, build_env_keys, preview_hostname,
                         pr_number, delivery_id, matched_ref, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, 'queued',
                            '{}'::text[], $8, $9, $10, $11, $12)
                    """,
                    deployment_id,
                    project_uuid,
                    org_uuid,
                    intent.git_sha,
                    intent.git_ref,
                    environment,
                    str(intent.trigger),
                    intent.preview_hostname,
                    intent.pr_number,
                    intent.delivery_id,
                    intent.matched_ref,
                    received_at,
                )
                await conn.execute(
                    """
                    INSERT INTO build_jobs
                        (deployment_id, org_id, project_id, status,
                         available_at, created_at, updated_at)
                    VALUES ($1, $2, $3, 'pending', $4, $4, $4)
                    """,
                    deployment_id,
                    org_uuid,
                    project_uuid,
                    received_at,
                )

                # The new row must exist before old rows can point at it through
                # the self-referencing `superseded_by` FK.
                old_ids = await conn.fetch(
                    """
                    UPDATE deployments
                    SET status = 'superseded',
                        finished_at = $4,
                        superseded_by = $5
                    WHERE project_id = $1
                      AND git_ref = $2
                      AND environment = $3
                      AND status = 'queued'
                      AND id <> $5
                    RETURNING id
                    """,
                    project_uuid,
                    intent.git_ref,
                    environment,
                    received_at,
                    deployment_id,
                )
                replaced = tuple(str(row["id"]) for row in old_ids)
                if replaced:
                    await conn.execute(
                        """
                        UPDATE build_jobs
                        SET status = 'cancelled',
                            last_error = 'superseded',
                            updated_at = $1
                        WHERE deployment_id = ANY($2::uuid[])
                          AND status = 'pending'
                        """,
                        received_at,
                        [uuid.UUID(value) for value in replaced],
                    )
                return RecordResult(
                    str(deployment_id), created=True, superseded=replaced
                )

    async def record_event(self, record: WebhookEventRecord) -> None:
        project_uuid = _uuid(record.project_id)
        deployment_uuid = _uuid(record.deployment_id) if record.deployment_id else None
        async with self._connection() as conn:
            await conn.execute(
                """
                INSERT INTO webhook_deliveries
                    (org_id, project_id, provider, delivery_id, event, ref,
                     sha, action, deployment_id, received_at)
                VALUES (
                    (SELECT org_id FROM projects WHERE id = $1),
                    $1, 'github', $2, $3, $4, $5, $6, $7, $8
                )
                ON CONFLICT (provider, delivery_id) DO UPDATE
                SET org_id = EXCLUDED.org_id,
                    project_id = EXCLUDED.project_id,
                    event = EXCLUDED.event,
                    ref = EXCLUDED.ref,
                    sha = EXCLUDED.sha,
                    action = EXCLUDED.action,
                    deployment_id = EXCLUDED.deployment_id,
                    received_at = EXCLUDED.received_at
                """,
                project_uuid,
                record.delivery_id,
                record.event,
                record.git_ref,
                record.git_sha,
                record.outcome,
                deployment_uuid,
                record.received_at,
            )

    # -- project lifecycle --------------------------------------------------

    async def set_project_status(
        self, project_id: str, status: ProjectStatus
    ) -> None:
        async with self._connection() as conn:
            await conn.execute(
                "UPDATE projects SET status = $1, updated_at = now() WHERE id = $2",
                str(status),
                _uuid(project_id),
            )

    async def mark_verified(
        self, project_id: str, method: VerificationMethod, *, at: datetime
    ) -> None:
        async with self._connection() as conn:
            await conn.execute(
                """
                UPDATE projects
                SET verification_method = $1,
                    verified_at = $2,
                    verification_checked_at = $2,
                    verification_error = NULL,
                    updated_at = $2
                WHERE id = $3
                """,
                _method_to_db(method),
                at,
                _uuid(project_id),
            )

    async def mark_unverified(
        self, project_id: str, *, reason: str, at: datetime
    ) -> None:
        async with self._connection() as conn:
            await conn.execute(
                """
                UPDATE projects
                SET verified_at = NULL,
                    verification_checked_at = $1,
                    verification_error = $2,
                    updated_at = $1
                WHERE id = $3
                """,
                at,
                reason[:4000],
                _uuid(project_id),
            )

    async def cancel_queued(
        self, project_id: str, *, reason: str, at: datetime
    ) -> int:
        project_uuid = _uuid(project_id)
        async with self._connection() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """
                    UPDATE deployments
                    SET status = 'cancelled',
                        finished_at = $1,
                        error_message = $2
                    WHERE project_id = $3 AND status = 'queued'
                    RETURNING id
                    """,
                    at,
                    reason[:4000],
                    project_uuid,
                )
                if rows:
                    await conn.execute(
                        """
                        UPDATE build_jobs
                        SET status = 'cancelled',
                            last_error = $1,
                            updated_at = $2
                        WHERE deployment_id = ANY($3::uuid[])
                          AND status = 'pending'
                        """,
                        reason[:4000],
                        at,
                        [row["id"] for row in rows],
                    )
        return len(rows)

    # -- webhook delivery idempotency --------------------------------------

    async def claim_delivery(self, delivery_id: str) -> bool:
        async with self._connection() as conn:
            claimed = await conn.fetchval(
                """
                INSERT INTO webhook_deliveries (provider, delivery_id)
                VALUES ('github', $1)
                ON CONFLICT (provider, delivery_id) DO NOTHING
                RETURNING id
                """,
                delivery_id,
            )
        return claimed is not None

    async def release_delivery(self, delivery_id: str) -> None:
        async with self._connection() as conn:
            await conn.execute(
                """
                DELETE FROM webhook_deliveries
                WHERE provider = 'github'
                  AND delivery_id = $1
                  AND event IS NULL
                  AND action IS NULL
                """,
                delivery_id,
            )


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"not a UUID: {value!r}") from exc


def _preview_key(key: str) -> tuple[str, str]:
    if key.startswith("pr:"):
        return "pr", key
    if key.startswith("branch:"):
        return "branch", key
    raise ValueError(f"unsupported preview key: {key!r}")


def _method_to_db(method: VerificationMethod) -> str:
    return _BRANCH if method is VerificationMethod.DEPLOY_BRANCH else _TXT


def _method_from_db(value: str | None) -> VerificationMethod | None:
    if value is None:
        return None
    if value == _BRANCH:
        return VerificationMethod.DEPLOY_BRANCH
    if value == _TXT:
        return VerificationMethod.TXT
    raise ValueError(f"unknown verification_method in projects: {value!r}")


def _project_from_row(row) -> Project:
    method = _method_from_db(row["verification_method"])
    return Project(
        id=str(row["id"]),
        org_id=str(row["org_id"]),
        provider=row["provider"],
        provider_repo_id=row["provider_repo_id"],
        slug=row["slug"],
        default_branch=row["default_branch"],
        status=ProjectStatus(row["status"]),
        verification_token=row["verification_token"] or "",
        verified=row["verified_at"] is not None and method is not None,
        verification_method=method,
        verified_at=row["verified_at"],
        verification_checked_at=row["verification_checked_at"],
        verification_error=row["verification_error"],
    )
