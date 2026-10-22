"""Pure-unit coverage for SPEC-11 admission, retry and autoscaler policy."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from buildplane.queue.admission import (
    InMemoryEnqueueRateLimiter,
    PostgresQueueAdmission,
    QueueAdmissionPolicy,
)
from buildplane.queue.autoscaling import desired_workers
from buildplane.queue.errors import QueueUnavailable
from buildplane.queue.retry import backoff_seconds, decide_retry, stable_jitter

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def test_free_plan_gets_a_429_at_queue_capacity():
    policy = QueueAdmissionPolicy(max_queue_depth=1000)
    decision = policy.decide(queue_depth=1000, plan_code="free")
    assert decision.accepted is False
    assert decision.status_code == 429
    assert decision.error == "build_queue_full"
    assert decision.retry_after_seconds == 5
    assert decision.position is None


def test_paid_plan_is_accepted_at_capacity_with_a_queue_position():
    policy = QueueAdmissionPolicy(max_queue_depth=1000)
    decision = policy.decide(queue_depth=1000, plan_code="pro")
    assert decision.accepted is True
    assert decision.status_code == 202
    assert decision.position == 1001


def test_free_plan_is_accepted_below_capacity():
    policy = QueueAdmissionPolicy(max_queue_depth=1000)
    decision = policy.decide(queue_depth=999, plan_code="free")
    assert decision.accepted is True
    assert decision.position == 1000


def test_in_memory_enqueue_limiter_enforces_its_window():
    limiter = InMemoryEnqueueRateLimiter(limit=2, window_seconds=60)

    async def scenario():
        assert await limiter.allow("org-a", now=NOW)
        assert await limiter.allow("org-a", now=NOW + timedelta(seconds=1))
        assert not await limiter.allow("org-a", now=NOW + timedelta(seconds=2))
        # A different org has its own bucket.
        assert await limiter.allow("org-b", now=NOW + timedelta(seconds=2))
        # The first event ages out after the window.
        assert await limiter.allow("org-a", now=NOW + timedelta(seconds=61))

    asyncio.run(scenario())


def test_admission_fails_closed_when_the_rate_limiter_backend_is_down():
    class _Connection:
        async def fetchrow(self, _sql, *_params):
            return {"plan_code": "free", "queue_depth": 0}

    class _FailingLimiter:
        async def allow(self, _org_id, *, now):
            raise ConnectionError("redis is down")

    admission = PostgresQueueAdmission(limiter=_FailingLimiter())

    async def scenario():
        with pytest.raises(QueueUnavailable):
            await admission.admit(_Connection(), org_id="00000000-0000-0000-0000-000000000001", now=NOW)

    asyncio.run(scenario())


def test_autoscaler_clamps_to_the_budget_ceiling_and_alerts():
    decision = desired_workers(
        queue_depth=500,
        avg_build_seconds=60,
        safety_factor=1.2,
        min_workers=1,
        max_workers=4,
    )
    assert decision.raw_workers == 10
    assert decision.desired_workers == 4
    assert decision.at_ceiling is True
    assert "BUDGET_MAX_WORKERS=4" in decision.alert


def test_autoscaler_keeps_the_warm_floor_when_idle():
    decision = desired_workers(
        queue_depth=0,
        avg_build_seconds=60,
        safety_factor=1.2,
        min_workers=2,
        max_workers=10,
    )
    assert decision.desired_workers == 2
    assert decision.at_ceiling is False
    assert decision.alert is None


def test_transient_failures_get_backoff_then_the_third_attempt_is_dead():
    first = decide_retry(attempts=0, max_attempts=3, retryable=True)
    assert first.retry is True
    assert first.dead_letter is False
    assert first.next_attempt == 1
    assert first.delay_seconds == 5

    second = decide_retry(attempts=1, max_attempts=3, retryable=True)
    assert second.retry is True
    assert second.delay_seconds == 10

    exhausted = decide_retry(attempts=2, max_attempts=3, retryable=True)
    assert exhausted.retry is False
    assert exhausted.dead_letter is True
    assert exhausted.next_attempt == 3
    assert exhausted.delay_seconds is None


def test_type_error_is_never_retried():
    decision = decide_retry(attempts=0, max_attempts=3, retryable=False)
    assert decision.retry is False
    assert decision.dead_letter is False
    assert decision.next_attempt == 0


def test_registry_rate_limit_jitter_is_stable_and_bounded():
    job_id = "job-123"
    jitter = stable_jitter(job_id, max_seconds=2)
    assert jitter == stable_jitter(job_id, max_seconds=2)
    assert 0 <= jitter <= 2
    assert backoff_seconds(3) == 40
