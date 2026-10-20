"""The trigger -> build-queue boundary (SPEC-13 step 1).

SPEC-11 owns claiming, leasing and backpressure. SPEC-13 only needs the producer
side: inserting the deployment and its job in the *same* caller transaction, so
a failed enqueue rolls the deployment insert back (SPEC-13 §8). The Protocol
lets SPEC-11 replace the adapter without changing the lifecycle service.
"""

from __future__ import annotations

import uuid
from typing import Protocol

import asyncpg

from runtime.lifecycle.errors import EnqueueFailed


class BuildQueue(Protocol):
    async def enqueue(
        self,
        conn: asyncpg.Connection,
        *,
        deployment_id: uuid.UUID,
        org_id: uuid.UUID,
        project_id: uuid.UUID,
        priority: int = 0,
    ) -> uuid.UUID: ...


class PostgresBuildQueue:
    """Insert one ``build_jobs`` row using the caller's transaction."""

    async def enqueue(
        self,
        conn: asyncpg.Connection,
        *,
        deployment_id: uuid.UUID,
        org_id: uuid.UUID,
        project_id: uuid.UUID,
        priority: int = 0,
    ) -> uuid.UUID:
        try:
            job_id = await conn.fetchval(
                "INSERT INTO build_jobs "
                "  (deployment_id, org_id, project_id, priority) "
                "VALUES ($1, $2, $3, $4) "
                "RETURNING id",
                deployment_id,
                org_id,
                project_id,
                priority,
            )
        except Exception as exc:  # boundary: never leak a driver message
            raise EnqueueFailed(
                "the build job could not be enqueued; trigger rolled back"
            ) from exc
        return job_id


__all__ = ["BuildQueue", "PostgresBuildQueue"]
