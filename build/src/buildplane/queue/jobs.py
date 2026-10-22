"""Postgres build queue: fair claiming, leases and the dead-letter queue.

SPEC-09 supplied the atomic claim/heartbeat/lease primitives. SPEC-11 adds the
contract that was missing from the claim order:

* FIFO with an explicit priority override (R-2/R-10);
* a per-org cap across all of the org's projects (R-9/R-12);
* a hard attempt ceiling and an inspectable DLQ (R-5);
* cancellation that beats retry/sweep (R-17);
* queue position/depth for the customer-facing queue number (Q-3).

The cap is enforced by taking a per-org row lock before selecting the job. The
first candidate query chooses the org with the oldest/priority-eligible work;
the lock then serialises claims for that org so two workers cannot both observe
``running < cap`` and overshoot it. ``SKIP LOCKED`` still protects the job row.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Sequence

import asyncpg

from buildplane.pipeline.models import ClaimedJob
from buildplane.queue.models import DeadLetter

LEASE_SECONDS = 60
HEARTBEAT_SECONDS = 15
DEFAULT_ORG_CONCURRENCY = 1

_CAP_SQL = """
    COALESCE(
        CASE
            WHEN (p.limits ->> 'parallelBuilds') ~ '^[0-9]+$'
                THEN (p.limits ->> 'parallelBuilds')::int
            WHEN (p.limits ->> 'concurrent_builds') ~ '^[0-9]+$'
                THEN (p.limits ->> 'concurrent_builds')::int
        END,
        $2
    )
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PostgresJobQueue:
    def __init__(
        self,
        database,
        *,
        lease_seconds: int = LEASE_SECONDS,
        heartbeat_seconds: int = HEARTBEAT_SECONDS,
        default_org_concurrency: int = DEFAULT_ORG_CONCURRENCY,
        clock=_utcnow,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if heartbeat_seconds <= 0 or heartbeat_seconds >= lease_seconds:
            raise ValueError("heartbeat_seconds must be between 1 and the lease")
        if default_org_concurrency <= 0:
            raise ValueError("default_org_concurrency must be positive")
        self._db = database
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.default_org_concurrency = default_org_concurrency
        self._clock = clock

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[asyncpg.Connection]:
        if hasattr(self._db, "acquire"):
            async with self._db.acquire() as connection:
                yield connection
        else:
            yield self._db

    async def claim(
        self,
        worker_id: str,
        *,
        now: datetime | None = None,
        org_ids: Sequence[str] | None = None,
    ) -> ClaimedJob | None:
        """Claim one job globally, or only from ``org_ids`` when supplied.

        The optional org scope exists for dedicated worker pools and for
        maintenance/diagnostic runs; normal workers omit it and get R-10's
        global FIFO next-eligible-org behaviour.
        """

        if not worker_id or not worker_id.strip():
            raise ValueError("worker_id is required")
        claimed_at = now or self._clock()
        try:
            return await self._claim(worker_id, claimed_at, org_ids=org_ids)
        except _NoLongerClaimable:
            return None

    async def _claim(
        self,
        worker_id: str,
        claimed_at: datetime,
        *,
        org_ids: Sequence[str] | None = None,
    ) -> ClaimedJob | None:
        params: list[object] = [claimed_at, self.default_org_concurrency]
        scope = ""
        if org_ids is not None:
            scoped = tuple(_uuid(org_id) for org_id in org_ids)
            if not scoped:
                return None
            params.append(list(scoped))
            scope = "AND j.org_id = ANY($3::uuid[])"
        async with self._connection() as conn:
            async with conn.transaction():
                # Choose the org whose oldest eligible job is at the front of
                # the global queue, but only if it has spare capacity. This is
                # the FIFO part of R-10; an org at its cap is skipped rather
                # than allowed to monopolise the next slot.
                candidate = await conn.fetchrow(
                    f"""
                    SELECT j.org_id
                    FROM build_jobs j
                    JOIN deployments d ON d.id = j.deployment_id
                    JOIN orgs o ON o.id = j.org_id
                    LEFT JOIN plans p ON p.id = o.plan_id
                    WHERE j.status = 'pending'
                      AND j.available_at <= $1::timestamptz
                      AND d.status = 'queued'
                      AND o.status = 'active'
                      {scope}
                      AND (
                          SELECT count(*)
                          FROM build_jobs r
                          WHERE r.org_id = j.org_id
                            AND r.status = 'running'
                      ) < {_CAP_SQL}
                    ORDER BY j.priority DESC, j.created_at, j.id
                    LIMIT 1
                    """,
                    *params,
                )
                if candidate is None:
                    return None

                # Serialise claims for one org. Without this, two workers can
                # both read running=cap-1 under READ COMMITTED and overshoot.
                org_id = candidate["org_id"]
                locked = await conn.fetchval(
                    "SELECT id FROM orgs WHERE id = $1 AND status = 'active' "
                    "FOR UPDATE",
                    org_id,
                )
                if locked is None:
                    return None

                capacity = await conn.fetchrow(
                    f"""
                    SELECT {_CAP_SQL} AS org_cap,
                           (
                               SELECT count(*)
                               FROM build_jobs r
                               WHERE r.org_id = o.id
                                 AND r.status = 'running'
                           ) AS running
                    FROM orgs o
                    LEFT JOIN plans p ON p.id = o.plan_id
                    WHERE o.id = $1
                    """,
                    org_id,
                    self.default_org_concurrency,
                )
                if (
                    capacity is None
                    or int(capacity["running"]) >= int(capacity["org_cap"])
                ):
                    # The org reached its cap while another claim was in
                    # flight. Releasing the org lock is safe; the next claim
                    # will choose another eligible org.
                    return None

                row = await conn.fetchrow(
                    """
                    WITH candidate AS (
                        SELECT j.id, j.deployment_id
                        FROM build_jobs j
                        JOIN deployments d ON d.id = j.deployment_id
                        WHERE j.status = 'pending'
                          AND j.available_at <= $1::timestamptz
                          AND d.status = 'queued'
                          AND j.org_id = $4
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
                    org_id,
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
        """Requeue expired leases, except where the deployment was cancelled.

        A cancelled deployment must never get a pending job back (R-17), even
        if the cancellation raced the worker's lease expiry.
        """

        expired_at = now or self._clock()
        async with self._connection() as conn:
            rows = await conn.fetch(
                """
                WITH expired AS (
                    UPDATE build_jobs AS j
                    SET status = CASE
                            WHEN d.status = 'building'
                                THEN 'pending'::job_status
                            ELSE 'cancelled'::job_status
                        END,
                        worker_id = NULL,
                        heartbeat_at = NULL,
                        lease_expires_at = NULL,
                        available_at = $1::timestamptz,
                        last_error = CASE
                            WHEN d.status = 'building' THEN 'lease_expired'
                            ELSE 'deployment_' || d.status::text
                        END,
                        updated_at = $1::timestamptz
                    FROM deployments d
                    WHERE j.deployment_id = d.id
                      AND j.status = 'running'
                      AND j.lease_expires_at IS NOT NULL
                      AND j.lease_expires_at < $1::timestamptz
                    RETURNING j.id, j.deployment_id, j.status
                ),
                returned AS (
                    UPDATE deployments
                    SET status = 'queued', stage = NULL, started_at = NULL
                    WHERE status = 'building'
                      AND id IN (
                          SELECT deployment_id
                          FROM expired
                          WHERE status = 'pending'
                      )
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

    async def cancel_deployment(
        self,
        deployment_id: str,
        *,
        reason: str = "cancelled",
        now: datetime | None = None,
    ) -> int:
        """Cancel pending/running work before touching the deployment row.

        Cancelling the job first is what makes R-17 race-safe: a claim can
        never observe a still-pending job for a deployment already marked
        cancelled.
        """

        at = now or self._clock()
        deployment_uuid = _uuid(deployment_id)
        async with self._connection() as conn:
            async with conn.transaction():
                result = await conn.execute(
                    """
                    UPDATE build_jobs
                    SET status = 'cancelled',
                        worker_id = NULL,
                        heartbeat_at = NULL,
                        lease_expires_at = NULL,
                        last_error = $2,
                        updated_at = $1::timestamptz
                    WHERE deployment_id = $3
                      AND status IN ('pending', 'running')
                    """,
                    at,
                    reason[:4000],
                    deployment_uuid,
                )
                await conn.execute(
                    """
                    UPDATE deployments
                    SET status = 'cancelled',
                        finished_at = COALESCE(finished_at, $1::timestamptz),
                        error_message = COALESCE(error_message, $2)
                    WHERE id = $3 AND status IN ('queued', 'building')
                    """,
                    at,
                    reason[:4000],
                    deployment_uuid,
                )
        return _updated_count(result)

    async def dead_letters(
        self,
        *,
        org_id: str | None = None,
        limit: int = 100,
    ) -> tuple[DeadLetter, ...]:
        """Return the inspectable DLQ rows, newest first (R-5)."""

        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        clauses = ["j.status = 'dead'"]
        params: list[object] = []
        if org_id is not None:
            params.append(_uuid(org_id))
            clauses.append(f"j.org_id = ${len(params)}")
        params.append(limit)
        limit_placeholder = f"${len(params)}"
        sql = f"""
            SELECT j.id, j.deployment_id, j.org_id, j.project_id,
                   j.attempts, j.max_attempts, j.priority, j.last_error,
                   j.created_at, j.updated_at,
                   d.git_sha, d.git_ref, d.environment,
                   d.status AS deployment_status
            FROM build_jobs j
            JOIN deployments d ON d.id = j.deployment_id
            WHERE {' AND '.join(clauses)}
            ORDER BY j.updated_at DESC, j.id
            LIMIT {limit_placeholder}
        """
        async with self._connection() as conn:
            rows = await conn.fetch(sql, *params)
        return tuple(_dead_letter_from_row(row) for row in rows)

    async def dead_letter(self, job_id: str) -> DeadLetter | None:
        async with self._connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT j.id, j.deployment_id, j.org_id, j.project_id,
                       j.attempts, j.max_attempts, j.priority, j.last_error,
                       j.created_at, j.updated_at,
                       d.git_sha, d.git_ref, d.environment,
                       d.status AS deployment_status
                FROM build_jobs j
                JOIN deployments d ON d.id = j.deployment_id
                WHERE j.id = $1 AND j.status = 'dead'
                """,
                _uuid(job_id),
            )
        return _dead_letter_from_row(row) if row is not None else None

    async def queue_depth(
        self,
        *,
        now: datetime | None = None,
        org_id: str | None = None,
    ) -> int:
        """Number of pending jobs whose deployment is still queued."""

        at = now or self._clock()
        scope = ""
        params: list[object] = [at]
        if org_id is not None:
            params.append(_uuid(org_id))
            scope = "AND j.org_id = $2"
        async with self._connection() as conn:
            value = await conn.fetchval(
                f"""
                SELECT count(*)::int
                FROM build_jobs j
                JOIN deployments d ON d.id = j.deployment_id
                WHERE j.status = 'pending'
                  AND j.available_at <= $1::timestamptz
                  AND d.status = 'queued'
                  {scope}
                """,
                *params,
            )
        return int(value or 0)

    async def position(
        self,
        job_id: str,
        *,
        now: datetime | None = None,
        org_id: str | None = None,
    ) -> int | None:
        """1-based queue position, or ``None`` when the job is not waiting."""

        at = now or self._clock()
        scope = ""
        params: list[object] = [at, _uuid(job_id)]
        if org_id is not None:
            params.append(_uuid(org_id))
            scope = "AND j.org_id = $3"
        async with self._connection() as conn:
            value = await conn.fetchval(
                f"""
                WITH ordered AS (
                    SELECT j.id,
                           row_number() OVER (
                               ORDER BY j.priority DESC, j.created_at, j.id
                           ) AS queue_position
                    FROM build_jobs j
                    JOIN deployments d ON d.id = j.deployment_id
                    WHERE j.status = 'pending'
                      AND j.available_at <= $1::timestamptz
                      AND d.status = 'queued'
                      {scope}
                )
                SELECT queue_position FROM ordered WHERE id = $2
                """,
                *params,
            )
        return int(value) if value is not None else None

    def lease_deadline(self, now: datetime | None = None) -> datetime:
        return (now or self._clock()) + timedelta(seconds=self.lease_seconds)


class _NoLongerClaimable(Exception):
    pass


def _dead_letter_from_row(row) -> DeadLetter:
    return DeadLetter(
        job_id=str(row["id"]),
        deployment_id=str(row["deployment_id"]),
        org_id=str(row["org_id"]),
        project_id=str(row["project_id"]),
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        priority=row["priority"],
        last_error=row["last_error"],
        git_sha=row["git_sha"],
        git_ref=row["git_ref"],
        environment=row["environment"],
        deployment_status=row["deployment_status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _updated_count(result: str) -> int:
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"not a UUID: {value!r}") from exc
