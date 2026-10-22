"""Enqueue admission, backpressure and the Redis-side quota boundary.

SPEC-11 R-6/R-8: enforce limits before a deployment becomes a build job. The
policy is pure; the Postgres adapter reads the global pending depth and the
org's plan in the same transaction as the deployment insert, so a rejected
enqueue cannot leave a queued deployment behind.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from buildplane.queue.errors import QueueUnavailable
from buildplane.queue.models import AdmissionDecision

DEFAULT_MAX_QUEUE_DEPTH = 1000
DEFAULT_RETRY_AFTER_SECONDS = 5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BuildEnqueueRateLimiter(Protocol):
    """The Redis-backed counter boundary (SPEC-11 R-8 / SPEC-20).

    Implementations may raise on a Redis outage. The admission layer turns that
    into ``QueueUnavailable`` so the caller rolls the deployment insert back.
    """

    async def allow(self, org_id: str, *, now: datetime) -> bool: ...


@dataclass(frozen=True, slots=True)
class QueueAdmissionPolicy:
    """The pure R-6 decision table."""

    max_queue_depth: int = DEFAULT_MAX_QUEUE_DEPTH
    free_plan_codes: frozenset[str] = frozenset({"free"})
    retry_after_seconds: int = DEFAULT_RETRY_AFTER_SECONDS

    def __post_init__(self) -> None:
        if self.max_queue_depth < 0:
            raise ValueError("max_queue_depth must not be negative")
        if self.retry_after_seconds <= 0:
            raise ValueError("retry_after_seconds must be positive")

    def decide(self, *, queue_depth: int, plan_code: str | None) -> AdmissionDecision:
        if queue_depth < 0:
            raise ValueError("queue_depth must not be negative")

        full = queue_depth >= self.max_queue_depth
        if full and (plan_code is None or plan_code in self.free_plan_codes):
            return AdmissionDecision(
                accepted=False,
                queue_depth=queue_depth,
                plan_code=plan_code,
                status_code=429,
                error="build_queue_full",
                retry_after_seconds=self.retry_after_seconds,
            )
        return AdmissionDecision(
            accepted=True,
            queue_depth=queue_depth,
            plan_code=plan_code,
            # Positions are 1-based, matching PostgresJobQueue.position(). The
            # new job sits behind every job already pending at admission time.
            position=queue_depth + 1,
            status_code=202,
        )


class PostgresQueueAdmission:
    """Read queue depth + plan in the caller's enqueue transaction."""

    def __init__(
        self,
        *,
        policy: QueueAdmissionPolicy | None = None,
        limiter: BuildEnqueueRateLimiter | None = None,
        default_plan_code: str = "free",
        clock=_utcnow,
    ) -> None:
        self._policy = policy or QueueAdmissionPolicy()
        self._limiter = limiter
        self._default_plan_code = default_plan_code
        self._clock = clock

    async def admit(
        self,
        conn,
        *,
        org_id: str | uuid.UUID,
        now: datetime | None = None,
    ) -> AdmissionDecision:
        at = now or self._clock()
        row = await conn.fetchrow(
            """
            SELECT COALESCE(p.code, $2) AS plan_code,
                   (SELECT count(*)::int
                      FROM build_jobs j
                      JOIN deployments d ON d.id = j.deployment_id
                     WHERE j.status = 'pending'
                       AND j.available_at <= $3::timestamptz
                       AND d.status = 'queued') AS queue_depth
            FROM orgs o
            LEFT JOIN plans p ON p.id = o.plan_id
            WHERE o.id = $1
            """,
            _uuid(org_id),
            self._default_plan_code,
            at,
        )
        if row is None:
            raise QueueUnavailable("org not found for build-queue admission")

        decision = self._policy.decide(
            queue_depth=int(row["queue_depth"]),
            plan_code=row["plan_code"],
        )
        if not decision.accepted or self._limiter is None:
            return decision

        try:
            allowed = await self._limiter.allow(str(org_id), now=at)
        except Exception as exc:
            # R-8/§9: unlike the webhook audit limiter, this counter protects a
            # quota. Fail closed and let the producer roll back the insert.
            raise QueueUnavailable(
                "build enqueue rate limiter unavailable"
            ) from exc
        if allowed:
            return decision
        return AdmissionDecision(
            accepted=False,
            queue_depth=decision.queue_depth,
            plan_code=decision.plan_code,
            status_code=429,
            error="build_quota_exceeded",
            retry_after_seconds=60,
        )


class InMemoryEnqueueRateLimiter:
    """Reference limiter for unit tests; production uses the Redis adapter."""

    def __init__(self, *, limit: int, window_seconds: int = 86_400) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[str, deque[datetime]] = {}

    async def allow(self, org_id: str, *, now: datetime) -> bool:
        cutoff = now - timedelta(seconds=self.window_seconds)
        events = self._events.setdefault(str(org_id), deque())
        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) >= self.limit:
            return False
        events.append(now)
        return True


def _uuid(value: str | uuid.UUID) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"not a UUID: {value!r}") from exc
