"""Atomic build-job claiming, heartbeats and lease sweep (SPEC-09 R-1/R-2).

This is the SPEC-09 slice of the queue: SPEC-11 owns fairness, retry policy,
backpressure and the dead-letter queue. The only operations here are the ones a
running build worker needs to avoid stranding a customer: claim with
``FOR UPDATE SKIP LOCKED``, heartbeat the 60-second lease, and return expired
leases to ``pending``.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import asyncpg

from buildplane.pipeline.models import ClaimedJob

LEASE_SECONDS = 60
HEARTBEAT_SECONDS = 15


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PostgresJobQueue:
    def __init__(
        self,
        database,
        *,
        lease_seconds: int = LEASE_SECONDS,
        heartbeat_seconds: int = HEARTBEAT_SECONDS,
        clock=_utcnow,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if heartbeat_seconds <= 0 or heartbeat_seconds >= lease_seconds:
            raise ValueError("heartbeat_seconds must be between 1 and the lease")
        self._db = database
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self._clock = clock

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[asyncpg.Connection]:
        if hasattr(self._db, "acquire"):
            async with self._db.acquire() as connection:
                yield connection
        else:
            yield self._db

    async def claim(
        self, worker_id: str, *, now: datetime | None = None
    ) -> ClaimedJob | None:
        if not worker_id or not worker_id.strip():
            raise ValueError("worker_id is required")
        claimed_at = now or self._clock()
        try:
            return await self._claim(worker_id, claimed_at)
        except _NoLongerClaimable:
            return None

    async def _claim(self, worker_id: str, claimed_at: datetime) -> ClaimedJob:
        async with self._connection() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    WITH candidate AS (
                        SELECT j.id, j.deployment_id
                        FROM build_jobs j
                        JOIN deployments d ON d.id = j.deployment_id
                        WHERE j.status = 'pending'
                          AND j.available_at <= $1::timestamptz
                          AND d.status = 'queued'
                        ORDER BY j.priority DESC, j.created_at, j.id
                        LIMIT 1
                        FOR UPDATE OF j, d SKIP LOCKED
                    )
                    UPDATE build_jobs AS j
                    SET status = 'running',
                        worker_id = $2,
                        heartbeat_at = $1::timestamptz,
                        lease_expires_at =
                            $1::timestamptz + ($3::int * interval '1 second'),
                        updated_at = $1::timestamptz
                    FROM candidate AS c
                    WHERE j.id = c.id
                    RETURNING j.id, j.deployment_id, j.org_id, j.project_id,
                              j.attempts, j.max_attempts, j.lease_expires_at
                    """,
                    claimed_at,
                    worker_id,
                    self.lease_seconds,
                )
                if row is None:
                    return None

                marked = await conn.execute(
                    """
                    UPDATE deployments
                    SET status = 'building', stage = 'fetch',
                        started_at = COALESCE(started_at, $1::timestamptz)
                    WHERE id = $2 AND status = 'queued'
                    """,
                    claimed_at,
                    row["deployment_id"],
                )
                if marked != "UPDATE 1":
                    # The deployment was cancelled between the locks; roll back
                    # the job transition so the queue stays consistent.
                    raise _NoLongerClaimable()

                deployment = await conn.fetchrow(
                    """
                    SELECT d.git_sha, d.git_ref, d.environment,
                           d.build_env_keys, d.manifest::text AS manifest,
                           d.manifest_hash, d.build_image,
                           d.preview_hostname, d.pr_number, d.delivery_id,
                           p.name AS project_name, p.slug AS project_slug,
                           p.provider, p.provider_repo_id
                    FROM deployments d
                    JOIN projects p ON p.id = d.project_id
                    WHERE d.id = $1
                    """,
                    row["deployment_id"],
                )
                if deployment is None:
                    raise _NoLongerClaimable()

                manifest = deployment["manifest"]
                if isinstance(manifest, str):
                    manifest = json.loads(manifest)
                return ClaimedJob(
                    job_id=str(row["id"]),
                    deployment_id=str(row["deployment_id"]),
                    org_id=str(row["org_id"]),
                    project_id=str(row["project_id"]),
                    project_name=deployment["project_name"],
                    project_slug=deployment["project_slug"],
                    provider=deployment["provider"],
                    provider_repo_id=deployment["provider_repo_id"],
                    git_sha=deployment["git_sha"],
                    git_ref=deployment["git_ref"],
                    environment=deployment["environment"],
                    attempts=row["attempts"],
                    max_attempts=row["max_attempts"],
                    build_env_keys=tuple(deployment["build_env_keys"] or ()),
                    manifest=manifest,
                    manifest_hash=deployment["manifest_hash"],
                    build_image=deployment["build_image"],
                    preview_hostname=deployment["preview_hostname"],
                    pr_number=deployment["pr_number"],
                    delivery_id=deployment["delivery_id"],
                    lease_expires_at=row["lease_expires_at"],
                )

    async def heartbeat(
        self,
        job_id: str,
        worker_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        beat = now or self._clock()
        async with self._connection() as conn:
            result = await conn.execute(
                """
                UPDATE build_jobs
                SET heartbeat_at = $1::timestamptz,
                    lease_expires_at =
                        $1::timestamptz + ($3::int * interval '1 second'),
                    updated_at = $1::timestamptz
                WHERE id = $2 AND status = 'running' AND worker_id = $4
                """,
                beat,
                _uuid(job_id),
                self.lease_seconds,
                worker_id,
            )
        return result == "UPDATE 1"

    async def sweep_expired(
        self, *, now: datetime | None = None
    ) -> tuple[tuple[str, str], ...]:
        """Return expired ``(job_id, deployment_id)`` pairs to pending."""

        expired_at = now or self._clock()
        async with self._connection() as conn:
            rows = await conn.fetch(
                """
                WITH expired AS (
                    UPDATE build_jobs
                    SET status = 'pending',
                        worker_id = NULL,
                        heartbeat_at = NULL,
                        lease_expires_at = NULL,
                        available_at = $1::timestamptz,
                        last_error = 'lease_expired',
                        updated_at = $1::timestamptz
                    WHERE status = 'running'
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at < $1::timestamptz
                    RETURNING id, deployment_id
                ),
                returned AS (
                    UPDATE deployments
                    SET status = 'queued', stage = NULL, started_at = NULL
                    WHERE status = 'building'
                      AND id IN (SELECT deployment_id FROM expired)
                    RETURNING id
                )
                SELECT id, deployment_id FROM expired ORDER BY id
                """,
                expired_at,
            )
        return tuple((str(row["id"]), str(row["deployment_id"])) for row in rows)

    async def release(
        self,
        job_id: str,
        worker_id: str,
        *,
        reason: str,
        available_at: datetime | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Return a still-owned job to pending without claiming it succeeded."""

        at = now or self._clock()
        async with self._connection() as conn:
            async with conn.transaction():
                result = await conn.execute(
                    """
                    UPDATE build_jobs
                    SET status = 'pending', worker_id = NULL,
                        heartbeat_at = NULL, lease_expires_at = NULL,
                        available_at = $3::timestamptz,
                        last_error = $4,
                        updated_at = $1::timestamptz
                    WHERE id = $2 AND status = 'running' AND worker_id = $5
                    """,
                    at,
                    _uuid(job_id),
                    available_at or at,
                    reason[:4000],
                    worker_id,
                )
                if result != "UPDATE 1":
                    return False
                await conn.execute(
                    """
                    UPDATE deployments
                    SET status = 'queued', stage = NULL, started_at = NULL
                    WHERE id = (SELECT deployment_id FROM build_jobs WHERE id = $1)
                      AND status = 'building'
                    """,
                    _uuid(job_id),
                )
        return True

    def lease_deadline(self, now: datetime | None = None) -> datetime:
        return (now or self._clock()) + timedelta(seconds=self.lease_seconds)


class _NoLongerClaimable(Exception):
    pass


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"not a UUID: {value!r}") from exc
