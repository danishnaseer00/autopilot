"""Orphaned queued-deployment reconciler (SPEC-11 §9).

A Redis/queue-dependency failure must fail the enqueue transaction, but a
process crash can still leave ``deployments.status='queued'`` without a live
``build_jobs`` row (for example after an operator cancelled a job). The
reconciler is deliberately idempotent and takes an advisory lock so multiple
control-plane replicas can run it concurrently.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

import asyncpg

from buildplane.queue.models import ReconciledDeployment


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PostgresQueueReconciler:
    def __init__(self, database, *, priority: int = 0, clock=_utcnow) -> None:
        self._db = database
        self._priority = priority
        self._clock = clock

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[asyncpg.Connection]:
        if hasattr(self._db, "acquire"):
            async with self._db.acquire() as connection:
                yield connection
        else:
            yield self._db

    async def reconcile(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
        org_id: str | None = None,
    ) -> tuple[ReconciledDeployment, ...]:
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        at = now or self._clock()
        params: list[object] = [at, limit, self._priority]
        scope = ""
        if org_id is not None:
            params.append(uuid.UUID(str(org_id)))
            scope = "AND d.org_id = $4"
        async with self._connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock("
                    "hashtext('spec11-build-queue-reconciler'))"
                )
                rows = await conn.fetch(
                    f"""
                    WITH orphaned AS (
                        SELECT d.id, d.org_id, d.project_id
                        FROM deployments d
                        WHERE d.status = 'queued'
                          {scope}
                          AND NOT EXISTS (
                              SELECT 1
                              FROM build_jobs j
                              WHERE j.deployment_id = d.id
                                AND j.status IN ('pending', 'running')
                          )
                        ORDER BY d.created_at, d.id
                        LIMIT $2
                        FOR UPDATE OF d SKIP LOCKED
                    ),
                    inserted AS (
                        INSERT INTO build_jobs
                            (deployment_id, org_id, project_id, status,
                             priority, available_at, created_at, updated_at)
                        SELECT id, org_id, project_id, 'pending',
                               $3, $1::timestamptz, $1::timestamptz,
                               $1::timestamptz
                        FROM orphaned
                        RETURNING id, deployment_id, org_id, project_id
                    )
                    SELECT id, deployment_id, org_id, project_id
                    FROM inserted
                    ORDER BY deployment_id
                    """,
                    *params,
                )
        return tuple(
            ReconciledDeployment(
                deployment_id=str(row["deployment_id"]),
                job_id=str(row["id"]),
                org_id=str(row["org_id"]),
                project_id=str(row["project_id"]),
            )
            for row in rows
        )
